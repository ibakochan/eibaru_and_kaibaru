import logging
from datetime import date
from django.utils import timezone
from django.db.models import Q
from .models import MemberPricingAdjustment

from .models import Discount, SubscriptionItem, Member

logger = logging.getLogger(__name__)




def get_member_pricing_adjustment(member_id):
    now = timezone.now()

    return (
        MemberPricingAdjustment.objects
        .filter(
            member_id=member_id,
        )
        .filter(
            Q(valid_from__isnull=True) | Q(valid_from__lte=now),
            Q(valid_until__isnull=True) | Q(valid_until__gte=now),
        )
    )
# =========================
# Helpers
# =========================

def apply_member_adjustments(
    member,
    amount,
    plan=None,
    proration_ratio=None,
    adjustments=None,
    pricing_map=False,
    apply_to=None,
):
    # -------------------------
    # DATA SOURCE RESOLUTION
    # -------------------------

    if apply_to != "subscription":
        return max(0, int(amount))

    if adjustments is None:
        if pricing_map:
            adjustments = member.pricing_adjustments.all()
        else:
            adjustments = get_member_pricing_adjustment(member.id)

    # -------------------------
    # NORMALIZE INPUT SAFETY
    # -------------------------
    if not adjustments:
        return max(0, int(amount))

    if isinstance(adjustments, dict):
        adjustments = adjustments.get(member.id, [])

    try:
        adjustments = list(adjustments)
    except TypeError:
        return max(0, int(amount))

    # -------------------------
    # PLAN FILTER PRE-PASS (keep logic same)
    # -------------------------
    filtered = []

    for adj in adjustments:

        if not hasattr(adj, "plans"):
            continue

        if pricing_map:
            plan_ids = getattr(adj, "plan_ids", None)

            if plan_ids is None:
                plan_ids = {p.id for p in adj.plans.all()}

            if plan_ids and plan is not None and plan.id not in plan_ids:
                continue

        else:
            if adj.plans.exists():
                if plan is None or not adj.plans.filter(id=plan.id).exists():
                    continue

        filtered.append(adj)

    # -------------------------
    # 1. PERCENTAGE ADJUSTMENTS
    # -------------------------
    for adj in [a for a in filtered if a.discount_type == "percentage"]:
        amount = int(amount * (1 - adj.value / 100))

    # -------------------------
    # 2. FIXED ADJUSTMENTS
    # -------------------------
    for adj in [a for a in filtered if a.discount_type == "fixed"]:
        value = adj.value

        if proration_ratio is not None:
            value = round(value * proration_ratio)

        amount -= value

    return max(0, int(amount))

def calculate_age(birth_date):
    if not birth_date:
        return None

    today = date.today()
    return (
        today.year
        - birth_date.year
        - ((today.month, today.day) < (birth_date.month, birth_date.day))
    )


# =========================
# Condition Engine
# =========================

def check_conditions(discount, member):
    """
    ALL conditions must pass.
    """

    owner = member.owner

    club = member.club

    for cond in discount.conditions.all():
        ctype = cond.type
        value = cond.value

        # -------------------------
        # Gender
        # -------------------------
        if ctype == "gender":
            if member.gender != value:
                return False

        # -------------------------
        # Age conditions
        # -------------------------
        elif ctype == "age_lt":
            age = calculate_age(member.birth_date)
            if age is None or age >= int(value):
                return False

        elif ctype == "age_gt":
            age = calculate_age(member.birth_date)
            if age is None or age <= int(value):
                return False



        # -------------------------
        # Family condition
        # -------------------------
        elif ctype == "is_family":
            # Count OTHER family members (excluding self)

            if getattr(member, "is_preview", False):
                count = member.family_count

            else:
                count = Member.objects.filter(
                    owner=owner,
                    club=club,
                    counts_for_family_discount=True
                ).exclude(id=member.id).count()

            if count < int(value):
                return False

        else:
            logger.warning(f"[DISCOUNT] Unknown condition type: {ctype}")
            return False

    return True


# =========================
# Fetch Discounts
# =========================

def get_applicable_discounts(club, apply_to):
    now = timezone.now()

    return Discount.objects.filter(
        club=club,
        apply_to=apply_to,
        active=True,
    ).filter(
        Q(valid_from__isnull=True) | Q(valid_from__lte=now),
        Q(valid_until__isnull=True) | Q(valid_until__gte=now),
    ).prefetch_related("conditions").order_by("-priority")


