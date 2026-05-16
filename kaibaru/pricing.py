import calendar
from dataclasses import dataclass
from datetime import date


# =========================================================
# PRORATION ENGINE
# (moved OUT of billing.py because it's pricing logic)
# =========================================================



def calculate_monthly_proration(today: date, monthly_price: int):
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    remaining_days = days_in_month - today.day + 1

    prorated_amount = int(monthly_price * remaining_days / days_in_month)

    return {
        "days_in_month": days_in_month,
        "remaining_days": remaining_days,
        "prorated_amount": prorated_amount,
    }


def calculate_regular_proration(today: date, anchor_day: int, monthly_price: int):
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

    prev_anchor_date = date(
        prev_anchor_year,
        prev_anchor_month,
        min(anchor_day, last_day_prev_month),
    )

    next_anchor_month = prev_anchor_month + 1
    next_anchor_year = prev_anchor_year

    if next_anchor_month > 12:
        next_anchor_month = 1
        next_anchor_year += 1

    last_day_next_month = calendar.monthrange(next_anchor_year, next_anchor_month)[1]

    next_anchor_date = date(
        next_anchor_year,
        next_anchor_month,
        min(anchor_day, last_day_next_month),
    )

    remaining_days = (next_anchor_date - today).days
    billing_period_days = (next_anchor_date - prev_anchor_date).days

    prorated_amount = int(
        monthly_price * remaining_days / billing_period_days
    )

    return {
        "prev_anchor_date": prev_anchor_date,
        "next_anchor_date": next_anchor_date,
        "remaining_days": remaining_days,
        "billing_period_days": billing_period_days,
        "prorated_amount": prorated_amount,
    }


# =========================================================
# CORE PRICING ORCHESTRATOR
# (THIS is what checkout + webhook + API will call)
# =========================================================

from .discounts import calculate_discounted_amount


def apply_pricing(
    *,
    club,
    member,
    plan=None,
    base_amount: int,
    apply_to: str,
    proration_ratio: float | None = None,
):
    """
    Single entry point for ALL pricing.

    - applies proration-aware discounts
    - returns final amount
    """

    return calculate_discounted_amount(
        club=club,
        member=member,
        plan=plan,
        base_amount=base_amount,
        apply_to=apply_to,
        proration_ratio=proration_ratio,
    )


# =========================================================
# JOINING FEE
# =========================================================

def calculate_joining_fee(club, member):
    from .discounts import calculate_discounted_amount

    return calculate_discounted_amount(
        club=club,
        member=member,
        base_amount=club.joining_fee,
        apply_to="joining_fee",
    )


# =========================================================
# SUBSCRIPTION PRICING HELPERS (used by checkout + webhook)
# =========================================================

def calculate_subscription_pricing(
    *,
    club,
    member,
    plan,
    plan_price: int,
    today: date,
    mode: str,
    anchor_day: int,
):
    """
    Returns ALL pricing needed for checkout or webhook.
    """

    if mode == "regular":
        proration = calculate_regular_proration(today, anchor_day, plan_price)
        base = proration["prorated_amount"]
        ratio = proration["remaining_days"] / proration["billing_period_days"]

    else:
        proration = calculate_monthly_proration(today, plan_price)
        base = proration["prorated_amount"]
        ratio = proration["remaining_days"] / proration["days_in_month"]

    final_amount = apply_pricing(
        club=club,
        member=member,
        plan=plan,
        base_amount=base,
        apply_to="subscription",
        proration_ratio=ratio,
    )

    return {
        "base_amount": base,
        "final_amount": final_amount,
        "proration": proration,
        "ratio": ratio,
        "savings": max(0, base - final_amount),
    }