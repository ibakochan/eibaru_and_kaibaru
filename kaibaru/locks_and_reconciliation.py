from django.core.cache import cache
from contextlib import contextmanager

import uuid
from django.db.models import Q

from collections import defaultdict
import stripe
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from .models import TicketGrant, Reservation, SubscriptionMutation, SubscriptionItem, Subscription, Club, Member, MembershipPlan, Invoice, InvoiceItem, Payment
from datetime import datetime, timedelta

from .tasks_emails import send_stripe_cash_transition_email, send_plan_deletion_emails
from .service_mutations import MutationLockedError

from .invoice_creation import create_local_invoice_from_stripe_invoice

import logging

logger = logging.getLogger(__name__)

class StripeSubscriptionReconciler:
    """
    DB is source of truth.

    DB model:
        SubscriptionItem rows

    Stripe model:
        SubscriptionItems grouped by price_id

    Rules:
    - Stripe must match DB quantity per price_id
    - No orphan Stripe items allowed
    - DB must store correct stripe_subscription_item_id
    - Subscription cancel_at_period_end must reflect DB intent
    """

    @staticmethod
    def _has_payment_been_attempted(*, invoice, club):
        """
        Determine whether Stripe has actually attempted payment
        for this invoice.
        """
    
        payment_intent_id = invoice.get("payment_intent")
    
        if not payment_intent_id:
            return False
    
        payment_intent = stripe.PaymentIntent.retrieve(
            payment_intent_id,
            stripe_account=club.stripe_account_id,
        )
    
        return payment_intent.get("status") not in [
            "requires_payment_method",
            "requires_confirmation",
        ]
        
    @staticmethod
    def _find_add_plan_invoice(*, mutation, club):
        invoice_id = (mutation.payload or {}).get("invoice_id")

        if invoice_id:
            try:
                return stripe.Invoice.retrieve(
                    invoice_id,
                    expand=["lines.data"],
                    stripe_account=club.stripe_account_id,
                )
            except stripe.error.InvalidRequestError:
                pass

        invoices = stripe.Invoice.list(
            subscription=mutation.subscription.stripe_subscription_id,
            limit=10,
            stripe_account=club.stripe_account_id,
        )

        for invoice in invoices.auto_paging_iter():
            if invoice.metadata.get("mutation_id") == str(mutation.id):
                return stripe.Invoice.retrieve(
                    invoice.id,
                    expand=["lines.data"],
                    stripe_account=club.stripe_account_id,
                )

        return None

    @staticmethod
    def reconcile(*, subscription, club):
        

        processed_mutation_ids = []
        with transaction.atomic():

            mutations = (
                SubscriptionMutation.objects
                .select_for_update()
                .filter(
                    subscription=subscription,
                    status__in=[
                        SubscriptionMutation.Status.PENDING,
                        SubscriptionMutation.Status.PROCESSING,
                    ],
                )
                .order_by("created_at")
            )

            logger.info(
                "[RECONCILE] subscription=%s found_mutations=%s",
                subscription.id,
                mutations.count(),
            )
    
            now = timezone.now()
    
            for mutation in mutations:
                logger.info(
                    "[RECONCILE] processing mutation id=%s type=%s status=%s invoice_status=%s",
                    mutation.id,
                    mutation.type,
                    mutation.status,
                    mutation.invoice_status,
                )
    
                item = mutation.item
                now = timezone.now()
    
                # -------------------------------------------------
                # CANCEL
                # -------------------------------------------------
                if mutation.type == SubscriptionMutation.MutationType.CANCEL:
    
                    if item:
                        item.deleted_at = now
                        item.access_until = subscription.access_until
                        item.save(update_fields=[
                            "deleted_at",
                            "access_until",
                        ])
                    
                    mutation.local_state_applied = True
                    mutation.status = SubscriptionMutation.Status.PROCESSING

                    mutation.save(update_fields=[
                        "local_state_applied",
                        "status",
                    ])
                    processed_mutation_ids.append(mutation.id) 
    
                # -------------------------------------------------
                # RESUME
                # -------------------------------------------------
                elif mutation.type == SubscriptionMutation.MutationType.RESUME:
    
                    if item:
                        item.deleted_at = None
                        item.access_until = None
                        item.save(update_fields=[
                            "deleted_at",
                            "access_until",
                        ])
                    
                    mutation.local_state_applied = True
                    mutation.status = SubscriptionMutation.Status.PROCESSING

                    mutation.save(update_fields=[
                        "local_state_applied",
                        "status",
                    ])
                    processed_mutation_ids.append(mutation.id) 

                elif mutation.type == SubscriptionMutation.MutationType.ADD_PLAN:
                    logger.info(
                        "[ADD_PLAN] mutation=%s payload=%s",
                        mutation.id,
                        mutation.payload,
                    )

                    payload = mutation.payload or {}
                
                    invoice = StripeSubscriptionReconciler._find_add_plan_invoice(
                        mutation=mutation,
                        club=club,
                    )

                    logger.info(
                        "[ADD_PLAN] mutation=%s invoice_found=%s",
                        mutation.id,
                        bool(invoice),
                    )
                
                
                                # =========================================================
                    # Invoice does not exist
                    # =========================================================
                    if not invoice:
                
                        if (
                            mutation.invoice_status
                            == SubscriptionMutation.InvoiceStatus.NOT_STARTED
                        ):
                
                            mutation.invoice_status = (
                                SubscriptionMutation.InvoiceStatus.RETRY
                            )
                
                            mutation.save(
                                update_fields=[
                                    "invoice_status",
                                ]
                            )
                
                            continue
                
                
                        elif (
                            mutation.invoice_status
                            == SubscriptionMutation.InvoiceStatus.RETRY
                        ):
                
                            mutation.status = (
                                SubscriptionMutation.Status.FAILED
                            )
                
                            mutation.save(
                                update_fields=[
                                    "status",
                                ]
                            )
                
                            continue
                
                
                    

                    

                    
                    elif invoice.status == "open":

                        payment_attempted = (
                            StripeSubscriptionReconciler
                            ._has_payment_been_attempted(
                                invoice=invoice,
                                club=club,
                            )
                        )
                    
                        if not payment_attempted:
                            # ---------------------------------------------
                            # PAYMENT WAS NEVER ATTEMPTED
                            # ---------------------------------------------
                    
                            logger.warning(
                                "[ADD_PLAN] mutation=%s invoice=%s "
                                "exists but payment was never attempted. "
                                "Voiding invoice and failing mutation.",
                                mutation.id,
                                invoice.id,
                            )
                    
                            # ---------------------------------------------
                            # VOID STRIPE INVOICE
                            # ---------------------------------------------
                    
                            if invoice.status == "open":
                                stripe.Invoice.void_invoice(
                                    invoice.id,
                                    stripe_account=club.stripe_account_id,
                                )
                    
                            # ---------------------------------------------
                            # ATOMIC LOCAL DB UPDATE
                            # ---------------------------------------------
                    
                            with transaction.atomic():
                    
                                # Void local invoice
                                local_invoice = Invoice.objects.filter(
                                    stripe_invoice_id=invoice.id,
                                    subscription=subscription,
                                ).first()
                    
                                if local_invoice:
                                    local_invoice.status = "void"
                                    local_invoice.save(
                                        update_fields=[
                                            "status",
                                        ]
                                    )
                    
                                # Fail mutation
                                mutation.invoice_status = (
                                    SubscriptionMutation.InvoiceStatus.FAILED
                                )
                    
                                mutation.status = (
                                    SubscriptionMutation.Status.FAILED
                                )
                    
                                mutation.processed_at = timezone.now()
                    
                                mutation.save(
                                    update_fields=[
                                        "invoice_status",
                                        "status",
                                        "processed_at",
                                    ]
                                )

                            logger.info(
                                "[ADD_PLAN] mutation=%s invoice=%s "
                                "payment was never attempted. "
                                "Stripe invoice voided, local invoice voided, "
                                "mutation marked failed.",
                                mutation.id,
                                invoice.id,
                            )

                            continue

                    elif invoice.status == "void":
                        # ---------------------------------------------
                        # STRIPE INVOICE IS ALREADY VOID
                        # ---------------------------------------------
                    
                        logger.warning(
                            "[ADD_PLAN] mutation=%s invoice=%s "
                            "is already void. Voiding local invoice "
                            "and failing mutation.",
                            mutation.id,
                            invoice.id,
                        )
                    
                        with transaction.atomic():
                    
                            # ---------------------------------------------
                            # VOID LOCAL INVOICE
                            # ---------------------------------------------
                    
                            local_invoice = Invoice.objects.filter(
                                stripe_invoice_id=invoice.id,
                                subscription=subscription,
                            ).first()
                    
                            if local_invoice and local_invoice.status != "void":
                                local_invoice.status = "void"
                                local_invoice.save(
                                    update_fields=[
                                        "status",
                                    ]
                                )
                    
                            # ---------------------------------------------
                            # FAIL MUTATION
                            # ---------------------------------------------
                    
                            mutation.invoice_status = (
                                SubscriptionMutation.InvoiceStatus.FAILED
                            )
                    
                            mutation.status = (
                                SubscriptionMutation.Status.FAILED
                            )
                    
                            mutation.processed_at = timezone.now()
                    
                            mutation.save(
                                update_fields=[
                                    "invoice_status",
                                    "status",
                                    "processed_at",
                                ]
                            )
                    
                        logger.info(
                            "[ADD_PLAN] mutation=%s invoice=%s "
                            "was already void. Local invoice voided "
                            "and mutation marked failed.",
                            mutation.id,
                            invoice.id,
                        )
                    
                        continue

                    local_invoice = Invoice.objects.filter(
                        stripe_invoice_id=invoice.id,
                        subscription=subscription,
                    ).first()

                    if not local_invoice:

                        initial_status = (
                            "paid"
                            if invoice.status == "paid"
                            else "open"
                        )
                    
                        local_invoice, local_payment = (
                            create_local_invoice_from_stripe_invoice(
                                stripe_invoice=invoice,
                                subscription=subscription,
                                billing_reason="add_plan",
                                initial_status=initial_status,
                                mutation=mutation,
                            )
                        )
                    
                    member_id = payload["member_id"]
                    plan_id = payload["plan_id"]
                
                
                    member = Member.objects.get(id=member_id)
                    plan = MembershipPlan.objects.get(id=plan_id)


                    frozen_price = payload.get("price_at_subscription")
                    if frozen_price is None:
                        raise ValueError(
                            f"ADD_PLAN mutation {mutation.id} is missing "
                            "frozen price_at_subscription"
                        )

                    frozen_stripe_price_id = payload.get("stripe_price_id")
                    if not frozen_stripe_price_id:
                        raise ValueError(
                            f"ADD_PLAN mutation {mutation.id} is missing "
                            "frozen stripe_price_id"
                        )
                
                
                    existing = SubscriptionItem.objects.filter(
                        subscription=subscription,
                        member=member,
                        plan=plan,
                    ).first()
                
                
                    if existing:
                        existing.deleted_at = None
                        existing.price_at_subscription = frozen_price
                        existing.stripe_price_id_at_subscription = frozen_stripe_price_id
                        existing.save(
                            update_fields=[
                                "deleted_at",
                                "price_at_subscription",
                                "stripe_price_id_at_subscription",
                            ]
                        )
                
                    else:
                
                        stripe_sub = stripe.Subscription.retrieve(
                            subscription.stripe_subscription_id,
                            expand=["items.data"],
                            stripe_account=club.stripe_account_id,
                        )
                
                        stripe_item = next(
                            (
                                i for i in stripe_sub["items"]["data"]
                                if i["price"]["id"] == frozen_stripe_price_id
                            ),
                            None
                        )
                
                        SubscriptionItem.objects.create(
                            subscription=subscription,
                            member=member,
                            plan=plan,
                            price_at_subscription=frozen_price,
                            stripe_price_id_at_subscription=frozen_stripe_price_id,
                            stripe_subscription_item_id=(
                                stripe_item["id"]
                                if stripe_item
                                else None
                            ),
                        )

                    if plan.plan_type == "ticket_plan":
                        ticket_grant_data = payload.get(
                            "ticket_grant",
                            {},
                        )

                        if not ticket_grant_data:
                            raise ValueError(
                                f"ADD_PLAN mutation {mutation.id} is missing "
                                "frozen ticket_grant data"
                            )

                        ticket_quantity = ticket_grant_data.get(
                            "quantity",
                            0,
                        )

                        if ticket_quantity > 0:

                            existing_grant = TicketGrant.objects.filter(
                                mutation=mutation,
                            ).first()
                            
                            
                            ticket_type_id = ticket_grant_data.get(
                                "ticket_type_id",
                            )
                            
                            expires_at = ticket_grant_data.get(
                                "expires_at",
                            )
                            
                            expires_at = (
                                datetime.fromisoformat(expires_at)
                                if expires_at
                                else None
                            )

                            if not existing_grant:

                                TicketGrant.objects.create(
                                    member=member,
                                    mutation=mutation,
                                    ticket_type_id=ticket_type_id,
                                    source=TicketGrant.Source.SUBSCRIPTION,
                                    quantity=ticket_quantity,
                                    expires_at=expires_at,
                                )
                
                
                    if invoice.status == "paid":
                        mutation.invoice_status = (
                            SubscriptionMutation.InvoiceStatus.PAID
                        )
                    else:
                        mutation.invoice_status = (
                            SubscriptionMutation.InvoiceStatus.OPEN
                        )
                
                    mutation.local_state_applied = True
                    mutation.status = SubscriptionMutation.Status.PROCESSING

                    mutation.save(update_fields=[
                        "invoice_status",
                        "local_state_applied",
                        "status",
                    ])
                    processed_mutation_ids.append(mutation.id) 
                

                                
                




                elif mutation.type == SubscriptionMutation.MutationType.CHANGE_PLAN:

                    payload = mutation.payload or {}
                
                    old_plan_id = payload["old_plan_id"]
                    new_plan_id = payload["new_plan_id"]
                    new_price_id = payload["new_price_id"]
                    new_price = payload["new_price"]
                    stripe_id = payload.get("new_stripe_item_id")
                                
                    if not item:
                        continue
                
                    # 1. close old item
                    item.deleted_at = now
                    item.access_until = subscription.access_until
                    item.save(update_fields=["deleted_at", "access_until"])
                
                    # 2. revive or create new item
                    new_item = SubscriptionItem.objects.filter(
                        subscription=subscription,
                        member_id=item.member_id,
                        plan_id=new_plan_id,
                    ).first()
                
                    if new_item:
                        new_item.deleted_at = None
                        new_item.access_start = item.access_until
                        new_item.source_item = item
                        new_item.price_at_subscription = new_price
                        new_item.stripe_price_id_at_subscription = new_price_id
                        if stripe_id:
                            new_item.stripe_subscription_item_id = stripe_id
                        new_item.save()
                
                    else:
                        SubscriptionItem.objects.create(
                            subscription=subscription,
                            member_id=item.member_id,
                            plan_id=new_plan_id,
                            price_at_subscription=new_price,
                            stripe_price_id_at_subscription=new_price_id,
                            source_item=item,
                            access_start=item.access_until,
                            stripe_subscription_item_id=payload.get("new_stripe_item_id"),
                        )
                    
                    mutation.local_state_applied = True
                    mutation.status = SubscriptionMutation.Status.PROCESSING

                    mutation.save(update_fields=[
                        "local_state_applied",
                        "status",
                    ])
                    processed_mutation_ids.append(mutation.id) 

                elif mutation.type == SubscriptionMutation.MutationType.CANCEL_CHANGE_PLAN:

                    payload = mutation.payload or {}
    
                    new_item_id = payload.get("new_item_id")
                    old_item_id = payload.get("old_item_id")
                    
                    old_item = item  # mutation.item = old_item by design
                    
                    if not old_item:
                        continue
                
                    new_item = SubscriptionItem.objects.filter(
                        id=new_item_id,
                        subscription=subscription
                    ).first()
            
                    # -------------------------------------------------
                    # 1. RESTORE OLD ITEM
                    # -------------------------------------------------
                    old_item.deleted_at = None
                    old_item.access_until = None
                    old_item.source_item = None
                    old_item.save(update_fields=[
                        "deleted_at",
                        "access_until",
                        "source_item",
                    ])
                    
                    # -------------------------------------------------
                    # 2. SOFT CANCEL NEW ITEM (IMPORTANT)
                    # -------------------------------------------------
                    if new_item:
                        new_item.deleted_at = now
                        new_item.access_until = now
                        new_item.source_item = None
                        new_item.save(update_fields=[
                            "deleted_at",
                            "access_until",
                            "source_item",
                        ])
                    
                    mutation.local_state_applied = True
                    mutation.status = SubscriptionMutation.Status.PROCESSING

                    mutation.save(update_fields=[
                        "local_state_applied",
                        "status",
                    ])
                    processed_mutation_ids.append(mutation.id) 

                

                elif mutation.type == SubscriptionMutation.MutationType.CASH_TO_STRIPE:

                    logger.info(
                        "[CASH_TO_STRIPE] mutation=%s subscription=%s",
                        mutation.id,
                        subscription.id,
                    )
                
                    if not subscription.stripe_subscription_id:
                        logger.warning(
                            "[CASH_TO_STRIPE] mutation=%s has no Stripe "
                            "subscription yet. Retrying later.",
                            mutation.id,
                        )
                
                        mutation.status = SubscriptionMutation.Status.PROCESSING
                        mutation.save(update_fields=["status"])
                
                        continue
                
                    # The checkout webhook has already changed the local
                    # subscription to Stripe and linked the Stripe subscription.
                    #
                    # There is no additional local item mutation to perform here.
                    mutation.local_state_applied = True
                    mutation.status = SubscriptionMutation.Status.PROCESSING
                
                    mutation.save(update_fields=[
                        "local_state_applied",
                        "status",
                    ])

                    processed_mutation_ids.append(mutation.id) 


                                    



                
                    

                        
        stripe_sub = stripe.Subscription.retrieve(
            subscription.stripe_subscription_id,
            stripe_account=club.stripe_account_id,
            expand=["items.data"],
        )

        stripe_items = stripe_sub["items"]["data"]

        # -------------------------------------------------
        # 1. DB truth
        # -------------------------------------------------
        db_items = subscription.items.filter(deleted_at__isnull=True)

        db_by_price = defaultdict(list)
        for item in db_items:
            db_by_price[item.stripe_price_id_at_subscription].append(item)

        db_counts = {
            price_id: len(items)
            for price_id, items in db_by_price.items()
        }

        # -------------------------------------------------
        # 2. Stripe truth (IMPORTANT: price → LIST)
        # -------------------------------------------------
        stripe_by_price = defaultdict(list)
        for si in stripe_items:
            price_id = si["price"]["id"]
            stripe_by_price[price_id].append(si)

        # -------------------------------------------------
        # 3. SYNC DB → STRIPE (CREATE / UPDATE / FIX)
        # -------------------------------------------------
        for price_id, db_rows in db_by_price.items():
            desired_qty = len(db_rows)
            stripe_group = stripe_by_price.get(price_id, [])

            # CASE A: no Stripe items → create one
            if not stripe_group:
                created = stripe.SubscriptionItem.create(
                    proration_behavior="none",
                    subscription=subscription.stripe_subscription_id,
                    price=price_id,
                    quantity=desired_qty,
                    stripe_account=club.stripe_account_id,
                )

                with transaction.atomic():
                    for row in db_rows:
                        row.stripe_subscription_item_id = created["id"]
                        row.save(update_fields=["stripe_subscription_item_id"])

                continue

            # CASE B: Stripe exists → pick primary item
            primary = stripe_group[0]

            # sync quantity
            if primary["quantity"] != desired_qty:
                stripe.SubscriptionItem.modify(
                    primary["id"],
                    proration_behavior="none",
                    quantity=desired_qty,
                    stripe_account=club.stripe_account_id,
                )

            # ensure DB rows reference correct Stripe item
            with transaction.atomic():
                for row in db_rows:
                    if row.stripe_subscription_item_id != primary["id"]:
                        row.stripe_subscription_item_id = primary["id"]
                        row.save(update_fields=["stripe_subscription_item_id"])

            # cleanup extra Stripe items for same price (important fix)
            for extra in stripe_group[1:]:
                stripe.SubscriptionItem.delete(
                    extra["id"],
                    proration_behavior="none",
                    stripe_account=club.stripe_account_id,
                )

        # -------------------------------------------------
        # 4. DELETE orphan Stripe items (not in DB)
        # -------------------------------------------------
        for price_id, stripe_group in stripe_by_price.items():
            if price_id not in db_by_price:
                for si in stripe_group:
                    stripe.SubscriptionItem.delete(
                        si["id"],
                        stripe_account=club.stripe_account_id,
                        proration_behavior="none",
                    )

        # -------------------------------------------------
        # 5. SUBSCRIPTION-LEVEL RECONCILIATION (IMPORTANT ADDITION)
        # -------------------------------------------------

        has_active_items = len(db_items) > 0

        desired_cancel_flag = not has_active_items

        if subscription.cancel_at_period_end != desired_cancel_flag:
            stripe.Subscription.modify(
                subscription.stripe_subscription_id,
                cancel_at_period_end=desired_cancel_flag,
                stripe_account=club.stripe_account_id,
            )

            subscription.cancel_at_period_end = desired_cancel_flag
            subscription.save(update_fields=["cancel_at_period_end"])



        if processed_mutation_ids:

            SubscriptionMutation.objects.filter(
                id__in=processed_mutation_ids,
                local_state_applied=True,
            ).update(
                stripe_reconciliation_finished=True,
                status=SubscriptionMutation.Status.SUCCEEDED,
                processed_at=timezone.now(),
            )

            logger.info(
                "[RECONCILE] subscription=%s successfully "
                "reconciled mutations=%s",
                subscription.id,
                processed_mutation_ids,
            )

        # -------------------------------------------------
        # 6. RETURN DEBUG INFO
        # -------------------------------------------------
        return {
            "db_counts": db_counts,
            "stripe_items": len(stripe_items),
            "stripe_by_price": {
                k: len(v) for k, v in stripe_by_price.items()
            }
        }