# =========================
# Core Pricing Engine
# =========================

def calculate_discounted_amount(
    *,
    club,
    member,
    base_amount,
    plan=None,
    apply_to,
    proration_ratio=None,
):
    """
    Stack rules:
    1. percentage discounts first (sequential)
    2. fixed discounts second
    3. clamp at 0
    """

    logger.info(
        f"[DISCOUNT] Start: member={member.id}, club={club.id}, "
        f"base={base_amount}, apply_to={apply_to}"
    )

    discounts = get_applicable_discounts(club, apply_to)

    applicable = []
    for d in discounts:
        try:

            if (
                d.apply_to == "subscription"
                and not d.conditions.exists()
            ):
                continue

            if not check_conditions(d, member):
                continue
    
            # =========================
            # NEW: PLAN FILTER
            # =========================
            # if discount has plans set, enforce restriction
            if d.plans.exists():
                if plan is None:
                    continue
    
                if not d.plans.filter(id=plan.id).exists():
                    continue

            applicable.append(d)
        except Exception as e:
            logger.exception(f"[DISCOUNT] Condition error discount={d.id}: {e}")

    if not applicable:
        return max(0, int(base_amount))

    amount = base_amount

    # -------------------------
    # 1. Percentage discounts
    # -------------------------
    for d in [x for x in applicable if x.discount_type == "percentage"]:
        before = amount
        amount = int(amount * (1 - d.value / 100))

        logger.debug(
            f"[DISCOUNT] % {d.value}%: {before} → {amount} (id={d.id})"
        )

    # -------------------------
    # 2. Fixed discounts
    # -------------------------
    for d in [x for x in applicable if x.discount_type == "fixed"]:
        before = amount

        if proration_ratio is not None:
            scaled_value = round(d.value * proration_ratio)
        else:
            scaled_value = d.value

        amount -= scaled_value


        logger.debug(
            f"[DISCOUNT] -{scaled_value} (orig {d.value}): {before} → {amount} (id={d.id})"
        )
    
    final_amount = max(0, int(amount))
    final_amount = apply_member_adjustments(member, final_amount, plan, proration_ratio, apply_to=apply_to,)

    logger.info(
        f"[DISCOUNT] Final: {final_amount} from base={base_amount}"
    )

    return final_amount


# =========================
# Joining Fee Rule Helper
# =========================

def apply_joining_fee_discount(club, member):
    """
    Joining fee ONLY applies discounts of type 'joining_fee'.
    """
    return calculate_discounted_amount(
        club=club,
        member=member,
        base_amount=club.joining_fee,
        apply_to="joining_fee",
    )
    


# =========================
# Subscription Pricing Helper
# =========================

def apply_subscription_discount(club, member, amount):
    return calculate_discounted_amount(
        club=club,
        member=member,
        base_amount=amount,
        apply_to="subscription",
    )


# =========================
# Debug Breakdown (UI/Admin)
# =========================

def calculate_discount_breakdown(*, club, member, base_amount, apply_to, proration_ratio=None):
    discounts = get_applicable_discounts(club, apply_to)

    applicable = [
        d
        for d in discounts
        if not (
            d.apply_to == "subscription"
            and not d.conditions.exists()
        )
        and check_conditions(d, member)
    ]

    amount = base_amount
    steps = []

    for d in [x for x in applicable if x.discount_type == "percentage"]:
        before = amount
        amount = int(amount * (1 - d.value / 100))

        steps.append({
            "type": "percentage",
            "value": d.value,
            "before": before,
            "after": amount,
            "discount_id": d.id,
        })

    for d in [x for x in applicable if x.discount_type == "fixed"]:
        before = amount

        if proration_ratio is not None:
            scaled_value = round(d.value * proration_ratio)
        else:
            scaled_value = d.value

        amount -= scaled_value

        steps.append({
            "type": "fixed",
            "value": d.value,
            "before": before,
            "after": amount,
            "discount_id": d.id,
            "applied_value": scaled_value,
        })

    return {
        "base_amount": base_amount,
        "final_amount": max(0, int(amount)),
        "steps": steps,
        "applied": [d.id for d in applicable],
    }


