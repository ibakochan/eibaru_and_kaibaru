import stripe
import logging

from django.conf import settings
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt

from .models import Club, Member, Subscription, SubscriptionItem, MembershipPlan, StripeWebhookEvent
from .tasks_emails import send_subscription_activated_emails


from datetime import datetime, timezone as dt_timezone
from django.utils import timezone

import calendar
from django.db import IntegrityError


logger = logging.getLogger(__name__)

stripe.api_key = settings.STRIPE_SECRET_KEY

# ---------------------------
# Connected account / member webhook
# ---------------------------
@csrf_exempt
def stripe_connected_webhook(request):
    payload = request.body
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE", "")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.STRIPE_CONNECTED_WEBHOOK_SECRET
        )
    except stripe.error.SignatureVerificationError:
        logger.error("[CONNECTED WEBHOOK] Signature verification failed")
        return HttpResponse(status=400)

    event_id = event["id"]

    try:
        StripeWebhookEvent.objects.create(event_id=event_id)
    except IntegrityError:
        return HttpResponse(status=200)

    account_id = event.get("account")
    if not account_id:
        logger.error("Missing account_id in webhook event %s", event_id)
        return HttpResponse(status=200)

    # ----------------- CONNECT EVENTS (member → club payments) -----------------

    
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]

        member_id = session["metadata"].get("member_id")
        subscription_id = session.get("subscription")
        if not subscription_id:
            return HttpResponse(status=200)



        member = Member.objects.filter(id=member_id).first()
        if not member:
            logger.error("Member not found for session %s", session["id"])
            return HttpResponse(status=200)

        sub = stripe.Subscription.retrieve(subscription_id, stripe_account=account_id, expand=["items.data.price"])

        club_id = session["metadata"].get("club_id")
        club = Club.objects.filter(id=club_id).first()
        if not club:
            logger.error("Club not found for session %s", session["id"])
            return HttpResponse(status=200)
        
        plan_id = session["metadata"].get("plan_id")
        plan = MembershipPlan.objects.filter(id=plan_id).first()
        if not plan:
            logger.error("Plan not found for session %s", session["id"])
            return HttpResponse(status=200)

        invoice_id = session.get("invoice")

        if not member.stripe_customer_id:
            member.stripe_customer_id = sub.customer
            member.save(update_fields=["stripe_customer_id"])

        sub_obj, created = Subscription.objects.get_or_create(
            stripe_subscription_id=sub.id,
            defaults={
                "member": member,
                "status": sub.status,
                "current_period_end": None,
                "last_invoice_id": invoice_id,
                "billing_mode": club.subscription_mode,
                "billing_anchor_day": club.stripe_anchor_date,
            }
        )

        if created and club.joining_fee > 0:
            stripe.InvoiceItem.create(
                customer=sub.customer,
                amount=club.joining_fee,
                currency="jpy",
                description="入会金",
                stripe_account=account_id
            )

        if created and club.subscription_mode == "regular":

            today = timezone.localtime().date()
            anchor_day = club.stripe_anchor_date  
            
            # Determine previous anchor date
            if today.day >= anchor_day:
                prev_anchor_month = today.month
                prev_anchor_year = today.year
            else:
                if today.month == 1:
                    prev_anchor_month = 12
                    prev_anchor_year = today.year - 1
                else:
                    prev_anchor_month = today.month - 1
                    prev_anchor_year = today.year
            
            last_day_prev_month = calendar.monthrange(prev_anchor_year, prev_anchor_month)[1]
            prev_anchor_date = datetime(
                prev_anchor_year,
                prev_anchor_month,
                min(anchor_day, last_day_prev_month),
                tzinfo=dt_timezone.utc
            ).date()
            
            # Determine next anchor date
            next_anchor_month = prev_anchor_month + 1
            next_anchor_year = prev_anchor_year
            if next_anchor_month > 12:
                next_anchor_month = 1
                next_anchor_year += 1
            
            last_day_next_month = calendar.monthrange(next_anchor_year, next_anchor_month)[1]
            next_anchor_date = datetime(
                next_anchor_year,
                next_anchor_month,
                min(anchor_day, last_day_next_month),
                tzinfo=dt_timezone.utc
            ).date()
            
            # Days from today until next anchor
            remaining_days = (next_anchor_date - today).days
            
            # Total days in the billing period
            billing_period_days = (next_anchor_date - prev_anchor_date).days
            
            # Calculate prorated amount based on actual billing period
            monthly_price = plan.price
            prorated_amount = int(monthly_price * remaining_days / billing_period_days)
            

            if today.day <= anchor_day:
                period_end_month = today.month
                period_end_year = today.year
            else:
                period_end_month = today.month + 1
                period_end_year = today.year
                if period_end_month > 12:
                    period_end_month = 1
                    period_end_year += 1
            
            last_day = calendar.monthrange(period_end_year, period_end_month)[1]
            day = min(anchor_day, last_day)
            
            period_end = datetime(period_end_year, period_end_month, day, tzinfo=dt_timezone.utc)
        
            # Charge for the remaining days until next anchor
            if prorated_amount > 0:
                stripe.InvoiceItem.create(
                    customer=sub.customer,
                    amount=prorated_amount,
                    currency="jpy",
                    description=f"Prorated membership ({remaining_days} days until next anchor)",
                    stripe_account=account_id
                )
        
            
        
            # Create and pay invoice
            invoice = stripe.Invoice.create(
                customer=sub.customer,
                subscription=sub.id,
                auto_advance=True,
                stripe_account=account_id
            )
            if invoice.amount_due > 0:
                stripe.Invoice.pay(invoice.id, stripe_account=account_id)
                
        if created and club.subscription_mode == "monthly":

            today = timezone.localtime().date()


            anchor_day = club.stripe_anchor_date

                # Compute next anchor-based period end
            if today.day <= anchor_day:
                month = today.month
                year = today.year
            else:
                month = today.month + 1
                year = today.year
                if month > 12:
                    month = 1
                    year += 1

            last_day = calendar.monthrange(year, month)[1]
            day = min(anchor_day, last_day)
            
            period_end = datetime(year, month, day, tzinfo=dt_timezone.utc)
            
                    
            days_in_month = calendar.monthrange(today.year, today.month)[1]
            remaining_days = days_in_month - today.day + 1    
        
            monthly_price = plan.price
            prorated_amount = int(monthly_price * remaining_days / days_in_month)
    
            stripe.InvoiceItem.create(
                customer=sub.customer,
                amount=prorated_amount,                    
                currency="jpy",
                description=f"Prorated membership ({remaining_days} days)",
                stripe_account=account_id
            )
        
            
        
            invoice = stripe.Invoice.create(
                customer=sub.customer,
                subscription=sub.id,
                auto_advance=True,
                stripe_account=account_id
            )
        
            if invoice.amount_due > 0:
                stripe.Invoice.pay(invoice.id, stripe_account=account_id)
            
            if today.day > club.stripe_anchor_date:
                stripe.InvoiceItem.create(
                    customer=sub.customer,
                    amount=plan.price,
                    currency="jpy",
                    description=f"{plan.name} 翌月分前払い",
                    stripe_account=account_id
                )


            
            

        
        

        


        if created:
            for item in sub["items"]["data"]:
        
                stripe_item_id = item["id"]
                price_id = item["price"]["id"]
    
                if price_id == plan.stripe_price_id:
        
                    SubscriptionItem.objects.get_or_create(
                        stripe_subscription_item_id=stripe_item_id,
                        defaults={
                            "subscription": sub_obj,
                            "plan": plan,
                            "quantity": item.get("quantity", 1)
                        }
                    )


            
    elif event["type"] == "invoice.paid":
        invoice = event["data"]["object"]
        logger.info(f"[invoice.paid] Received invoice: {invoice.get('id')}")

        # -------- Extract subscription ID (robust for new Stripe structure) --------
        subscription_id = invoice.get("subscription")

        if not subscription_id:
            subscription_id = (
                invoice.get("parent", {})
                .get("subscription_details", {})
                .get("subscription")
            )

        if not subscription_id:
            for line in invoice.get("lines", {}).get("data", []):
                subscription_id = (
                    line.get("parent", {})
                    .get("subscription_item_details", {})
                    .get("subscription")
                )
                if subscription_id:
                    logger.info(f"[invoice.paid] Found subscription {subscription_id} from invoice line")
                    break

        if not subscription_id:
            logger.warning(f"[invoice.paid] No subscription found on invoice {invoice.get('id')}")
            return HttpResponse(status=200)

        # -------- Find local subscription --------
        sub = Subscription.objects.filter(stripe_subscription_id=subscription_id).first()
        if not sub:
            logger.warning(
                "[invoice.paid] Subscription not found for invoice %s (subscription=%s)",
                invoice["id"],
                subscription_id
            )
            return HttpResponse(status=200)

        logger.info(f"[invoice.paid] Found subscription {sub.id} for invoice {invoice['id']}")

        # -------- Idempotency check --------
        if sub.last_invoice_id == invoice["id"]:
            logger.info(f"[invoice.paid] Invoice {invoice['id']} already processed, skipping")
            return HttpResponse(status=200)

        sub.last_invoice_id = invoice["id"]

        # -------- Extract period end from invoice lines --------
        periods = [
            line["period"]["end"]
            for line in invoice.get("lines", {}).get("data", [])
            if line.get("period") and line["period"].get("end")
        ]

        period_end_ts = max(periods) if periods else None
        logger.info(f"[invoice.paid] Calculated period_end_ts: {period_end_ts}")

        if period_end_ts:
            period_end = datetime.fromtimestamp(period_end_ts, tz=dt_timezone.utc)
            today = timezone.localtime().date()

            # If period_end is essentially today (±1 day), override with next anchor
            if abs((period_end.date() - today).days) <= 1:
                anchor_day = sub.billing_anchor_day  # club anchor
                # Determine next anchor month/year
                if today.day <= anchor_day:
                    month, year = today.month, today.year
                else:
                    month, year = today.month + 1, today.year
                    if month > 12:
                        month = 1
                        year += 1
                last_day = calendar.monthrange(year, month)[1]                    
                day = min(anchor_day, last_day)
                period_end = datetime(year, month, day, tzinfo=dt_timezone.utc)
                logger.info(f"[invoice.paid] Overriding period_end to next anchor: {period_end}")

            sub.current_period_end = period_end
            logger.info(f"[invoice.paid] Updated current_period_end to {sub.current_period_end}")
            if sub.billing_mode == "regular":
                sub.access_until = period_end
                logger.info(f"[invoice.paid] Updated access_until to {sub.access_until}")
            

            if sub.billing_mode == "monthly":
                year = sub.current_period_end.year
                month = sub.current_period_end.month
                last_day = calendar.monthrange(year, month)[1]

                sub.access_until = datetime(
                    year, month, last_day, 23, 59, 59, tzinfo=dt_timezone.utc
                )
                logger.info(f"[invoice.paid] Updated access_until to {sub.access_until}")

        else:
            logger.warning(f"[invoice.paid] No valid period_end found on invoice {invoice['id']}")

        # -------- Finalize --------
        sub.status = "active"
        sub.save()

        member = sub.member

        if not member.has_paid_joining_fee:
            member.has_paid_joining_fee = True
            member.save(update_fields=["has_paid_joining_fee"])
            logger.info(f"[invoice.paid] Marked member {member.id} as having paid joining fee")

        logger.info(
            "[invoice.paid] Processed invoice %s for subscription %s (status=%s)",
            invoice["id"],
            subscription_id,
            sub.status
        )

        SubscriptionItem.objects.filter(
            subscription=sub
        ).update(monthly_double_resume_charge_prevention=False)
    
    elif event["type"] == "invoice.payment_failed":
        invoice = event["data"]["object"]
        sub = Subscription.objects.filter(
            stripe_subscription_id=invoice.get("subscription")
        ).first()
        if sub:
            sub.status = "past_due"
            sub.save()

    elif event["type"] == "customer.subscription.deleted":
        stripe_sub = event["data"]["object"]
        sub = Subscription.objects.filter(stripe_subscription_id=stripe_sub["id"]).first()
        if sub:
            sub.status = "canceled"
            sub.save()

    elif event["type"] == "customer.subscription.updated":
        stripe_sub = event["data"]["object"]
    
        sub = Subscription.objects.filter(
            stripe_subscription_id=stripe_sub["id"]
        ).first()
    
        if sub:
            sub.cancel_at_period_end = stripe_sub.get("cancel_at_period_end", False)
            sub.status = stripe_sub.get("status", sub.status)
    
            period_end_ts = stripe_sub.get("current_period_end")
            if period_end_ts:
                sub.current_period_end = datetime.fromtimestamp(
                    period_end_ts, tz=dt_timezone.utc
                )
    
            sub.save()


    return HttpResponse(status=200)