class CacheLockError(Exception):
    pass


@contextmanager
def subscription_lock(subscription_id: int, timeout: int = 300):
    key = f"stripe_sub_lock:{subscription_id}"
    token = str(uuid.uuid4())

    acquired = cache.add(key, token, timeout=timeout)
    if not acquired:
        raise CacheLockError("SUBSCRIPTION_LOCKED")

    try:
        yield
    finally:
        # best-effort release only
        try:
            if cache.get(key) == token:
                cache.delete(key)
        except Exception:
            pass  # never block release path


class MembershipPlanDeletionReconciler:
    """
    Shared logic for cancelling active SubscriptionItems that belong
    to a MembershipPlan scheduled for deletion, and for finalizing the
    plan's deletion once no active items remain.

    Used by BOTH:
        - delete_membership_plan_task (fired once, right after an
          owner schedules a plan for deletion)
        - reconcile_scheduled_plan_deletions (periodic safety-net)

    Design notes / safety:
        - Reuses the existing per-subscription cache lock
          (subscription_lock) and the existing SubscriptionMutation
          locking (assert_mutation_not_locked, raised as
          MutationLockedError) instead of inventing new locking.
        - Re-checks item.deleted_at under the subscription lock before
          attempting to cancel, so running this twice (or running the
          one-shot task concurrently with the periodic reconciler) is
          safe - an already-cancelled item is simply skipped.
        - One member's cancellation failure never stops the others
          from being attempted.
        - The plan is only ever marked is_deleted=True once a DB
          re-check confirms there are zero remaining active items for
          it. That final UPDATE is itself idempotent (it targets
          is_deleted=False), so double-finalizing is harmless.
    """

    @staticmethod
    def process_plan(plan):
        # Imported locally to avoid a circular import:
        # service_subscription imports StripeSubscriptionReconciler
        # from this module.
        from .service_subscription import SubscriptionItemService

        stripe.api_key = settings.STRIPE_SECRET_KEY

        active_items = list(
            SubscriptionItem.objects
            .filter(plan=plan, deleted_at__isnull=True)
            .select_related("subscription", "member", "member__owner")
        )

        cancelled_owner_map = defaultdict(lambda: {
            "members": set(),
            "plans": set(),
            "access_until": None,
        })

        cancelled_count = 0
        failed_count = 0

        for item in active_items:
            subscription = item.subscription

            try:
                with subscription_lock(subscription.id, timeout=300):

                    # Re-read under the lock: another run (the
                    # one-shot task or the periodic reconciler) may
                    # have already cancelled this item.
                    item.refresh_from_db()

                    if item.deleted_at is not None:
                        continue

                    SubscriptionItemService.cancel_item(
                        item=item,
                        subscription=subscription,
                        club=plan.club,
                    )

            except CacheLockError:
                logger.info(
                    "[PLAN DELETE] subscription locked, will retry "
                    "later plan=%s subscription=%s item=%s",
                    plan.id, subscription.id, item.id,
                )
                failed_count += 1
                continue

            except MutationLockedError:
                logger.info(
                    "[PLAN DELETE] mutation locked, will retry later "
                    "plan=%s subscription=%s item=%s",
                    plan.id, subscription.id, item.id,
                )
                failed_count += 1
                continue

            except Exception:
                logger.exception(
                    "[PLAN DELETE] failed to cancel item=%s plan=%s "
                    "subscription=%s",
                    item.id, plan.id, subscription.id,
                )
                failed_count += 1
                continue

            cancelled_count += 1

            owner = item.member.owner if item.member else None

            if owner:
                group = cancelled_owner_map[owner.id]
                group["members"].add(item.member.full_name)
                group["plans"].add(plan.name)
                group["access_until"] = subscription.access_until

        # -----------------------------------------------------------
        # Only mark the plan actually deleted once a fresh DB query
        # confirms there are zero active items left. This must be
        # re-queried (not derived from the `active_items` snapshot
        # above) because other concurrent processes may also be
        # cancelling items for this plan.
        # -----------------------------------------------------------
        remaining_active = SubscriptionItem.objects.filter(
            plan=plan,
            deleted_at__isnull=True,
        ).exists()

        finalized = False

        if not remaining_active:
            updated = MembershipPlan.objects.filter(
                id=plan.id,
                is_deleted=False,
            ).update(
                is_deleted=True,
                deleted_at=timezone.now(),
            )
            finalized = bool(updated)

        if cancelled_owner_map:
            serializable_owner_map = {
                str(owner_id): {
                    "members": list(data["members"]),
                    "plans": list(data["plans"]),
                    "access_until": (
                        data["access_until"].isoformat()
                        if data["access_until"]
                        else None
                    ),
                }
                for owner_id, data in cancelled_owner_map.items()
            }

            # The cancellations/finalization above are already
            # committed at this point. A broker hiccup while enqueuing
            # the notification email must not be reported as if the
            # cancellation work itself failed.
            try:
                send_plan_deletion_emails.delay(serializable_owner_map)
            except Exception:
                logger.exception(
                    "[PLAN DELETE] Failed to enqueue deletion "
                    "notification emails for plan=%s",
                    plan.id,
                )

        result = {
            "plan_id": plan.id,
            "cancelled": cancelled_count,
            "failed": failed_count,
            "remaining_active": remaining_active and not finalized,
            "finalized": finalized,
        }

        logger.info("[PLAN DELETE] plan processed result=%s", result)

        return result

    @staticmethod
    def reconcile_all():
        plans = MembershipPlan.objects.filter(
            scheduled_for_deletion=True,
            is_deleted=False,
        )

        results = []

        for plan in plans:
            try:
                results.append(
                    MembershipPlanDeletionReconciler.process_plan(plan)
                )
            except Exception:
                logger.exception(
                    "[PLAN DELETE RECONCILE] Unexpected failure "
                    "plan=%s",
                    plan.id,
                )

        return results


