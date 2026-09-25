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
from django.core.exceptions import ValidationError
from .user_errors import (
    json_validation_error_response,
    json_validation_errors,
)
from django.core.cache import cache

from django.db import transaction
logger = logging.getLogger(__name__)
from .service_subscription import SubscriptionItemService
from .service_cash_to_stripe import CashToStripeSubscriptionService
from .service_subscription_cash import CashSubscriptionItemService

from .service_mutations import MutationLockedError

from .service_member_checkout import MemberSubscriptionCheckoutService

from .service_cash_subscription import MemberCashSubscriptionService

from .service_add_plan_cash import CashAddPlanService
from .service_reservation import MemberReservationService

from .models import TicketType, TicketPackage, Club, Member, MembershipPlan, SubscriptionItem, Subscription, StripeCustomer, Lesson

stripe.api_key = settings.STRIPE_SECRET_KEY

OWNER_CLUB_SIGNUP_ERROR = (
    "オーナーご自身が、ご自身のクラブに申し込む必要はありません。"
)


def reject_owner_club_signup(request, club):
    user = getattr(request, "user", None)
    if (
        user is not None
        and getattr(user, "is_authenticated", False)
        and user.id == club.owner_id
    ):
        return JsonResponse(
            {"error": OWNER_CLUB_SIGNUP_ERROR},
            status=400,
        )
    return None

from .stripe_service import get_or_create_stripe_customer
from .service_add_plan import SubscriptionAddPlanService

from .service_ticket_purchase import TicketPurchaseService

from .locks_and_reconciliation import subscription_lock, CacheLockError, StripeSubscriptionReconciler, CheckoutSubscriptionReconciler

import urllib.parse

from .service_visitor_reservation import VisitorReservationService
from .service_trial_reservation import TrialReservationService
from .service_event import EventReservationService
from .models import Event

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
                "error": "この契約は現在同期処理中です。しばらくしてから再度お試しください。"
            },
            status=429
        )

    except ValidationError as e:
        return json_validation_error_response(e)
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

    except ValidationError as e:
        return json_validation_error_response(e)
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
            return JsonResponse({"error": "すでに契約済みです。"}, status=400)
       

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
        return JsonResponse({"error": "契約がありません。"}, status=400)

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
@json_validation_errors
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
            {"error": "クレジットカード契約の操作は、クレジットカード用の手続きから行ってください。"},
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
        scheduled_for_deletion=False,
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

    except ValidationError as e:
        return json_validation_error_response(e)
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
@json_validation_errors
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
            {"error": "クレジットカード契約の操作は、クレジットカード用の手続きから行ってください。"},
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
    
    except ValidationError as e:
        return json_validation_error_response(e)
    except Exception as e:
        logger.error(f"Stripe delete failed for item {item.id}: {e}")
        return JsonResponse({"error": str(e)}, status=500)

    return JsonResponse({
        "success": True,
        "message": get_cancel_success_message(subscription)
    })





