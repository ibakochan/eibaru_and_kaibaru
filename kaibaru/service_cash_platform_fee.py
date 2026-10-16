import hashlib
import logging
from datetime import datetime

import stripe
from django.conf import settings
from django.db import transaction
from django.db.models import Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import Invoice, Subscription
from .discounts import calculate_discounted_amount
from .pricing import get_effective_subscription_price


logger = logging.getLogger(__name__)

CASH_FEE_STATUSES = ("open", "paid")
CASH_FEE_PAYMENT_METHODS = ("cash", "bank_transfer")
CASH_SUB_BILLING_METHODS = ("cash", "bank_transfer", "manual")
CASH_SUB_ACTIVE_STATUSES = ("active", "trialing", "past_due", "pending")
SUBSCRIPTION_BILLING_REASONS = (
    "initial_subscription",
    "subscription_cycle",
    "add_plan",
)


def previous_month(today):
    if today.month == 1:
        return today.year - 1, 12
    return today.year, today.month - 1


def month_bounds(year, month):
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime(year, month, 1), tz)
    if month == 12:
        end = timezone.make_aware(datetime(year + 1, 1, 1), tz)
    else:
        end = timezone.make_aware(datetime(year, month + 1, 1), tz)
    return start, end


def total_platform_fee(amounts):
    """1% of the summed invoice amounts, rounded half up to the nearest yen.

    Cash and bank-transfer invoices are added first. The Stripe fee still
    rounds each charge on its own.
    """
    total = 0
    for amount in amounts:
        try:
            amount = int(amount or 0)
        except (TypeError, ValueError):
            continue
        total += amount
    if total <= 0:
        return 0
    return (total + 50) // 100


def _unbilled_cash_invoices(queryset, start, end):
    """Cash invoices whose fee month is [start, end).

    A Stripe invoice switched to cash belongs to the month of converted_at.
    Invoices that were cash from the start use issued_at.
    """
    return queryset.annotate(
        fee_effective_at=Coalesce("converted_at", "issued_at"),
    ).filter(
        fee_effective_at__gte=start,
        fee_effective_at__lt=end,
        platform_fee_applied=False,
        status__in=CASH_FEE_STATUSES,
        payment_method__in=CASH_FEE_PAYMENT_METHODS,
    )


def apply_cash_platform_fees_for_month(year, month):
    """Add 1% of one month's cash and bank-transfer invoices to each club bill.

    Open and paid invoices that have not been billed yet are summed, then
    1% of that total is rounded to the nearest yen. Converted Stripe invoices
    use converted_at for the month. If Stripe already has this month's fee,
    the invoices are marked and no second item is created.
    """
    stripe.api_key = settings.STRIPE_SECRET_KEY
    start, end = month_bounds(year, month)

    club_ids = (
        _unbilled_cash_invoices(Invoice.objects, start, end)
        .values_list("club_id", flat=True)
        .distinct()
    )

    results = []
    for club_id in club_ids:
        try:
            results.append(
                _apply_club_month(club_id, year, month, start, end)
            )
        except Exception:
            logger.exception(
                "[CASH PLATFORM FEE] Failed for club=%s %s-%s",
                club_id,
                year,
                month,
            )
            results.append({
                "club_id": club_id,
                "applied": False,
                "reason": "error",
            })
    return results


def apply_previous_month_cash_platform_fees(today=None):
    today = today or timezone.localdate()
    year, month = previous_month(today)
    return apply_cash_platform_fees_for_month(year, month)


