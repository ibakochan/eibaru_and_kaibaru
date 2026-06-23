import calendar
from datetime import datetime, timezone as dt_timezone

from django.db.models import Prefetch
from.models import SubscriptionItem

from datetime import date

def get_next_month_start(today: date):
    if today.month == 12:
        return date(today.year + 1, 1, 1)
    return date(today.year, today.month + 1, 1)




def resolve_and_apply_subscription_period(sub, period_end_ts, today):
    period_end = datetime.fromtimestamp(period_end_ts, tz=dt_timezone.utc)

    if abs((period_end.date() - today).days) <= 1:
        anchor_ts = get_next_billing_cycle_anchor(today, sub.billing_anchor_day)
        period_end = datetime.fromtimestamp(anchor_ts, tz=dt_timezone.utc)

    sub.current_period_end = period_end

    if sub.billing_mode == "regular":
        sub.access_until = period_end

    elif sub.billing_mode == "monthly":
        year = period_end.year
        month = period_end.month
        last_day = calendar.monthrange(year, month)[1]

        sub.access_until = datetime(
            year, month, last_day, 23, 59, 59, tzinfo=dt_timezone.utc
        )

    return {
        "access_start": today,
        "access_end": sub.access_until.date() if sub.access_until else period_end.date(),
        "current_period_end": period_end.date(),
    }



def extract_subscription_id_from_invoice(invoice):
    """
    Extract subscription ID from Stripe invoice across different Stripe formats.
    """

    # 1. Direct field (most common)
    subscription_id = invoice.get("subscription")
    if subscription_id:
        return subscription_id

    # 2. New Stripe "parent.subscription_details"
    subscription_id = (
        invoice.get("parent", {})
        .get("subscription_details", {})
        .get("subscription")
    )
    if subscription_id:
        return subscription_id

    # 3. Fallback: invoice lines
    for line in invoice.get("lines", {}).get("data", []):
        subscription_id = (
            line.get("parent", {})
            .get("subscription_item_details", {})
            .get("subscription")
        )
        if subscription_id:
            return subscription_id

    return None

def get_next_billing_cycle_anchor(today, anchor_day):
    """
    Returns unix timestamp for next billing anchor.
    """
    if not anchor_day:
        return None

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

    return int(anchor_date.timestamp())




def get_cancel_quantity_action(current_qty):
    """
    Returns:
        ("delete", None)
        ("modify", new_qty)
    """
    if current_qty <= 1:
        return ("delete", None)

    return ("modify", current_qty - 1)



def should_set_monthly_resume_prevention(today, subscription):
    """
    If canceled after anchor in monthly mode,
    prevent future resume double charge.
    """
    return (
        subscription.billing_mode == "monthly"
        and today.day > subscription.billing_anchor_day
    )

def should_cancel_subscription(remaining_items_exist):
    return not remaining_items_exist




def get_cancel_success_message(subscription):
    end_date = (
        subscription.access_until.strftime("%Y/%m/%d")
        if subscription.access_until
        else "次回更新日"
    )

    return f"プランは削除されました。 {end_date} まで利用可能です"


# -------------------------
# NEW RESUME HELPERS
# -------------------------




def get_resume_item_action(existing_item):
    """
    Returns:
        ("modify", stripe_item_id, new_qty)
        ("create", None, 1)
    """
    if existing_item:
        return (
            "modify",
            existing_item["id"],
            existing_item["quantity"] + 1,
        )

    return ("create", None, 1)


def should_charge_resume_next_month(today, subscription, item):
    return (
        subscription.billing_mode == "monthly"
        and today.day > subscription.billing_anchor_day
        and not item.monthly_double_resume_charge_prevention
    )


def get_resume_charge_amount(item):
    return item.price_at_subscription or item.plan.price


def get_resume_success_message():
    return "解約を取り消しました。プランが再開されました"

def compute_access_period_preview(today, mode, anchor_day):
    """
    PURE function:
    no DB, no Stripe, no mutation.
    """
    if mode == "regular":
        next_anchor_ts = get_next_billing_cycle_anchor(today, anchor_day)
        current_period_end = datetime.fromtimestamp(next_anchor_ts, tz=dt_timezone.utc)

        access_until = current_period_end

    else:
        year = today.year
        month = today.month

        # monthly UX rule: month-end window
        if anchor_day and today.day > anchor_day:
            month += 1
            if month > 12:
                month = 1
                year += 1

        last_day = calendar.monthrange(year, month)[1]

        access_until = datetime(
            year, month, last_day, 23, 59, 59, tzinfo=dt_timezone.utc
        )

        current_period_end = datetime.fromtimestamp(
            get_next_billing_cycle_anchor(today, anchor_day),
            tz=dt_timezone.utc
        )

    return {
        "access_start": today,
        "access_until": access_until,
        "current_period_end": current_period_end,
        "days": (access_until.date() - today).days + 1,
    }