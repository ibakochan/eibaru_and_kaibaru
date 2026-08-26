from celery import shared_task
from django.utils import timezone
from django.conf import settings
import stripe
import logging

from datetime import datetime

from .tasks_emails import send_subscription_activated_emails, send_club_deleted_emails, send_invoice_created_email

logger = logging.getLogger(__name__)

from .models import Participation, Club, Subscription, SubscriptionItem, Invoice, InvoiceItem
from .utils import sync_member_quantity
from django.db.models import Exists, OuterRef, CharField
from django.db.models.functions import TruncDate, Cast


from .locks_and_reconciliation import (
    StripeSubscriptionReconciler,
    CheckoutSubscriptionReconciler,
    StripeToCashInvoiceReconciler,
    subscription_lock, 
    CacheLockError,
)


from django.db import transaction, IntegrityError

from datetime import timedelta



from .pricing import get_effective_subscription_price
from .discounts import calculate_discounted_amount



CASH_BILLING_METHODS = [
    "cash",
    "bank_transfer",
    "manual",
]

@shared_task
def reconcile_stripe_to_cash_invoices():
    """
    Periodic safety-net for Stripe → cash invoice transitions.

    The actual reconciliation logic lives in
    StripeToCashInvoiceReconciler.
    """

    stripe.api_key = settings.STRIPE_SECRET_KEY

    logger.info(
        "[STRIPE TO CASH TASK] Starting reconciliation"
    )

    try:

        result = (
            StripeToCashInvoiceReconciler
            .reconcile_recent_invoices()
        )

        logger.info(
            "[STRIPE TO CASH TASK] Finished reconciliation "
            "result=%s",
            result,
        )

        return result

    except Exception:

        logger.exception(
            "[STRIPE TO CASH TASK] Reconciliation failed"
        )

        raise

@shared_task
def schedule_cash_subscription_cycle_invoices():

    today = timezone.localdate()

    # The billing cycle key is based on the subscription's
    # current_period_end date, using the same logic as the
    # invoice creation task.
    subscriptions = (
        Subscription.objects
        .annotate(
            billing_cycle_key_for_scheduler=Cast(
                TruncDate("current_period_end"),
                output_field=CharField(),
            ),
        )
        .filter(
            billing_method__in=CASH_BILLING_METHODS,
            status="active",
            current_period_end__isnull=False,
            current_period_end__date__lte=today,
        )
        .annotate(
            cycle_invoice_exists=Exists(
                Invoice.objects.filter(
                    subscription_id=OuterRef("pk"),
                    billing_reason="subscription_cycle",
                    billing_cycle_key=OuterRef(
                        "billing_cycle_key_for_scheduler"
                    ),
                )
            )
        )
        .filter(
            cycle_invoice_exists=False
        )
        .order_by(
            "current_period_end",
            "id",
        )
        .values_list("id", flat=True)[:50]
    )

    count = 0

    for subscription_id in subscriptions:
        create_cash_subscription_cycle_invoice.delay(
            subscription_id
        )
        count += 1

    logger.info(
        "[CASH BILLING] Scheduled %s subscription invoices",
        count,
    )

    return {
        "scheduled": count,
    }