def _apply_club_month(club_id, year, month, start, end):
    with transaction.atomic():
        invoices = list(
            _unbilled_cash_invoices(
                Invoice.objects.select_for_update().select_related("club"),
                start,
                end,
            )
            .filter(club_id=club_id)
            .order_by("id")
        )
        if not invoices:
            return {
                "club_id": club_id,
                "applied": False,
                "reason": "none",
            }

        fee = total_platform_fee(invoice.amount_due for invoice in invoices)
        club = invoices[0].club
        invoice_ids = [invoice.id for invoice in invoices]

        if fee > 0 and not club.stripe_customer_id:
            logger.warning(
                "[CASH PLATFORM FEE] Club=%s has no platform customer. "
                "Leaving %s invoices unmarked.",
                club_id,
                len(invoice_ids),
            )
            return {
                "club_id": club_id,
                "applied": False,
                "reason": "no_platform_customer",
                "fee": fee,
            }

        if fee > 0:
            if _cash_platform_fee_item_exists(
                club.stripe_customer_id,
                club_id,
                year,
                month,
            ):
                logger.info(
                    "[CASH PLATFORM FEE] Club=%s %s-%s already billed. "
                    "Marking %s invoices.",
                    club_id,
                    year,
                    month,
                    len(invoice_ids),
                )
            else:
                stripe.InvoiceItem.create(
                    customer=club.stripe_customer_id,
                    amount=fee,
                    currency="jpy",
                    description=f"現金・振込の手数料（{year}年{month}月）",
                    metadata=_cash_platform_fee_metadata(
                        club_id,
                        year,
                        month,
                    ),
                    idempotency_key=_idempotency_key(
                        club_id,
                        year,
                        month,
                        invoice_ids,
                    ),
                )

        Invoice.objects.filter(id__in=invoice_ids).update(
            platform_fee_applied=True,
        )

    logger.info(
        "[CASH PLATFORM FEE] Club=%s %s-%s fee=%s invoices=%s",
        club_id,
        year,
        month,
        fee,
        len(invoice_ids),
    )
    return {
        "club_id": club_id,
        "applied": True,
        "fee": fee,
        "invoice_count": len(invoice_ids),
    }


def _cash_platform_fee_metadata(club_id, year, month):
    return {
        "type": "cash_platform_fee",
        "club_id": str(club_id),
        "year": str(year),
        "month": str(month),
    }


def _item_metadata(item):
    metadata = item.get("metadata") if hasattr(item, "get") else None
    if metadata is None:
        metadata = getattr(item, "metadata", None)
    return metadata or {}


def _is_cash_platform_fee_item(item, club_id, year, month):
    metadata = _item_metadata(item)
    expected = _cash_platform_fee_metadata(club_id, year, month)
    return all(
        str(metadata.get(key, "")) == value
        for key, value in expected.items()
    )


def _cash_platform_fee_item_exists(customer_id, club_id, year, month):
    """True when this club already has a platform fee item for that month."""
    starting_after = None
    while True:
        params = {"customer": customer_id, "limit": 100}
        if starting_after:
            params["starting_after"] = starting_after
        page = stripe.InvoiceItem.list(**params)
        for item in page.data:
            if _is_cash_platform_fee_item(item, club_id, year, month):
                return True
        if not page.has_more or not page.data:
            return False
        starting_after = page.data[-1].id


def _idempotency_key(club_id, year, month, invoice_ids):
    digest = hashlib.sha256(
        ",".join(str(invoice_id) for invoice_id in invoice_ids).encode()
    ).hexdigest()[:16]
    return f"cash_platform_fee_{club_id}_{year}{month:02d}_{digest}"


def _cash_invoices_for_month(club_ids, year, month):
    """Open/paid cash invoices in that month (applied or not)."""
    if not club_ids:
        return Invoice.objects.none()
    start, end = month_bounds(year, month)
    return (
        Invoice.objects.filter(club_id__in=club_ids)
        .annotate(fee_effective_at=Coalesce("converted_at", "issued_at"))
        .filter(
            fee_effective_at__gte=start,
            fee_effective_at__lt=end,
            status__in=CASH_FEE_STATUSES,
            payment_method__in=CASH_FEE_PAYMENT_METHODS,
        )
        .select_related("club", "subscription", "payer")
        .prefetch_related("items")
        .order_by("fee_effective_at", "id")
    )


def _cash_invoice_amounts_for_month(club_ids, year, month):
    """All open/paid cash invoices in that month, applied or not."""
    return list(
        _cash_invoices_for_month(club_ids, year, month).values_list(
            "amount_due", flat=True
        )
    )