def build_discount_context(members):
    from .discounts import calculate_age

    # -------------------------
    # Ages
    # -------------------------
    member_ages = {}

    for m in members:
        if m.birth_date:
            member_ages[m.id] = calculate_age(m.birth_date)

    # -------------------------
    # Family totals
    # -------------------------
    total_by_owner = {}

    for m in members:
        if m.owner_id is None:
            continue

        if m.owner_id not in total_by_owner:
            total_by_owner[m.owner_id] = 0

        if m.counts_for_family_discount:
            total_by_owner[m.owner_id] += 1

    # -------------------------
    # Family counts per member
    # -------------------------
    family_counts = {}

    for m in members:
        if m.owner_id is None:
            family_counts[m.id] = 0
        else:
            total = total_by_owner.get(m.owner_id, 0)

            exclude_self = (
                1 if m.counts_for_family_discount else 0
            )

            family_counts[m.id] = max(
                0,
                total - exclude_self,
            )

    return {
        "member_ages": member_ages,
        "family_counts": family_counts,
    }



def get_member_discounts(
    member,
    discounts_list,
    *,
    member_ages,
    family_counts,
):
    applicable = []

    for d in discounts_list:
        passes = True

        if (
            d.apply_to == "subscription"
            and not d.conditions.exists()
        ):
            continue

        for cond in d.conditions.all():
            ctype = cond.type
            value = cond.value

            # -------------------------
            # Gender
            # -------------------------
            if ctype == "gender":
                if member.gender != value:
                    passes = False
                    break

            # -------------------------
            # Age less than
            # -------------------------
            elif ctype == "age_lt":
                age = member_ages.get(member.id)

                if age is None or age >= int(value):
                    passes = False
                    break

            # -------------------------
            # Age greater than
            # -------------------------
            elif ctype == "age_gt":
                age = member_ages.get(member.id)

                if age is None or age <= int(value):
                    passes = False
                    break

            # -------------------------
            # Family
            # -------------------------
            elif ctype == "is_family":
                count = family_counts.get(member.id, 0)

                if count < int(value):
                    passes = False
                    break

        if passes:
            applicable.append(d)

    return applicable



def build_discount_plan_ids_map(discounts):
    result = {}

    for d in discounts:
        result[d.id] = {
            p.id for p in d.plans.all()
        }

    return result



def build_member_discount_map(
    members,
    discounts,
    *,
    member_ages,
    family_counts,
):
    result = {}

    for member in members:
        result[member.id] = get_member_discounts(
            member,
            discounts,
            member_ages=member_ages,
            family_counts=family_counts,
        )

    return result



def apply_discounts(
    *,
    member,
    member_adjustments,
    member_id,
    base_amount,
    discount_type,
    plan=None,
    proration_ratio=None,
    member_subscription_discounts,
    member_joining_discounts,
    subscription_discount_plan_ids,
    joining_discount_plan_ids,
):
    # -------------------------
    # Select source
    # -------------------------
    if discount_type == "subscription":
        applicable = (
            member_subscription_discounts.get(member_id, [])
        )

        plan_ids_map = subscription_discount_plan_ids

    else:
        applicable = (
            member_joining_discounts.get(member_id, [])
        )

        plan_ids_map = joining_discount_plan_ids

    # -------------------------
    # Filter by plan restriction
    # -------------------------
    filtered = []

    for d in applicable:
        plan_ids = plan_ids_map[d.id]

        if (
            plan_ids
            and (
                plan is None
                or plan.id not in plan_ids
            )
        ):
            continue

        filtered.append(d)

    # -------------------------
    # Apply discounts
    # -------------------------
    amount = base_amount

    # Percentage first
    for d in [
        x for x in filtered
        if x.discount_type == "percentage"
    ]:
        amount = int(
            amount * (1 - d.value / 100)
        )

    # Fixed second
    for d in [
        x for x in filtered
        if x.discount_type == "fixed"
    ]:
        if proration_ratio is not None:
            scaled = round(
                d.value * proration_ratio
            )
        else:
            scaled = d.value

        amount -= scaled
    
    pricing_map = True

    final = max(0, int(amount))
    final = apply_member_adjustments(member, final, plan, proration_ratio, member_adjustments, pricing_map, apply_to=discount_type,)

    return {
        "base": base_amount,
        "final": final,
        "savings": max(
            0,
            base_amount - final,
        ),
    }