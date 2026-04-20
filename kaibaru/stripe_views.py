import stripe
from django.conf import settings
from django.http import JsonResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from .tasks import cancel_stripe_subscription
from .tasks_emails import send_subscription_canceled_emails
from django.shortcuts import get_object_or_404, redirect

from datetime import datetime, timezone as dt_timezone
from django.utils import timezone
import calendar
import logging
from django.core.cache import cache

from .utils import is_near_anchor, is_valid_billing_day
from django.db import transaction
logger = logging.getLogger(__name__)

from .models import Club, Member, MembershipPlan, SubscriptionItem, Subscription, StripeCustomer

stripe.api_key = settings.STRIPE_SECRET_KEY

from .stripe_service import get_or_create_stripe_customer



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
def cancel_member_subscription(request, item_id):
    item = get_object_or_404(
        SubscriptionItem,
        id=item_id,
        subscription__owner=request.user
    )
    subscription = item.subscription
    club = subscription.club

    today = timezone.localtime().date()
    if is_near_anchor(today, subscription.billing_anchor_day) or not is_valid_billing_day(today):
        return JsonResponse({
            "error": "毎月2日〜27日のみ変更可能です。また、請求日の前後1日は変更できません。別の日にお試しください。"
        }, status=400)
    
    try:
        # 🔥 Fetch fresh Stripe subscription
        stripe_sub = stripe.Subscription.retrieve(
            subscription.stripe_subscription_id,
            stripe_account=club.stripe_account_id,
            expand=["items.data"]
        )
        
        stripe_item = next(
            (i for i in stripe_sub["items"]["data"]
             if i["price"]["id"] == item.plan.stripe_price_id),
            None
        )
        
        if not stripe_item:
            return JsonResponse(
                {"error": "Stripe item not found"},
                status=400
            )
        
        # 🔥 SAFE decrement (re-fetch quantity from Stripe object, not local math)
        current_qty = stripe_item["quantity"]
        
        if current_qty <= 1:
            stripe.SubscriptionItem.delete(
                stripe_item["id"],
                stripe_account=club.stripe_account_id
            )
        else:
            stripe.SubscriptionItem.modify(
                stripe_item["id"],
                quantity=current_qty - 1,
                proration_behavior="none",
                stripe_account=club.stripe_account_id
            )
                
        item.deleted_at = timezone.now()

        item.access_until = subscription.access_until

        item.save(update_fields=["deleted_at", "access_until"])
        
    except Exception as e:
        logger.error(f"Stripe delete failed for item {item.id}: {e}")
        return JsonResponse(
            {"error": str(e)},
            status=500
        )



    # ✅ Check if subscription should fully cancel
    remaining_items = subscription.items.filter(
        deleted_at__isnull=True
    ).exclude(id=item.id)

    if not remaining_items.exists():
        subscription.cancel_at_period_end = True
        subscription.save(update_fields=["cancel_at_period_end"])

    end_date = (
        subscription.access_until.strftime('%Y/%m/%d')
        if subscription.access_until
        else "次回更新日"
    )

    return JsonResponse({
        "success": True,
        "message": f"プランは削除されました。 {end_date} まで利用可能です"
    })