def _expected_cash_subscription_rows(club_ids, exclude_subscription_ids):
    """Active cash subs with no invoice yet this fee window, for UI listing."""
    if not club_ids:
        return []
    subscriptions = (
        Subscription.objects.filter(
            club_id__in=club_ids,
            billing_method__in=CASH_SUB_BILLING_METHODS,
            status__in=CASH_SUB_ACTIVE_STATUSES,
        )
        .exclude(id__in=exclude_subscription_ids or [])
        .select_related("club")
        .prefetch_related("items__member", "items__plan")
    )
    rows = []
    for subscription in subscriptions:
        items = []
        total = 0
        payer_name = None
        for item in subscription.items.all():
            if item.deleted_at is not None or not item.member_id or not item.plan_id:
                continue
            base = get_effective_subscription_price(item)
            discounted = calculate_discounted_amount(
                club=subscription.club,
                member=item.member,
                plan=item.plan,
                base_amount=base,
                apply_to="subscription",
            )
            amount = max(0, int(discounted))
            total += amount
            if payer_name is None and item.member_id:
                payer_name = item.member.full_name
            plan_name = getattr(item.plan, "name", None) or "プラン"
            items.append({"description": plan_name, "amount": amount})
        if total <= 0:
            continue
        club = subscription.club
        rows.append({
            "id": f"expected-{subscription.id}",
            "kind": "expected_subscription",
            "club_id": club.id,
            "club_subdomain": club.subdomain,
            "billing_member_name": payer_name or "",
            "amount_due": total,
            "amount_paid": 0,
            "currency": "jpy",
            "status": "scheduled",
            "billing_method": subscription.billing_method,
            "due_date": None,
            "issued_at": None,
            "billing_reason": "subscription_cycle",
            "items": items,
        })
    return rows


def list_cash_fee_line_items_for_clubs(
    club_ids,
    today=None,
    *,
    next_invoice_uses_current_month=False,
):
    """Invoices (+ expected cash subs) that feed the next platform 1% fee."""
    today = today or timezone.localdate()
    club_ids = list(club_ids or [])
    if not club_ids:
        return {
            "cash_invoices": [],
            "cash_expected": [],
            "source_year": None,
            "source_month": None,
        }

    if not next_invoice_uses_current_month:
        year, month = previous_month(today)
        invoices = list(_cash_invoices_for_month(club_ids, year, month))
        return {
            "cash_invoices": invoices,
            "cash_expected": [],
            "source_year": year,
            "source_month": month,
        }

    start, end = month_bounds(today.year, today.month)
    invoices = list(
        Invoice.objects.filter(club_id__in=club_ids)
        .annotate(fee_effective_at=Coalesce("converted_at", "issued_at"))
        .filter(
            fee_effective_at__gte=start,
            fee_effective_at__lt=end,
            status__in=CASH_FEE_STATUSES,
            payment_method__in=CASH_FEE_PAYMENT_METHODS,
        )
        .select_related("club", "subscription", "payer")
        .prefetch_related("items")
        .order_by("fee_effective_at", "id")
    )
    _, invoiced_sub_ids = _cash_subscription_invoice_total(club_ids, start, end)
    expected = _expected_cash_subscription_rows(club_ids, invoiced_sub_ids)
    return {
        "cash_invoices": invoices,
        "cash_expected": expected,
        "source_year": today.year,
        "source_month": today.month,
    }


def _cash_subscription_invoice_total(club_ids, start, end):
    if not club_ids:
        return 0, set()
    rows = (
        Invoice.objects.filter(
            club_id__in=club_ids,
            subscription_id__isnull=False,
            billing_reason__in=SUBSCRIPTION_BILLING_REASONS,
            status__in=CASH_FEE_STATUSES,
            payment_method__in=CASH_FEE_PAYMENT_METHODS,
        )
        .annotate(fee_effective_at=Coalesce("converted_at", "issued_at"))
        .filter(
            fee_effective_at__gte=start,
            fee_effective_at__lt=end,
        )
        .values_list("subscription_id", "amount_due")
    )
    invoiced_subscription_ids = set()
    total = 0
    for subscription_id, amount_due in rows:
        invoiced_subscription_ids.add(subscription_id)
        try:
            total += int(amount_due or 0)
        except (TypeError, ValueError):
            continue
    return total, invoiced_subscription_ids


def _expected_cash_subscription_total_excluding(club_ids, exclude_subscription_ids):
    if not club_ids:
        return 0
    subscriptions = (
        Subscription.objects.filter(
            club_id__in=club_ids,
            billing_method__in=CASH_SUB_BILLING_METHODS,
            status__in=CASH_SUB_ACTIVE_STATUSES,
        )
        .exclude(id__in=exclude_subscription_ids or [])
        .select_related("club")
        .prefetch_related("items__member", "items__plan")
    )
    total = 0
    for subscription in subscriptions:
        for item in subscription.items.all():
            if item.deleted_at is not None or not item.member_id or not item.plan_id:
                continue
            base = get_effective_subscription_price(item)
            discounted = calculate_discounted_amount(
                club=subscription.club,
                member=item.member,
                plan=item.plan,
                base_amount=base,
                apply_to="subscription",
            )
            total += max(0, int(discounted))
    return total


