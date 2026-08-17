from django.core.cache import cache
from contextlib import contextmanager

import uuid

from collections import defaultdict
import stripe
from django.db import transaction
from django.utils import timezone
from .models import SubscriptionMutation, SubscriptionItem, Subscription, Club, Member, MembershipPlan, Invoice, InvoiceItem, Payment
from datetime import timedelta

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
                
                
                    # =========================================================
                    # Invoice exists but unpaid
                    # =========================================================
                    if invoice.status != "paid":

                        try:
                            stripe.Invoice.void_invoice(
                                invoice.id,
                                stripe_account=club.stripe_account_id,
                            )
                    
                        except stripe.error.InvalidRequestError:
                            # probably became paid between retrieve and void
                            pass
                    
                    
                        invoice = stripe.Invoice.retrieve(
                            invoice.id,
                            expand=["lines.data"],
                            stripe_account=club.stripe_account_id,
                        )
                    
                    
                    if invoice.status != "paid":
                    
                        mutation.invoice_status = (
                            SubscriptionMutation.InvoiceStatus.FAILED
                        )
                    
                        mutation.status = (
                            SubscriptionMutation.Status.FAILED
                        )
                    
                        mutation.payload["failure_reason"] = (
                            f"Invoice not paid. Final status: {invoice.status}"
                        )
                    
                        mutation.save(
                            update_fields=[
                                "invoice_status",
                                "status",
                                "payload",
                            ]
                        )
                    
                        continue
                                    
                
                
                    # =========================================================
                    # PAYMENT COMPLETE → DB REPAIR
                    # =========================================================
                    local_invoice, created = Invoice.objects.get_or_create(
                        stripe_invoice_id=invoice.id,
                        defaults={
                            "club": club,
                            "mutation": mutation,
                            "payer": subscription.owner,
                            "payer_name": subscription.owner.get_full_name(),
                            "payer_email": subscription.owner.email,
                            "subscription": subscription,
                            "status": "paid",
                            "amount_due": invoice.amount_due,
                            "amount_paid": invoice.amount_paid,
                            "currency": invoice.currency,
                        }
                    )
                    
                    
                    if created:
                        for line in invoice.lines.data:
                    
                            metadata = line.metadata or {}
                    
                            member_id = metadata.get("member_id")
                    
                            member_obj = None
                    
                            if member_id:
                                member_obj = Member.objects.filter(
                                    id=member_id
                                ).first()
                    
                            InvoiceItem.objects.create(
                                invoice=local_invoice,
                                member=member_obj,
                                description=line.description,
                                amount=line.amount,
                                quantity=line.quantity or 1,
                            )
                    
                    
                    Payment.objects.get_or_create(
                        invoice=local_invoice,
                        defaults={
                            "club": club,
                            "method": "stripe",
                            "amount": invoice.amount_paid,
                            "currency": invoice.currency,
                            "status": "succeeded",
                            "paid_at": timezone.now(),
                        }
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
                
                
                    mutation.invoice_status = (
                        SubscriptionMutation.InvoiceStatus.PAID
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
        window_start = now - timedelta(days=1)
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
