import stripe
from django.conf import settings
from django.http import JsonResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from .tasks import cancel_stripe_subscription
from .tasks_emails import send_subscription_canceled_emails
from django.shortcuts import get_object_or_404, redirect
from django.db.models import Q

from datetime import datetime, timezone as dt_timezone
from django.utils import timezone
import calendar
import logging
from django.core.cache import cache

from django.db import transaction
logger = logging.getLogger(__name__)
from .service_subscription import SubscriptionItemService

from .models import Club, Member, MembershipPlan, SubscriptionItem, Subscription, StripeCustomer

stripe.api_key = settings.STRIPE_SECRET_KEY

from .stripe_service import get_or_create_stripe_customer

from .billing import (
    get_next_month_start,
    get_next_billing_cycle_anchor,
    get_cancel_quantity_action,
    should_set_monthly_resume_prevention,
    should_cancel_subscription,
    get_cancel_success_message,
    get_resume_item_action,
    should_charge_resume_next_month,
    get_resume_charge_amount,
    get_resume_success_message,
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
    assert_item_unlocked,
)



from .pricing import calculate_joining_fee, calculate_subscription_pricing

from .discounts import (
    calculate_discounted_amount,
    calculate_discount_breakdown,
    check_conditions,
    calculate_age,
    apply_joining_fee_discount,
    apply_subscription_discount,
)

@require_POST
@login_required
def create_checkout_session(request, club_id):

    club = get_object_or_404(Club, id=club_id, is_deleted=False)

    if club.owner != request.user:
        return HttpResponseForbidden("You do not own this club")
    
    if club.stripe_subscription_id:
        sub = stripe.Subscription.retrieve(club.stripe_subscription_id)

        if sub.status in ["active", "trialing", "past_due", "unpaid", "incomplete"]:
            return JsonResponse({"error": "Already subscribed"}, status=400)
       

    if not club.stripe_customer_id:
        customer = stripe.Customer.create(
            name=club.title or club.subdomain,
            metadata={"club_id": str(club.id)},
        )
        club.stripe_customer_id = customer.id
        club.save()
    else:
        customer = stripe.Customer.retrieve(club.stripe_customer_id)

    active_members = Member.objects.filter(
        club=club
    ).exclude(is_kyukai=True, is_kyukai_paid=True).count()

    billable_members = max(active_members, 1)

    session = stripe.checkout.Session.create(
        customer=customer.id,
        mode="subscription",
        payment_method_types=["card"],
        line_items=[
            {"price": settings.STRIPE_BASE_PRICE_ID, "quantity": 1},
            {"price": settings.STRIPE_MEMBER_PRICE_ID, "quantity": billable_members},
        ],
        metadata={"club_id": club.id},
        success_url=f"https://{club.subdomain}.kaibaru.jp/?payment=success",
        cancel_url=f"https://{club.subdomain}.kaibaru.jp/?payment=cancel",
    )

    return JsonResponse({"id": session.id})




@login_required
@require_POST
def unsubscribe(request, club_id):

    club = get_object_or_404(Club, id=club_id, is_deleted=False)

    if club.owner != request.user:
        return HttpResponseForbidden()

    if not club.stripe_subscription_id:
        return JsonResponse({"error": "No subscription"}, status=400)

    sub = stripe.Subscription.modify(
        club.stripe_subscription_id,
        cancel_at_period_end=True
    )

    club.subscription_cancel_at_period_end = True


    period_end_ts = sub.get("current_period_end")
    if period_end_ts:
        club.subscription_current_period_end = datetime.fromtimestamp(
            period_end_ts,
            tz=dt_timezone.utc
        )

    club.save(update_fields=[
        "subscription_cancel_at_period_end",
        "subscription_current_period_end"
    ])

    return JsonResponse({"success": True})


@login_required
@require_POST
def resume_club_subscription(request, club_id):

    

    club = get_object_or_404(Club, id=club_id, is_deleted=False)
    if club.owner != request.user:
        return HttpResponseForbidden()

    sub = stripe.Subscription.modify(
        club.stripe_subscription_id,
        cancel_at_period_end=False
    )

    

    club.subscription_cancel_at_period_end = False
    club.save(update_fields=["subscription_cancel_at_period_end"])

    return JsonResponse({"success": True})