@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=60,
    retry_kwargs={"max_retries": 5},
)
def create_cash_subscription_cycle_invoice(self, subscription_id):
    """
    Create the recurring local invoice for one non-Stripe subscription.

    Idempotency:
        subscription + billing_cycle_key

    The billing_cycle_key is the date represented by
    subscription.current_period_end.

    IMPORTANT:
        Creating the invoice does NOT advance current_period_end.
        The subscription period should only advance when payment
        is actually recorded.
    """

    today = timezone.localdate()

    try:
        with transaction.atomic():

            # ---------------------------------------------------------
            # 1. Lock the subscription
            # ---------------------------------------------------------
            subscription = (
                Subscription.objects
                .select_for_update()
                .select_related(
                    "club",
                    "owner",
                )
                .get(id=subscription_id)
            )

            # ---------------------------------------------------------
            # 2. Safety checks
            # ---------------------------------------------------------

            if subscription.billing_method not in CASH_BILLING_METHODS:
                logger.info(
                    "[CASH BILLING] Skipping subscription=%s "
                    "billing_method=%s",
                    subscription.id,
                    subscription.billing_method,
                )
                return {
                    "success": True,
                    "skipped": True,
                    "reason": "not_cash_billing",
                }

            if subscription.status != "active":
                logger.info(
                    "[CASH BILLING] Skipping subscription=%s "
                    "status=%s",
                    subscription.id,
                    subscription.status,
                )
                return {
                    "success": True,
                    "skipped": True,
                    "reason": "subscription_not_active",
                }

            if not subscription.current_period_end:
                logger.warning(
                    "[CASH BILLING] Subscription=%s has no "
                    "current_period_end",
                    subscription.id,
                )
                return {
                    "success": False,
                    "skipped": True,
                    "reason": "missing_current_period_end",
                }

            # ---------------------------------------------------------
            # 3. Determine billing cycle
            # ---------------------------------------------------------

            billing_cycle_start = timezone.localtime(
                subscription.current_period_end
            ).date()

            # Not due yet.
            if billing_cycle_start > today:
                logger.debug(
                    "[CASH BILLING] Subscription=%s not due. "
                    "cycle=%s today=%s",
                    subscription.id,
                    billing_cycle_start,
                    today,
                )

                return {
                    "success": True,
                    "skipped": True,
                    "reason": "not_due",
                }

            billing_cycle_key = billing_cycle_start.isoformat()

            # ---------------------------------------------------------
            # 4. IDEMPOTENCY CHECK
            # ---------------------------------------------------------

            existing_invoice = (
                Invoice.objects
                .filter(
                    subscription=subscription,
                    billing_reason="subscription_cycle",
                    billing_cycle_key=billing_cycle_key,
                )
                .first()
            )

            if existing_invoice:
                logger.info(
                    "[CASH BILLING] Invoice already exists. "
                    "subscription=%s invoice=%s cycle=%s",
                    subscription.id,
                    existing_invoice.id,
                    billing_cycle_key,
                )

                return {
                    "success": True,
                    "already_exists": True,
                    "invoice_id": existing_invoice.id,
                    "amount_due": existing_invoice.amount_due,
                    "billing_cycle_key": billing_cycle_key,
                }

            # ---------------------------------------------------------
            # 5. Load active subscription items
            # ---------------------------------------------------------

            subscription_items = list(
                SubscriptionItem.objects
                .filter(
                    subscription=subscription,
                    deleted_at__isnull=True,
                )
                .select_related(
                    "member",
                    "plan",
                )
                .order_by("id")
            )

            if not subscription_items:
                logger.warning(
                    "[CASH BILLING] Subscription=%s has no "
                    "active subscription items",
                    subscription.id,
                )

                return {
                    "success": False,
                    "skipped": True,
                    "reason": "no_active_items",
                }

            # ---------------------------------------------------------
            # 6. Calculate invoice items
            #
            # THIS IS THE SAME CALCULATION AS invoice.created
            # ---------------------------------------------------------

            calculated_items = []
            total = 0

            for subscription_item in subscription_items:

                member = subscription_item.member
                plan = subscription_item.plan

                if not member:
                    logger.warning(
                        "[CASH BILLING] SubscriptionItem=%s "
                        "has no member",
                        subscription_item.id,
                    )
                    continue

                if not plan:
                    logger.warning(
                        "[CASH BILLING] SubscriptionItem=%s "
                        "has no plan",
                        subscription_item.id,
                    )
                    continue

                # Same as your Stripe invoice.created webhook.
                base = get_effective_subscription_price(
                    subscription_item
                )

                discounted = calculate_discounted_amount(
                    club=subscription.club,
                    member=member,
                    plan=plan,
                    base_amount=base,
                    apply_to="subscription",
                )

                amount = max(0, int(discounted))

                if amount <= 0:
                    continue

                calculated_items.append(
                    {
                        "member": member,
                        "plan": plan,
                        "amount": amount,
                        "description": (
                            f"{member.full_name} "
                            f"{plan.name}"
                        ),
                    }
                )

                total += amount

            total = max(0, int(total))

            # ---------------------------------------------------------
            # 7. Create Invoice
            # ---------------------------------------------------------

            invoice = Invoice.objects.create(
                club=subscription.club,
                mutation=None,

                payer=subscription.owner,
                payer_name=(
                    subscription.owner.get_full_name()
                    if subscription.owner
                    else None
                ),
                payer_email=(
                    subscription.owner.email
                    if subscription.owner
                    else None
                ),

                subscription=subscription,

                status="open",

                amount_due=total,
                amount_paid=0,

                currency="jpy",

                due_date=subscription.current_period_end,

                stripe_invoice_id=None,

                billing_reason="subscription_cycle",

                billing_cycle_key=billing_cycle_key,
            )

            # ---------------------------------------------------------
            # 8. Create InvoiceItems
            #
            # All happen inside the same transaction.
            # ---------------------------------------------------------

            invoice_items = [
                InvoiceItem(
                    invoice=invoice,
                    member=item["member"],
                    description=item["description"],
                    amount=item["amount"],
                    quantity=1,
                )
                for item in calculated_items
            ]

            if invoice_items:
                InvoiceItem.objects.bulk_create(
                    invoice_items,
                    batch_size=500,
                )

            transaction.on_commit(
                lambda invoice_id=invoice.id:
                    send_invoice_created_email.delay(invoice_id)
            )

            logger.info(
                "[CASH BILLING] Created invoice=%s "
                "subscription=%s cycle=%s amount=%s items=%s",
                invoice.id,
                subscription.id,
                billing_cycle_key,
                total,
                len(invoice_items),
            )

            # ---------------------------------------------------------
            # 9. DO NOT advance current_period_end here
            # ---------------------------------------------------------
            #
            # Invoice creation means:
            #
            #     "Customer owes this amount."
            #
            # Payment should be responsible for:
            #
            #     current_period_end
            #     access_until
            #
            # advancement.
            # ---------------------------------------------------------

            logger.info(
                "[CASH BILLING] Created invoice=%s "
                "subscription=%s cycle=%s amount=%s items=%s",
                invoice.id,
                subscription.id,
                billing_cycle_key,
                total,
                len(invoice_items),
            )

            return {
                "success": True,
                "invoice_id": invoice.id,
                "subscription_id": subscription.id,
                "amount_due": total,
                "invoice_item_count": len(invoice_items),
                "billing_cycle_key": billing_cycle_key,
            }

    except IntegrityError:

        # -------------------------------------------------------------
        # The unique constraint is the final idempotency guarantee.
        #
        # This can happen if two workers somehow race despite the
        # SELECT ... FOR UPDATE.
        # -------------------------------------------------------------

        logger.info(
            "[CASH BILLING] Duplicate invoice prevented by "
            "database constraint subscription=%s",
            subscription_id,
        )

        invoice = (
            Invoice.objects
            .filter(
                subscription_id=subscription_id,
                billing_reason="subscription_cycle",
            )
            .order_by("-id")
            .first()
        )

        return {
            "success": True,
            "already_exists": True,
            "invoice_id": invoice.id if invoice else None,
        }