@login_required
@require_POST
@json_validation_errors
def resume_member_subscription(request, item_id):
    item = get_object_or_404(
        SubscriptionItem,
        id=item_id,
        subscription__owner=request.user
    )

    if (
        not item.plan
        or item.plan.is_deleted
        or item.plan.scheduled_for_deletion
    ):
        return JsonResponse(
            {
                "error": (
                    "このプランは削除されているため、"
                    "再開できません。"
                )
            },
            status=400,
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
            {"error": "クレジットカード契約の操作は、クレジットカード用の手続きから行ってください。"},
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

    except ValidationError as e:
        return json_validation_error_response(e)
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
@json_validation_errors
def cancel_member_plan_change(request, new_item_id):
    new_item = get_object_or_404(
        SubscriptionItem,
        id=new_item_id,
        subscription__owner=request.user
    )


    if not new_item.source_item:
        return JsonResponse({"error": "このプランは変更対象ではありません"}, status=400)



    old_item = new_item.source_item

    if old_item.plan and (
        old_item.plan.is_deleted
        or old_item.plan.scheduled_for_deletion
    ):
        return JsonResponse(
            {
                "error": (
                    "このプランはすでに削除されているため、"
                    "プラン変更を取り消すことはできません。"
                )
            },
            status=400,
        )


   
    subscription = old_item.subscription

    if subscription.billing_method != "stripe":
        return JsonResponse(
            {"error": "クレジットカード契約の操作は、クレジットカード用の手続きから行ってください。"},
            status=400
        )

    club = subscription.club


    try:
        with subscription_lock(subscription.id, timeout=300):
            SubscriptionItemService.cancel_change(
                new_item=new_item,
                old_item=old_item,
                subscription=subscription,
                club=club,
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

    except ValidationError as e:
        return json_validation_error_response(e)
    except Exception as e:
        logger.error(f"Cancel plan change failed {new_item.id}: {e}")
        return JsonResponse({"error": str(e)}, status=500)



    return JsonResponse({
        "success": True,
        "message": "プラン変更を取り消しました"
    })



@login_required
@require_POST
@json_validation_errors
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
            {"error": "現金払いの契約では、クレジットカード用の手続きは使えません。"},
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
        scheduled_for_deletion=False,
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


    except ValidationError as e:
        return json_validation_error_response(e)
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
@json_validation_errors
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
            {"error": "現金払いの契約では、クレジットカード用の手続きは使えません。"},
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


    except ValidationError as e:
        return json_validation_error_response(e)
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
@json_validation_errors
def resume_cash_member_subscription(request, item_id):

    item = get_object_or_404(
        SubscriptionItem,
        id=item_id,
        subscription__owner=request.user
    )

    if (
        not item.plan
        or item.plan.is_deleted
        or item.plan.scheduled_for_deletion
    ):
        return JsonResponse(
            {
                "error": (
                    "このプランは削除されているため、"
                    "再開できません。"
                )
            },
            status=400,
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
            {"error": "現金払いの契約では、クレジットカード用の手続きは使えません。"},
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


    except ValidationError as e:
        return json_validation_error_response(e)
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
@json_validation_errors
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

    if old_item.plan and (
        old_item.plan.is_deleted
        or old_item.plan.scheduled_for_deletion
    ):
        return JsonResponse(
            {
                "error": (
                    "このプランはすでに削除されているため、"
                    "プラン変更を取り消すことはできません。"
                )
            },
            status=400,
        )

    subscription = old_item.subscription


    if subscription.billing_method == "stripe":

        return JsonResponse(
            {"error": "現金払いの契約では、クレジットカード用の手続きは使えません。"},
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


    except ValidationError as e:
        return json_validation_error_response(e)
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
        return JsonResponse({"error": "この操作を行う権限がありません。"}, status=403)

    

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
@json_validation_errors
def create_member_checkout_session(request, club_id, plan_id):
    

    club = get_object_or_404(Club, id=club_id, is_deleted=False)
    owner_block = reject_owner_club_signup(request, club)
    if owner_block:
        return owner_block
    if club.subscription_mode not in ["regular", "monthly"]:
        return JsonResponse({"error": "課金設定が正しくありません。月謝または定期課金を設定してください。"}, status=400)


    if not club.stripe_anchor_date:
        return JsonResponse({"error": "請求日が設定されていません。クラブの課金設定を確認してください。"}, status=400)

    if not club.stripe_account_id:
        return JsonResponse({"error": "このクラブはまだクレジットカード決済（Stripe）に接続されていません。"}, status=400)

    member_id = request.POST.get("member_id")
    member = get_object_or_404(Member, id=member_id, club=club)

    today = timezone.localtime().date()

    
    
    if member.owner != request.user:
        return JsonResponse({"error": "この操作を行う権限がありません。"}, status=403)

    billing_user = member.owner

    stripe_customer_obj = get_or_create_stripe_customer(billing_user, club)

    if not billing_user:
        return JsonResponse(
            {"error": "この会員の支払い担当者が設定されていません。"},
            status=400
        )

    plan = get_object_or_404(
        MembershipPlan,
        id=plan_id,
        club=club,
        is_deleted=False,
        scheduled_for_deletion=False,
        active=True,
    )
    if not plan.stripe_price_id:
        return JsonResponse({"error": "このプランの決済設定が完了していません。"}, status=400)

    
    

    existing = SubscriptionItem.objects.filter(
        member=member,
        plan=plan,
        subscription__club=member.club,
    ).filter(
        active_items_q()
    ).exists()
    
    if existing:
        return JsonResponse(
            {"error": "このプランはすでに契約中、または解約後の利用期間中です。"},
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
@json_validation_errors
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

    owner_block = reject_owner_club_signup(request, club)
    if owner_block:
        return owner_block

    if club.subscription_mode not in [
        "regular",
        "monthly",
    ]:
        return JsonResponse(
            {"error": "課金設定が正しくありません。月謝または定期課金を設定してください。"},
            status=400
        )


    if not club.stripe_anchor_date:
        return JsonResponse(
            {"error": "請求日が設定されていません。クラブの課金設定を確認してください。"},
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
            {"error": "この操作を行う権限がありません。"},
            status=403
        )


    plan = get_object_or_404(
        MembershipPlan,
        id=plan_id,
        club=club,
        is_deleted=False,
        scheduled_for_deletion=False,
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
                "このプランはすでに契約中、または解約後の利用期間中です。"
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


    except ValidationError as e:
        return json_validation_error_response(e)
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
@json_validation_errors
def add_plan_to_subscription_view(request, club_id, plan_id):

    club = get_object_or_404(Club, id=club_id, is_deleted=False)

    owner_block = reject_owner_club_signup(request, club)
    if owner_block:
        return owner_block

    if club.subscription_mode not in ["regular", "monthly"]:
        return JsonResponse({"error": "課金設定が正しくありません。月謝または定期課金を設定してください。"}, status=400)

    if not club.stripe_anchor_date:
        return JsonResponse({"error": "請求日が設定されていません。クラブの課金設定を確認してください。"}, status=400)

    if not club.stripe_account_id:
        return JsonResponse({"error": "このクラブはまだクレジットカード決済（Stripe）に接続されていません。"}, status=400)

    member_id = request.POST.get("member_id")
    member = get_object_or_404(Member, id=member_id, club=club)

    today = timezone.localtime().date()

    if member.owner != request.user:
        return JsonResponse({"error": "この操作を行う権限がありません。"}, status=403)

    billing_user = member.owner

    stripe_customer_obj = get_or_create_stripe_customer(billing_user, club)

    if not billing_user:
        return JsonResponse(
            {"error": "この会員の支払い担当者が設定されていません。"},
            status=400
        )

    plan = get_object_or_404(
        MembershipPlan,
        id=plan_id,
        club=club,
        is_deleted=False,
        scheduled_for_deletion=False,
        active=True,
    )

    if not plan.stripe_price_id:
        return JsonResponse({"error": "このプランの決済設定が完了していません。"}, status=400)

    existing = SubscriptionItem.objects.filter(
        member=member,
        plan=plan,
        subscription__club=member.club,
    ).filter(
        active_items_q()
    ).exists()

    if existing:
        return JsonResponse(
            {"error": "このプランはすでに契約中、または解約後の利用期間中です。"},
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
            {"error": "クレジットカード契約が見つかりません。"},
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
    
    except ValidationError as e:
        return json_validation_error_response(e)
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
@json_validation_errors
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

    owner_block = reject_owner_club_signup(request, club)
    if owner_block:
        return owner_block

    if club.subscription_mode not in [
        "regular",
        "monthly",
    ]:
        return JsonResponse(
            {"error": "課金設定が正しくありません。月謝または定期課金を設定してください。"},
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
            {"error": "この操作を行う権限がありません。"},
            status=403
        )



    plan = get_object_or_404(
        MembershipPlan,
        id=plan_id,
        club=club,
        is_deleted=False,
        scheduled_for_deletion=False,
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
                "このプランはすでに契約中です。"
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
            {"error": "現金払いの契約が見つかりません。"},
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


    except ValidationError as e:
        return json_validation_error_response(e)
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
@require_POST
@json_validation_errors
def migrate_cash_subscription_to_stripe(request, club_id):
  
    """
    Migrate an existing cash subscription to Stripe.

    This does NOT create a new local Subscription.

    It creates a Stripe Checkout session containing the plans
    already present on the existing cash subscription.
    """

    # ---------------------------------------------------------
    # CLUB
    # ---------------------------------------------------------

    club = get_object_or_404(
        Club,
        id=club_id,
        is_deleted=False,
    )

    owner_block = reject_owner_club_signup(request, club)
    if owner_block:
        return owner_block

    # ---------------------------------------------------------
    # BASIC STRIPE CONFIGURATION
    # ---------------------------------------------------------

    if club.subscription_mode not in [
        "regular",
        "monthly",
    ]:
        return JsonResponse(
            {"error": "課金設定が正しくありません。月謝または定期課金を設定してください。"},
            status=400,
        )

    if not club.stripe_account_id:
        return JsonResponse(
            {"error": "このクラブはまだクレジットカード決済（Stripe）に接続されていません。"},
            status=400,
        )

    # ---------------------------------------------------------
    # MEMBER
    # ---------------------------------------------------------

    member_id = request.POST.get("member_id")

    if not member_id:
        return JsonResponse(
            {"error": "会員を選択してください。"},
            status=400,
        )

    member = get_object_or_404(
        Member,
        id=member_id,
        club=club,
    )

    logger.info(
        "[cash_to_stripe] Ownership check: member.owner=%s "
        "(id=%s) request.user=%s (id=%s)",
        member.owner,
        member.owner.id if member.owner else None,
        request.user,
        request.user.id if request.user.is_authenticated else None,
    )

    if member.owner != request.user:
        return JsonResponse(
            {"error": "この操作を行う権限がありません。"},
            status=403,
        )

    billing_user = member.owner

    if not billing_user:
        return JsonResponse(
            {"error": "この会員の支払い担当者が設定されていません。"},
            status=400,
        )

    

    # ---------------------------------------------------------
    # EXISTING LOCAL SUBSCRIPTION
    # ---------------------------------------------------------

    subscription = (
        Subscription.objects
        .filter(
            owner=billing_user,
            club=club,
        )
        .order_by("-id")
        .first()
    )

    if not subscription:
        return JsonResponse(
            {"error": "契約が見つかりません。"},
            status=400,
        )

    # ---------------------------------------------------------
    # MUST CURRENTLY BE CASH
    # ---------------------------------------------------------

    if subscription.billing_method != "cash":
        return JsonResponse(
            {
                "error": (
                    "現金払いの契約のみ、クレジットカード決済へ変更できます。"
                )
            },
            status=400,
        )

    # ---------------------------------------------------------
    # MUST NOT ALREADY HAVE STRIPE SUBSCRIPTION
    # ---------------------------------------------------------

    if subscription.stripe_subscription_id:
        return JsonResponse(
            {
                "error": (
                    "この契約はすでにクレジットカード決済に接続されています。"
                )
            },
            status=400,
        )

    today = timezone.localtime().date()

    error = validate_plan_change_window(
        today=today,
        subscription=subscription,
    )

    if error:
        return JsonResponse(
            {"error": error},
            status=400,
        )

    # ---------------------------------------------------------
    # IMPORTANT:
    # DO NOT MIGRATE IF THERE IS AN OPEN LOCAL INVOICE
    # ---------------------------------------------------------

    open_invoice_exists = (
        subscription.invoices
        .filter(status="open")
        .exists()
    )

    if open_invoice_exists:
        return JsonResponse(
            {
                "error": (
                    "このサブスクリプションには未払いの請求書があります。"
                    "未払い請求書を処理してからStripeへ変更してください。"
                )
            },
            status=400,
        )

    # ---------------------------------------------------------
    # ACTIVE ITEMS
    # ---------------------------------------------------------

    active_items = (
        subscription.items
        .filter(deleted_at__isnull=True)
        .select_related("member", "plan")
    )

    if not active_items.exists():
        return JsonResponse(
            {
                "error": (
                    "有効なプランがない契約は、クレジットカード決済へ変更できません。"
                )
            },
            status=400,
        )

    # ---------------------------------------------------------
    # MAKE SURE THE REQUESTED MEMBER ACTUALLY BELONGS
    # TO THIS SUBSCRIPTION
    # ---------------------------------------------------------

    member_has_item = active_items.filter(
        member=member,
    ).exists()

    if not member_has_item:
        return JsonResponse(
            {
                "error": (
                    "この会員は現在の契約に有効なプランを持っていません。"
                )
            },
            status=400,
        )

    # ---------------------------------------------------------
    # PREVENT DUPLICATE / CURRENT STRIPE CHECKOUT
    # ---------------------------------------------------------

    stripe_customer_obj = get_or_create_stripe_customer(
        billing_user,
        club,
    )

    existing_stripe_subs = stripe.Subscription.list(
        customer=stripe_customer_obj.stripe_customer_id,
        status="all",
        stripe_account=club.stripe_account_id,
        limit=100,
    )

    for stripe_sub in existing_stripe_subs.auto_paging_iter():

        if stripe_sub.status not in [
            "active",
            "trialing",
            "past_due",
            "unpaid",
            "incomplete",
        ]:
            continue

        if stripe_sub.metadata.get("club_id") != str(club.id):
            continue

        if stripe_sub.metadata.get("type") == "cash_to_stripe":
            return JsonResponse(
                {
                    "error": (
                        "クレジットカードへの変更はすでに処理中です。"
                        "完了するまでお待ちください。"
                    )
                },
                status=400,
            )

        return JsonResponse(
            {
                "error": (
                    "このクラブにはすでにクレジットカード契約があります。"
                )
            },
            status=400,
        )

    # ---------------------------------------------------------
    # LOCK
    # ---------------------------------------------------------

    try:

        with subscription_lock(
            subscription.id,
            timeout=300,
        ):

            # Re-check inside the lock.
            subscription.refresh_from_db()

            if subscription.billing_method != "cash":
                return JsonResponse(
                    {
                        "error": (
                            "この契約はすでに現金払いではありません。"
                        )
                    },
                    status=400,
                )

            if subscription.stripe_subscription_id:
                return JsonResponse(
                    {
                        "error": (
                            "この契約はすでにクレジットカード決済に接続されています。"
                        )
                    },
                    status=400,
                )

            # Re-check open invoices inside the lock.
            if (
                subscription.invoices
                .filter(status="open")
                .exists()
            ):
                return JsonResponse(
                    {
                        "error": (
                            "このサブスクリプションには"
                            "未払いの請求書があります。"
                        )
                    },
                    status=400,
                )

            result = (
                CashToStripeSubscriptionService
                .create_checkout_session(
                    club=club,
                    subscription=subscription,
                    billing_user=billing_user,
                )
            )

    except CacheLockError:

        return JsonResponse(
            {
                "error": (
                    "前回のリクエストがまだ処理中です。"
                    "数分後に再度お試しください。"
                )
            },
            status=429,
        )

    except ValueError as e:

        return JsonResponse(
            {"error": str(e)},
            status=400,
        )

    except ValidationError as e:
        return json_validation_error_response(e)
    except Exception as e:

        logger.exception(
            "Cash to Stripe migration failed "
            "subscription=%s",
            subscription.id,
        )

        return JsonResponse(
            {"error": str(e)},
            status=500,
        )

    return JsonResponse(result)

@login_required
@require_POST
@json_validation_errors
def change_stripe_payment_method(request, club_id):

    # ---------------------------------------------------------
    # CLUB
    # ---------------------------------------------------------

    club = get_object_or_404(
        Club,
        id=club_id,
        is_deleted=False,
    )



    # ---------------------------------------------------------
    # BASIC STRIPE CONFIGURATION
    # ---------------------------------------------------------

    if club.subscription_mode not in [
        "regular",
        "monthly",
    ]:
        return JsonResponse(
            {"error": "課金設定が正しくありません。月謝または定期課金を設定してください。"},
            status=400,
        )

    if not club.stripe_account_id:
        return JsonResponse(
            {"error": "このクラブはまだクレジットカード決済（Stripe）に接続されていません。"},
            status=400,
        )

    # ---------------------------------------------------------
    # MEMBER
    #
    # Same ownership model as cash → Stripe migration.
    # ---------------------------------------------------------

    member_id = request.POST.get("member_id")

    if not member_id:
        return JsonResponse(
            {"error": "会員を選択してください。"},
            status=400,
        )

    member = get_object_or_404(
        Member,
        id=member_id,
        club=club,
    )

    if member.owner != request.user:
        return JsonResponse(
            {"error": "この操作を行う権限がありません。"},
            status=403,
        )

    billing_user = member.owner

    if not billing_user:
        return JsonResponse(
            {"error": "この会員の支払い担当者が設定されていません。"},
            status=400,
        )

    # ---------------------------------------------------------
    # EXISTING LOCAL SUBSCRIPTION
    # ---------------------------------------------------------

    subscription = (
        Subscription.objects
        .filter(
            owner=billing_user,
            club=club,
        )
        .order_by("-id")
        .first()
    )

    if not subscription:
        return JsonResponse(
            {"error": "契約が見つかりません。"},
            status=400,
        )

    # ---------------------------------------------------------
    # MUST CURRENTLY BE STRIPE
    # ---------------------------------------------------------

    if subscription.billing_method != "stripe":
        return JsonResponse(
            {
                "error": (
                    "クレジットカード契約のみ、支払い方法を変更できます。"
                )
            },
            status=400,
        )

    # ---------------------------------------------------------
    # MUST HAVE STRIPE SUBSCRIPTION
    # ---------------------------------------------------------

    if not subscription.stripe_subscription_id:
        return JsonResponse(
            {
                "error": (
                    "この契約にはクレジットカード情報がありません。"
                )
            },
            status=400,
        )

    # ---------------------------------------------------------
    # DO NOT ALLOW PAYMENT METHOD CHANGE WHILE
    # SUBSCRIPTION IS SCHEDULED FOR CANCELLATION
    # ---------------------------------------------------------

    if subscription.cancel_at_period_end:
        return JsonResponse(
            {
                "error": (
                    "このサブスクリプションは解約予定のため、"
                    "支払い方法を変更できません。"
                )
            },
            status=400,
        )

    # ---------------------------------------------------------
    # BILLING WINDOW VALIDATION
    #
    # Same rule used by the other subscription mutations.
    # ---------------------------------------------------------

    today = timezone.localtime().date()

    error = validate_plan_change_window(
        today=today,
        subscription=subscription,
    )

    if error:
        return JsonResponse(
            {"error": error},
            status=400,
        )

    # ---------------------------------------------------------
    # STRIPE CUSTOMER
    #
    # Your existing helper handles:
    # - local StripeCustomer lookup
    # - deleted customer
    # - missing customer
    # - creation
    # ---------------------------------------------------------

    stripe_customer_obj = get_or_create_stripe_customer(
        billing_user,
        club,
    )

    # ---------------------------------------------------------
    # VERIFY STRIPE SUBSCRIPTION
    # ---------------------------------------------------------

    try:

        stripe_sub = stripe.Subscription.retrieve(
            subscription.stripe_subscription_id,
            stripe_account=club.stripe_account_id,
        )

    except stripe.error.StripeError as e:

        logger.exception(
            "[CHANGE PAYMENT METHOD] Failed to retrieve "
            "Stripe subscription=%s",
            subscription.stripe_subscription_id,
        )

        return JsonResponse(
            {"error": str(e)},
            status=400,
        )

    # ---------------------------------------------------------
    # VERIFY STRIPE SUBSCRIPTION BELONGS TO EXPECTED CUSTOMER
    # ---------------------------------------------------------

    if (
        stripe_sub.get("customer")
        != stripe_customer_obj.stripe_customer_id
    ):
        logger.error(
            "[CHANGE PAYMENT METHOD] Stripe customer mismatch "
            "subscription=%s stripe_subscription=%s "
            "local_customer=%s stripe_customer=%s",
            subscription.id,
            subscription.stripe_subscription_id,
            stripe_customer_obj.stripe_customer_id,
            stripe_sub.get("customer"),
        )

        return JsonResponse(
            {"error": "Stripeの顧客情報が一致しません。サポートにお問い合わせください。"},
            status=400,
        )

    # ---------------------------------------------------------
    # CREATE STRIPE BILLING PORTAL SESSION
    #
    # The customer enters the new card on Stripe.
    # Your application never receives card information.
    # ---------------------------------------------------------

    try:

        session = stripe.billing_portal.Session.create(
            customer=stripe_customer_obj.stripe_customer_id,
            return_url=(
                f"https://{club.subdomain}.kaibaru.jp/"
                "?payment_method=updated"
            ),
            stripe_account=club.stripe_account_id,
        )

    except stripe.error.StripeError as e:

        logger.exception(
            "[CHANGE PAYMENT METHOD] Failed to create "
            "Billing Portal session subscription=%s",
            subscription.id,
        )

        return JsonResponse(
            {"error": str(e)},
            status=400,
        )

    return JsonResponse(
        {
            "url": session.url,
        }
    )


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
        return JsonResponse({"error": "この操作を行う権限がありません。"}, status=403)

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
            return JsonResponse({"error": "Stripeアカウントが無効です。"}, status=400)

        if Club.objects.filter(stripe_account_id=stripe_account_id).exclude(id=club.id).exists():
            return JsonResponse(
                {"error": "このStripeアカウントは別のクラブにすでに接続されています。"},
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


@require_POST
@json_validation_errors
def create_visitor_reservation(
    request,
    lesson_id,
):

    lesson = get_object_or_404(
        Lesson,
        id=lesson_id,
    )

    

    club = lesson.club

    owner_block = reject_owner_club_signup(request, club)
    if owner_block:
        return owner_block

    user = (
        request.user
        if request.user.is_authenticated
        else None
    )

    if user:
            member_exists = Member.objects.filter(
                club=club,
                user=request.user,
            ).exists()
        
            if member_exists:
                return JsonResponse(
                    {
                        "error": "会員の方は会員向けの予約方法をご利用ください。"
                    },
                    status=400,
                )

    if club.is_deleted:
        return JsonResponse(
            {
                "error": "このクラブは利用できません。"
            },
            status=400,
        )

    full_name = request.POST.get(
        "full_name",
        "",
    ).strip()

    email = request.POST.get(
        "email",
        "",
    ).strip()

    phone_number = request.POST.get(
        "phone_number",
        "",
    ).strip()

    age_raw = request.POST.get("age", "").strip()
    gender = request.POST.get("gender", "").strip()

    reservation_date = request.POST.get(
        "reservation_date",
        "",
    ).strip()

    if not full_name:
        return JsonResponse(
            {
                "error": "お名前を入力してください。"
            },
            status=400,
        )

    if not user and not email:
        return JsonResponse(
            {
                "error": (
                    "メールアドレスを入力してください。"
                )
            },
            status=400,
        )

    if user and not user.email:
        return JsonResponse(
            {
                "error": (
                    "アカウントにメールアドレスが"
                    "登録されていません。"
                )
            },
            status=400,
        )

    if not reservation_date:
        return JsonResponse(
            {
                "error": "予約日を指定してください。"
            },
            status=400,
        )

    try:

        reservation_date = datetime.strptime(
            reservation_date,
            "%Y-%m-%d",
        ).date()

    except ValueError:

        return JsonResponse(
            {
                "error": "予約日の形式が正しくありません。"
            },
            status=400,
        )

    age = None
    if age_raw:
        try:
            age = int(age_raw)
        except ValueError:
            return JsonResponse(
                {
                    "error": "年齢の形式が正しくありません。"
                },
                status=400,
            )

        if age < 0 or age > 120:
            return JsonResponse(
                {
                    "error": "年齢の形式が正しくありません。"
                },
                status=400,
            )

    if gender and gender not in ("male", "female"):
        return JsonResponse(
            {
                "error": "性別の指定が正しくありません。"
            },
            status=400,
        )

    try:

        result = VisitorReservationService.create_reservation(
            club=club,
            lesson=lesson,
            reservation_date=reservation_date,
            full_name=full_name,
            email=email,
            phone_number=phone_number,
            user=user,
            age=age,
            gender=gender,
        )

    except ValueError as e:

        return JsonResponse(
            {
                "error": str(e)
            },
            status=400,
        )

    except Exception:

        logger.exception(
            "Visitor reservation creation failed"
        )

        return JsonResponse(
            {
                "error": "予約処理中にエラーが発生しました。"
            },
            status=500,
        )

    return JsonResponse(result)


@require_POST
@json_validation_errors
def create_trial_reservation(
    request,
    lesson_id,
):

    lesson = get_object_or_404(
        Lesson,
        id=lesson_id,
    )

    club = lesson.club

    owner_block = reject_owner_club_signup(request, club)
    if owner_block:
        return owner_block

    user = (
        request.user
        if request.user.is_authenticated
        else None
    )

    if user:
        member_exists = Member.objects.filter(
            club=club,
            user=request.user,
        ).exists()

        if member_exists:
            return JsonResponse(
                {
                    "error": "会員の方は体験予約をご利用できません。"
                },
                status=400,
            )

    if club.is_deleted:
        return JsonResponse(
            {
                "error": "このクラブは利用できません。"
            },
            status=400,
        )

    email = request.POST.get(
        "email",
        "",
    ).strip()

    phone_number = request.POST.get(
        "phone_number",
        "",
    ).strip()

    reservation_date = request.POST.get(
        "reservation_date",
        "",
    ).strip()

    if not user and not email:
        return JsonResponse(
            {
                "error": (
                    "メールアドレスを入力してください。"
                )
            },
            status=400,
        )

    if user and not user.email:
        return JsonResponse(
            {
                "error": (
                    "アカウントにメールアドレスが"
                    "登録されていません。"
                )
            },
            status=400,
        )

    if not reservation_date:
        return JsonResponse(
            {
                "error": "予約日を指定してください。"
            },
            status=400,
        )

    try:

        reservation_date = datetime.strptime(
            reservation_date,
            "%Y-%m-%d",
        ).date()

    except ValueError:

        return JsonResponse(
            {
                "error": "予約日の形式が正しくありません。"
            },
            status=400,
        )

    try:
        participants = (
            TrialReservationService.participants_from_post(
                request.POST
            )
        )
    except ValueError as exc:
        return JsonResponse(
            {"error": str(exc)},
            status=400,
        )

    try:

        result = TrialReservationService.create_reservations(
            club=club,
            lesson=lesson,
            reservation_date=reservation_date,
            email=email,
            phone_number=phone_number,
            user=user,
            participants=participants,
        )

    except ValueError as e:

        return JsonResponse(
            {
                "error": str(e)
            },
            status=400,
        )

    except Exception:

        logger.exception(
            "Trial reservation creation failed"
        )

        return JsonResponse(
            {
                "error": "予約処理中にエラーが発生しました。"
            },
            status=500,
        )

    return JsonResponse(result)


@require_POST
@json_validation_errors
def create_member_reservation(
    request,
    lesson_id,
):
    if not request.user.is_authenticated:
        return JsonResponse(
            {
                "error": (
                    "会員予約にはログインが必要です。"
                )
            },
            status=401,
        )

    lesson = get_object_or_404(
        Lesson,
        id=lesson_id,
    )

    club = lesson.club

    owner_block = reject_owner_club_signup(request, club)
    if owner_block:
        return owner_block

    member_id = request.POST.get(
        "member_id"
    )

    if not member_id:
        return JsonResponse(
            {
                "error": "会員を指定してください。"
            },
            status=400,
        )

    member = get_object_or_404(
        Member,
        id=member_id,
        club=club,
    )

    payment_method = request.POST.get(
        "payment_method",
        "stripe",
    ).strip()

    if payment_method not in ("stripe", "ticket"):
        return JsonResponse(
            {
                "error": "無効な支払い方法です。"
            },
            status=400,
        )

    ticket_grant_id = None

    if payment_method == "ticket":
        raw_ticket_grant_id = (
            request.POST.get("ticket_grant_id") or ""
        ).strip()

        if not raw_ticket_grant_id:
            return JsonResponse(
                {
                    "error": "使用するチケットを選択してください。"
                },
                status=400,
            )

        try:
            ticket_grant_id = int(raw_ticket_grant_id)
        except ValueError:
            return JsonResponse(
                {
                    "error": "使用するチケットを選択してください。"
                },
                status=400,
            )

    # The logged-in user must either be the
    # member or the owner managing that member.
    if (
        member.user_id != request.user.id
        and member.owner_id != request.user.id
    ):
        return JsonResponse(
            {
                "error": (
                    "この会員の予約を"
                    "作成する権限がありません。"
                )
            },
            status=403,
        )

    if club.is_deleted:
        return JsonResponse(
            {
                "error": (
                    "このクラブは利用できません。"
                )
            },
            status=400,
        )

    reservation_date = request.POST.get(
        "reservation_date",
        "",
    ).strip()

    if not reservation_date:
        return JsonResponse(
            {
                "error": "予約日を指定してください。"
            },
            status=400,
        )

    try:
        reservation_date = datetime.strptime(
            reservation_date,
            "%Y-%m-%d",
        ).date()

    except ValueError:
        return JsonResponse(
            {
                "error": (
                    "予約日の形式が正しくありません。"
                )
            },
            status=400,
        )

    try:
        result = (
            MemberReservationService
            .create_reservation(
                club=club,
                lesson=lesson,
                member=member,
                reservation_date=reservation_date,
                payment_method=payment_method,
                ticket_grant_id=ticket_grant_id,
            )
        )

    except ValueError as e:
        return JsonResponse(
            {
                "error": str(e)
            },
            status=400,
        )

    except Exception:
        logger.exception(
            "Member reservation creation failed"
        )

        return JsonResponse(
            {
                "error": (
                    "予約処理中にエラーが発生しました。"
                )
            },
            status=500,
        )

    return JsonResponse(result)


@require_POST
@json_validation_errors
def create_member_event_reservation(request, event_id):
    if not request.user.is_authenticated:
        return JsonResponse(
            {"error": "会員予約にはログインが必要です。"},
            status=401,
        )

    event = get_object_or_404(Event, id=event_id)
    club = event.club

    owner_block = reject_owner_club_signup(request, club)
    if owner_block:
        return owner_block

    member_id = request.POST.get("member_id")

    if not member_id:
        return JsonResponse(
            {"error": "会員を指定してください。"},
            status=400,
        )

    member = get_object_or_404(Member, id=member_id, club=club)

    if (
        member.owner_id != request.user.id
    ):
        return JsonResponse(
            {"error": "この会員の予約を作成する権限がありません。"},
            status=403,
        )

    try:
        result = EventReservationService.create_member_reservation(
            event=event,
            member=member,
        )
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception:
        logger.exception("Member event reservation creation failed")
        return JsonResponse(
            {"error": "予約処理中にエラーが発生しました。"},
            status=500,
        )

    return JsonResponse(result)


@require_POST
@json_validation_errors
def create_visitor_event_reservation(request, event_id):
    event = get_object_or_404(Event, id=event_id)

    owner_block = reject_owner_club_signup(request, event.club)
    if owner_block:
        return owner_block

    if event.club.is_deleted:
        return JsonResponse(
            {"error": "このクラブは利用できません。"},
            status=400,
        )

    user = request.user if request.user.is_authenticated else None

    try:
        result = EventReservationService.create_visitor_reservation(
            event=event,
            full_name=request.POST.get("full_name", ""),
            email=request.POST.get("email", ""),
            phone_number=request.POST.get("phone_number", ""),
            user=user,
        )
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception:
        logger.exception("Visitor event reservation creation failed")
        return JsonResponse(
            {"error": "予約処理中にエラーが発生しました。"},
            status=500,
        )

    return JsonResponse(result)


@require_POST
@json_validation_errors
def create_ticket_purchase(
    request,
    package_id,
):
    if not request.user.is_authenticated:
        return JsonResponse(
            {
                "error": "チケット購入にはログインが必要です。"
            },
            status=401,
        )

    package = get_object_or_404(
        TicketPackage,
        id=package_id,
    )

    club = package.club

    owner_block = reject_owner_club_signup(request, club)
    if owner_block:
        return owner_block

    if club.is_deleted:
        return JsonResponse(
            {
                "error": "このクラブは利用できません。"
            },
            status=400,
        )

    member_id = request.POST.get("member_id")

    if not member_id:
        return JsonResponse(
            {
                "error": "会員を指定してください。"
            },
            status=400,
        )

    member = get_object_or_404(
        Member,
        id=member_id,
        club=club,
    )

    # The logged-in user must either be the member
    # or the owner managing that member.
    if (
        member.user_id != request.user.id
        and member.owner_id != request.user.id
    ):
        return JsonResponse(
            {
                "error": (
                    "この会員のチケットを"
                    "購入する権限がありません。"
                )
            },
            status=403,
        )

    try:

        result = (
            TicketPurchaseService
            .create_purchase(
                club=club,
                package=package,
                member=member,
            )
        )

    except ValueError as e:

        return JsonResponse(
            {
                "error": str(e)
            },
            status=400,
        )

    except Exception:

        logger.exception(
            "Ticket purchase creation failed"
        )

        return JsonResponse(
            {
                "error": (
                    "チケット購入処理中に"
                    "エラーが発生しました。"
                )
            },
            status=500,
        )

    return JsonResponse(result)

    