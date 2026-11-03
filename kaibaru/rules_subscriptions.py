import calendar

from django.db.models import Q
from django.utils import timezone
from django.core.exceptions import ValidationError

from .models import SubscriptionItem, MembershipPlan, SubscriptionMutation
from .rules_eligibility import assert_member_eligible_for_plan


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
            "選択されたプランの一部がこのクラブでは利用できません。"
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
        raise ValidationError(
            "このグループではすでに別のプランを契約しています。"
            "グループ内では1つのプランのみ契約できます。"
        )


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
        raise ValidationError(
            "同じグループのプランは同時に契約できません。"
            "グループ内では1つのプランのみ選択してください。"
        )


def resulting_plan_ids(subscription, member, *, add_plan_id=None, drop_plan_id=None, now=None):
    """
    Plans this member would still hold after a resume or an undone
    plan change. Grace items count, because they are still on the
    subscription. Drop runs before add, so undoing a change removes
    the new plan and puts the old one back.
    """
    now = now or timezone.now()
    plan_ids = set(
        SubscriptionItem.objects.filter(
            subscription=subscription,
            member=member,
        ).filter(
            active_items_q(now)
        ).values_list("plan_id", flat=True)
    )
    if drop_plan_id:
        plan_ids.discard(drop_plan_id)
    if add_plan_id:
        plan_ids.add(add_plan_id)
    plan_ids.discard(None)
    return plan_ids


def assert_one_plan_per_group(plan_ids, club):
    """Refuse when the plans a member would hold include two from one group."""
    plan_ids = {pid for pid in plan_ids if pid}
    if not plan_ids:
        return

    plans = MembershipPlan.objects.filter(
        id__in=plan_ids,
        club=club,
    ).select_related("group")
    plans_by_id = {plan.id: plan for plan in plans}

    validate_group_rule(
        plan_ids & set(plans_by_id),
        plans_by_id,
    )


def assert_bundle_rule(plan_ids, club):
    """Refuse when the plans a member would hold break a bundle rule."""
    plan_ids = {pid for pid in plan_ids if pid}
    if not plan_ids:
        return
    validate_bundle_rule(plan_ids, get_bundle_map(club))


def assert_resulting_plan_rules(plan_ids, club):
    """Group and bundle rules for the plans a member would hold afterward."""
    assert_one_plan_per_group(plan_ids, club)
    assert_bundle_rule(plan_ids, club)


def assert_resume_group_exclusive(item):
    """
    Resuming puts this plan back beside whatever else the member still
    holds. Allowed when that set keeps one plan per group and does not
    break a bundle rule.
    """
    if item.plan_id is None:
        return
    assert_resulting_plan_rules(
        resulting_plan_ids(
            item.subscription,
            item.member,
            add_plan_id=item.plan_id,
        ),
        item.subscription.club,
    )


def assert_cancel_change_group_exclusive(old_item, new_item):
    """
    Undoing a plan change restores the old plan and drops the new one.
    Allowed when the restored set keeps one plan per group and does not
    break a bundle rule.
    """
    assert_resulting_plan_rules(
        resulting_plan_ids(
            old_item.subscription,
            old_item.member,
            add_plan_id=old_item.plan_id,
            drop_plan_id=new_item.plan_id,
        ),
        old_item.subscription.club,
    )


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
            raise ValidationError(
                "セットプランは1つだけ契約できます。"
                "複数のセットプランを同時に契約することはできません。"
            )

    # prevent partial bundle mixing
    intersection = active & bundle_members

    if intersection and len(intersection) != len(bundle_members):
        raise ValidationError(
            "セットプランとその内訳プランを同時に契約することはできません。"
            "セットプランか個別プランのどちらかを選んでください。"
        )


def assert_plan_is_activatable(plan, member=None):
    """
    A plan that is scheduled for deletion or already deleted must
    never gain a new/reactivated active SubscriptionItem.

    This is the single, shared gate used by every flow that starts a
    NEW activation of a plan (new subscription/checkout, add plan,
    change plan, resume). It intentionally does not consider whether
    OTHER unrelated SubscriptionItems already exist on the plan -
    existing historical/active rows are handled separately by the
    plan-deletion task/reconciliation and must not be invalidated by
    this check.

    When ``member`` is provided, age/gender restrictions on the plan
    are also enforced. Existing subscribers who later fall outside
    those restrictions are not automatically moved off the plan.
    """
    if plan is None:
        return

    if plan.scheduled_for_deletion or plan.is_deleted:
        raise ValidationError(
            "このプランは削除されているため、選択できません。"
        )

    if member is not None:
        assert_member_eligible_for_plan(member, plan)


def validate_subscription_transition(
    subscription,
    member,
    new_plan,
    old_plan_id=None,
):
    assert_plan_is_activatable(new_plan, member)

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

ANCHOR_WINDOW_BLOCKED = (
    "このクラブの請求日の前後1日は、サブスクリプションの変更ができません。"
)


def subscription_day_block(today, anchor_day):
    """Why a subscription change is blocked on this date, or None."""
    if is_near_anchor(today, anchor_day):
        return ANCHOR_WINDOW_BLOCKED
    if not is_valid_billing_day(today):
        return "毎月2日〜27日のみ変更可能です。"
    return None


def validate_plan_change_window(today, subscription):
    """
    Returns:
        None if allowed
        error message string if blocked
    """
    return subscription_day_block(today, subscription.billing_anchor_day)

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


def plan_price_edit_block(*, plan, new_price, new_apply, today):
    """Refuse any plan price change outside the billing-safe days.

    Allowed only on the 2nd–27th, and only when that day is more than one
    day away from the club billing day and from every active subscriber's
    billing day. A cut still changes current bills when the legacy price
    is kept, so the price number itself is what is locked.
    """
    if int(new_price) == int(plan.price) and bool(new_apply) == bool(
        plan.apply_current_price_to_existing
    ):
        return None

    anchors = set()
    club_anchor = getattr(getattr(plan, "club", None), "stripe_anchor_date", None)
    if club_anchor:
        anchors.add(club_anchor)
    anchors.update(
        SubscriptionItem.objects.filter(
            plan_id=plan.id,
            deleted_at__isnull=True,
            subscription__billing_anchor_day__isnull=False,
        ).values_list("subscription__billing_anchor_day", flat=True)
    )

    for anchor in anchors:
        if is_near_anchor(today, anchor):
            return "請求日の前後1日は、プランの料金を変更できません。"

    if today.day < 2 or today.day > 27:
        return "プランの料金は毎月2日〜27日のみ変更できます。"

    return None


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