@login_required
@require_POST
def change_member_plan(request, item_id, new_plan_id):

    item = get_object_or_404(
        SubscriptionItem,
        id=item_id,
        subscription__owner=request.user
    )

    state = item_state(item)

    if state == "expired":
        return JsonResponse({"error": "このプランは変更できません（有効期限切れ）"}, status=400)

    error_response = assert_item_unlocked(item)
    if error_response:
        return error_response

    lock_key = f"change_item:{item.id}:{new_plan_id}"
    if not cache.add(lock_key, True, timeout=10):
        return JsonResponse({"error": "Please wait"}, status=429)

    subscription = item.subscription

    club = subscription.club
    today = timezone.localtime().date()

    error = validate_plan_change_window(today=today, subscription=subscription)
    if error:
        return JsonResponse({"error": error}, status=400)

    new_plan = get_object_or_404(
        MembershipPlan,
        id=new_plan_id,
        club=club,
        active=True
    )

    exists = SubscriptionItem.objects.filter(
        subscription=subscription,
        member=item.member,
        plan=new_plan
    ).filter(active_items_q()).exists()

    if exists:
        return JsonResponse({"error": "このプランはすでに契約中です"}, status=400)

    if item.plan_id == new_plan.id:
        return JsonResponse({"error": "同じプランには変更できません"}, status=400)

    validate_subscription_transition(
        subscription=subscription,
        member=item.member,
        new_plan=new_plan,
        old_plan_id=item.plan_id
    )

    old_item_is_grace = (item_state(item) == "grace")
    try:
        new_item = SubscriptionItemService.change_plan(
            item=item,
            new_plan=new_plan,
            subscription=subscription,
            club=club,
            old_item_is_grace=old_item_is_grace
        )

    except Exception as e:
        logger.error(f"Plan change failed for item {item.id}: {e}")
        return JsonResponse({"error": str(e)}, status=500)

    return JsonResponse({
        "success": True,
        "message": "プラン変更を予約しました",
        "new_item_id": new_item.id
    })


@login_required
@require_POST
def cancel_member_subscription(request, item_id):

    item = get_object_or_404(
        SubscriptionItem,
        id=item_id,
        subscription__owner=request.user
    )

    state = item_state(item)

    if state in ("grace", "expired"):
        return JsonResponse({"error": "このプランはすでにキャンセルされています"}, status=400)

    error_response = assert_item_unlocked(item)
    if error_response:
        return error_response

    if item.deleted_at is not None:
        return JsonResponse({"error": "このプランはすでに解約されています"}, status=400)

    lock_key = f"cancel_item:{item.id}"
    if not cache.add(lock_key, True, timeout=10):
        return JsonResponse({"error": "Please wait"}, status=429)

    subscription = item.subscription

    club = subscription.club
    today = timezone.localtime().date()

    error = validate_plan_change_window(today=today, subscription=subscription)
    if error:
        return JsonResponse({"error": error}, status=400)

    try:
        SubscriptionItemService.cancel_item(
            item=item,
            subscription=subscription,
            club=club
        )

    except Exception as e:
        logger.error(f"Stripe delete failed for item {item.id}: {e}")
        return JsonResponse({"error": str(e)}, status=500)

    remaining_items = subscription.items.filter(
        deleted_at__isnull=True
    ).exclude(id=item.id)

    if should_cancel_subscription(remaining_items.exists()):
        subscription.cancel_at_period_end = True
        subscription.save(update_fields=["cancel_at_period_end"])

    return JsonResponse({
        "success": True,
        "message": get_cancel_success_message(subscription)
    })






