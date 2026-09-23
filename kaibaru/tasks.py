from celery import shared_task
from django.utils import timezone
from django.conf import settings
import stripe
import logging

from datetime import datetime, timedelta


from .tasks_emails import send_subscription_activated_emails, send_club_deleted_emails

logger = logging.getLogger(__name__)

from .models import Participation, Club, Subscription, Invoice, MembershipPlan
from .utils import sync_member_quantity
from django.db.models import Exists, OuterRef, CharField
from django.db.models.functions import TruncDate, Cast


from .locks_and_reconciliation import (
    MemberReservationPaymentReconciler,
    StripeSubscriptionReconciler,
    CheckoutSubscriptionReconciler,
    StripeToCashInvoiceReconciler,
    MembershipPlanDeletionReconciler,
    subscription_lock, 
    CacheLockError,
)

from .service_cycle_invoice_create import (
    CashSubscriptionCycleInvoiceService,
)





CASH_BILLING_METHODS = [
    "cash",
    "bank_transfer",
    "manual",
]

@shared_task
def reconcile_externally_canceled_stripe_subscriptions():
    """
    Periodic safety-net for Stripe subscriptions that may have
    been canceled directly in Stripe.

    This is intentionally separate from payment-failure reconciliation.
    """

    stripe.api_key = settings.STRIPE_SECRET_KEY

    logger.info(
        "[STRIPE CANCELED TASK] Starting reconciliation"
    )

    try:

        result = (
            StripeToCashInvoiceReconciler
            .reconcile_canceled_stripe_subscriptions()
        )

        logger.info(
            "[STRIPE CANCELED TASK] Finished reconciliation "
            "result=%s",
            result,
        )

        return result

    except Exception:

        logger.exception(
            "[STRIPE CANCELED TASK] Reconciliation failed"
        )

        raise

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

    try:
        return CashSubscriptionCycleInvoiceService.create(
            subscription_id=subscription_id
        )

    except Exception:
        logger.exception(
            "[CASH BILLING] Failed creating cycle invoice "
            "subscription=%s",
            subscription_id,
        )
        raise

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



@shared_task
def reconcile_expired_canceling_subscriptions():
    """
    Safety-net for subscriptions that were scheduled to cancel
    at the end of their Stripe billing period.

    Normal flow:
        Stripe cancels subscription
        -> customer.subscription.deleted webhook
        -> local subscription is cleaned up

    This task handles the case where that webhook was missed.

    We intentionally do NOT cancel Stripe subscriptions here.
    Stripe is allowed to perform its normal scheduled cancellation.

    Once Stripe confirms the subscription is canceled, the local
    subscription is updated accordingly.
    """

    cutoff = timezone.now() - timedelta(hours=24)

    subscriptions = (
        Subscription.objects
        .filter(
            cancel_at_period_end=True,
            stripe_subscription_id__isnull=False,
            current_period_end__isnull=False,
            current_period_end__lte=cutoff,
        )
        .select_related("club")
        .order_by("current_period_end", "id")
    )

    checked = 0
    cleaned = 0
    skipped = 0
    failed = 0

    for subscription in subscriptions:

        checked += 1

        try:

            with subscription_lock(
                subscription.id,
                timeout=300,
            ):

                # Re-read inside the lock because the subscription may
                # have changed since the queryset above was evaluated.
                subscription.refresh_from_db()

                if not subscription.cancel_at_period_end:
                    skipped += 1
                    continue

                if not subscription.stripe_subscription_id:
                    skipped += 1
                    continue

                if (
                    not subscription.current_period_end
                    or subscription.current_period_end > cutoff
                ):
                    skipped += 1
                    continue

                club = subscription.club

                stripe_subscription_id = (
                    subscription.stripe_subscription_id
                )

                logger.info(
                    "[CANCEL RECONCILIATION] Checking subscription=%s "
                    "stripe_subscription=%s period_end=%s",
                    subscription.id,
                    stripe_subscription_id,
                    subscription.current_period_end,
                )

                # -------------------------------------------------
                # Check Stripe's actual state.
                #
                # We do NOT cancel it here.
                # -------------------------------------------------

                stripe_sub = stripe.Subscription.retrieve(
                    stripe_subscription_id,
                    stripe_account=club.stripe_account_id,
                )

                logger.info(
                    "[CANCEL RECONCILIATION] Stripe subscription=%s "
                    "status=%s cancel_at_period_end=%s",
                    stripe_subscription_id,
                    stripe_sub.status,
                    stripe_sub.cancel_at_period_end,
                )

                # -------------------------------------------------
                # Stripe has already canceled it.
                #
                # This is the missed-webhook recovery path.
                # -------------------------------------------------

                if stripe_sub.status in [
                    "canceled",
                    "incomplete_expired",
                ]:

                    subscription.status = "canceled"
                    subscription.stripe_subscription_id = None
                    subscription.cancel_at_period_end = False

                    subscription.save(
                        update_fields=[
                            "status",
                            "stripe_subscription_id",
                            "cancel_at_period_end",
                        ]
                    )

                    cleaned += 1

                    logger.warning(
                        "[CANCEL RECONCILIATION] Repaired local "
                        "subscription=%s after Stripe cancellation. "
                        "stripe_subscription=%s",
                        subscription.id,
                        stripe_subscription_id,
                    )

                    continue

                # -------------------------------------------------
                # Stripe has not canceled it yet.
                #
                # Leave it alone. Stripe remains responsible for
                # completing the scheduled cancellation.
                # -------------------------------------------------

                logger.info(
                    "[CANCEL RECONCILIATION] Stripe subscription=%s "
                    "is still active/status=%s. Leaving unchanged.",
                    stripe_subscription_id,
                    stripe_sub.status,
                )

                skipped += 1

        except CacheLockError:

            logger.info(
                "[CANCEL RECONCILIATION] Subscription locked. "
                "Skipping subscription=%s",
                subscription.id,
            )

            skipped += 1

        except stripe.error.InvalidRequestError:

            logger.exception(
                "[CANCEL RECONCILIATION] Stripe subscription=%s "
                "could not be retrieved for local subscription=%s",
                subscription.stripe_subscription_id,
                subscription.id,
            )

            failed += 1

        except Exception:

            logger.exception(
                "[CANCEL RECONCILIATION] Unexpected failure "
                "subscription=%s",
                subscription.id,
            )

            failed += 1

    logger.info(
        "[CANCEL RECONCILIATION] Finished "
        "checked=%s cleaned=%s skipped=%s failed=%s",
        checked,
        cleaned,
        skipped,
        failed,
    )

    return {
        "checked": checked,
        "cleaned": cleaned,
        "skipped": skipped,
        "failed": failed,
    }


