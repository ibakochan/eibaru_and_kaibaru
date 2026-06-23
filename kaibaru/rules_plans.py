from .models import MembershipPlan
def enforce_membership_plan_invariants(club):
    """
    Ensures the club's membership plans are always in a valid state:
    - groups must have 0 or >=2 plans (never 1)
    - bundles must have 0 or >=2 plans (never 1)
    - orphaned group/default references are fixed
    """

    # -------------------------
    # 1. FIX GROUPS
    # -------------------------
    groups = club.membershipplangroup_set.all()

    for group in groups:
        plans = list(group.plans.all())

        # invalid group (1 or 0 plans)
        if len(plans) < 2:
            # detach plans
            for p in plans:
                p.group = None
                p.save(update_fields=["group"])

            # if this was default group reference, null it safely
            if group.default_plan and group.default_plan.group_id != group.id:
                group.default_plan = None

            group.delete()

    # -------------------------
    # 2. FIX BUNDLES
    # -------------------------
    plans = club.membership_plans.prefetch_related("bundled_plans").all()

    for plan in plans:
        bundle_ids = list(plan.bundled_plans.values_list("id", flat=True))

        # invalid bundle
        if 0 < len(bundle_ids) < 2:
            plan.bundled_plans.clear()

    # -------------------------
    # 3. FIX DEFAULT PLAN SAFETY
    # -------------------------
    # ensure group default always belongs to group
    for group in club.membershipplangroup_set.all():
        if group.default_plan and group.default_plan.group_id != group.id:
            group.default_plan = None
            group.save(update_fields=["default_plan"])

        if group.default_plan is None and group.plans.exists():
            # fallback: highest price
            group.default_plan = max(group.plans.all(), key=lambda p: p.price)
            group.save(update_fields=["default_plan"])

       

def would_break_any_bundle(plan):
    # all plans that include this plan in their bundle
    affected = MembershipPlan.objects.filter(
        bundled_plans=plan
    ).prefetch_related("bundled_plans")

    for p in affected:
        # Count only ACTIVE (non-soft-deleted) plans remaining after removing this plan
        remaining = (
            p.bundled_plans
            .exclude(id=plan.id)
            .exclude(is_deleted=True)
            .count()
        )

        # If removing this plan leaves 0 or 1 active plans, it's invalid
        # (0 = empty bundle, 1 = single-plan bundle — both violate invariant)
        if remaining < 2:
            return True

    return False