@login_required
@require_POST
def resume_member_subscription(request, item_id):
    item = get_object_or_404(
        SubscriptionItem,
        id=item_id,
        subscription__owner=request.user
    )

    state = item_state(item)

    if state == "expired":
        return JsonResponse({"error": "このプランは変更できません（有効期限切れ）"}, status=400)
    
    error_response = assert_item_unlocked(item)
    if error_response:
        return error_response

    lock_key = f"resume_item:{item.id}"
    if not cache.add(lock_key, True, timeout=10):
        return JsonResponse({"error": "Please wait"}, status=429)

    now = timezone.now()

    if not can_resume_subscription(item, now):
        return JsonResponse(
            {"error": "このプランは再開できません（再開可能期間を過ぎています）"},
            status=400
        )

    if item.deleted_at is None:
        return JsonResponse(
            {"error": "このプランは既に有効です"},
            status=400
        )

    subscription = item.subscription
    club = subscription.club
    today = timezone.localtime().date()

    error = validate_plan_change_window(
        today=today,
        subscription=subscription,
    )

    if error:
        return JsonResponse({"error": error}, status=400)

    stripe_customer_obj = get_or_create_stripe_customer(
        subscription.owner,
        club
    )

    try:
        stripe_sub = stripe.Subscription.retrieve(
            subscription.stripe_subscription_id,
            stripe_account=club.stripe_account_id,
            expand=["items.data"],
        )

        stripe_items = stripe_sub["items"]["data"]

        existing_item = next(
            (
                i for i in stripe_items
                if i["price"]["id"] == item.stripe_price_id_at_subscription
            ),
            None
        )

        action, stripe_item_id, qty = get_resume_item_action(existing_item)

        if action == "modify":
            stripe.SubscriptionItem.modify(
                stripe_item_id,
                quantity=qty,
                proration_behavior="none",
                stripe_account=club.stripe_account_id,
            )

        else:
            new_item = stripe.SubscriptionItem.create(
                subscription=stripe_sub["id"],
                price=item.stripe_price_id_at_subscription,
                quantity=1,
                proration_behavior="none",
                stripe_account=club.stripe_account_id,
            )
            stripe_item_id = new_item["id"]



        item.deleted_at = None
        item.access_until = None
        item.stripe_subscription_item_id = stripe_item_id

        item.save(update_fields=[
            "stripe_subscription_item_id",
            "deleted_at",
            "access_until",
        ])

    except Exception as e:
        logger.error(f"Failed to resume Stripe item {item.id}: {e}")
        return JsonResponse({"error": str(e)}, status=500)

    subscription.cancel_at_period_end = False
    subscription.save(update_fields=["cancel_at_period_end"])

    return JsonResponse({
        "success": True,
        "message": get_resume_success_message()
    })




@login_required
@require_POST
def cancel_member_plan_change(request, new_item_id):
    new_item = get_object_or_404(
        SubscriptionItem,
        id=new_item_id,
        subscription__owner=request.user
    )


    if not new_item.source_item:
        return JsonResponse({"error": "このプランは変更対象ではありません"}, status=400)

    lock_key = f"cancel_change:{new_item.id}"
    if not cache.add(lock_key, True, timeout=10):
        return JsonResponse({"error": "Please wait"}, status=429)

    old_item = new_item.source_item

    error_response = assert_item_unlocked(old_item)
    if error_response:
        return error_response
   
    subscription = old_item.subscription

    club = subscription.club

    old_plan_deleted = old_item.plan and old_item.plan.deleted_at is not None

    try:
        stripe_sub = stripe.Subscription.retrieve(
            subscription.stripe_subscription_id,
            stripe_account=club.stripe_account_id,
            expand=["items.data"]
        )

        # =========================================================
        # 1. REMOVE NEW STRIPE ITEM (decrement or delete)
        # =========================================================
        new_stripe_item = next(
            (i for i in stripe_sub["items"]["data"]
             if i["id"] == new_item.stripe_subscription_item_id),
            None
        )

        if new_stripe_item:
            qty = new_stripe_item["quantity"]

            if qty > 1:
                stripe.SubscriptionItem.modify(
                    new_stripe_item["id"],
                    quantity=qty - 1,
                    proration_behavior="none",
                    stripe_account=club.stripe_account_id
                )
            else:
                stripe.SubscriptionItem.delete(
                    new_stripe_item["id"],
                    proration_behavior="none",
                    stripe_account=club.stripe_account_id
                )

        # =========================================================
        # 2. RESTORE OLD STRIPE ITEM safely
        # =========================================================
        if not old_plan_deleted:
            old_stripe_item = next(
                (i for i in stripe_sub["items"]["data"]
                 if i["price"]["id"] == old_item.plan.stripe_price_id),
                None
            )

            if old_stripe_item:
                stripe.SubscriptionItem.modify(
                    old_stripe_item["id"],
                    quantity=old_stripe_item["quantity"] + 1,
                    proration_behavior="none",
                    stripe_account=club.stripe_account_id
                )
                restored_id = old_stripe_item["id"]
            else:
                created = stripe.SubscriptionItem.create(
                    subscription=subscription.stripe_subscription_id,
                    price=old_item.plan.stripe_price_id,
                    quantity=old_item.quantity or 1,
                    proration_behavior="none",
                    stripe_account=club.stripe_account_id
                )
                restored_id = created["id"]

            old_item.stripe_subscription_item_id = restored_id

            # =========================================================
            # 3. DB restore old item
            # =========================================================
            old_item.deleted_at = None
            old_item.access_until = None
            old_item.save(update_fields=[
                "deleted_at",
                "access_until",
                "stripe_subscription_item_id"
            ])

        # =========================================================
        # 4. remove scheduled item
        # =========================================================
        new_item.delete()

    except Exception as e:
        logger.error(f"Cancel plan change failed {new_item.id}: {e}")
        return JsonResponse({"error": str(e)}, status=500)

    return JsonResponse({
        "success": True,
        "message": "プラン変更を取り消しました"
    })



