from celery import shared_task
from django.utils import timezone
from django.conf import settings
import stripe
import logging

from .models import Club
from datetime import datetime

from .tasks_emails import send_subscription_activated_emails, send_club_deleted_emails

logger = logging.getLogger(__name__)

from .models import Participation
from .utils import sync_member_quantity



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