class CheckoutSubscriptionReconciler:

    """
    Finds Stripe subscriptions created by member checkout
    that never received successful initialization in the app.

    Direction:
        Stripe -> orphan detection -> cleanup

    This does NOT repair DB from Stripe.
    If DB exists, mutation reconciliation owns consistency.
    """

    @staticmethod
    def reconcile_recent_checkouts():

        now = timezone.now()

        # Only inspect subscriptions where:
        # - webhook should have already arrived
        # - but not so old that we scan everything forever
        window_start = now - timedelta(days=5)
        window_end = now - timedelta(hours=1)

        logger.info(
            "[CHECKOUT RECONCILE] Starting scan window_start=%s window_end=%s",
            window_start,
            window_end,
        )

        clubs = Club.objects.filter(
            stripe_account_id__isnull=False
        )

        logger.info(
            "[CHECKOUT RECONCILE] Found clubs=%s",
            clubs.count(),
        )

        for club in clubs:

            logger.info(
                "[CHECKOUT RECONCILE] Checking club=%s stripe_account=%s",
                club.id,
                club.stripe_account_id,
            )

            subscriptions = stripe.Subscription.list(
                created={
                    "gte": int(window_start.timestamp()),
                    "lte": int(window_end.timestamp()),
                },
                status="all",
                limit=100,
                stripe_account=club.stripe_account_id,
            )

            logger.info(
                "[CHECKOUT RECONCILE] Stripe returned subscriptions for club=%s",
                club.id,
            )

            count = 0

            for stripe_sub in subscriptions.auto_paging_iter():

                count += 1

                logger.info(
                    "[CHECKOUT RECONCILE] Checking stripe subscription=%s status=%s metadata=%s",
                    stripe_sub.id,
                    stripe_sub.status,
                    stripe_sub.metadata,
                )

                CheckoutSubscriptionReconciler.check_subscription(
                    stripe_sub=stripe_sub,
                    club=club,
                )

            logger.info(
                "[CHECKOUT RECONCILE] Finished club=%s checked=%s subscriptions",
                club.id,
                count,
            )

        logger.info(
            "[CHECKOUT RECONCILE] Completed scan"
        )


    @staticmethod
    def check_subscription(*, stripe_sub, club):

        logger.info(
            "[ORPHAN CHECK] Starting subscription=%s",
            stripe_sub.id,
        )

        logger.info(
            "[ORPHAN CHECK] metadata=%s",
            stripe_sub.metadata,
        )


        # Only handle subscriptions created by your member checkout flow
        if stripe_sub.metadata.get("type") != "checkout":

            logger.info(
                "[ORPHAN CHECK] Ignored subscription=%s reason=wrong_source source=%s",
                stripe_sub.id,
                stripe_sub.metadata.get("type"),
            )

            return


        logger.info(
            "[ORPHAN CHECK] Subscription=%s passed source check",
            stripe_sub.id,
        )


        # Already initialized locally
        exists = Subscription.objects.filter(
            stripe_subscription_id=stripe_sub.id
        ).exists()


        logger.info(
            "[ORPHAN CHECK] subscription=%s exists_locally=%s",
            stripe_sub.id,
            exists,
        )


        if exists:

            logger.info(
                "[ORPHAN CHECK] Skipping subscription=%s reason=already_initialized",
                stripe_sub.id,
            )

            return


        # Nothing to clean if Stripe already ended it
        if stripe_sub.status in [
            "canceled",
            "incomplete_expired",
        ]:

            logger.info(
                "[ORPHAN CHECK] Skipping subscription=%s reason=already_finished status=%s",
                stripe_sub.id,
                stripe_sub.status,
            )

            return


        logger.info(
            "[ORPHAN CHECK] Found orphan subscription=%s. Canceling...",
            stripe_sub.id,
        )


        CheckoutSubscriptionReconciler.cancel_orphan(
            stripe_sub=stripe_sub,
            club=club,
        )


    @staticmethod
    def cancel_orphan(*, stripe_sub, club):

        logger.info(
            "[ORPHAN CLEANUP] Cancel request subscription=%s account=%s",
            stripe_sub.id,
            club.stripe_account_id,
        )


        result = stripe.Subscription.cancel(
            stripe_sub.id,
            stripe_account=club.stripe_account_id,
            idempotency_key=f"orphan_cleanup_{stripe_sub.id}",
        )


        logger.info(
            "[ORPHAN CLEANUP] Cancel completed subscription=%s status=%s",
            result.id,
            result.status,
        )