import urllib.parse

@login_required
@require_POST
def create_stripe_account_link(request, club_id):
    """
    Start Stripe OAuth flow to connect an existing account or create a new one.
    """
    club = get_object_or_404(Club, id=club_id, is_deleted=False)

    if club.owner != request.user:
        return JsonResponse({"error": "Not allowed"}, status=403)

    

    if club.stripe_onboarding_completed:
        return JsonResponse({"message": "Stripe already connected"})

    redirect_uri = f"https://kaibaru.jp/stripe_oauth_callback/"
    params = {
        "response_type": "code",
        "client_id": settings.STRIPE_CLIENT_ID,
        "scope": "read_write",
        "stripe_user[email]": club.owner.email,
        "state": str(club.id),  # track which club this is for
        "redirect_uri": redirect_uri,
    }

    stripe_oauth_url = "https://connect.stripe.com/oauth/authorize?" + urllib.parse.urlencode(params)

    return JsonResponse({"url": stripe_oauth_url})

@login_required
@require_POST
def create_member_checkout_session(request, club_id, plan_id):
    

    club = get_object_or_404(Club, id=club_id, is_deleted=False)
    if club.subscription_mode not in ["regular", "monthly"]:
        return JsonResponse({"error": "Invalid billing configuration"}, status=400)


    if not club.stripe_anchor_date:
        return JsonResponse({"error": "Billing anchor not configured"}, status=400)

    if not club.stripe_account_id:
        return JsonResponse({"error": "Club has no Stripe account"}, status=400)

    member_id = request.POST.get("member_id")
    member = get_object_or_404(Member, id=member_id, club=club)

    today = timezone.localtime().date()

    
    
    if member.owner != request.user:
        return JsonResponse({"error": "Not allowed"}, status=403)

    billing_user = member.owner

    stripe_customer_obj = get_or_create_stripe_customer(billing_user, club)

    if not billing_user:
        return JsonResponse(
            {"error": "No billing owner set for this member"},
            status=400
        )

    plan = get_object_or_404(MembershipPlan, id=plan_id, club=club, active=True)
    if not plan.stripe_price_id:
        return JsonResponse({"error": "Plan not configured correctly"}, status=400)

    
    

    existing = SubscriptionItem.objects.filter(
        member=member,
        plan=plan,
        subscription__owner=member.owner,
        subscription__club=member.club,
        subscription__status__in=["active", "trialing", "pending"],
    ).filter(
        active_items_q()
    ).exists()
    
    if existing:
        return JsonResponse(
            {"error": "Already has this plan (active or grace period)"},
            status=400
        )





    # -------------------------
    # Check for existing subscription
    # -------------------------
    
    sub = Subscription.objects.filter(
        owner=member.owner,
        club=member.club,
        status__in=["active", "trialing", "past_due", "incomplete", "pending"]
    ).first()

    validate_subscription_transition(
        subscription=sub,
        member=member,
        new_plan=plan,
        old_plan_id=None
    )

    ensure_group_exclusive(sub, member, plan)

    today = timezone.localtime().date()

    if sub:
        if is_near_anchor(today, sub.billing_anchor_day) or not is_valid_billing_day(today):
            return JsonResponse({
                "error": "毎月2日〜27日のみ変更可能です。また、請求日の前後1日は変更できません。別の日にお試しください。"
            }, status=400)


    else:
        if is_near_anchor(today, club.stripe_anchor_date) or not is_valid_billing_day(today):
            return JsonResponse({
                "error": "毎月2日〜27日のみ変更可能です。また、請求日の前後1日は変更できません。別の日にお試しください。"
            }, status=400)

    charged_next_month = False
    # -------------------------
    # CASE 1: Subscription exists → add item
    # -------------------------
    if sub:
        lock_key = f"add_item:{member.id}:{plan.id}"
        if not cache.add(lock_key, True, timeout=15):
            return JsonResponse({"error": "Please wait"}, status=429)
        
        
        stripe_customer_obj = get_or_create_stripe_customer(billing_user, club)
    
        # -------------------------
        # REGULAR MODE
        # -------------------------
        if sub.billing_mode == "regular":
    
            


            #yoyo
            sub_data = stripe.Subscription.retrieve(
                sub.stripe_subscription_id,
                expand=["items.data"],
                stripe_account=club.stripe_account_id
            )
        
            # --- Check if the price already exists ---
            existing_item = next(
                (i for i in sub_data["items"]["data"]
                 if i["price"]["id"] == plan.stripe_price_id),
                None
            )
        
            # --- If exists, modify (or increment quantity), else create new ---
            if existing_item:
                stripe_item_id = existing_item["id"]

                stripe.SubscriptionItem.modify(
                    stripe_item_id,
                    quantity=existing_item["quantity"] + 1,
                    proration_behavior="none",
                    stripe_account=club.stripe_account_id
                )

            else:
                # Create a new subscription item
                stripe_item = stripe.SubscriptionItem.create(
                    subscription=sub.stripe_subscription_id,
                    price=plan.stripe_price_id,   # ✅ use SAME price always
                    quantity=1,
                    proration_behavior="none",
                    stripe_account=club.stripe_account_id
                )
                stripe_item_id = stripe_item.id

            if club.joining_fee > 0 and not member.has_been_charged_joining_fee:

                final_amount = calculate_joining_fee(club, member)

                if final_amount > 0:
                    stripe.InvoiceItem.create(
                        customer=stripe_customer_obj.stripe_customer_id,
                        amount=final_amount,
                        currency="jpy",
                        description=f"{member.full_name} 入会金",
                        metadata={
                            "member_id": member.id,
                            "club_id": club.id,
                            "plan_id": plan.id,
                            "type": "joining fee",
                        },
                        stripe_account=club.stripe_account_id
                    )

                member.has_been_charged_joining_fee = True
                member.save(update_fields=["has_been_charged_joining_fee"])




    
            pricing = calculate_subscription_pricing(
                club=club,
                member=member,
                plan=plan,
                plan_price=plan.price,
                today=today,
                mode=sub.billing_mode,
                anchor_day=sub.billing_anchor_day,
            )

            final_amount = pricing["final_amount"]
            
            # Charge for the remaining days until next anchor
            if final_amount > 0:
                stripe.InvoiceItem.create(
                    customer=stripe_customer_obj.stripe_customer_id,
                    amount=final_amount,
                    currency="jpy",
                    description=f"Prorated membership ({pricing['proration']['remaining_days']} days until next anchor)",
                    metadata={
                        "member_id": member.id,
                        "club_id": club.id,
                        "plan_id": plan.id,
                        "type": "prorations",
                    },
                    stripe_account=club.stripe_account_id
                )
                            
    
    


    
        # -------------------------
        # MONTHLY MODE
        # -------------------------
        else:

            anchor_day = sub.billing_anchor_day
    
            # Disable Stripe proration
            sub_data = stripe.Subscription.retrieve(
                sub.stripe_subscription_id,
                expand=["items.data"],
                stripe_account=club.stripe_account_id
            )
        
            # --- Check if the price already exists ---
            existing_item = next(
                (i for i in sub_data["items"]["data"]
                 if i["price"]["id"] == plan.stripe_price_id),
                None
            )
        
            # --- If exists, modify (or increment quantity), else create new ---
            if existing_item:
                stripe_item_id = existing_item["id"]

                stripe.SubscriptionItem.modify(
                    stripe_item_id,
                    quantity=existing_item["quantity"] + 1,
                    proration_behavior="none",
                    stripe_account=club.stripe_account_id
                )
            
            else:
                # Create a new subscription item
                stripe_item = stripe.SubscriptionItem.create(
                    subscription=sub.stripe_subscription_id,
                    price=plan.stripe_price_id,   # ✅ use SAME price always
                    quantity=1,
                    proration_behavior="none",
                    stripe_account=club.stripe_account_id
                )
                stripe_item_id = stripe_item.id

            if club.joining_fee > 0 and not member.has_been_charged_joining_fee:

                final_joining_amount = calculate_joining_fee(club, member)

                if final_joining_amount > 0:
                    stripe.InvoiceItem.create(
                        customer=stripe_customer_obj.stripe_customer_id,
                        amount=final_joining_amount,
                        currency="jpy",
                        description=f"{member.full_name} 入会金",
                        metadata={
                            "member_id": member.id,
                            "club_id": club.id,
                            "plan_id": plan.id,
                            "type": "joining fee",
                        },
                        stripe_account=club.stripe_account_id
                    )

                member.has_been_charged_joining_fee = True
                member.save(update_fields=["has_been_charged_joining_fee"])

            



    
            pricing = calculate_subscription_pricing(
                club=club,
                member=member,
                plan=plan,
                plan_price=plan.price,
                today=today,
                mode="monthly",
                anchor_day=sub.billing_anchor_day,
            )

            final_amount = pricing["final_amount"]
    
            # Charge remaining days of this month
            if final_amount > 0:
                stripe.InvoiceItem.create(
                    customer=stripe_customer_obj.stripe_customer_id,
                    amount=final_amount,
                    currency="jpy",
                    description=f"Prorated membership ({pricing['proration']['remaining_days']} days)",
                    metadata={
                        "member_id": member.id,
                        "club_id": club.id,
                        "plan_id": plan.id,
                        "type": "prorations",
                    },
                    stripe_account=club.stripe_account_id
                )
        
            if today.day > anchor_day:
                final_next_month_amount = calculate_discounted_amount(
                    club=club,
                    member=member,
                    plan=plan,
                    base_amount=plan.price,
                    apply_to="subscription",
                )

                stripe.InvoiceItem.create(
                    customer=stripe_customer_obj.stripe_customer_id,
                    amount=final_next_month_amount,
                    currency="jpy",
                    description=f"{plan.name} 翌月分前払い",
                    metadata={
                        "member_id": member.id,
                        "club_id": club.id,
                        "plan_id": plan.id,
                        "type": "next month fee",
                    },
                    stripe_account=club.stripe_account_id
                )


        invoice = stripe.Invoice.create(
            customer=stripe_customer_obj.stripe_customer_id,
            subscription=sub.stripe_subscription_id,
            auto_advance=True,
            stripe_account=club.stripe_account_id
        )

        if invoice.amount_due > 0:
            stripe.Invoice.pay(
                invoice.id,
                stripe_account=club.stripe_account_id
            )
            

        SubscriptionItem.objects.create(
            member=member,
            subscription=sub,
            plan=plan,
            stripe_subscription_item_id=stripe_item_id,

            price_at_subscription=plan.price,
            stripe_price_id_at_subscription=plan.stripe_price_id,
            quantity=1,
            monthly_double_resume_charge_prevention=charged_next_month
        )
    
        return JsonResponse({
            "success": True,
            "message": "Plan added to existing subscription"
        })

    # -------------------------
    # CASE 2: No subscription yet → Checkout
    # -------------------------
    
    billing_cycle_anchor = get_next_billing_cycle_anchor(
        today=today,
        anchor_day=club.stripe_anchor_date
    )
        
    subscription_data = {
        "metadata": {
            "member_id": member.id,
            "club_id": club.id,
            "plan_id": plan.id,
        },
    }
    if billing_cycle_anchor:
        now = int(timezone.now().timestamp())

        if billing_cycle_anchor <= now:
            billing_cycle_anchor = now + 60
        subscription_data["billing_cycle_anchor"] = billing_cycle_anchor
        
        subscription_data["proration_behavior"] = "none"


    stripe_customer_obj = get_or_create_stripe_customer(billing_user, club)
    
    line_items = [
        {
            "price": plan.stripe_price_id,
            "quantity": 1,
        }
    ]

    

    pricing = calculate_subscription_pricing(
        club=club,
        member=member,
        plan=plan,
        plan_price=plan.price,
        today=today,
        mode=club.subscription_mode,
        anchor_day=club.stripe_anchor_date,
    )
    
    joining_fee = calculate_joining_fee(club, member)
    prorated_amount = pricing["final_amount"]
    remaining_days = pricing["proration"]["remaining_days"]
    
    next_month_amount = 0
    if (today.day > club.stripe_anchor_date) and club.subscription_mode == "monthly":
        next_month_amount = calculate_discounted_amount(
            club=club,
            member=member,
            plan=plan,
            base_amount=plan.price,
            apply_to="subscription",
        )
    
    
    session = stripe.checkout.Session.create(
        customer=stripe_customer_obj.stripe_customer_id,
        mode="subscription",
        payment_method_types=["card"],
        line_items=line_items,
        metadata={
            "member_id": member.id,
            "club_id": club.id,
            "plan_id": plan.id,
        },
        subscription_data=subscription_data,
        success_url=f"https://{club.subdomain}.kaibaru.jp/?subscription=success",
        cancel_url=f"https://{club.subdomain}.kaibaru.jp/?subscription=cancel",
        stripe_account=club.stripe_account_id,

        custom_text={
            "submit": {
                "message": (
                    f"今回のお支払い予定:\n"
                    f"・入会金: ¥{joining_fee}\n"
                    f"・日割り料金 ({remaining_days}日): ¥{prorated_amount}\n"
                    f"{'・翌月前払い: ¥' + str(next_month_amount) if next_month_amount else ''}\n\n"
                    f"※最終金額はシステム計算に基づき確定されます"
                )
            }
        },
    )

    return JsonResponse({"id": session.id})