@login_required
@require_POST
def resume_member_subscription(request, item_id):
    item = get_object_or_404(
        SubscriptionItem,
        id=item_id,
        subscription__owner=request.user
    )

    subscription = item.subscription
    club = subscription.club

    stripe_customer_obj = get_or_create_stripe_customer(subscription.owner, club)

    today = timezone.localtime().date()
    if is_near_anchor(today, subscription.billing_anchor_day) or not is_valid_billing_day(today):
        return JsonResponse({
            "error": "毎月2日〜27日のみ変更可能です。また、請求日の前後1日は変更できません。別の日にお試しください。"
        }, status=400)
    

    if not item.deleted_at:
        return JsonResponse({
            "success": True,
            "message": "プランはすでに有効です"
        })

    try:
        # 🔥 Fetch fresh Stripe subscription
        stripe_sub = stripe.Subscription.retrieve(
            subscription.stripe_subscription_id,
            stripe_account=club.stripe_account_id,
            expand=["items.data"]
        )


        # 🔍 Check if the plan already exists in Stripe
        existing_item = None

        # First, try to retrieve the Stripe item by its ID
        stripe_items = stripe_sub["items"]["data"]

        existing_item = next(
            (i for i in stripe_items
             if i["price"]["id"] == item.plan.stripe_price_id),
            None
        )
        

        
        if existing_item:
            # 🔥 Increment quantity (THIS is your real "resume")
            stripe.SubscriptionItem.modify(
                existing_item["id"],
                quantity=existing_item["quantity"] + 1,
                proration_behavior="none",
                stripe_account=club.stripe_account_id
            )
            stripe_item_id = existing_item["id"]
        
        else:
            # 🔥 Only create if Stripe truly has no item for this plan
            new_item = stripe.SubscriptionItem.create(
                subscription=stripe_sub["id"],
                price=item.plan.stripe_price_id,
                quantity=1,
                proration_behavior="none",
                stripe_account=club.stripe_account_id
            )
            stripe_item_id = new_item["id"]
        
        
        today = timezone.localtime().date()

        if subscription.billing_mode == "monthly":
            anchor_day = subscription.billing_anchor_day
        
            if today.day > anchor_day and not item.monthly_double_resume_charge_prevention:
                # 🔥 Charge ONE extra month (next invoice)
                stripe.InvoiceItem.create(
                    customer=stripe_customer_obj.stripe_customer_id,
                    amount=item.plan.price,
                    currency="jpy",
                    description=f"{item.plan.name} 再開による翌月分請求",
                    stripe_account=club.stripe_account_id
                )
        
                item.monthly_double_resume_charge_prevention = True 

        # ✅ Clear deleted flag and save
        item.deleted_at = None
        item.access_until = None
        item.stripe_subscription_item_id = stripe_item_id

        item.save(update_fields=["stripe_subscription_item_id", "deleted_at", "access_until", "monthly_double_resume_charge_prevention"])

    except Exception as e:
        logger.error(f"Failed to resume Stripe item {item.id}: {e}")
        return JsonResponse({"error": str(e)}, status=500)

    # ✅ Keep subscription active
    subscription.cancel_at_period_end = False
    subscription.save(update_fields=["cancel_at_period_end"])

    return JsonResponse({
        "success": True,
        "message": "解約を取り消しました。プランが再開されました"
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

    # -------------------------
    # Prevent duplicate plan
    # -------------------------
    existing = SubscriptionItem.objects.filter(
        subscription__owner=member.owner,
        subscription__club=member.club,
        plan=plan,
        member=member,
        deleted_at__isnull=True,
        subscription__status__in=["active", "trialing", "past_due", "incomplete", "pending"],
    ).exists()

    if existing:
        return JsonResponse({"error": "Already subscribed to this plan"}, status=400)

    expired_item = SubscriptionItem.objects.filter(
        subscription__owner=member.owner,
        subscription__club=member.club,
        plan=plan,
        member=member,
        deleted_at__isnull=False,
        access_until__lt=timezone.now()
    ).order_by("-access_until").first()

    # -------------------------
    # Check for existing subscription
    # -------------------------
    
    sub = Subscription.objects.filter(
        owner=member.owner,
        club=member.club,
        status__in=["active", "trialing", "past_due", "incomplete", "pending"]
    ).first()

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
                (item for item in sub_data["items"]["data"] if item["price"]["id"] == plan.stripe_price_id),
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
                stripe.InvoiceItem.create(
                    customer=stripe_customer_obj.stripe_customer_id,
                    amount=club.joining_fee,
                    currency="jpy",
                    description=f"{member.full_name} 入会金",
                    stripe_account=club.stripe_account_id
                )

                member.has_been_charged_joining_fee = True
                member.save(update_fields=["has_been_charged_joining_fee"])




    
            today = timezone.localtime().date()

            # Use the subscription's anchor day
            anchor_day = sub.billing_anchor_day
            
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
            
            # Calculate prorated amount
            monthly_price = plan.price
            prorated_amount = int(monthly_price * remaining_days / billing_period_days)
            
            # Charge for the remaining days until next anchor
            if prorated_amount > 0:
                stripe.InvoiceItem.create(
                    customer=stripe_customer_obj.stripe_customer_id,
                    amount=prorated_amount,
                    currency="jpy",
                    description=f"Prorated membership ({remaining_days} days until next anchor)",
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
                (item for item in sub_data["items"]["data"] if item["price"]["id"] == plan.stripe_price_id),
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
                stripe.InvoiceItem.create(
                    customer=stripe_customer_obj.stripe_customer_id,
                    amount=club.joining_fee,
                    currency="jpy",
                    description=f"{member.full_name} 入会金",
                    stripe_account=club.stripe_account_id
                )

                member.has_been_charged_joining_fee = True
                member.save(update_fields=["has_been_charged_joining_fee"])

            



    
            today = timezone.localtime().date()
    
            days_in_month = calendar.monthrange(today.year, today.month)[1]
            remaining_days = days_in_month - today.day + 1
    
            monthly_price = plan.price
            prorated_amount = int(monthly_price * remaining_days / days_in_month)
    
            # Charge remaining days of this month
            if prorated_amount > 0:
                stripe.InvoiceItem.create(
                    customer=stripe_customer_obj.stripe_customer_id,
                    amount=prorated_amount,
                    currency="jpy",
                    description=f"Prorated membership ({remaining_days} days)",
                    stripe_account=club.stripe_account_id
                )
    
    
    
            if today.day > anchor_day:
                stripe.InvoiceItem.create(
                    customer=stripe_customer_obj.stripe_customer_id,
                    amount=plan.price,
                    currency="jpy",
                    description=f"{plan.name} 翌月分前払い",
                    stripe_account=club.stripe_account_id
                )

                        

        if expired_item:
    # ✅ REUSE old item
            expired_item.stripe_subscription_item_id = stripe_item_id
            expired_item.deleted_at = None
            expired_item.access_until = None
            expired_item.quantity = 1
            expired_item.save(update_fields=[
                "stripe_subscription_item_id",
                "deleted_at",
                "access_until",
                "quantity"
            ])
        else:
            # ✅ Create new item (normal case)
            SubscriptionItem.objects.create(
                member=member,
                subscription=sub,
                plan=plan,
                stripe_subscription_item_id=stripe_item_id,
                quantity=1
            )
    
        return JsonResponse({
            "success": True,
            "message": "Plan added to existing subscription"
        })

    # -------------------------
    # CASE 2: No subscription yet → Checkout
    # -------------------------
    
    billing_cycle_anchor = None
    
    now_ts = int(timezone.now().timestamp())

    if club.stripe_anchor_date:
        today = timezone.localtime().date()
        anchor_day = club.stripe_anchor_date

    # Decide the next anchor month
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
    
        anchor_date = datetime(year, month, day, tzinfo=dt_timezone.utc)
    
        billing_cycle_anchor = int(anchor_date.timestamp())    
        
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

    

    
    
    
    session = stripe.checkout.Session.create(
        customer=stripe_customer_obj.stripe_customer_id,
        mode="subscription",
        payment_method_types=["card"],
        custom_text={
            "submit": {
                "message": "本日の請求額はStripe上では¥0と表示されますが、実際の金額は前の画面をご確認ください。"
            }
        },
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