class StripeToCashInvoiceReconciler:
    
    STRIPE_STATUS_CHECK_INTERVAL = timedelta(hours=24)
    STRIPE_STATUS_CHECK_BATCH_SIZE = 10000

    @staticmethod
    def reconcile_canceled_subscription(*, subscription_id):
        """
        Reconcile one specific local Stripe subscription after Stripe
        has confirmed that the corresponding Stripe subscription was deleted.

        This is the targeted version used by the
        customer.subscription.deleted webhook.

        Unlike reconcile_canceled_stripe_subscriptions(), this method
        does NOT scan the database for subscriptions.

        It:

            1. Locks the local subscription.
            2. Confirms it is still a Stripe subscription.
            3. Moves local OPEN Stripe invoices to cash.
            4. Changes the subscription billing method to cash.
            5. Clears the Stripe subscription ID.
            6. Clears cancel_at_period_end.
            7. Restores past_due/unpaid to active.

        Paid invoices are untouched.
        Void invoices are untouched.
        """

        try:
            with subscription_lock(
                subscription_id,
                timeout=300,
            ):

                with transaction.atomic():

                    subscription = (
                        Subscription.objects
                        .select_for_update()
                        .select_related("club")
                        .get(id=subscription_id)
                    )

                    # -------------------------------------------------
                    # Already converted by another process.
                    # -------------------------------------------------

                    if subscription.billing_method != "stripe":

                        logger.info(
                            "[STRIPE CANCELED RECONCILE] "
                            "Subscription=%s already has "
                            "billing_method=%s. Nothing to do.",
                            subscription.id,
                            subscription.billing_method,
                        )

                        return "skipped"

                    if not subscription.stripe_subscription_id:

                        logger.info(
                            "[STRIPE CANCELED RECONCILE] "
                            "Subscription=%s has no Stripe "
                            "subscription ID. Nothing to do.",
                            subscription.id,
                        )

                        return "skipped"

                    stripe_subscription_id = (
                        subscription.stripe_subscription_id
                    )

                    # -------------------------------------------------
                    # Move LOCAL OPEN Stripe invoices to cash.
                    #
                    # Paid invoices remain paid.
                    # Void invoices remain void.
                    # Existing cash invoices remain unchanged.
                    # -------------------------------------------------

                    updated_invoice_count = (
                        Invoice.objects
                        .filter(
                            subscription=subscription,
                            status="open",
                            payment_method="stripe",
                        )
                        .update(
                            payment_method="cash",
                        )
                    )

                    # -------------------------------------------------
                    # Convert local subscription to cash.
                    # -------------------------------------------------

                    subscription.billing_method = "cash"
                    subscription.stripe_subscription_id = None
                    subscription.cancel_at_period_end = False

                    if subscription.status in [
                        "past_due",
                        "unpaid",
                    ]:
                        subscription.status = "active"

                    subscription.stripe_status_checked_at = (
                        timezone.now()
                    )

                    subscription.save(
                        update_fields=[
                            "billing_method",
                            "stripe_subscription_id",
                            "cancel_at_period_end",
                            "status",
                            "stripe_status_checked_at",
                        ]
                    )

                    logger.warning(
                        "[STRIPE CANCELED RECONCILE] "
                        "Targeted reconciliation completed. "
                        "subscription=%s "
                        "stripe_subscription=%s "
                        "billing_method=cash "
                        "invoices_changed=%s",
                        subscription.id,
                        stripe_subscription_id,
                        updated_invoice_count,
                    )

                    return "succeeded"

        except Subscription.DoesNotExist:

            logger.warning(
                "[STRIPE CANCELED RECONCILE] "
                "Local subscription=%s no longer exists.",
                subscription_id,
            )

            return "skipped"

        except CacheLockError:

            logger.info(
                "[STRIPE CANCELED RECONCILE] "
                "Subscription locked. subscription=%s",
                subscription_id,
            )

            raise

        except Exception:

            logger.exception(
                "[STRIPE CANCELED RECONCILE] "
                "Targeted reconciliation failed "
                "subscription=%s",
                subscription_id,
            )

            raise

    @staticmethod
    def reconcile_canceled_stripe_subscriptions():
        """
        Periodically check local Stripe subscriptions to make sure
        Stripe has not been canceled externally.

        Only a confirmed Stripe terminal status causes a local
        Stripe subscription to be converted to cash.

        Stripe API failures are NEVER treated as cancellation.

        Checked subscriptions are marked with stripe_status_checked_at
        so they are not repeatedly checked on every task run.
        """

        now = timezone.now()
        check_before = (
            now
            - StripeToCashInvoiceReconciler.STRIPE_STATUS_CHECK_INTERVAL
        )

        subscriptions = (
            Subscription.objects
            .filter(
                billing_method="stripe",
                stripe_subscription_id__isnull=False,
            )
            .filter(
                Q(stripe_status_checked_at__isnull=True)
                | Q(stripe_status_checked_at__lt=check_before)
            )
            .select_related("club")
            .order_by(
                "stripe_status_checked_at",
                "id",
            )[
                :StripeToCashInvoiceReconciler.STRIPE_STATUS_CHECK_BATCH_SIZE
            ]
        )

        checked = 0
        processed = 0
        skipped = 0
        failed = 0

        logger.info(
            "[STRIPE CANCELED RECONCILE] Starting scan "
            "batch_size=%s check_before=%s",
            StripeToCashInvoiceReconciler.STRIPE_STATUS_CHECK_BATCH_SIZE,
            check_before,
        )

        for subscription in subscriptions:

            checked += 1

            try:

                with subscription_lock(
                    subscription.id,
                    timeout=300,
                ):

                    # -------------------------------------------------
                    # Re-read after acquiring the lock.
                    # -------------------------------------------------

                    subscription.refresh_from_db()

                    if (
                        subscription.billing_method != "stripe"
                        or not subscription.stripe_subscription_id
                    ):
                        skipped += 1
                        continue

                    club = subscription.club

                    stripe_subscription_id = (
                        subscription.stripe_subscription_id
                    )

                    # -------------------------------------------------
                    # Retrieve the actual Stripe subscription.
                    #
                    # IMPORTANT:
                    # An API error is NOT treated as cancellation.
                    # -------------------------------------------------

                    try:

                        stripe_subscription = (
                            stripe.Subscription.retrieve(
                                stripe_subscription_id,
                                stripe_account=club.stripe_account_id,
                            )
                        )

                    except stripe.error.StripeError:

                        logger.exception(
                            "[STRIPE CANCELED RECONCILE] "
                            "Could not retrieve Stripe subscription=%s "
                            "for local subscription=%s. "
                            "Leaving local state unchanged.",
                            stripe_subscription_id,
                            subscription.id,
                        )

                        failed += 1
                        continue

                    logger.info(
                        "[STRIPE CANCELED RECONCILE] "
                        "subscription=%s stripe_subscription=%s "
                        "status=%s cancel_at_period_end=%s",
                        subscription.id,
                        stripe_subscription.id,
                        stripe_subscription.status,
                        stripe_subscription.cancel_at_period_end,
                    )

                    # -------------------------------------------------
                    # Stripe explicitly says the subscription has ended.
                    # -------------------------------------------------

                    if stripe_subscription.status in [
                        "canceled",
                        "incomplete_expired",
                    ]:

                        with transaction.atomic():

                            subscription = (
                                Subscription.objects
                                .select_for_update()
                                .get(id=subscription.id)
                            )

                            # Another process may have already handled it.
                            if (
                                subscription.billing_method != "stripe"
                                or not subscription.stripe_subscription_id
                            ):
                                skipped += 1
                                continue

                            # -------------------------------------------------
                            # Move LOCAL OPEN invoices to cash.
                            #
                            # Paid invoices remain paid.
                            # Void invoices remain void.
                            # -------------------------------------------------

                            updated_invoice_count = (
                                Invoice.objects
                                .filter(
                                    subscription=subscription,
                                    status="open",
                                    payment_method="stripe",
                                )
                                .update(
                                    payment_method="cash",
                                )
                            )

                            # -------------------------------------------------
                            # Convert subscription to cash.
                            # -------------------------------------------------

                            subscription.billing_method = "cash"
                            subscription.stripe_subscription_id = None
                            subscription.cancel_at_period_end = False

                            if subscription.status in [
                                "past_due",
                                "unpaid",
                            ]:
                                subscription.status = "active"

                            subscription.stripe_status_checked_at = now

                            subscription.save(
                                update_fields=[
                                    "billing_method",
                                    "stripe_subscription_id",
                                    "cancel_at_period_end",
                                    "status",
                                    "stripe_status_checked_at",
                                ]
                            )

                            logger.warning(
                                "[STRIPE CANCELED RECONCILE] "
                                "Converted subscription=%s to cash "
                                "after confirmed Stripe cancellation. "
                                "stripe_subscription=%s "
                                "invoices_changed=%s",
                                subscription.id,
                                stripe_subscription.id,
                                updated_invoice_count,
                            )

                        processed += 1
                        continue

                    # -------------------------------------------------
                    # Stripe is still alive.
                    #
                    # We simply record that we successfully checked it.
                    # -------------------------------------------------

                    with transaction.atomic():

                        subscription = (
                            Subscription.objects
                            .select_for_update()
                            .get(id=subscription.id)
                        )

                        if (
                            subscription.billing_method != "stripe"
                            or not subscription.stripe_subscription_id
                        ):
                            skipped += 1
                            continue

                        subscription.stripe_status_checked_at = now

                        subscription.save(
                            update_fields=[
                                "stripe_status_checked_at",
                            ]
                        )

                    skipped += 1

            except CacheLockError:

                logger.info(
                    "[STRIPE CANCELED RECONCILE] "
                    "Subscription locked. subscription=%s",
                    subscription.id,
                )

                skipped += 1

            except Exception:

                logger.exception(
                    "[STRIPE CANCELED RECONCILE] "
                    "Unexpected failure subscription=%s",
                    subscription.id,
                )

                failed += 1

        logger.info(
            "[STRIPE CANCELED RECONCILE] Finished "
            "checked=%s processed=%s skipped=%s failed=%s",
            checked,
            processed,
            skipped,
            failed,
        )

        return {
            "checked": checked,
            "processed": processed,
            "skipped": skipped,
            "failed": failed,
        }  
  
    FAILURE_GRACE_DAYS = 10

    @staticmethod
    def reconcile_recent_invoices():
        """
        Find invoices that have been in Stripe payment failure for
        at least FAILURE_GRACE_DAYS and reconcile them.

        Also picks up invoices whose transition was already started
        but not marked succeeded.

        This makes interrupted transitions repairable.
        """

        now = timezone.now()
        cutoff = now - timedelta(
            days=StripeToCashInvoiceReconciler.FAILURE_GRACE_DAYS
        )

        invoices = (
            Invoice.objects
            .filter(
                subscription__isnull=False,
            )
            .filter(
                Q(
                    payment_method="stripe",
                    status="open",
                    stripe_payment_failed_at__isnull=False,
                    stripe_payment_failed_at__lte=cutoff,
                )
                |
                Q(
                    stripe_cash_transition_status="started",
                )
            )
            .exclude(
                stripe_cash_transition_status="succeeded",
            )
            .select_related(
                "subscription",
                "subscription__club",
            )
            .order_by("id")
        )

        logger.info(
            "[STRIPE TO CASH] Starting invoice reconciliation "
            "cutoff=%s",
            cutoff,
        )

        processed = 0
        skipped = 0
        failed = 0

        for invoice in invoices:

            subscription = invoice.subscription

            if not subscription:
                logger.warning(
                    "[STRIPE TO CASH] Invoice=%s has no subscription. "
                    "Skipping.",
                    invoice.id,
                )
                skipped += 1
                continue

            if not subscription.stripe_subscription_id:
                logger.warning(
                    "[STRIPE TO CASH] Invoice=%s subscription=%s "
                    "has no Stripe subscription ID. Skipping.",
                    invoice.id,
                    subscription.id,
                )
                skipped += 1
                continue

            try:

                with subscription_lock(
                    subscription.id,
                    timeout=300,
                ):

                    result = (
                        StripeToCashInvoiceReconciler
                        .reconcile_invoice(
                            invoice_id=invoice.id,
                        )
                    )

                    if result == "succeeded":
                        processed += 1

                    elif result == "skipped":
                        skipped += 1

                    else:
                        failed += 1

            except CacheLockError:

                logger.info(
                    "[STRIPE TO CASH] Subscription locked. "
                    "Skipping subscription=%s",
                    subscription.id,
                )

                skipped += 1

            except Invoice.DoesNotExist:

                logger.warning(
                    "[STRIPE TO CASH] Invoice=%s no longer exists.",
                    invoice.id,
                )

                skipped += 1

            except Exception:

                logger.exception(
                    "[STRIPE TO CASH] Unexpected failure "
                    "invoice=%s subscription=%s",
                    invoice.id,
                    subscription.id,
                )

                failed += 1

        logger.info(
            "[STRIPE TO CASH] Reconciliation finished "
            "processed=%s skipped=%s failed=%s",
            processed,
            skipped,
            failed,
        )

        return {
            "processed": processed,
            "skipped": skipped,
            "failed": failed,
        }

    @staticmethod
    def reconcile_invoice(*, invoice_id):
        """
        Reconcile one invoice.

        Determines whether this is:

            subscription_cycle
                → subscription-level migration

            anything else
                → invoice-only migration
        """

        local_invoice = (
            Invoice.objects
            .select_related(
                "subscription",
                "subscription__club",
            )
            .get(id=invoice_id)
        )

        subscription = local_invoice.subscription

        if not subscription:
            logger.warning(
                "[STRIPE TO CASH] Invoice=%s has no subscription.",
                local_invoice.id,
            )
            return "skipped"

        club = subscription.club

        if not subscription.stripe_subscription_id:
            logger.warning(
                "[STRIPE TO CASH] Invoice=%s subscription=%s "
                "has no Stripe subscription ID.",
                local_invoice.id,
                subscription.id,
            )
            return "skipped"

        # ---------------------------------------------------------
        # START / RESUME TRANSITION
        # ---------------------------------------------------------

        if (
            local_invoice.stripe_cash_transition_status
            != "started"
        ):

            local_invoice.stripe_cash_transition_status = "started"

            local_invoice.save(
                update_fields=[
                    "stripe_cash_transition_status",
                ]
            )

            logger.warning(
                "[STRIPE TO CASH] Starting transition "
                "invoice=%s subscription=%s "
                "billing_reason=%s failed_at=%s",
                local_invoice.id,
                subscription.id,
                local_invoice.billing_reason,
                local_invoice.stripe_payment_failed_at,
            )

        else:

            logger.info(
                "[STRIPE TO CASH] Resuming transition "
                "invoice=%s subscription=%s "
                "billing_reason=%s",
                local_invoice.id,
                subscription.id,
                local_invoice.billing_reason,
            )

        # ---------------------------------------------------------
        # TRIGGERING STRIPE INVOICE
        # ---------------------------------------------------------

        if not local_invoice.stripe_invoice_id:
            logger.warning(
                "[STRIPE TO CASH] Invoice=%s has no "
                "stripe_invoice_id.",
                local_invoice.id,
            )
            return "failed"

        stripe_invoice = stripe.Invoice.retrieve(
            local_invoice.stripe_invoice_id,
            stripe_account=club.stripe_account_id,
        )

        logger.info(
            "[STRIPE TO CASH] Trigger invoice=%s "
            "Stripe status=%s billing_reason=%s",
            stripe_invoice.id,
            stripe_invoice.status,
            local_invoice.billing_reason,
        )

        # ---------------------------------------------------------
        # PAYMENT WON
        #
        # Do not convert anything to cash.
        # invoice.paid is responsible for the successful payment.
        # ---------------------------------------------------------

        if stripe_invoice.status == "paid":

            logger.info(
                "[STRIPE TO CASH] Invoice=%s is already paid. "
                "Stopping transition.",
                stripe_invoice.id,
            )

            return "skipped"

        # ---------------------------------------------------------
        # VOID TRIGGERING INVOICE
        # ---------------------------------------------------------

        stripe_invoice = (
            StripeToCashInvoiceReconciler
            .ensure_stripe_invoice_void(
                stripe_invoice=stripe_invoice,
                club=club,
            )
        )

        if not stripe_invoice:
            return "failed"

        # Stripe payment won during the void attempt.
        if stripe_invoice.status == "paid":

            logger.warning(
                "[STRIPE TO CASH] Invoice=%s was paid while "
                "fallback was processing. Stopping.",
                stripe_invoice.id,
            )

            return "skipped"

        if stripe_invoice.status != "void":

            logger.warning(
                "[STRIPE TO CASH] Invoice=%s could not be "
                "confirmed void. status=%s.",
                stripe_invoice.id,
                stripe_invoice.status,
            )

            return "failed"

        # ---------------------------------------------------------
        # ROUTE BASED ON BILLING REASON
        # ---------------------------------------------------------

        if local_invoice.billing_reason in [
            "initial_subscription",
            "subscription_cycle",
        ]:

            return (
                StripeToCashInvoiceReconciler
                .reconcile_subscription_invoice(
                    invoice_id=local_invoice.id,
                )
            )

        return (
            StripeToCashInvoiceReconciler
            .reconcile_non_cycle_invoice(
                invoice_id=local_invoice.id,
            )
        )

    @staticmethod
    def ensure_stripe_invoice_void(
        *,
        stripe_invoice,
        club,
    ):
        """
        Make sure an unpaid Stripe invoice is void.

        Returns:
            Stripe invoice object after verification.

        Returns None if the invoice cannot safely be transitioned.

        Paid:
            returned unchanged.

        Void:
            returned unchanged.

        Open/uncollectible:
            voided and retrieved again.

        Draft/unknown:
            None.
        """

        if stripe_invoice.status == "paid":

            return stripe_invoice

        if stripe_invoice.status == "void":

            return stripe_invoice

        if stripe_invoice.status not in [
            "open",
            "uncollectible",
        ]:

            logger.warning(
                "[STRIPE TO CASH] Stripe invoice=%s has "
                "unsupported status=%s. Cannot void.",
                stripe_invoice.id,
                stripe_invoice.status,
            )

            return None

        logger.info(
            "[STRIPE TO CASH] Voiding Stripe invoice=%s "
            "status=%s",
            stripe_invoice.id,
            stripe_invoice.status,
        )

        try:

            stripe.Invoice.void_invoice(
                stripe_invoice.id,
                stripe_account=club.stripe_account_id,
                idempotency_key=(
                    f"stripe_to_cash_void_{stripe_invoice.id}"
                ),
            )

        except stripe.error.InvalidRequestError as e:

            logger.warning(
                "[STRIPE TO CASH] Void request failed "
                "invoice=%s: %s",
                stripe_invoice.id,
                e,
            )

        # ---------------------------------------------------------
        # ALWAYS RETRIEVE AGAIN
        # ---------------------------------------------------------

        refreshed = stripe.Invoice.retrieve(
            stripe_invoice.id,
            stripe_account=club.stripe_account_id,
        )

        logger.info(
            "[STRIPE TO CASH] Stripe invoice=%s "
            "status after void attempt=%s",
            refreshed.id,
            refreshed.status,
        )

        return refreshed

    @staticmethod
    def reconcile_non_cycle_invoice(*, invoice_id):
        """
        Handle a failed non-cycle invoice.

        Only the triggering invoice is moved to cash.

        The subscription remains Stripe.
        The Stripe subscription remains active.
        """

        with transaction.atomic():

            local_invoice = (
                Invoice.objects
                .select_for_update()
                .select_related(
                    "subscription",
                    "subscription__club",
                )
                .get(id=invoice_id)
            )

            subscription = local_invoice.subscription

            if not subscription:
                return "skipped"

            club = subscription.club

            if not local_invoice.stripe_invoice_id:
                return "failed"

            stripe_invoice = stripe.Invoice.retrieve(
                local_invoice.stripe_invoice_id,
                stripe_account=club.stripe_account_id,
            )

            # -----------------------------------------------------
            # Stripe won the race.
            # -----------------------------------------------------

            if stripe_invoice.status == "paid":

                logger.info(
                    "[STRIPE TO CASH] Non-cycle invoice=%s "
                    "was paid. Leaving local invoice unchanged.",
                    local_invoice.id,
                )

                return "skipped"

            # -----------------------------------------------------
            # Make sure Stripe invoice is void.
            # -----------------------------------------------------

            stripe_invoice = (
                StripeToCashInvoiceReconciler
                .ensure_stripe_invoice_void(
                    stripe_invoice=stripe_invoice,
                    club=club,
                )
            )

            if not stripe_invoice:
                return "failed"

            if stripe_invoice.status == "paid":

                logger.warning(
                    "[STRIPE TO CASH] Non-cycle invoice=%s "
                    "was paid during transition.",
                    local_invoice.id,
                )

                return "skipped"

            if stripe_invoice.status != "void":

                logger.warning(
                    "[STRIPE TO CASH] Non-cycle invoice=%s "
                    "is not confirmed void. status=%s.",
                    local_invoice.id,
                    stripe_invoice.status,
                )

                return "failed"

            # -----------------------------------------------------
            # Local invoice remains OPEN.
            #
            # Only collection method changes.
            # -----------------------------------------------------

            local_invoice.payment_method = "cash"
            local_invoice.stripe_cash_transition_status = "succeeded"

            local_invoice.save(
                update_fields=[
                    "payment_method",
                    "stripe_cash_transition_status",
                ]
            )

            transaction.on_commit(
                lambda invoice_id=local_invoice.id:
                    send_stripe_cash_transition_email.delay(invoice_id)
            )

            logger.warning(
                "[STRIPE TO CASH] Non-cycle invoice=%s "
                "moved from Stripe collection to cash. "
                "subscription=%s remains Stripe.",
                local_invoice.id,
                subscription.id,
            )

        return "succeeded"

    @staticmethod
    def reconcile_subscription_invoice(*, invoice_id):
        """
        Handle a failed subscription-cycle invoice.

        This performs a subscription-level Stripe → cash migration.

        Steps:

            1. Ensure triggering invoice is void.
            2. Find all Stripe invoices belonging to subscription.
            3. Leave paid invoices alone.
            4. Leave void invoices alone.
            5. Void open/uncollectible invoices.
            6. Verify every relevant Stripe invoice.
            7. Atomically change all local open invoices to cash.
            8. Change subscription billing_method to cash.
            9. Cancel Stripe subscription.
            10. Verify Stripe subscription cancellation.
            11. Mark triggering invoice transition succeeded.
        """

        local_invoice = (
            Invoice.objects
            .select_related(
                "subscription",
                "subscription__club",
            )
            .get(id=invoice_id)
        )

        subscription = local_invoice.subscription
        club = subscription.club

        # ---------------------------------------------------------
        # FIND ALL STRIPE INVOICES FOR SUBSCRIPTION
        # ---------------------------------------------------------

        logger.warning(
            "[STRIPE TO CASH] Cycle invoice=%s triggered "
            "subscription-level migration subscription=%s",
            local_invoice.id,
            subscription.id,
        )

        stripe_invoices = stripe.Invoice.list(
            subscription=subscription.stripe_subscription_id,
            limit=100,
            stripe_account=club.stripe_account_id,
        )

        all_stripe_invoices_safe = True

        for stripe_invoice in stripe_invoices.auto_paging_iter():

            logger.info(
                "[STRIPE TO CASH] Checking subscription=%s "
                "Stripe invoice=%s status=%s",
                subscription.id,
                stripe_invoice.id,
                stripe_invoice.status,
            )

            # -----------------------------------------------------
            # Paid → leave it alone.
            # -----------------------------------------------------

            if stripe_invoice.status == "paid":
                continue

            # -----------------------------------------------------
            # Already void → leave it alone.
            # -----------------------------------------------------

            if stripe_invoice.status == "void":
                continue

            # -----------------------------------------------------
            # Open / uncollectible → void and verify.
            # -----------------------------------------------------

            if stripe_invoice.status in [
                "open",
                "uncollectible",
            ]:

                result = (
                    StripeToCashInvoiceReconciler
                    .ensure_stripe_invoice_void(
                        stripe_invoice=stripe_invoice,
                        club=club,
                    )
                )

                if not result:
                    all_stripe_invoices_safe = False
                    break

                # Stripe won the race.
                if result.status == "paid":
                    continue

                if result.status != "void":

                    logger.warning(
                        "[STRIPE TO CASH] Stripe invoice=%s "
                        "could not be confirmed void. status=%s.",
                        result.id,
                        result.status,
                    )

                    all_stripe_invoices_safe = False
                    break

                continue

            # -----------------------------------------------------
            # Draft or unexpected status.
            # -----------------------------------------------------

            logger.warning(
                "[STRIPE TO CASH] Stripe invoice=%s "
                "has unsupported status=%s. "
                "Stopping subscription migration.",
                stripe_invoice.id,
                stripe_invoice.status,
            )

            all_stripe_invoices_safe = False
            break

        if not all_stripe_invoices_safe:

            logger.warning(
                "[STRIPE TO CASH] Could not safely clean all "
                "Stripe invoices for subscription=%s. "
                "Local billing state unchanged.",
                subscription.id,
            )

            return "failed"

        # ---------------------------------------------------------
        # ATOMIC LOCAL TRANSITION
        #
        # Only subscription-level invoices reach this point.#
        # All local open invoices become cash.
        # Subscription becomes cash.
        # ---------------------------------------------------------

        with transaction.atomic():

            subscription = (
                Subscription.objects
                .select_for_update()
                .get(id=subscription.id)
            )

            updated_invoice_count = (
                Invoice.objects
                .filter(
                    subscription=subscription,
                    status="open",
                )
                .update(
                    payment_method="cash",
                )
            )

            subscription.billing_method = "cash"

            if subscription.status == "past_due":
                subscription.status = "active"

            subscription.save(
                update_fields=[
                    "billing_method",
                    "status",
                ]
            )

            logger.info(
                "[STRIPE TO CASH] Local cycle transition committed "
                "subscription=%s invoices_changed=%s "
                "billing_method=cash status=%s",
                subscription.id,
                updated_invoice_count,
                subscription.status,
            )

        # ---------------------------------------------------------
        # CANCEL STRIPE SUBSCRIPTION
        #
        # Outside DB transaction.
        #
        # If this fails, the triggering invoice remains "started"
        # and the next reconciliation run will repair it.
        # ---------------------------------------------------------

        stripe_sub = stripe.Subscription.retrieve(
            subscription.stripe_subscription_id,
            stripe_account=club.stripe_account_id,
        )

        if stripe_sub.status not in [
            "canceled",
            "incomplete_expired",
        ]:

            logger.warning(
                "[STRIPE TO CASH] Canceling Stripe subscription=%s "
                "status=%s",
                stripe_sub.id,
                stripe_sub.status,
            )

            try:

                stripe.Subscription.cancel(
                    stripe_sub.id,
                    stripe_account=club.stripe_account_id,
                    idempotency_key=(
                        f"stripe_to_cash_cancel_{subscription.id}"
                    ),
                )

            except stripe.error.InvalidRequestError as e:

                logger.warning(
                    "[STRIPE TO CASH] Could not cancel Stripe "
                    "subscription=%s: %s",
                    stripe_sub.id,
                    e,
                )

            # -----------------------------------------------------
            # Verify cancellation.
            # -----------------------------------------------------

            stripe_sub = stripe.Subscription.retrieve(
                subscription.stripe_subscription_id,
                stripe_account=club.stripe_account_id,
            )

        # ---------------------------------------------------------
        # ONLY MARK SUCCEEDED AFTER STRIPE IS CONFIRMED CANCELED
        # ---------------------------------------------------------

        if stripe_sub.status not in [
            "canceled",
            "incomplete_expired",
        ]:

            logger.warning(
                "[STRIPE TO CASH] Subscription=%s is locally cash "
                "but Stripe subscription=%s is still status=%s. "
                "Leaving transition started for retry.",
                subscription.id,
                stripe_sub.id,
                stripe_sub.status,
            )

            return "failed"

        with transaction.atomic():

            local_invoice = (
                Invoice.objects
                .select_for_update()
                .get(id=invoice_id)
            )

            subscription.stripe_subscription_id = None
            subscription.cancel_at_period_end = False
            subscription.save(
                update_fields=[
                    "stripe_subscription_id",
                    "cancel_at_period_end",
                ]
            )

            local_invoice.stripe_cash_transition_status = "succeeded"

            local_invoice.save(
                update_fields=[
                    "stripe_cash_transition_status",
                ]
            )

            transaction.on_commit(
                lambda invoice_id=local_invoice.id:
                    send_stripe_cash_transition_email.delay(invoice_id)
            )

        logger.warning(
            "[STRIPE TO CASH] Successfully completed "
            "subscription-level Stripe → cash transition "
            "trigger_invoice=%s subscription=%s",
            invoice_id,
            subscription.id,
        )

        return "succeeded"


