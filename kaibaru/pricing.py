import calendar
from dataclasses import dataclass
from datetime import date, timedelta

from django.utils import timezone


# =========================================================
# PRORATION ENGINE
# (moved OUT of billing.py because it's pricing logic)
# =========================================================
def get_effective_subscription_price(item):
    """
    Returns the billable base price for an existing subscription item.

    - If the plan forces current pricing, use current plan price.
    - Otherwise preserve grandfather pricing, but allow price reductions.
    """
    if item.plan.apply_current_price_to_existing:
        return item.plan.price

    return min(
        item.price_at_subscription or item.plan.price,
        item.plan.price,
    )


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

    if plan.plan_type == "ticket_plan":

        ticket_proration = calculate_ticket_proration(
            today=today,
            anchor_day=anchor_day,
            plan_price=plan_price,
            ticket_quantity=plan.ticket_quantity,
            mode=mode,
        )

        base = ticket_proration["base_amount"]

        # IMPORTANT:
        # Use the rounded ticket ratio for discounts.
        ratio = ticket_proration["ticket_ratio"]

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

            # Keep the same general structure so existing
            # callers don't break.
            "proration": ticket_proration["calendar_proration"],

            # This is now the financially relevant ratio.
            "ratio": ratio,

            "savings": max(
                0,
                base - final_amount,
            ),

            # Ticket-specific information.
            "ticket_quantity": ticket_proration[
                "ticket_quantity"
            ],
            "raw_ticket_quantity": ticket_proration[
                "raw_ticket_quantity"
            ],
            "calendar_ratio": ticket_proration[
                "calendar_ratio"
            ],
            "ticket_ratio": ticket_proration[
                "ticket_ratio"
            ],
        }

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


def calculate_ticket_proration(
    *,
    today: date,
    anchor_day: int,
    plan_price: int,
    ticket_quantity: int,
    mode: str,
):
    """
    Calculate first-period pricing for a ticket plan.

    The calendar proration determines how much of the monthly
    ticket entitlement the member should receive.

    Because tickets are discrete, the raw ticket quantity is
    rounded first. The rounded quantity then determines the
    financial proration ratio.

    Example:

        8 tickets
        calendar ratio = 0.33

        raw tickets = 8 * 0.33 = 2.64
        rounded tickets = 3

        ticket ratio = 3 / 8 = 0.375

    The ticket ratio is then used for both:
        - the amount charged
        - fixed discount proration
        - member fixed adjustments
    """

    if mode == "regular":
        calendar_proration = calculate_regular_proration(
            today,
            anchor_day,
            plan_price,
        )

        calendar_ratio = (
            calendar_proration["remaining_days"]
            / calendar_proration["billing_period_days"]
        )

    else:
        calendar_proration = calculate_monthly_proration(
            today,
            plan_price,
        )

        calendar_ratio = (
            calendar_proration["remaining_days"]
            / calendar_proration["days_in_month"]
        )

    # ---------------------------------------------------------
    # Convert calendar proration into discrete tickets.
    #
    # Use normal half-up rounding rather than Python's banker's
    # rounding.
    # ---------------------------------------------------------

    from decimal import Decimal, ROUND_HALF_UP

    raw_ticket_quantity = (
        Decimal(ticket_quantity)
        * Decimal(str(calendar_ratio))
    )

    prorated_ticket_quantity = int(
        raw_ticket_quantity.quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP,
        )
    )

    # A ticket plan should never produce zero tickets for an
    # actual subscription.
    prorated_ticket_quantity = max(
        1,
        min(
            ticket_quantity,
            prorated_ticket_quantity,
        ),
    )

    # ---------------------------------------------------------
    # IMPORTANT:
    #
    # Discounts use the rounded ticket ratio, NOT the original
    # calendar ratio.
    # ---------------------------------------------------------

    ticket_ratio = (
        prorated_ticket_quantity
        / ticket_quantity
    )

    base_amount = int(
        plan_price * ticket_ratio
    )

    return {
        "calendar_proration": calendar_proration,
        "calendar_ratio": calendar_ratio,
        "raw_ticket_quantity": float(
            raw_ticket_quantity
        ),
        "ticket_quantity": prorated_ticket_quantity,
        "ticket_ratio": ticket_ratio,
        "base_amount": base_amount,
    }


def calculate_ticket_expiration(*, plan, granted_at):
    """
    Return when a ticket grant from this plan expires.

    never            -> None
    end_of_month     -> last second of the grant's local calendar month
    days_after_grant -> granted_at + ticket_expiration_days
    """
    mode = plan.ticket_expiration_mode

    if mode == "never":
        return None

    if timezone.is_naive(granted_at):
        granted_at = timezone.make_aware(granted_at)

    local_granted = timezone.localtime(granted_at)

    if mode == "end_of_month":
        last_day = calendar.monthrange(
            local_granted.year,
            local_granted.month,
        )[1]

        return local_granted.replace(
            day=last_day,
            hour=23,
            minute=59,
            second=59,
            microsecond=0,
        )

    if mode == "days_after_grant":
        days = plan.ticket_expiration_days or 0
        if days <= 0:
            return None
        return granted_at + timedelta(days=days)

    return None