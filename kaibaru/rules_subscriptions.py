from django.db.models import Q
from django.utils import timezone
from django.db.models import Q
from django.utils import timezone
from django.core.exceptions import ValidationError

from .models import SubscriptionItem, MembershipPlan

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


def validate_subscription_transition(subscription, member, new_plan, old_plan_id=None):
    now = timezone.now()

    items = SubscriptionItem.objects.filter(
        subscription=subscription,
        member=member,
    ).filter(
        active_items_q(now)
    ).select_related("plan")

    active_plan_ids = {i.plan_id for i in items}

    # simulate transition (old item removed, new added)
    if old_plan_id:
        active_plan_ids.discard(old_plan_id)

    active_plan_ids.add(new_plan.id)

    plans = MembershipPlan.objects.filter(
        id__in=active_plan_ids
    ).select_related("group")

    plans_by_id = {p.id: p for p in plans}

    bundle_map = get_bundle_map(new_plan.club)

    validate_group_rule(active_plan_ids, plans_by_id)
    validate_bundle_rule(active_plan_ids, bundle_map)



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

    # Monthly billing processing lock
    if (
        subscription.billing_mode == "monthly"
        and today.day > anchor_day
        and current_period_end
        and current_period_end.month == today.month
    ):
        return "請求処理中のため、この期間はプランの変更ができません。しばらくしてからお試しください。"

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
    return item_state(item) == "grace"

def assert_item_unlocked(item):
    if item.plan_change_locked:
        return JsonResponse(
            {"error": "このプランは請求処理中のためロックされています。しばらくしてからもう一度お試しください。"},
            status=409
        )
    return None