class MemberReservationPaymentReconciler:
    """
    Reconciles old unpaid member, visitor, and trial reservations
    against Stripe.

    There are two Stripe payment paths:

    1. Existing Stripe customer/payment method
       ----------------------------------------
       Reservation -> PaymentIntent

       The PaymentIntent is created and confirmed immediately.
       If the application crashes after Stripe succeeds but before
       the local reservation is marked PAID, reconciliation can
       recover the payment.

    2. Stripe Checkout
       ----------------
       Reservation -> Checkout Session

       The Checkout Session is allowed to expire after the configured
       Checkout lifetime. The webhook normally marks the reservation
       PAID, but reconciliation handles missed/delayed webhooks.

    Important safety rule:

        Reservation age alone NEVER determines whether a reservation
        should be deleted.

    The reservation must first be reconciled against Stripe.

    Existing-card PaymentIntent:
        succeeded
            -> PAID

        requires_payment_method / canceled
            -> DELETE

        processing / requires_action / requires_confirmation /
        requires_capture
            -> WAIT

    Checkout Session:
        payment_status == paid
            -> PAID

        status == expired
            -> DELETE

        open / unpaid
            -> WAIT

        Stripe/API error
            -> WAIT / FAILED
            never delete
    """

    HOLD_MINUTES = 31

    # ---------------------------------------------------------
    # PaymentIntent statuses
    # ---------------------------------------------------------

    TERMINAL_PAYMENT_INTENT_FAILURE_STATUSES = {
        "requires_payment_method",
        "canceled",
    }

    NON_TERMINAL_PAYMENT_INTENT_STATUSES = {
        "requires_confirmation",
        "requires_action",
        "processing",
        "requires_capture",
    }

    # ---------------------------------------------------------
    # Checkout Session statuses
    # ---------------------------------------------------------

    CHECKOUT_EXPIRED_STATUS = "expired"

    @staticmethod
    def _checkout_matches_reservation(metadata, reservation_id):
        if str(metadata.get("reservation_id")) == str(reservation_id):
            return True

        raw_ids = metadata.get("reservation_ids") or ""
        return str(reservation_id) in {
            part.strip()
            for part in str(raw_ids).split(",")
            if part.strip()
        }

    @staticmethod
    def _queue_customer_paid_email(reservation):
        if reservation.reservation_type not in (
            Reservation.ReservationType.VISITOR,
            Reservation.ReservationType.TRIAL,
        ):
            return

        reservation_id = reservation.id

        def send(reservation_id=reservation_id):
            from .tasks_emails import (
                send_visitor_reservation_confirmation_email,
            )

            send_visitor_reservation_confirmation_email.delay(
                reservation_id
            )

        transaction.on_commit(send)

    @classmethod
    def reconcile_old_unpaid_reservations(cls):
        """
        Find old unpaid reservations and reconcile them against
        the Stripe object associated with the reservation.

        Only reservations older than HOLD_MINUTES are considered.
        Visitor and trial holds start when checkout starts, not
        when the email request was created.

        Stripe/API failures never cause destructive cleanup.
        """

        now = timezone.now()

        cutoff = (
            now
            - timedelta(minutes=cls.HOLD_MINUTES)
        )

        hold_expired = Q(checkout_started_at__lt=cutoff) | Q(
            checkout_started_at__isnull=True,
            created_at__lt=cutoff,
        )

        reservations = (
            Reservation.objects
            .filter(
                status=Reservation.Status.UNPAID,
                payment_method="stripe",
                reservation_type__in=[
                    Reservation.ReservationType.MEMBER,
                    Reservation.ReservationType.VISITOR,
                    Reservation.ReservationType.TRIAL,
                ],
            )
            .filter(hold_expired)
            .select_related("club")
            .order_by(
                "created_at",
                "id",
            )
        )

        checked = 0
        paid = 0
        deleted = 0
        waiting = 0
        skipped = 0
        failed = 0

        logger.info(
            "[MEMBER RESERVATION RECONCILE] "
            "Starting scan cutoff=%s",
            cutoff,
        )

        for reservation in reservations:

            checked += 1

            try:

                result = cls.reconcile_reservation(
                    reservation_id=reservation.id
                )

                if result == "paid":
                    paid += 1

                elif result == "deleted":
                    deleted += 1

                elif result == "waiting":
                    waiting += 1

                elif result == "skipped":
                    skipped += 1

                else:
                    failed += 1

            except Reservation.DoesNotExist:

                skipped += 1

                logger.info(
                    "[MEMBER RESERVATION RECONCILE] "
                    "Reservation=%s disappeared before processing.",
                    reservation.id,
                )

            except Exception:

                failed += 1

                logger.exception(
                    "[MEMBER RESERVATION RECONCILE] "
                    "Unexpected error reservation=%s",
                    reservation.id,
                )

        logger.info(
            "[MEMBER RESERVATION RECONCILE] "
            "Finished checked=%s paid=%s deleted=%s "
            "waiting=%s skipped=%s failed=%s",
            checked,
            paid,
            deleted,
            waiting,
            skipped,
            failed,
        )

        return {
            "checked": checked,
            "paid": paid,
            "deleted": deleted,
            "waiting": waiting,
            "skipped": skipped,
            "failed": failed,
        }

    # =========================================================
    # SINGLE RESERVATION
    # =========================================================

    @classmethod
    def reconcile_reservation(
        cls,
        *,
        reservation_id,
    ):

        with transaction.atomic():

            reservation = (
                Reservation.objects
                .select_for_update()
                .select_related("club")
                .get(id=reservation_id)
            )

            # -------------------------------------------------
            # Another process already completed it.
            # -------------------------------------------------

            if (
                reservation.status
                == Reservation.Status.PAID
            ):

                logger.info(
                    "[MEMBER RESERVATION RECONCILE] "
                    "Reservation=%s already paid.",
                    reservation.id,
                )

                return "skipped"

            # -------------------------------------------------
            # It may have been deleted or changed by another
            # process between the initial query and acquiring
            # this lock.
            # -------------------------------------------------

            if (
                reservation.status
                != Reservation.Status.UNPAID
            ):

                logger.info(
                    "[MEMBER RESERVATION RECONCILE] "
                    "Reservation=%s status=%s. Skipping.",
                    reservation.id,
                    reservation.status,
                )

                return "skipped"

            # -------------------------------------------------
            # Member, visitor, and trial Stripe reservations.
            # -------------------------------------------------

            if (
                reservation.payment_method
                != "stripe"
                or reservation.reservation_type
                not in (
                    Reservation.ReservationType.MEMBER,
                    Reservation.ReservationType.VISITOR,
                    Reservation.ReservationType.TRIAL,
                )
            ):

                logger.info(
                    "[MEMBER RESERVATION RECONCILE] "
                    "Reservation=%s type=%s is not reconciled "
                    "here. Skipping.",
                    reservation.id,
                    reservation.reservation_type,
                )

                return "skipped"

            club = reservation.club

            if (
                not club
                or not club.stripe_account_id
            ):

                logger.warning(
                    "[MEMBER RESERVATION RECONCILE] "
                    "Reservation=%s has no Stripe account. "
                    "Leaving unchanged.",
                    reservation.id,
                )

                return "failed"

            # -------------------------------------------------
            # Re-check age after acquiring the lock.
            # -------------------------------------------------

            cutoff = (
                timezone.now()
                - timedelta(
                    minutes=cls.HOLD_MINUTES
                )
            )

            started_at = (
                reservation.checkout_started_at
                or reservation.created_at
            )

            if started_at >= cutoff:

                logger.info(
                    "[MEMBER RESERVATION RECONCILE] "
                    "Reservation=%s is not old enough yet.",
                    reservation.id,
                )

                return "skipped"

            # =================================================
            # CHECKOUT PATH
            # =================================================

            if (
                reservation.reservation_type
                in (
                    Reservation.ReservationType.VISITOR,
                    Reservation.ReservationType.TRIAL,
                )
                or reservation.stripe_checkout_session_id
            ):

                return cls._reconcile_checkout(
                    reservation=reservation,
                    club=club,
                )

            # =================================================
            # PAYMENT INTENT PATH
            # =================================================

            return cls._reconcile_payment_intent(
                reservation=reservation,
                club=club,
            )

    # =========================================================
    # PAYMENT INTENT RECONCILIATION
    # =========================================================

    @classmethod
    def _reconcile_payment_intent(
        cls,
        *,
        reservation,
        club,
    ):

        payment_intent = None

        # -----------------------------------------------------
        # Fast path:
        # locally stored PaymentIntent ID.
        # -----------------------------------------------------

        if reservation.stripe_payment_intent_id:

            try:

                payment_intent = (
                    stripe.PaymentIntent.retrieve(
                        reservation.stripe_payment_intent_id,
                        stripe_account=(
                            club.stripe_account_id
                        ),
                    )
                )

            except stripe.error.InvalidRequestError:

                logger.warning(
                    "[MEMBER RESERVATION RECONCILE] "
                    "Reservation=%s stored PaymentIntent=%s "
                    "could not be retrieved. Falling back "
                    "to metadata search.",
                    reservation.id,
                    reservation.stripe_payment_intent_id,
                )

                payment_intent = None

            except stripe.error.StripeError:

                logger.exception(
                    "[MEMBER RESERVATION RECONCILE] "
                    "Stripe error retrieving PaymentIntent=%s "
                    "reservation=%s. Leaving unchanged.",
                    reservation.stripe_payment_intent_id,
                    reservation.id,
                )

                return "failed"

        # -----------------------------------------------------
        # Recovery path:
        #
        # The PaymentIntent may have succeeded but the process
        # crashed before stripe_payment_intent_id was written
        # locally.
        #
        # reservation_id is stored in PaymentIntent metadata.
        # -----------------------------------------------------

        if payment_intent is None:

            try:

                search_result = (
                    stripe.PaymentIntent.search(
                        query=(
                            "metadata['reservation_id']:"
                            f"'{reservation.id}'"
                        ),
                        limit=10,
                        stripe_account=(
                            club.stripe_account_id
                        ),
                    )
                )

            except stripe.error.StripeError:

                logger.exception(
                    "[MEMBER RESERVATION RECONCILE] "
                    "Could not search PaymentIntents for "
                    "reservation=%s. Leaving unchanged.",
                    reservation.id,
                )

                return "failed"

            payment_intents = list(
                search_result.auto_paging_iter()
            )

            if payment_intents:

                # -------------------------------------------------
                # Prefer a successful PaymentIntent.
                # -------------------------------------------------

                succeeded = [
                    pi
                    for pi in payment_intents
                    if pi.status == "succeeded"
                ]

                if succeeded:

                    payment_intent = max(
                        succeeded,
                        key=lambda pi: pi.created or 0,
                    )

                else:

                    payment_intent = max(
                        payment_intents,
                        key=lambda pi: pi.created or 0,
                    )

        # -----------------------------------------------------
        # No PaymentIntent exists.
        #
        # This means the existing-card payment path never
        # created a PaymentIntent.
        #
        # Since this reservation is already older than the
        # hold period, there is nothing in Stripe that can
        # eventually turn it into a successful payment.
        # -----------------------------------------------------

        if payment_intent is None:

            logger.warning(
                "[MEMBER RESERVATION RECONCILE] "
                "Reservation=%s has no matching PaymentIntent. "
                "Deleting unpaid reservation.",
                reservation.id,
            )

            reservation.delete()

            return "deleted"

        logger.info(
            "[MEMBER RESERVATION RECONCILE] "
            "Reservation=%s PaymentIntent=%s status=%s",
            reservation.id,
            payment_intent.id,
            payment_intent.status,
        )

        # -----------------------------------------------------
        # SUCCESS
        # -----------------------------------------------------

        if payment_intent.status == "succeeded":

            reservation.status = (
                Reservation.Status.PAID
            )

            reservation.paid_at = (
                timezone.now()
            )

            reservation.stripe_payment_intent_id = (
                payment_intent.id
            )

            reservation.save(
                update_fields=[
                    "status",
                    "paid_at",
                    "stripe_payment_intent_id",
                ]
            )

            logger.warning(
                "[MEMBER RESERVATION RECONCILE] "
                "Recovered successful PaymentIntent. "
                "reservation=%s payment_intent=%s",
                reservation.id,
                payment_intent.id,
            )

            cls._queue_customer_paid_email(reservation)

            return "paid"

        # -----------------------------------------------------
        # DEFINITIVE FAILURE
        # -----------------------------------------------------

        if (
            payment_intent.status
            in cls.TERMINAL_PAYMENT_INTENT_FAILURE_STATUSES
        ):

            logger.warning(
                "[MEMBER RESERVATION RECONCILE] "
                "PaymentIntent=%s has terminal failure "
                "status=%s. Deleting reservation=%s.",
                payment_intent.id,
                payment_intent.status,
                reservation.id,
            )

            reservation.delete()

            return "deleted"

        # -----------------------------------------------------
        # STILL PROCESSING / REQUIRES ACTION
        #
        # Never delete these.
        # -----------------------------------------------------

        if (
            payment_intent.status
            in cls.NON_TERMINAL_PAYMENT_INTENT_STATUSES
        ):

            logger.info(
                "[MEMBER RESERVATION RECONCILE] "
                "PaymentIntent=%s status=%s. "
                "Leaving reservation=%s unchanged.",
                payment_intent.id,
                payment_intent.status,
                reservation.id,
            )

            # Store recovered PaymentIntent ID if necessary.
            if (
                reservation.stripe_payment_intent_id
                != payment_intent.id
            ):

                reservation.stripe_payment_intent_id = (
                    payment_intent.id
                )

                reservation.save(
                    update_fields=[
                        "stripe_payment_intent_id",
                    ]
                )

            return "waiting"

        # -----------------------------------------------------
        # Unknown Stripe status.
        #
        # Never make a destructive decision about a status
        # we do not explicitly understand.
        # -----------------------------------------------------

        logger.warning(
            "[MEMBER RESERVATION RECONCILE] "
            "PaymentIntent=%s has unknown status=%s. "
            "Leaving reservation=%s unchanged.",
            payment_intent.id,
            payment_intent.status,
            reservation.id,
        )

        return "waiting"

    # =========================================================
    # CHECKOUT RECONCILIATION
    # =========================================================

    @classmethod
    def _reconcile_checkout(
        cls,
        *,
        reservation,
        club,
    ):

        checkout_session = None

        # -----------------------------------------------------
        # Fast path:
        # locally stored Checkout Session ID.
        # -----------------------------------------------------

        if reservation.stripe_checkout_session_id:

            try:

                checkout_session = (
                    stripe.checkout.Session.retrieve(
                        reservation.stripe_checkout_session_id,
                        stripe_account=(
                            club.stripe_account_id
                        ),
                    )
                )

            except stripe.error.InvalidRequestError:

                logger.warning(
                    "[MEMBER RESERVATION RECONCILE] "
                    "Reservation=%s stored Checkout Session=%s "
                    "could not be retrieved. Falling back "
                    "to metadata search.",
                    reservation.id,
                    reservation.stripe_checkout_session_id,
                )

                checkout_session = None

            except stripe.error.StripeError:

                logger.exception(
                    "[MEMBER RESERVATION RECONCILE] "
                    "Stripe error retrieving Checkout Session=%s "
                    "reservation=%s. Leaving unchanged.",
                    reservation.stripe_checkout_session_id,
                    reservation.id,
                )

                return "failed"

        # -----------------------------------------------------
        # Recovery path:
        #
        # The Checkout Session may have been created successfully
        # but the application crashed before the Session ID was
        # saved locally.
        #
        # reservation_id exists in the Checkout metadata.
        # -----------------------------------------------------

        if checkout_session is None:

            try:

                sessions = (
                    stripe.checkout.Session.list(
                        limit=100,
                        stripe_account=(
                            club.stripe_account_id
                        ),
                    )
                )

            except stripe.error.StripeError:

                logger.exception(
                    "[MEMBER RESERVATION RECONCILE] "
                    "Could not list Checkout Sessions for "
                    "reservation=%s. Leaving unchanged.",
                    reservation.id,
                )

                return "failed"

            matching_sessions = []

            for session in sessions.auto_paging_iter():

                metadata = (
                    session.get("metadata", {})
                )

                if cls._checkout_matches_reservation(
                    metadata,
                    reservation.id,
                ):

                    matching_sessions.append(
                        session
                    )

            if matching_sessions:

                # Prefer a paid session.
                paid_sessions = [
                    session
                    for session in matching_sessions
                    if session.get("payment_status")
                    == "paid"
                ]

                if paid_sessions:

                    checkout_session = max(
                        paid_sessions,
                        key=lambda session:
                            session.get("created", 0),
                    )

                else:

                    checkout_session = max(
                        matching_sessions,
                        key=lambda session:
                            session.get("created", 0),
                    )

        # -----------------------------------------------------
        # No Checkout Session found.
        #
        # IMPORTANT:
        #
        # We do NOT immediately delete here.
        #
        # The session may have been created but Stripe's list
        # endpoint may not have returned it yet, or Stripe may
        # be experiencing a temporary API inconsistency.
        #
        # Since we don't have definitive Stripe evidence that
        # the checkout expired or failed, leave the reservation.
        # -----------------------------------------------------

        if checkout_session is None:

            logger.warning(
                "[MEMBER RESERVATION RECONCILE] "
                "Reservation=%s has no matching Checkout Session. "
                "Leaving reservation unchanged because Stripe "
                "state cannot be confirmed.",
                reservation.id,
            )

            return "waiting"

        session_id = checkout_session.id

        session_status = (
            checkout_session.get("status")
        )

        payment_status = (
            checkout_session.get("payment_status")
        )

        logger.info(
            "[MEMBER RESERVATION RECONCILE] "
            "Reservation=%s CheckoutSession=%s "
            "status=%s payment_status=%s",
            reservation.id,
            session_id,
            session_status,
            payment_status,
        )

        # -----------------------------------------------------
        # PAYMENT SUCCESS
        #
        # This is the missed-webhook recovery path.
        # -----------------------------------------------------

        if payment_status == "paid":

            payment_intent_id = (
                checkout_session.get(
                    "payment_intent"
                )
            )

            reservation.status = (
                Reservation.Status.PAID
            )

            reservation.paid_at = (
                timezone.now()
            )

            if payment_intent_id:

                reservation.stripe_payment_intent_id = (
                    payment_intent_id
                )

            reservation.stripe_checkout_session_id = (
                session_id
            )

            reservation.save(
                update_fields=[
                    "status",
                    "paid_at",
                    "stripe_payment_intent_id",
                    "stripe_checkout_session_id",
                ]
            )

            logger.warning(
                "[MEMBER RESERVATION RECONCILE] "
                "Recovered successful Checkout payment. "
                "reservation=%s session=%s payment_intent=%s",
                reservation.id,
                session_id,
                payment_intent_id,
            )

            cls._queue_customer_paid_email(reservation)

            return "paid"

        # -----------------------------------------------------
        # CHECKOUT EXPIRED
        #
        # Stripe explicitly confirms the checkout hold is dead.
        # This is safe to delete.
        # -----------------------------------------------------

        if (
            session_status
            == cls.CHECKOUT_EXPIRED_STATUS
        ):

            logger.warning(
                "[MEMBER RESERVATION RECONCILE] "
                "Checkout Session=%s expired without payment. "
                "Deleting reservation=%s.",
                session_id,
                reservation.id,
            )

            reservation.delete()

            return "deleted"

        # -----------------------------------------------------
        # CHECKOUT STILL OPEN / PAYMENT NOT COMPLETE
        #
        # Do not delete merely because the local reservation is
        # old. Stripe has not told us that the Checkout Session
        # has expired yet.
        # -----------------------------------------------------

        logger.info(
            "[MEMBER RESERVATION RECONCILE] "
            "Checkout Session=%s still active/unpaid. "
            "status=%s payment_status=%s. "
            "Leaving reservation=%s unchanged.",
            session_id,
            session_status,
            payment_status,
            reservation.id,
        )

        if (
            reservation.stripe_checkout_session_id
            != session_id
        ):

            reservation.stripe_checkout_session_id = (
                session_id
            )

            reservation.save(
                update_fields=[
                    "stripe_checkout_session_id",
                ]
            )

        return "waiting"

        