@shared_task
def reconcile_member_reservation_payments():
    """
    Periodic safety-net for member, visitor, and trial
    reservations paid through Stripe.

    Handles the failure window where:

        Stripe PaymentIntent succeeds
                ↓
        application crashes
                ↓
        Reservation remains UNPAID

    The reconciler checks Stripe directly and repairs the local
    reservation state.

    Old reservations with definitively failed PaymentIntents are
    deleted.
    """

    stripe.api_key = settings.STRIPE_SECRET_KEY

    logger.info(
        "[MEMBER RESERVATION TASK] "
        "Starting payment reconciliation"
    )

    try:

        result = (
            MemberReservationPaymentReconciler
            .reconcile_old_unpaid_reservations()
        )

        logger.info(
            "[MEMBER RESERVATION TASK] "
            "Finished payment reconciliation result=%s",
            result,
        )

        return result

    except Exception:

        logger.exception(
            "[MEMBER RESERVATION TASK] "
            "Payment reconciliation failed"
        )

        raise


@shared_task
def delete_membership_plan_task(plan_id):
    """
    Fired once when an owner schedules a MembershipPlan for deletion
    (MembershipPlanViewSet.perform_destroy).

    Cancels every active SubscriptionItem for the plan using the
    existing SubscriptionItemService.cancel_item(), then marks the
    plan is_deleted=True once (and only once) zero active items
    remain.

    Safe to run more than once:
        - Re-loads the plan from the DB and no-ops if it's already
          deleted, or if it was never actually scheduled.
        - Delegates the cancellation/finalization work to
          MembershipPlanDeletionReconciler.process_plan(), which is
          shared with reconcile_scheduled_plan_deletions() below and
          is itself idempotent/lock-safe.

    If some cancellations fail (Stripe error, subscription lock held,
    worker crash, etc.) the plan is simply left with
    scheduled_for_deletion=True / is_deleted=False, and
    reconcile_scheduled_plan_deletions() will retry the remaining
    items on its next run.
    """

    stripe.api_key = settings.STRIPE_SECRET_KEY

    try:
        plan = MembershipPlan.objects.get(id=plan_id)
    except MembershipPlan.DoesNotExist:
        logger.warning(
            "[PLAN DELETE TASK] plan=%s no longer exists",
            plan_id,
        )
        return {"plan_id": plan_id, "status": "missing"}

    if plan.is_deleted:
        logger.info(
            "[PLAN DELETE TASK] plan=%s already deleted, nothing to do",
            plan_id,
        )
        return {"plan_id": plan_id, "status": "already_deleted"}

    if not plan.scheduled_for_deletion:
        logger.warning(
            "[PLAN DELETE TASK] plan=%s is not scheduled for "
            "deletion; skipping",
            plan_id,
        )
        return {"plan_id": plan_id, "status": "not_scheduled"}

    logger.info(
        "[PLAN DELETE TASK] Starting cancellation for plan=%s",
        plan_id,
    )

    result = MembershipPlanDeletionReconciler.process_plan(plan)

    logger.info(
        "[PLAN DELETE TASK] Finished plan=%s result=%s",
        plan_id,
        result,
    )

    return result


@shared_task
def reconcile_scheduled_plan_deletions():
    """
    Periodic safety-net for MembershipPlan deletions.

    delete_membership_plan_task() can fail partway through (Stripe
    errors, held subscription/mutation locks, a worker crash, etc.),
    so this task periodically re-scans every plan that is still
    scheduled_for_deletion=True / is_deleted=False, retries
    cancellation of its remaining active SubscriptionItems, and
    finalizes (is_deleted=True) any plan that now has zero active
    items left.

    Plans with active items remaining are simply left
    scheduled_for_deletion=True for the next periodic run to retry.
    """

    stripe.api_key = settings.STRIPE_SECRET_KEY

    logger.info(
        "[PLAN DELETE RECONCILE] Starting scan"
    )

    results = MembershipPlanDeletionReconciler.reconcile_all()

    logger.info(
        "[PLAN DELETE RECONCILE] Finished scan results=%s",
        results,
    )

    return results