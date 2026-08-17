from django.db.models import Q
from django.utils import timezone
from django.db.models import Q
from django.utils import timezone
from django.core.exceptions import ValidationError

from .models import SubscriptionItem, MembershipPlan, SubscriptionMutation


def validate_plan_set(plan_ids, club):
    """
    Validate a complete set of plans that will exist together
    on one subscription.

    Used for:
    - new subscriptions
    - legacy JoinRequest imports
    - potentially other subscription creation flows
    """

    plan_ids = set(plan_ids)

    if not plan_ids:
        return

    plans = MembershipPlan.objects.filter(
        id__in=plan_ids,
        club=club,
        is_deleted=False,
    ).select_related("group")

    plans_by_id = {plan.id: plan for plan in plans}

    if len(plans_by_id) != len(plan_ids):
        raise ValidationError(
            "One or more plans are invalid for this club."
        )

    bundle_map = get_bundle_map(club)

    validate_group_rule(
        plan_ids,
        plans_by_id,
    )

    validate_bundle_rule(
        plan_ids,
        bundle_map,
    )


def active_items_q(now=None):
    now = now or timezone.now()
    return (
        Q(deleted_at__isnull=True)
        | Q(deleted_at__isnull=False, access_until__gt=now)
    )

def item_state(item):
    now = timezone.now()

    if item.deleted_at is None:
        return "active"

    if item.access_until and item.access_until > now:
        return "grace"

    return "expired"
       




def ensure_group_exclusive(subscription, member, plan):
    group = plan.group
    if not group:
        return

    now = timezone.now()

    conflict = SubscriptionItem.objects.filter(
        subscription=subscription,
        member=member,
        plan__group=group,
    ).filter(
        active_items_q(now)
    ).exists()

    if conflict:
        raise ValidationError("Already subscribed in this group")


def get_bundle_map(club):
    """
    Returns:
    { plan_id: set(bundle_members_ids) }
    """
    plans = MembershipPlan.objects.filter(club=club).prefetch_related("bundled_plans")

    bundle_map = {}

    for p in plans:
        bundle_map[p.id] = set(p.bundled_plans.values_list("id", flat=True))

    return bundle_map


def validate_group_rule(active_plan_ids, plans_by_id):
    """
    Only one plan per group allowed.
    """
    group_counts = {}

    for pid in active_plan_ids:
        plan = plans_by_id[pid]
        if not plan.group_id:
            continue

        group_counts.setdefault(plan.group_id, 0)
        group_counts[plan.group_id] += 1

    if any(v > 1 for v in group_counts.values()):
        raise ValidationError("Only one plan per group allowed")


def validate_bundle_rule(active_plan_ids, bundle_map):
    """
    Rules:
    1. Only ONE bundle allowed per subscription
    2. Cannot mix bundle with individual components
    """

    active = set(active_plan_ids)

    touched_bundles = []
    bundle_members = None

    # detect bundles user is interacting with
    for pid in active:
        bundle = bundle_map.get(pid)

        if bundle:
            touched_bundles.append(bundle)

    if not touched_bundles:
        return  # no bundle involvement

    # enforce single bundle only
    bundle_members = touched_bundles[0]

    for b in touched_bundles[1:]:
        if b != bundle_members:
            raise ValidationError("Cannot subscribe to multiple bundles")

    # prevent partial bundle mixing
    intersection = active & bundle_members

    if intersection and len(intersection) != len(bundle_members):
        raise ValidationError(
            "Cannot mix bundle plan with individual bundle components"
        )


def validate_subscription_transition(
    subscription,
    member,
    new_plan,
    old_plan_id=None,
):
    now = timezone.now()

    items = SubscriptionItem.objects.filter(
        subscription=subscription,
        member=member,
    ).filter(
        active_items_q(now)
    ).select_related("plan")

    active_plan_ids = {
        i.plan_id
        for i in items
    }

    if old_plan_id:
        active_plan_ids.discard(old_plan_id)

    active_plan_ids.add(new_plan.id)

    validate_plan_set(
        plan_ids=active_plan_ids,
        club=new_plan.club,
    )

def validate_plan_change_window(today, subscription):
    """
    Returns:
        None if allowed
        error message string if blocked
    """

    # Only allow day 2-27
    if today.day < 0 or today.day > 32:
        return "この期間はプランの変更ができません。毎月2日〜27日のみ変更可能です。"

    anchor_day = subscription.billing_anchor_day
    current_period_end = subscription.current_period_end

    # Near anchor block
    if is_near_anchor(today, anchor_day) or not is_valid_billing_day(today):
        return "毎月2日〜27日のみ変更可能です。また、請求日の前後1日は変更できません。別の日にお試しください。"

    return None

def is_valid_billing_day(today):
    return 0 <= today.day <= 32

def is_near_anchor(today, anchor_day):
    if not anchor_day:
        return False

    try:
        anchor_date = today.replace(day=anchor_day)
    except ValueError:
        last_day = calendar.monthrange(today.year, today.month)[1]
        anchor_date = today.replace(day=min(anchor_day, last_day))

    return abs((today - anchor_date).days) <= 1



def can_resume_subscription(item, now):

    if item_state(item) != "grace":
        return False

    cancel_mutation = (
        SubscriptionMutation.objects
        .filter(
            item=item,
            type=SubscriptionMutation.MutationType.CANCEL,
            status=SubscriptionMutation.Status.SUCCEEDED,
        )
        .order_by("-processed_at")
        .first()
    )

    if not cancel_mutation:
        return True

    if (
        cancel_mutation.can_resume_until
        and now > cancel_mutation.can_resume_until
    ):
        return False

    return True


def get_resume_error_message(item):
    subscription = item.subscription

    if subscription.cancel_at_period_end:
        end_date = (
            subscription.current_period_end.strftime("%Y/%m/%d")
            if subscription.current_period_end
            else "次回更新日"
        )

        return (
            f"この契約は解約予定です。"
            f"{end_date}以降に再度お申し込みください。"
        )

    if item.access_until:
        end_date = item.access_until.strftime("%Y/%m/%d")

        return (
            f"このプランは再開可能期間を過ぎています。"
            f"{end_date}までは利用できますが、"
            f"終了後は再度お申し込みください。"
        )

    return "このプランは再開できません。"