@login_required
def stripe_oauth_callback(request):
    """
    Handles Stripe OAuth redirect for connecting existing accounts.
    Exchanges the 'code' for a Stripe account ID and optionally
    starts onboarding if the account is not fully enabled yet.
    """
    code = request.GET.get("code")
    state = request.GET.get("state")  # club_id passed in state
    error = request.GET.get("error")

    if error:
        return JsonResponse({"error": f"Stripe OAuth failed: {error}"}, status=400)

    # Validate club
    club = get_object_or_404(Club, id=state, is_deleted=False)

    if club.owner != request.user:
        return JsonResponse({"error": "Not allowed"}, status=403)

    if club.stripe_account_id and club.stripe_onboarding_completed:
        return JsonResponse({"message": "Stripe already connected"})

    

    try:
        # Exchange code for access token
        resp = stripe.OAuth.token(
            grant_type="authorization_code",
            code=code
        )
        stripe_account_id = resp["stripe_user_id"]

        if not stripe_account_id:
            return JsonResponse({"error": "Invalid Stripe account"}, status=400)

        if Club.objects.filter(stripe_account_id=stripe_account_id).exclude(id=club.id).exists():
            return JsonResponse(
                {"error": "This Stripe account is already connected to another club."},
                status=400
            )

        # Save to Club
        club.stripe_account_id = stripe_account_id

        account = stripe.Account.retrieve(stripe_account_id)

        club.stripe_charges_enabled = account.get("charges_enabled", False)
        club.stripe_payouts_enabled = account.get("payouts_enabled", False)
        club.stripe_details_submitted = account.get("details_submitted", False)
        
        # ✅ SAFER: only mark onboarding complete when details_submitted
        club.stripe_onboarding_completed = club.stripe_details_submitted
        club.save()
        
        # If onboarding not finished, create AccountLink
        if not club.stripe_onboarding_completed:
            account_link = stripe.AccountLink.create(
                account=stripe_account_id,
                refresh_url=f"https://{club.subdomain}.kaibaru.jp/owner/settings",
                return_url=f"https://{club.subdomain}.kaibaru.jp/owner/settings",
                type="account_onboarding",
            )
            return redirect(account_link.url)
        
        # Already onboarded
        return redirect(f"https://{club.subdomain}.kaibaru.jp/owner/settings")

    except stripe.error.StripeError as e:
        return JsonResponse({"error": str(e)}, status=400)

