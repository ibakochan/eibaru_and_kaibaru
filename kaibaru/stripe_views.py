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
from .service_subscription_cash import CashSubscriptionItemService

from .service_mutations import MutationLockedError

from .service_member_checkout import MemberSubscriptionCheckoutService

from .service_cash_subscription import MemberCashSubscriptionService

from .service_add_plan_cash import CashAddPlanService

from .models import Club, Member, MembershipPlan, SubscriptionItem, Subscription, StripeCustomer

stripe.api_key = settings.STRIPE_SECRET_KEY

from .stripe_service import get_or_create_stripe_customer
from .service_add_plan import SubscriptionAddPlanService

from .locks_and_reconciliation import subscription_lock, CacheLockError, StripeSubscriptionReconciler, CheckoutSubscriptionReconciler

import urllib.parse

from .billing import (
    get_next_month_start,
    get_next_billing_cycle_anchor,
    should_set_monthly_resume_prevention,
    should_cancel_subscription,
    get_cancel_success_message,
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
    get_resume_error_message,
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


@login_required
@require_POST
def reconcile_subscription_mutations_manual(request, subscription_id):

    subscription = get_object_or_404(
        Subscription,
        id=subscription_id,
        owner=request.user,
    )

    club = subscription.club

    try:
        with subscription_lock(subscription.id, timeout=300):

            result = StripeSubscriptionReconciler.reconcile(
                subscription=subscription,
                club=club,
            )

    except CacheLockError:
        return JsonResponse(
            {
                "error": "Subscription is currently being reconciled"
            },
            status=429
        )

    except Exception as e:
        logger.exception(
            "Manual mutation reconciliation failed subscription=%s",
            subscription.id,
        )

        return JsonResponse(
            {
                "error": str(e)
            },
            status=500
        )

    return JsonResponse(
        {
            "success": True,
            "subscription_id": subscription.id,
            "result": result,
        }
    )

@login_required
@require_POST
def reconcile_checkout_subscriptions_manual(request):

    try:
        CheckoutSubscriptionReconciler.reconcile_recent_checkouts()

    except Exception as e:
        logger.exception(
            "Manual checkout reconciliation failed"
        )

        return JsonResponse(
            {
                "error": str(e)
            },
            status=500
        )

    return JsonResponse(
        {
            "success": True,
            "message": "Checkout reconciliation completed"
        }
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
    ).count()

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



    now = timezone.now()


    subscription = item.subscription

    if subscription.billing_method != "stripe":
        return JsonResponse(
            {"error": "Stripe subscription must use stripe operation"},
            status=400
        )

    club = subscription.club
    today = timezone.localtime().date()

    error = validate_plan_change_window(today=today, subscription=subscription)
    if error:
        return JsonResponse({"error": error}, status=400)

    new_plan = get_object_or_404(
        MembershipPlan,
        id=new_plan_id,
        club=club,
        is_deleted=False,
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
        # ===============================
        # 🔒 SINGLE SOURCE OF TRUTH LOCK
        # ===============================
        with subscription_lock(subscription.id, timeout=300):

            new_item = SubscriptionItemService.change_plan(
                item=item,
                new_plan=new_plan,
                subscription=subscription,
                club=club,
                old_item_is_grace=old_item_is_grace
            )

    except CacheLockError:
        return JsonResponse(
            {"error": "前回のリクエストがまだ処理中です。数分後に再度お試しください。"},
            status=429
        )
    
    except MutationLockedError as e:
        return JsonResponse(
            {
                "error": str(e),
                "blocked_until": e.blocked_until.isoformat() if e.blocked_until else None
            },
            status=409
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



    now = timezone.now()


    if item.deleted_at is not None:
        return JsonResponse({"error": "このプランはすでに解約されています"}, status=400)



    subscription = item.subscription

    if subscription.billing_method != "stripe":
        return JsonResponse(
            {"error": "Stripe subscription must use stripe operation"},
            status=400
        )

    club = subscription.club
    today = timezone.localtime().date()

    error = validate_plan_change_window(today=today, subscription=subscription)
    if error:
        return JsonResponse({"error": error}, status=400)



    try:
        with subscription_lock(subscription.id, timeout=300):

            SubscriptionItemService.cancel_item(
                item=item,
                subscription=subscription,
                club=club
            )

    except CacheLockError:
        return JsonResponse(
            {"error": "前回のリクエストがまだ処理中です。数分後に再度お試しください。"},
            status=429
        )
    
    except MutationLockedError as e:
        return JsonResponse(
            {
                "error": str(e),
                "blocked_until": e.blocked_until.isoformat() if e.blocked_until else None
            },
            status=409
        )
    
    except Exception as e:
        logger.error(f"Stripe delete failed for item {item.id}: {e}")
        return JsonResponse({"error": str(e)}, status=500)

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
    




    now = timezone.now()

    if not can_resume_subscription(item, now):
        return JsonResponse(
            {"error": get_resume_error_message(item)},
            status=400
        )

    if item.deleted_at is None:
        return JsonResponse(
            {"error": "このプランは既に有効です"},
            status=400
        )

    subscription = item.subscription

    if subscription.billing_method != "stripe":
        return JsonResponse(
            {"error": "Stripe subscription must use stripe operation"},
            status=400
        )

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
        with subscription_lock(subscription.id, timeout=300):

            resumed_item = SubscriptionItemService.resume_item(
                item=item,
                subscription=subscription,
                club=club
            )

            

    except CacheLockError:
        return JsonResponse(
            {"error": "前回のリクエストがまだ処理中です。数分後に再度お試しください。"},
            status=429
        )
    
    except MutationLockedError as e:
        return JsonResponse(
            {
                "error": str(e),
                "blocked_until": e.blocked_until.isoformat() if e.blocked_until else None
            },
            status=409
        )

    except Exception as e:
        logger.error(f"Failed to resume Stripe item {item.id}: {e}")
        return JsonResponse({"error": str(e)}, status=500)

    return JsonResponse({
        "success": True,
        "message": get_resume_success_message(),
        "new_item_id": resumed_item.id
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



    old_item = new_item.source_item


   
    subscription = old_item.subscription

    if subscription.billing_method != "stripe":
        return JsonResponse(
            {"error": "Stripe subscription must use stripe operation"},
            status=400
        )

    club = subscription.club

    old_plan_deleted = old_item.plan and old_item.plan.deleted_at is not None

    try:
        with subscription_lock(subscription.id, timeout=300):
            SubscriptionItemService.cancel_change(
                new_item=new_item,
                old_item=old_item,
                subscription=subscription,
                club=club,
                old_plan_deleted=old_plan_deleted,
            )

    except CacheLockError:
        return JsonResponse(
            {
                "error": "前回のリクエストがまだ処理中です。数分後に再度お試しください。"
            },
            status=429
        )

    except MutationLockedError as e:
        return JsonResponse(
            {
                "error": str(e),
                "blocked_until": e.blocked_until.isoformat() if e.blocked_until else None
            },
            status=409
        )

    except Exception as e:
        logger.error(f"Cancel plan change failed {new_item.id}: {e}")
        return JsonResponse({"error": str(e)}, status=500)



    return JsonResponse({
        "success": True,
        "message": "プラン変更を取り消しました"
    })



@login_required
@require_POST
def change_cash_member_plan(request, item_id, new_plan_id):

    item = get_object_or_404(
        SubscriptionItem,
        id=item_id,
        subscription__owner=request.user
    )


    state = item_state(item)

    if state == "expired":
        return JsonResponse(
            {"error": "このプランは変更できません（有効期限切れ）"},
            status=400
        )


    subscription = item.subscription


    if subscription.billing_method == "stripe":
        return JsonResponse(
            {"error": "Non Stripe subscription can't use Stripe operation"},
            status=400
        )


    club = subscription.club
    today = timezone.localtime().date()


    error = validate_plan_change_window(
        today=today,
        subscription=subscription,
    )

    if error:
        return JsonResponse(
            {"error": error},
            status=400
        )


    new_plan = get_object_or_404(
        MembershipPlan,
        id=new_plan_id,
        club=club,
        is_deleted=False,
        active=True
    )


    exists = SubscriptionItem.objects.filter(
        subscription=subscription,
        member=item.member,
        plan=new_plan
    ).filter(
        active_items_q()
    ).exists()


    if exists:
        return JsonResponse(
            {"error": "このプランはすでに契約中です"},
            status=400
        )


    if item.plan_id == new_plan.id:
        return JsonResponse(
            {"error": "同じプランには変更できません"},
            status=400
        )


    validate_subscription_transition(
        subscription=subscription,
        member=item.member,
        new_plan=new_plan,
        old_plan_id=item.plan_id,
    )


    old_item_is_grace = (
        item_state(item) == "grace"
    )


    try:

        with subscription_lock(
            subscription.id,
            timeout=300
        ):

            new_item = CashSubscriptionItemService.change_plan(
                item=item,
                new_plan=new_plan,
                subscription=subscription,
                club=club,
                old_item_is_grace=old_item_is_grace,
            )


    except CacheLockError:

        return JsonResponse(
            {
                "error":
                "前回のリクエストがまだ処理中です。数分後に再度お試しください。"
            },
            status=429
        )


    except Exception as e:

        logger.error(
            f"Cash plan change failed for item {item.id}: {e}"
        )

        return JsonResponse(
            {"error": str(e)},
            status=500
        )


    return JsonResponse(
        {
            "success": True,
            "message": "プラン変更を予約しました",
            "new_item_id": new_item.id,
        }
    )



@login_required
@require_POST
def cancel_cash_member_subscription(request, item_id):

    item = get_object_or_404(
        SubscriptionItem,
        id=item_id,
        subscription__owner=request.user
    )


    state = item_state(item)

    if state in ("grace", "expired"):
        return JsonResponse(
            {
                "error":
                "このプランはすでにキャンセルされています"
            },
            status=400
        )


    if item.deleted_at is not None:
        return JsonResponse(
            {
                "error":
                "このプランはすでに解約されています"
            },
            status=400
        )


    subscription = item.subscription


    if subscription.billing_method == "stripe":
        return JsonResponse(
            {"error": "Non Stripe subscription can't use Stripe operation"},
            status=400
        )


    club = subscription.club
    today = timezone.localtime().date()


    error = validate_plan_change_window(
        today=today,
        subscription=subscription,
    )

    if error:
        return JsonResponse(
            {"error": error},
            status=400
        )


    try:

        with subscription_lock(
            subscription.id,
            timeout=300
        ):

            CashSubscriptionItemService.cancel_item(
                item=item,
                subscription=subscription,
                club=club,
            )


    except CacheLockError:

        return JsonResponse(
            {
                "error":
                "前回のリクエストがまだ処理中です。数分後に再度お試しください。"
            },
            status=429
        )


    except Exception as e:

        logger.error(
            f"Cash delete failed for item {item.id}: {e}"
        )

        return JsonResponse(
            {"error": str(e)},
            status=500
        )


    return JsonResponse(
        {
            "success": True,
            "message": get_cancel_success_message(subscription),
        }
    )



@login_required
@require_POST
def resume_cash_member_subscription(request, item_id):

    item = get_object_or_404(
        SubscriptionItem,
        id=item_id,
        subscription__owner=request.user
    )


    state = item_state(item)


    if state == "expired":
        return JsonResponse(
            {
                "error":
                "このプランは変更できません（有効期限切れ）"
            },
            status=400
        )


    now = timezone.now()


    if not can_resume_subscription(item, now):

        return JsonResponse(
            {
                "error":
                get_resume_error_message(item)
            },
            status=400
        )


    if item.deleted_at is None:

        return JsonResponse(
            {
                "error":
                "このプランは既に有効です"
            },
            status=400
        )


    subscription = item.subscription


    if subscription.billing_method == "stripe":

        return JsonResponse(
            {"error": "Non Stripe subscription can't use Stripe operation"},
            status=400
        )


    club = subscription.club
    today = timezone.localtime().date()


    error = validate_plan_change_window(
        today=today,
        subscription=subscription,
    )


    if error:
        return JsonResponse(
            {"error": error},
            status=400
        )


    try:

        with subscription_lock(
            subscription.id,
            timeout=300
        ):

            resumed_item = (
                CashSubscriptionItemService.resume_item(
                    item=item,
                    subscription=subscription,
                    club=club,
                )
            )


    except CacheLockError:

        return JsonResponse(
            {
                "error":
                "前回のリクエストがまだ処理中です。数分後に再度お試しください。"
            },
            status=429
        )


    except Exception as e:

        logger.error(
            f"Failed to resume cash item {item.id}: {e}"
        )

        return JsonResponse(
            {"error": str(e)},
            status=500
        )


    return JsonResponse(
        {
            "success": True,
            "message": get_resume_success_message(),
            "new_item_id": resumed_item.id,
        }
    )



@login_required
@require_POST
def cancel_cash_member_plan_change(request, new_item_id):

    new_item = get_object_or_404(
        SubscriptionItem,
        id=new_item_id,
        subscription__owner=request.user
    )


    if not new_item.source_item:

        return JsonResponse(
            {
                "error":
                "このプランは変更対象ではありません"
            },
            status=400
        )


    old_item = new_item.source_item


    subscription = old_item.subscription


    if subscription.billing_method == "stripe":

        return JsonResponse(
            {"error": "Non Stripe subscription can't use Stripe operation"},
            status=400
        )


    club = subscription.club


    old_plan_deleted = (
        old_item.plan
        and old_item.plan.deleted_at is not None
    )


    try:

        with subscription_lock(
            subscription.id,
            timeout=300
        ):

            CashSubscriptionItemService.cancel_change(
                new_item=new_item,
                old_item=old_item,
                subscription=subscription,
                club=club,
                old_plan_deleted=old_plan_deleted,
            )


    except CacheLockError:

        return JsonResponse(
            {
                "error":
                "前回のリクエストがまだ処理中です。数分後に再度お試しください。"
            },
            status=429
        )


    except Exception as e:

        logger.error(
            f"Cancel cash plan change failed {new_item.id}: {e}"
        )

        return JsonResponse(
            {"error": str(e)},
            status=500
        )


    return JsonResponse(
        {
            "success": True,
            "message": "プラン変更を取り消しました"
        }
    )



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

    plan = get_object_or_404(MembershipPlan, id=plan_id, club=club, is_deleted=False, active=True)
    if not plan.stripe_price_id:
        return JsonResponse({"error": "Plan not configured correctly"}, status=400)

    
    

    existing = SubscriptionItem.objects.filter(
        member=member,
        plan=plan,
        subscription__club=member.club,
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
    ).order_by("-id").first()

    ACTIVE_STATUSES = ["active", "trialing", "past_due", "incomplete", "pending"]
    CANCELED_STATUSES = ["canceled"]

    is_active_sub = sub and sub.status in ACTIVE_STATUSES
    is_canceled_sub = sub and sub.status in CANCELED_STATUSES

    if is_active_sub:
        return JsonResponse({
            "error": "もうすでに登録されています"
        }, status=400)

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

    existing_stripe_subs = stripe.Subscription.list(
        customer=stripe_customer_obj.stripe_customer_id,
        status="all",
        stripe_account=club.stripe_account_id,
        limit=100,
    )

    for stripe_sub in existing_stripe_subs.auto_paging_iter():

        if stripe_sub.status in [
            "active",
            "trialing",
            "past_due",
            "incomplete",
        ]:
            if stripe_sub.metadata.get("club_id") == str(club.id):
                return JsonResponse(
                    {"error": "現在、登録処理中の状態です。しばらくお待ちください。登録が正常に完了する場合がありますが、問題が発生した場合は最大2時間程度で自動的に解除され、その後再度お申し込みいただけます。"},
                    status=400
                )
    
    result = MemberSubscriptionCheckoutService.create_checkout_session(
        club=club,
        member=member,
        plan=plan,
        billing_user=billing_user,
    )

    return JsonResponse(result)


@login_required
@require_POST
def create_member_cash_subscription(
    request,
    club_id,
    plan_id,
):

    club = get_object_or_404(
        Club,
        id=club_id,
        is_deleted=False,
    )


    if club.subscription_mode not in [
        "regular",
        "monthly",
    ]:
        return JsonResponse(
            {"error": "Invalid billing configuration"},
            status=400
        )


    if not club.stripe_anchor_date:
        return JsonResponse(
            {"error": "Billing anchor not configured"},
            status=400
        )


    member_id = request.POST.get("member_id")

    member = get_object_or_404(
        Member,
        id=member_id,
        club=club,
    )


    if member.owner != request.user:
        return JsonResponse(
            {"error": "Not allowed"},
            status=403
        )


    plan = get_object_or_404(
        MembershipPlan,
        id=plan_id,
        club=club,
        is_deleted=False,
        active=True,
    )


    existing = SubscriptionItem.objects.filter(
        member=member,
        plan=plan,
        subscription__club=club,
    ).filter(
        active_items_q()
    ).exists()


    if existing:
        return JsonResponse(
            {
                "error":
                "Already has this plan (active or grace period)"
            },
            status=400
        )



    sub = Subscription.objects.filter(
        owner=member.owner,
        club=club,
    ).order_by("-id").first()



    ACTIVE_STATUSES = [
        "active",
        "trialing",
        "past_due",
        "pending",
    ]


    if sub and sub.status in ACTIVE_STATUSES:
        return JsonResponse(
            {
                "error":
                "もうすでに登録されています"
            },
            status=400
        )



    validate_subscription_transition(
        subscription=sub,
        member=member,
        new_plan=plan,
        old_plan_id=None,
    )


    ensure_group_exclusive(
        sub,
        member,
        plan,
    )


    today = timezone.localtime().date()


    if sub:

        if (
            is_near_anchor(
                today,
                sub.billing_anchor_day
            )
            or not is_valid_billing_day(today)
        ):
            return JsonResponse(
                {
                    "error":
                    "毎月2日〜27日のみ変更可能です"
                },
                status=400
            )


    else:

        if (
            is_near_anchor(
                today,
                club.stripe_anchor_date
            )
            or not is_valid_billing_day(today)
        ):
            return JsonResponse(
                {
                    "error":
                    "毎月2日〜27日のみ変更可能です"
                },
                status=400
            )


    try:

        with subscription_lock(
            sub.id if sub else member.owner.id,
            timeout=300,
        ):

            result = (
                MemberCashSubscriptionService
                .create_cash_subscription(
                    club=club,
                    member=member,
                    plan=plan,
                )
            )


    except CacheLockError:

        return JsonResponse(
            {
                "error":
                "前回のリクエストがまだ処理中です"
            },
            status=429
        )


    except Exception as e:

        logger.exception(
            "Cash subscription creation failed"
        )

        return JsonResponse(
            {
                "error": str(e)
            },
            status=500
        )


    return JsonResponse(result)



@login_required
@require_POST
def add_plan_to_subscription_view(request, club_id, plan_id):

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

    plan = get_object_or_404(MembershipPlan, id=plan_id, club=club, is_deleted=False, active=True)

    if not plan.stripe_price_id:
        return JsonResponse({"error": "Plan not configured correctly"}, status=400)

    existing = SubscriptionItem.objects.filter(
        member=member,
        plan=plan,
        subscription__club=member.club,
    ).filter(
        active_items_q()
    ).exists()

    if existing:
        return JsonResponse(
            {"error": "Already has this plan (active or grace period)"},
            status=400
        )

    sub = Subscription.objects.filter(
        owner=member.owner,
        club=member.club,
        billing_method="stripe",
        status__in=["active", "trialing", "past_due", "incomplete", "pending"]
    ).first()
    
    if not sub:
        return JsonResponse(
            {"error": "Stripe subscription not found"},
            status=400
        )



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

    try:
        with subscription_lock(sub.id, timeout=300):

            result = SubscriptionAddPlanService.add_plan_to_existing_subscription(
                club=club,
                member=member,
                plan=plan,
                subscription=sub,
            )

    except CacheLockError:
        return JsonResponse(
            {
               "error": "前回のリクエストがまだ処理中です。数分後に再度お試しください。"
            },
            status=429
        )
    
    except MutationLockedError as e:
        return JsonResponse(
            {
                "error": str(e),
                "blocked_until": (
                    e.blocked_until.isoformat()
                    if e.blocked_until
                    else None
                )
            },
            status=409
        )
    
    except Exception as e:
        logger.error(
            f"Add plan failed for member {member.id}, plan {plan.id}: {e}"
        )
        return JsonResponse(
            {"error": str(e)},
            status=500
        )
        
    return JsonResponse(
        result,
        status=result.get("status", 200)
    )


@login_required
@require_POST
def add_plan_to_cash_subscription_view(
    request,
    club_id,
    plan_id,
):

    club = get_object_or_404(
        Club,
        id=club_id,
        is_deleted=False,
    )


    if club.subscription_mode not in [
        "regular",
        "monthly",
    ]:
        return JsonResponse(
            {"error": "Invalid billing configuration"},
            status=400
        )


    member_id = request.POST.get("member_id")


    member = get_object_or_404(
        Member,
        id=member_id,
        club=club,
    )


    if member.owner != request.user:

        return JsonResponse(
            {"error": "Not allowed"},
            status=403
        )



    plan = get_object_or_404(
        MembershipPlan,
        id=plan_id,
        club=club,
        is_deleted=False,
        active=True,
    )



    existing = SubscriptionItem.objects.filter(
        member=member,
        plan=plan,
        subscription__club=club,
    ).filter(
        active_items_q()
    ).exists()


    if existing:

        return JsonResponse(
            {
                "error":
                "Already has this plan"
            },
            status=400
        )



    subscription = Subscription.objects.filter(
        owner=member.owner,
        club=club,
        billing_method__in=[
            "cash",
            "bank_transfer",
            "manual",
        ],
        status__in=[
            "active",
            "pending",
        ],
    ).first()

    if not subscription:
        return JsonResponse(
            {"error": "Cash subscription not found"},
            status=400
        )

    







    validate_subscription_transition(
        subscription=subscription,
        member=member,
        new_plan=plan,
        old_plan_id=None,
    )


    ensure_group_exclusive(
        subscription,
        member,
        plan,
    )


    try:

        with subscription_lock(
            subscription.id,
            timeout=300,
        ):

            result = (
                CashAddPlanService
                .add_plan_to_existing_subscription(
                    club=club,
                    member=member,
                    plan=plan,
                    subscription=subscription,
                )
            )


    except CacheLockError:

        return JsonResponse(
            {
                "error":
                "前回のリクエストがまだ処理中です"
            },
            status=429
        )


    except Exception as e:

        logger.exception(
            "Cash add plan failed"
        )

        return JsonResponse(
            {
                "error": str(e)
            },
            status=500
        )


    return JsonResponse(result)


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

