import stripe
import logging

from django.conf import settings
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt

from .models import Club, Member, Subscription, SubscriptionItem, MembershipPlan, StripeWebhookEvent, StripeCustomer, Invoice, InvoiceItem, Payment
from .tasks_emails import send_subscription_activated_emails, send_invoice_paid_email
from django.db import transaction

from datetime import datetime, timezone as dt_timezone
from django.utils import timezone
from datetime import timedelta

import calendar
from django.db import IntegrityError

from .stripe_service import get_or_create_stripe_customer
logger = logging.getLogger(__name__)

stripe.api_key = settings.STRIPE_SECRET_KEY

from .locks_and_reconciliation import subscription_lock, CacheLockError

from .discounts import calculate_discounted_amount
from .billing import (
    get_next_month_start,
    get_next_billing_cycle_anchor,
    should_set_monthly_resume_prevention,
    should_cancel_subscription,
    get_cancel_success_message,
    should_charge_resume_next_month,
    get_resume_charge_amount,
    get_resume_success_message,
    extract_subscription_id_from_invoice,
    resolve_and_apply_subscription_period
)

from .invoice_creation import (
    create_local_invoice_from_stripe_invoice,
    mark_local_invoice_paid,
)

from .rules_subscriptions import (
    active_items_q,
    ensure_group_exclusive,
    get_bundle_map,
    validate_group_rule,
    validate_bundle_rule,
    validate_subscription_transition,
    validate_plan_change_window,
    is_valid_billing_day,
    is_near_anchor,
    can_resume_subscription,
    item_state,
)

from .pricing import calculate_joining_fee, calculate_subscription_pricing, get_effective_subscription_price

now = timezone.now()
# ---------------------------
# Connected account / member webhook
# ---------------------------
def mark_webhook_success(event_record):
    event_record.status = "succeeded"
    event_record.processed_at = timezone.now()
    event_record.needs_reconciliation = False
    event_record.save(
        update_fields=[
            "status",
            "processed_at",
            "needs_reconciliation",
        ]
    )

def mark_webhook_failed(event_record, error):
    event_record.status = "failed"
    event_record.error = error
    event_record.processed_at = timezone.now()
    event_record.save(
        update_fields=[
            "status",
            "error",
            "processed_at",
        ]
    )

def webhook_ok(event_record):
    mark_webhook_success(event_record)
    return HttpResponse(status=200)

def mark_member_joining_fee_paid(member_id):
    member = Member.objects.filter(id=member_id).first()

    if member and not member.has_paid_joining_fee:
        member.has_paid_joining_fee = True
        member.has_been_charged_joining_fee = True
        member.save(
            update_fields=[
                "has_paid_joining_fee",
                "has_been_charged_joining_fee",
            ]
        )

