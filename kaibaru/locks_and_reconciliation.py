from django.core.cache import cache
from contextlib import contextmanager

import uuid
from django.db.models import Q

from collections import defaultdict
import stripe
from django.db import transaction
from django.utils import timezone
from .models import SubscriptionMutation, SubscriptionItem, Subscription, Club, Member, MembershipPlan, Invoice, InvoiceItem, Payment
from datetime import timedelta

from .tasks_emails import send_stripe_cash_transition_email

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
                    
                    mutation.status = SubscriptionMutation.Status.SUCCEEDED
                    mutation.processed_at = now
                    mutation.save(update_fields=[
                        "status",
                        "processed_at",
                    ])
    
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
                    
                    mutation.status = SubscriptionMutation.Status.SUCCEEDED
                    mutation.processed_at = now
                    mutation.save(update_fields=[
                        "status",
                        "processed_at",
                    ])


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
                
                
                    existing = SubscriptionItem.objects.filter(
                        subscription=subscription,
                        member=member,
                        plan=plan,
                    ).first()
                
                
                    if existing:
                        existing.deleted_at = None
                        existing.price_at_subscription = plan.price
                        existing.stripe_price_id_at_subscription = (
                            plan.stripe_price_id
                        )
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
                                if i["price"]["id"] == plan.stripe_price_id
                            ),
                            None
                        )
                
                        SubscriptionItem.objects.create(
                            subscription=subscription,
                            member=member,
                            plan=plan,
                            price_at_subscription=plan.price,
                            stripe_price_id_at_subscription=plan.stripe_price_id,
                            stripe_subscription_item_id=(
                                stripe_item["id"]
                                if stripe_item
                                else None
                            ),
                        )
                
                
                    if invoice.status == "paid":
                        mutation.invoice_status = (
                            SubscriptionMutation.InvoiceStatus.PAID
                        )
                    else:
                        mutation.invoice_status = (
                            SubscriptionMutation.InvoiceStatus.OPEN
                        )
                
                    mutation.status = (
                        SubscriptionMutation.Status.SUCCEEDED
                    )
                
                    mutation.processed_at = now
                
                    mutation.save(
                        update_fields=[
                            "invoice_status",
                            "status",
                            "processed_at",
                        ]
                    )
                
                                
                




                elif mutation.type == SubscriptionMutation.MutationType.CHANGE_PLAN:

                    payload = mutation.payload or {}
                
                    old_plan_id = payload["old_plan_id"]
                    new_plan_id = payload["new_plan_id"]
                    new_price_id = payload["new_price_id"]
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
                        if stripe_id:
                            new_item.stripe_subscription_item_id = stripe_id
                        new_item.save()
                
                    else:
                        SubscriptionItem.objects.create(
                            subscription=subscription,
                            member_id=item.member_id,
                            plan_id=new_plan_id,
                            price_at_subscription=new_price_id,
                            stripe_price_id_at_subscription=new_price_id,
                            source_item=item,
                            access_start=item.access_until,
                            stripe_subscription_item_id=payload.get("new_stripe_item_id"),
                        )
                    
                    mutation.status = SubscriptionMutation.Status.SUCCEEDED
                    mutation.processed_at = now
                    mutation.save(update_fields=[
                        "status",
                        "processed_at",
                    ])

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
                    
                    mutation.status = SubscriptionMutation.Status.SUCCEEDED
                    mutation.processed_at = now
                    mutation.save(update_fields=[
                        "status",
                        "processed_at",
                    ])
                        



                
                    

                        
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
        window_start = now - timedelta(days=10)
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


        