@shared_task
def reconcile_subscription_mutations():
    """
    Periodic safety-net:
    Finds subscriptions with pending mutations
    and runs the subscription reconciler.
    """

    stripe.api_key = settings.STRIPE_SECRET_KEY

    subscriptions = (
        Subscription.objects
        .filter(
            mutations__status__in=[
                "pending",
                "processing",
            ]
        )
        .distinct()
    )

    for subscription in subscriptions:
        try:
            with subscription_lock(subscription.id):
                StripeSubscriptionReconciler.reconcile(
                    subscription=subscription,
                    club=subscription.club,
                )
            
        except CacheLockError:
            logger.info(
                "Skipping reconciliation, subscription locked=%s",
                subscription.id,
            )

        except Exception:
            logger.exception(
                f"Mutation reconciliation failed for subscription={subscription.id}"
            )


@shared_task
def reconcile_checkout_subscriptions():
    """
    Periodic safety-net:
    Finds orphan Stripe subscriptions created through checkout
    and cleans them up.
    """

    stripe.api_key = settings.STRIPE_SECRET_KEY
    logger.info("[TASK] checkout reconciliation task started")
    try:
        CheckoutSubscriptionReconciler.reconcile_recent_checkouts()

    except Exception:
        logger.exception(
            "Checkout subscription reconciliation failed"
        )


@shared_task
def reconcile_single_club_subscription(club_id):
    today = timezone.localdate()
    stripe.api_key = settings.STRIPE_SECRET_KEY

    try:
        club = Club.objects.get(id=club_id, is_deleted=False)
    except Club.DoesNotExist:
        return

    if not club.stripe_subscription_id:
        return

    sub = stripe.Subscription.retrieve(club.stripe_subscription_id)

    if sub.status != "active":
        club.subscription_active = False
        club.save()
        return

    invoices = stripe.Invoice.list(
        customer=club.stripe_customer_id,
        limit=1
    )

    if not invoices.data:
        return

    invoice = invoices.data[0]

    if invoice.status == "paid" and invoice.id != club.last_paid_invoice_id:
        club.last_paid_invoice_id = invoice.id
        if invoice.get("lines") and invoice["lines"]["data"]:
            period_end_ts = invoice["lines"]["data"][0]["period"]["end"]

            if period_end_ts:
                period_end_dt = datetime.fromtimestamp(period_end_ts, tz=timezone.utc)

                club.subscription_current_period_end = period_end_dt
                club.expiration_date = period_end_dt
        club.subscription_active = True
        club.save()

        send_subscription_activated_emails.delay(
            club.id,
            invoice.id,
        )


@shared_task
def reset_monthly_participation_counts():
    """
    Monthly task:
    - Reset participation monthly counts
    - Reset level counts if enabled
    - Run once per month safely
    """

    today = timezone.localdate()


    clubs = Club.objects.filter(is_deleted=False)


    for club in clubs:
        if club.last_reset and (
            club.last_reset.year == today.year
            and club.last_reset.month == today.month
        ):
            continue

        Participation.objects.filter(
            member__club=club
        ).update(monthly_count=0)


        club.last_reset = today
        club.save()

        logger.info(
            f"[CELERY] Monthly reset completed for club={club.id}"
        )