def _non_subscription_cash_invoice_total(club_ids, start, end):
    if not club_ids:
        return 0
    total = (
        Invoice.objects.filter(
            club_id__in=club_ids,
            status__in=CASH_FEE_STATUSES,
            payment_method__in=CASH_FEE_PAYMENT_METHODS,
        )
        .filter(Q(subscription_id__isnull=True) | ~Q(billing_reason__in=SUBSCRIPTION_BILLING_REASONS))
        .annotate(fee_effective_at=Coalesce("converted_at", "issued_at"))
        .filter(
            fee_effective_at__gte=start,
            fee_effective_at__lt=end,
        )
        .aggregate(total=Sum("amount_due"))
        .get("total")
    )
    try:
        return int(total or 0)
    except (TypeError, ValueError):
        return 0


def estimate_cash_platform_fees_for_clubs(
    club_ids,
    today=None,
    *,
    next_invoice_uses_current_month=False,
):
    """1% cash fee for the *next* platform invoice only (never both months).

    Before this month's platform cycle is paid:
      next fee = 1% of previous calendar month's cash/bank invoices.

    After this month's platform cycle is paid (next invoice is next month):
      next fee = 1% of (cash invoices already issued this month
      + expected totals for active cash subscriptions that still have no
      invoice this month
      + non-subscription cash invoices so far this month).

    cash_fee_previous_year/month label the cash source month that feeds
    cash_fee_next_yen (previous or current calendar month).
    """
    today = today or timezone.localdate()
    club_ids = list(club_ids or [])
    empty = {
        "cash_fee_next_yen": 0,
        "cash_fee_next_base_yen": 0,
        "cash_fee_previous_year": None,
        "cash_fee_previous_month": None,
        "cash_fee_current_month_estimate_yen": 0,
        "cash_fee_current_month_base_yen": 0,
        "cash_fee_current_month_from_subscription_invoices_yen": 0,
        "cash_fee_current_month_from_expected_subscriptions_yen": 0,
        "cash_fee_current_month_from_other_invoices_yen": 0,
    }
    if not club_ids:
        return empty

    if not next_invoice_uses_current_month:
        prev_year, prev_month = previous_month(today)
        previous_amounts = _cash_invoice_amounts_for_month(
            club_ids,
            prev_year,
            prev_month,
        )
        previous_base = 0
        for amount in previous_amounts:
            try:
                previous_base += int(amount or 0)
            except (TypeError, ValueError):
                continue
        return {
            "cash_fee_next_yen": total_platform_fee(previous_amounts),
            "cash_fee_next_base_yen": previous_base,
            "cash_fee_previous_year": prev_year,
            "cash_fee_previous_month": prev_month,
            "cash_fee_current_month_estimate_yen": 0,
            "cash_fee_current_month_base_yen": 0,
            "cash_fee_current_month_from_subscription_invoices_yen": 0,
            "cash_fee_current_month_from_expected_subscriptions_yen": 0,
            "cash_fee_current_month_from_other_invoices_yen": 0,
        }

    start, end = month_bounds(today.year, today.month)
    sub_invoice_total, invoiced_sub_ids = _cash_subscription_invoice_total(
        club_ids,
        start,
        end,
    )
    expected_remaining = _expected_cash_subscription_total_excluding(
        club_ids,
        invoiced_sub_ids,
    )
    other_total = _non_subscription_cash_invoice_total(club_ids, start, end)
    current_base = sub_invoice_total + expected_remaining + other_total
    current_fee = total_platform_fee([current_base]) if current_base else 0

    return {
        "cash_fee_next_yen": current_fee,
        "cash_fee_next_base_yen": current_base,
        "cash_fee_previous_year": today.year,
        "cash_fee_previous_month": today.month,
        # Kept for API compatibility; always zero — next invoice uses
        # cash_fee_next_* only.
        "cash_fee_current_month_estimate_yen": 0,
        "cash_fee_current_month_base_yen": 0,
        "cash_fee_current_month_from_subscription_invoices_yen": sub_invoice_total,
        "cash_fee_current_month_from_expected_subscriptions_yen": expected_remaining,
        "cash_fee_current_month_from_other_invoices_yen": other_total,
    }