# ---------------------------
# Platform / club webhook
# ---------------------------
@csrf_exempt
def stripe_platform_webhook(request):
    payload = request.body
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE", "")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
        )
    except stripe.error.SignatureVerificationError:
        logger.error("[PLATFORM WEBHOOK] Signature verification failed")
        return HttpResponse(status=400)

    event_id = event["id"]

    try:
        StripeWebhookEvent.objects.create(event_id=event_id)
    except IntegrityError:
        return HttpResponse(status=200)

    # ----------------- PLATFORM EVENTS -----------------
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        club_id = session["metadata"].get("club_id")
        subscription_id = session.get("subscription")
        if not subscription_id:
            logger.warning("Subscription not found for session %s", session["id"])
            return HttpResponse(status=200)

        


        club = Club.objects.filter(id=club_id, is_deleted=False).first()
        if not club:
            logger.error("Platform webhook: club not found for session %s", session["id"])
            return HttpResponse(status=200)


        customer_id = session.get("customer")
        if not club.stripe_subscription_id:
            club.stripe_subscription_id = subscription_id
            club.stripe_customer_id = customer_id
            club.save()

    elif event["type"] == "invoice.payment_succeeded":
        invoice = event["data"]["object"]
        club = Club.objects.filter(
            stripe_customer_id=invoice["customer"], is_deleted=False
        ).first()

        if not club:
            logger.warning("No club object found")
            return HttpResponse(status=200)

        if club.last_paid_invoice_id == invoice["id"]:
            return HttpResponse(status=200)

        subscription_id = invoice.get("subscription")
        if subscription_id:
            sub = stripe.Subscription.retrieve(subscription_id)
            club.subscription_cancel_at_period_end = sub.get("cancel_at_period_end", False)
    
        # Always try to set current_period_end from invoice lines
        if invoice.get("lines") and invoice["lines"]["data"]:
            period_end_ts = max(
                line["period"]["end"] for line in invoice["lines"]["data"]
            )
            if period_end_ts:
                period_end_dt = datetime.fromtimestamp(period_end_ts, tz=dt_timezone.utc)
                club.subscription_current_period_end = period_end_dt
                club.expiration_date = period_end_dt
    
        club.last_paid_invoice_id = invoice["id"]
        club.subscription_active = True
        club.save()

        send_subscription_activated_emails.delay(club.id, invoice["id"])

    elif event["type"] == "account.updated":
        account = event["data"]["object"]
        stripe_account_id = account["id"]

        club = Club.all_objects.filter(stripe_account_id=stripe_account_id).first()
        if club:
            club.stripe_charges_enabled = account.get("charges_enabled", False)
            club.stripe_payouts_enabled = account.get("payouts_enabled", False)
            club.stripe_details_submitted = account.get("details_submitted", False)
            club.stripe_onboarding_completed = club.stripe_details_submitted
            club.save()
            logger.info(f"[PLATFORM WEBHOOK] Updated Stripe onboarding for club {club.subdomain}")

    elif event["type"] == "customer.subscription.deleted":

        sub = event["data"]["object"]

        club = Club.all_objects.filter(
            stripe_subscription_id=sub["id"]
        ).first()

        if club:
            club.subscription_active = False
            club.is_deleted = True
            club.deleted_at = dj_timezone.localdate()
            club.save()

    return HttpResponse(status=200)