@shared_task
def reconcile_stripe_subscriptions():
    """
    Daily safety-net task:
    - Reconcile Stripe member quantities
    - Ensure subscription status is correct
    - Extend expiration only for NEW paid invoices
    - Freeze or delete expired clubs safely
    """

    today = timezone.localdate()

    stripe.api_key = settings.STRIPE_SECRET_KEY

    clubs = Club.objects.filter(is_deleted=False)


    for club in clubs:
        try:
            if club.stripe_subscription_id:
                try:
                    sub = stripe.Subscription.retrieve(club.stripe_subscription_id)
                except stripe.error.InvalidRequestError:
                    logger.warning(
                        f"[CELERY] Stripe subscription missing for club={club.id}"
                    )
                    club.subscription_active = False
                    club.stripe_subscription_id = None
                    club.save()
                    continue


                if club.expiration_date:
                    expiration_date = timezone.localtime(club.expiration_date).date()
                    days_expired = max((today - expiration_date).days, 0)
                else:
                    days_expired = 0
                if days_expired >= 28:
                    logger.warning(
                        f"[CELERY] Deleting unpaid subscription club={club.id}"
                    )
                    canceled = False
                    try:
                        stripe.Subscription.delete(club.stripe_subscription_id)
                        canceled = True
                        logger.info(
                            f"[CELERY] Stripe subscription canceled for club={club.id}"
                        )
                    except Exception as e:
                        logger.error(
                            f"[CELERY] Failed to cancel Stripe sub for club={club.id}: {e}"
                        )

                    if canceled:
                        owner = club.owner
                        owner_name = owner.get_full_name() if owner else ""
                        owner_email = owner.email if owner else settings.SERVER_EMAIL

                        club_data = {
                            "subdomain": club.subdomain,
                            "owner_name": owner_name,
                            "owner_email": owner_email,
                            "reason": "お支払いが確認できず、一定期間が経過したため",
                        }


                        send_club_deleted_emails.delay(club_data)
                        club.is_deleted = True
                        club.deleted_at = today
                        club.save()
                    else:
                        club.subscription_active = False
                        club.save()

                    continue



                if sub.status != "active":
                    if club.subscription_active:
                        logger.warning(
                            f"[CELERY] Subscription inactive: club={club.id}"
                        )
                    club.subscription_active = False
                    club.save()
                  
                    continue
 
                sync_member_quantity(club)
 
                invoices = stripe.Invoice.list(
                    customer=club.stripe_customer_id,
                    limit=1
                )

                if invoices.data:
                    invoice = invoices.data[0]

                    if invoice.status == "paid" and invoice.id != club.last_paid_invoice_id:
                        club.last_paid_invoice_id = invoice.id
                        if invoice.get("lines") and invoice["lines"]["data"]:
                            period_end_ts = invoice["lines"]["data"][0]["period"]["end"]
                            if period_end_ts:
                                club.subscription_current_period_end = datetime.fromtimestamp(
                                    period_end_ts, tz=timezone.utc
                                )
                                club.expiration_date = club.subscription_current_period_end
                        club.subscription_active = True
                        club.save()

 
                        logger.info(
                            f"[CELERY] Invoice applied: club={club.id}, "
                            f"invoice={invoice.id}, expiration={club.expiration_date}"
                        )
 
            else:
                if club.expiration_date:
                    expiration_date = timezone.localtime(club.expiration_date).date()
                    days_expired = max((today - expiration_date).days, 0)

                    if days_expired >= 1 and club.subscription_active:
                        club.subscription_active = False
                        club.save()
 
                    if days_expired >= 7:
                        logger.warning(
                            f"[CELERY] Deleting expired club={club.id}"
                        )

                        owner = club.owner
                        owner_name = owner.get_full_name() if owner else ""
                        owner_email = owner.email if owner else settings.SERVER_EMAIL

                        club_data = {
                            "subdomain": club.subdomain,
                            "owner_name": owner_name,
                            "owner_email": owner_email,
                            "reason": "お支払いが確認できず、一定期間が経過したため",
                        }

                        send_club_deleted_emails.delay(club_data)

                        club.is_deleted = True
                        club.deleted_at = today
                        club.save()

        except stripe.error.InvalidRequestError as e:
            logger.error(
                f"[CELERY] Stripe error for club={club.id}: {e}"
            )
            club.subscription_active = False
            club.save()

        except Exception as e:
            logger.exception(
                f"[CELERY] Unexpected error for club={club.id}: {e}"
            )



@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=300,
    retry_kwargs={"max_retries": 20},
)
def cancel_stripe_subscription(self, subscription_id):
    stripe.api_key = settings.STRIPE_SECRET_KEY
    stripe.Subscription.delete(subscription_id)