@csrf_exempt
def stripe_connected_webhook(request):
    payload = request.body
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE", "")

    today = timezone.localtime().date()


    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.STRIPE_CONNECTED_WEBHOOK_SECRET
        )
    except stripe.error.SignatureVerificationError:
        logger.error("[CONNECTED WEBHOOK] Signature verification failed")
        return HttpResponse(status=400)

    event_id = event["id"]

    event_record, created = StripeWebhookEvent.objects.get_or_create(
        event_id=event_id,
        defaults={
            "status": "processing"
       }
    )

    if event_record.status == "succeeded":
        return HttpResponse(status=200)

    account_id = event.get("account")
    if not account_id:
        logger.error("Missing account_id in webhook event %s", event_id)
        return webhook_ok(event_record)

    # ----------------- CONNECT EVENTS (member → club payments) -----------------

    
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]

        member_id = session["metadata"].get("member_id")
        subscription_id = session.get("subscription")

        if subscription_id:
            event_record.stripe_subscription_id = subscription_id
            event_record.needs_reconciliation = True
            event_record.save(
                update_fields=[
                    "stripe_subscription_id",
                    "needs_reconciliation",
                ]
            )

        if not subscription_id:
            return webhook_ok(event_record)



        member = Member.objects.filter(id=member_id).first()
        if not member:
            logger.error("Member not found for session %s", session["id"])
            return webhook_ok(event_record)

        sub = stripe.Subscription.retrieve(subscription_id, stripe_account=account_id, expand=["items.data.price"])
        if sub.status == "canceled":
            logger.info(
                "Ignoring checkout completion for already canceled subscription"
            )
            return webhook_ok(event_record)

        club_id = session["metadata"].get("club_id")
        club = Club.objects.filter(id=club_id).first()
        if not club:
            logger.error("Club not found for session %s", session["id"])
            return webhook_ok(event_record)
        
        plan_id = session["metadata"].get("plan_id")
        plan = MembershipPlan.objects.filter(id=plan_id).first()
        if not plan:
            logger.error("Plan not found for session %s", session["id"])
            return webhook_ok(event_record)


        stripe_customer_obj = get_or_create_stripe_customer(member.owner, club)

        if stripe_customer_obj.stripe_customer_id != sub.customer:
            stripe_customer_obj.stripe_customer_id = sub.customer
            stripe_customer_obj.save(update_fields=["stripe_customer_id"])
        
        with transaction.atomic():
            sub_obj, created = Subscription.objects.get_or_create(
                owner=member.owner,
                club=club,
                defaults={
                    "stripe_subscription_id": sub.id,
                    "status": sub.status,
                    "current_period_end": None,
                    "billing_mode": club.subscription_mode,
                    "billing_anchor_day": club.stripe_anchor_date,
                }
            )
    
            if not created:
                fields_to_update = ["status", "cancel_at_period_end"]
                if sub_obj.stripe_subscription_id != sub.id:
                    sub_obj.stripe_subscription_id = sub.id
                    fields_to_update.append("stripe_subscription_id")
                sub_obj.status = sub.status
                sub_obj.cancel_at_period_end = False
                sub_obj.save(update_fields=fields_to_update)
    
            stripe_item_id = None
    
            for item in sub["items"]["data"]:
                if item["price"]["id"] == plan.stripe_price_id:
                    stripe_item_id = item["id"]
                    break
    
    
            item, created = SubscriptionItem.objects.get_or_create(
                subscription=sub_obj,
                member=member,
                plan=plan,
                defaults={
                    "price_at_subscription": plan.price,
                    "stripe_price_id_at_subscription": plan.stripe_price_id,
                    "stripe_subscription_item_id": stripe_item_id,
                }
            )
            
            if not created:
                item.deleted_at = None
                item.price_at_subscription = plan.price
                item.stripe_price_id_at_subscription = plan.stripe_price_id
                item.stripe_subscription_item_id = stripe_item_id
            
                item.save(
                    update_fields=[
                        "deleted_at",
                        "price_at_subscription",
                        "stripe_price_id_at_subscription",
                        "stripe_subscription_item_id",
                    ]
                )
                    




                # 🔥 JOINING FEE LOGIC (PER MEMBER)
        if club.joining_fee > 0 and not member.has_paid_joining_fee:
            final_amount = calculate_joining_fee(club, member)
            stripe.InvoiceItem.create(
                customer=sub.customer,
                amount=final_amount,
                currency="jpy",
                description=f"{member.full_name} 入会金",
                metadata={
                    "member_id": member.id,
                    "club_id": club.id,
                    "plan_id": plan.id,
                    "type": "joining_fee",
                },
                idempotency_key=f"{event_id}_joining_fee",
                stripe_account=account_id
            )
    
    
        if club.subscription_mode == "regular":

            
            pricing = calculate_subscription_pricing(
                club=club,
                member=member,
                plan=plan,
                plan_price=plan.price,
                today=today,
                mode="regular",
                anchor_day=club.stripe_anchor_date,
            )

            proration = pricing["proration"]
            prorated_amount = pricing["final_amount"]
            remaining_days = proration["remaining_days"]
            

        
            # Charge for the remaining days until next anchor
            if prorated_amount > 0:
                stripe.InvoiceItem.create(
                    customer=sub.customer,
                    amount=prorated_amount,
                    currency="jpy",
                    description=f"Prorated membership ({pricing['proration']['remaining_days']} days until next anchor)",
                    metadata={
                        "member_id": member.id,
                        "club_id": club.id,
                        "plan_id": plan.id,
                        "type": "prorations",
                    },
                    idempotency_key=f"{event_id}_prorated_fee",
                    stripe_account=account_id
                )
        
            
        
            # Create and pay invoice
            invoice = stripe.Invoice.create(
                customer=sub.customer,
                subscription=sub.id,
                auto_advance=True,
                metadata={
                    "type": "initial_subscription",
                    "club_id": str(club.id),
                    "member_id": str(member.id),
                    "plan_id": str(plan.id),
                },
                idempotency_key=f"{event_id}_create_nvoice",
                stripe_account=account_id
            )

            invoice = stripe.Invoice.retrieve(
                invoice.id,
                stripe_account=account_id,
            )

            if invoice.status == "draft":
                invoice = stripe.Invoice.finalize_invoice(
                    invoice.id,
                    stripe_account=account_id,
                )
            elif invoice.status in ["open", "paid", "void", "uncollectible"]:
                logger.info(
                    "[checkout.session.completed] Invoice %s already finalized "
                    "with status=%s",
                    invoice.id,
                    invoice.status,
                )
            else:
                logger.warning(
                    "[checkout.session.completed] Unexpected invoice status=%s "
                    "for invoice=%s",
                    invoice.status,
                    invoice.id,
                )
            
            
            invoice = stripe.Invoice.retrieve(
                invoice.id,
                expand=["lines.data"],
                stripe_account=account_id,
            )
            
            
            local_invoice, local_payment = (
                create_local_invoice_from_stripe_invoice(
                    stripe_invoice=invoice,
                    subscription=sub_obj,
                    billing_reason="initial_subscription",
                    initial_status="open",
                )
            )

            logger.info(
                "[checkout.session.completed] Created local invoice "
                "local_invoice=%s stripe_invoice=%s payment=%s",
                local_invoice.id,
                invoice.id,
                local_payment.id,
            )

            if invoice.amount_due > 0:
                stripe.Invoice.pay(invoice.id, stripe_account=account_id, idempotency_key=f"{event_id}_pay_invoice")


                
        if club.subscription_mode == "monthly":

                    
            pricing = calculate_subscription_pricing(
                club=club,
                member=member,
                plan=plan,
                plan_price=plan.price,
                today=today,
                mode="monthly",
                anchor_day=club.stripe_anchor_date,
            )

            proration = pricing["proration"]
            prorated_amount = pricing["final_amount"]
            remaining_days = proration["remaining_days"]
            
            if prorated_amount > 0:
                stripe.InvoiceItem.create(
                    customer=sub.customer,
                    amount=prorated_amount,                    
                    currency="jpy",
                    description=f"Prorated membership ({pricing['proration']['remaining_days']} days)",
                    metadata={
                        "member_id": member.id,
                        "club_id": club.id,
                        "plan_id": plan.id,
                        "type": "prorations",
                    },
                    idempotency_key=f"{event_id}_proration_fee",
                    stripe_account=account_id
                )
        
            
        
            
            charged_next_month = False
            if today.day > club.stripe_anchor_date:
                final_next_month_amount = calculate_discounted_amount(
                    club=club,
                    member=member,
                    plan=plan,
                    base_amount=plan.price,
                    apply_to="subscription",
                )

                stripe.InvoiceItem.create(
                    customer=sub.customer,
                    amount=final_next_month_amount,
                    currency="jpy",
                    description=f"{plan.name} 翌月分前払い",
                    metadata={
                        "type": "next_month_fee",
                        "member_id": member.id,
                        "plan_id": plan.id,
                        "club_id": club.id,
                    },
                    idempotency_key=f"{event_id}_next_month_fee",
                    stripe_account=account_id
                )
                charged_next_month = True

            invoice = stripe.Invoice.create(
                customer=sub.customer,
                subscription=sub.id,
                auto_advance=True,
                metadata={
                    "type": "initial_subscription",
                    "club_id": str(club.id),
                    "member_id": str(member.id),
                    "plan_id": str(plan.id),
                },
                idempotency_key=f"{event_id}_invoice_create",
                stripe_account=account_id
            )

            invoice = stripe.Invoice.retrieve(
                invoice.id,
                stripe_account=account_id,
            )
            
            if invoice.status == "draft":
                invoice = stripe.Invoice.finalize_invoice(
                    invoice.id,
                    stripe_account=account_id,
                )
            elif invoice.status in ["open", "paid", "void", "uncollectible"]:
                logger.info(
                    "[checkout.session.completed] Invoice %s already finalized "
                    "with status=%s",
                    invoice.id,
                    invoice.status,
                )
            else:
                logger.warning(
                    "[checkout.session.completed] Unexpected invoice status=%s "
                    "for invoice=%s",
                    invoice.status,
                    invoice.id,
                )
            
            
            invoice = stripe.Invoice.retrieve(
                invoice.id,
                expand=["lines.data"],
                stripe_account=account_id,
            )
            
            local_invoice, local_payment = (
                create_local_invoice_from_stripe_invoice(
                    stripe_invoice=invoice,
                    subscription=sub_obj,
                    billing_reason="initial_subscription",
                    initial_status="open",
                )
            )
            
            logger.info(
                "[checkout.session.completed] Created local invoice "
                "local_invoice=%s stripe_invoice=%s payment=%s",
                local_invoice.id,
                invoice.id,
                local_payment.id,
            )
        
            if invoice.amount_due > 0:
                stripe.Invoice.pay(invoice.id, idempotency_key=f"{event_id}_invoice_pay", stripe_account=account_id)





            


            
            

        
        

        


        


            
    elif event["type"] == "invoice.paid":
        invoice = event["data"]["object"]
        logger.info(f"[invoice.paid] Received invoice: {invoice.get('id')}")

        
        
        is_cycle = invoice.get("billing_reason") == "subscription_cycle"
        


        subscription_id = extract_subscription_id_from_invoice(invoice)

        if not subscription_id:
            logger.warning(f"[invoice.paid] No subscription found on invoice {invoice.get('id')}")
            return webhook_ok(event_record)

        # -------- Find local subscription --------
        sub = Subscription.objects.filter(stripe_subscription_id=subscription_id, club__stripe_account_id=account_id).first()
        
        if not sub:
            logger.warning(
                "[invoice.paid] Subscription not found for invoice %s (subscription=%s)",
                invoice["id"],
                subscription_id
            )
            return HttpResponse(status=500)
        
        # -------- Idempotency check --------
        if sub.last_invoice_id == invoice["id"]:
            logger.info(f"[invoice.paid] Invoice {invoice['id']} already processed, skipping")
            return webhook_ok(event_record)
        
        invoice_type = invoice.get("metadata", {}).get("type")

        if invoice_type == "initial_subscription":

            logger.info(
                "[invoice.paid] Processing initial subscription invoice=%s",
                invoice["id"],
            )
        
            local_invoice = Invoice.objects.filter(
                stripe_invoice_id=invoice["id"],
                subscription=sub,
            ).first()
        
 
            if not local_invoice:
                logger.warning(
                    "[invoice.paid] Local invoice missing for Stripe invoice=%s. "
                    "Creating local invoice before marking it paid.",
                    invoice["id"],
                )
        
                local_invoice, _ = create_local_invoice_from_stripe_invoice(
                    stripe_invoice=invoice,
                    subscription=sub,
                    billing_reason="initial_subscription",
                    initial_status="open",
                )



            local_invoice, local_payment = mark_local_invoice_paid(
                local_invoice=local_invoice,
                stripe_invoice=invoice,
            )

            logger.info(
                "[invoice.paid] Local invoice=%s marked paid, "
                "payment=%s succeeded",
                local_invoice.id,
                local_payment.id,
            )

        else:        
            logger.info(
                "[invoice.paid] No initial-subscription local invoice handling "
                "for invoice=%s billing_event=%s",
                invoice["id"],
                invoice_type,
            )  


        
        
        is_first_invoice = sub.last_invoice_id is None
        should_update_period = is_first_invoice or is_cycle

        logger.info(f"[invoice.paid] Found subscription {sub.id} for invoice {invoice['id']}")
        

        email_items = []

        primary_member_id = None
        primary_plan_id = None

        total_amount = invoice.get("amount_paid", 0)

        for line in invoice.get("lines", {}).get("data", []):
            metadata = line.get("metadata", {})

            charge_type = metadata.get("type")
            member_id = metadata.get("member_id")

            if member_id and charge_type in [
                "subscription",
                "prorations",
                "joining_fee",
                "next_month_fee",
            ]:
                mark_member_joining_fee_paid(member_id)
            
            plan_id = metadata.get("plan_id")
            

            if member_id and not primary_member_id:
                primary_member_id = member_id

            if plan_id and not primary_plan_id:
                primary_plan_id = plan_id

            if charge_type == "joining_fee":
                email_items.append("入会金")

            elif charge_type == "prorations":
                email_items.append("日割り料金")




    
            


            

        


        

        # -------- Extract period end from invoice lines --------
        periods = [
            line["period"]["end"]
            for line in invoice.get("lines", {}).get("data", [])
            if line.get("period") and line["period"].get("end")
        ]

        period_end_ts = max(periods) if periods else None
        logger.info(f"[invoice.paid] Calculated period_end_ts: {period_end_ts}")

        if period_end_ts and should_update_period:
            resolve_and_apply_subscription_period(sub, period_end_ts, today)
        else:
            logger.info(
                f"[invoice.paid] Skipping period update for invoice {invoice['id']} "
                f"(period_end_ts={period_end_ts}, should_update_period={should_update_period})"
            )

        # -------- Finalize --------

        sub.status = "active"
        sub.last_invoice_id = invoice["id"]
        sub.save()

        members = Member.objects.filter(
            subscription_items__subscription=sub
        ).distinct()
        
        member = Member.objects.filter(id=primary_member_id).first()
        plan = MembershipPlan.objects.filter(id=primary_plan_id).only("name").first()

        if email_items and member and plan:
            logger.info("[invoice.paid] QUEUING send_invoice_paid_email task")
            send_invoice_paid_email.delay(
                member_id=member.id,
                amount=total_amount,
                items=email_items,
                period_end=sub.access_until,
                plan_name=plan.name,
            )
        logger.info(
            "[invoice.paid] Email debug → email_items=%s primary_member_id=%s primary_plan_id=%s",
            email_items,
            primary_member_id,
            primary_plan_id,
        )
        logger.info(
            "[invoice.paid] Resolved → member=%s plan=%s",
            member.id if member else None,
            plan.id if plan else None,
        )

        logger.info(
            "[invoice.paid] Processed invoice %s for subscription %s (status=%s)",
            invoice["id"],
            subscription_id,
            sub.status
        )


    
    elif event["type"] == "invoice.payment_failed":
        invoice = event["data"]["object"]
        sub = Subscription.objects.filter(
            stripe_subscription_id=invoice.get("subscription"), club__stripe_account_id=account_id
        ).first()
        if sub:
            sub.status = "past_due"
            sub.save()

    elif event["type"] == "customer.subscription.deleted":
        stripe_sub = event["data"]["object"]
        
        sub = Subscription.objects.filter(stripe_subscription_id=stripe_sub["id"], club__stripe_account_id=account_id).first()
        if sub:
            try:
                with subscription_lock(sub.id, timeout=300):
                    sub.status = "canceled"
                    sub.save()

            except CacheLockError:
                logger.info(
                    "Subscription locked, retrying webhook later subscription=%s",
                    sub.id,
                )
                return HttpResponse(status=409)

    
    elif event["type"] == "invoice.created":
        invoice = event["data"]["object"]

        billing_reason = invoice.get("billing_reason")
        is_cycle = billing_reason == "subscription_cycle"

        if not is_cycle:
            logger.info(
                f"[invoice.created] Ignored invoice {invoice.get('id')} "
                f"because billing_reason={billing_reason}"
            )
            return webhook_ok(event_record)



        logger.info(f"[invoice.created] Processing invoice {invoice.get('id')}")
    
        def extract_subscription_from_invoice(invoice):
            # 1. direct (sometimes present)
            sub_id = invoice.get("subscription")
            if sub_id:
                return sub_id
    
            # 2. parent.subscription_details (your case)
            parent = invoice.get("parent", {})
            sub_details = parent.get("subscription_details", {})
            if sub_details.get("subscription"):
                return sub_details["subscription"]
    
            # 3. fallback: scan invoice lines
            for line in invoice.get("lines", {}).get("data", []):
                parent = line.get("parent", {})
                details = parent.get("subscription_item_details", {})
                if details.get("subscription"):
                    return details["subscription"]
    
            return None
    
        subscription_id = extract_subscription_from_invoice(invoice)
    
        if not subscription_id:
            logger.warning(f"[invoice.created] No subscription on invoice {invoice.get('id')}")
            return webhook_ok(event_record)
    
        sub = Subscription.objects.filter(
            stripe_subscription_id=subscription_id,
            club__stripe_account_id=account_id
        ).first()
    
        if not sub:
            logger.warning(f"[invoice.created] Subscription not found {subscription_id}")
            return HttpResponse(status=500)

        try:
            with subscription_lock(sub.id, timeout=300):


            
                # ------------------------------------------------------------
                # 1. Gather members in this subscription
                # ------------------------------------------------------------
                members = Member.objects.filter(
                    subscription_items__subscription=sub
                ).distinct()
            
                if not members.exists():
                    logger.warning(f"[invoice.created] No members found for subscription {sub.id}")
                    return HttpResponse(status=500)
            
                # ------------------------------------------------------------
                # 2. Stripe total
                # ------------------------------------------------------------
                stripe_total = invoice.get("amount_due", 0)
            
                # ------------------------------------------------------------
                # 3. Compute expected total using NEW unified engine
                # ------------------------------------------------------------
                expected_total = 0
        
                for member in members:
                    items = SubscriptionItem.objects.filter(
                        subscription=sub,
                        member=member,
                        deleted_at__isnull=True,
                    ).select_related("plan")
                
                    if not items.exists():
                        continue
                
                    member_total = 0
                
                    for item in items:
                        if not item.plan:
                            continue
                
                        base = get_effective_subscription_price(item)
                
                        discounted = calculate_discounted_amount(
                            club=sub.club,
                            member=member,
                            plan=item.plan,
                            base_amount=base,
                            apply_to="subscription",
                        )
                
                        member_total += discounted
                
                    expected_total += member_total
                
                expected_total = max(0, int(expected_total))
            
                # ------------------------------------------------------------
                # 4. Delta calculation
                # ------------------------------------------------------------
                delta = stripe_total - expected_total
            
                logger.info(
                    f"[invoice.created] Stripe={stripe_total}, Expected={expected_total}, Delta={delta}"
                )
            
                # ------------------------------------------------------------
                # 5. Apply correction ONLY if needed
                # ------------------------------------------------------------
                if abs(delta) > 0:
            
                    stripe.InvoiceItem.create(
                        customer=invoice["customer"],
                        invoice=invoice["id"],
                        amount=-delta,
                        currency="jpy",
                        description="Automated pricing adjustment (discount reconciliation)",
                        metadata={
                            "type": "pricing_delta",
                            "subscription_id": sub.id,
                        },
                        idempotency_key=f"{event_id}_discount",
                        stripe_account=account_id
                    )
            
                    logger.info(f"[invoice.created] Applied delta adjustment: {-delta}")
            
        except CacheLockError:
            logger.info(
                "Subscription locked, retrying webhook later subscription=%s",
                sub.id,
            )
            return HttpResponse(status=409)

    return webhook_ok(event_record)

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

    event_record, created = StripeWebhookEvent.objects.get_or_create(
        event_id=event_id,
        defaults={
            "status": "processing"
        }
    )

    if event_record.status == "succeeded":
        return HttpResponse(status=200)

    # ----------------- PLATFORM EVENTS -----------------
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        club_id = session["metadata"].get("club_id")
        subscription_id = session.get("subscription")
        if not subscription_id:
            logger.warning("Subscription not found for session %s", session["id"])
            return webhook_ok(event_record)

        


        club = Club.objects.filter(id=club_id, is_deleted=False).first()
        if not club:
            logger.error("Platform webhook: club not found for session %s", session["id"])
            return webhook_ok(event_record)


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
            return webhook_ok(event_record)

        if club.last_paid_invoice_id == invoice["id"]:
            return webhook_ok(event_record)

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

    return webhook_ok(event_record) 