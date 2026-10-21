"""Past platform invoices for an owner (請求歴).

Each row is a paid Kaibaru platform Stripe invoice, with the same fee lines
as 次回請求, plus previous-month cash 1% and previous-month Stripe member
invoice fees (Kaibaru application fee 1% and Stripe's processing fee when
available from Charge balance transactions).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone as dt_timezone

import stripe
from django.conf import settings
from django.db.models.functions import Coalesce
from django.utils import timezone

from .charge_amounts import platform_fee_yen
from .service_paid_reservations import (
    paid_reservations_for_clubs,
    reservation_application_fee_yen,
)
from .models import Club, Invoice, PlatformSubscription
from .service_cash_platform_fee import (
    month_bounds,
    previous_month,
    total_platform_fee,
)
from .service_platform_subscription import (
    CLUB_BASE_YEN,
    CLUB_EXTRA_YEN,
    MEMBER_FULL_CAP,
    MEMBER_FULL_YEN,
    MEMBER_REDUCED_YEN,
)

logger = logging.getLogger(__name__)

HISTORY_LIMIT = 36


def _stripe():
    stripe.api_key = settings.STRIPE_SECRET_KEY


def _ts_to_local_date(ts):
    if not ts:
        return None
    return timezone.localtime(
        datetime.fromtimestamp(int(ts), tz=dt_timezone.utc)
    ).date()


def _owner_club_ids(user_id):
    return list(
        Club.objects.filter(owner_id=user_id, is_deleted=False)
        .values_list("id", flat=True)
    )


def _cash_totals_for_month(club_ids, year, month):
    amounts = list(
        Invoice.objects.filter(club_id__in=club_ids)
        .annotate(fee_effective_at=Coalesce("converted_at", "issued_at"))
        .filter(
            fee_effective_at__gte=month_bounds(year, month)[0],
            fee_effective_at__lt=month_bounds(year, month)[1],
            status__in=("open", "paid"),
            payment_method__in=("cash", "bank_transfer"),
        )
        .values_list("amount_due", flat=True)
    )
    base = 0
    for amount in amounts:
        try:
            base += int(amount or 0)
        except (TypeError, ValueError):
            continue
    return {
        "cash_invoice_base_yen": base,
        "cash_fee_yen": total_platform_fee(amounts),
        "cash_invoice_count": len(amounts),
    }


def _stripe_member_fee_totals(club_ids, year, month):
    """Kaibaru 1% + Stripe processing fees on member Stripe invoices that month.

    Fee month is the calendar month *before* the platform payment, same as cash.
    Kaibaru fee uses the same half-up 1% as live charges. Stripe processing fee
    is read from each invoice's paid charge balance_transaction when present.
    """
    start, end = month_bounds(year, month)
    invoices = list(
        Invoice.objects.filter(
            club_id__in=club_ids,
            status="paid",
            payment_method="stripe",
        )
        .annotate(fee_effective_at=Coalesce("converted_at", "issued_at"))
        .filter(
            fee_effective_at__gte=start,
            fee_effective_at__lt=end,
        )
        .select_related("club")
    )
    base = 0
    kaibaru_fee = 0
    stripe_fee = 0
    stripe_fee_known = 0
    positive_count = 0
    for inv in invoices:
        try:
            amount = int(inv.amount_paid or inv.amount_due or 0)
        except (TypeError, ValueError):
            amount = 0
        if amount <= 0:
            continue
        positive_count += 1
        base += amount
        kaibaru_fee += platform_fee_yen(amount)
        if stripe_fee_known >= 40:
            continue
        fee = _stripe_processing_fee_for_invoice(inv)
        if fee is not None:
            stripe_fee += fee
            stripe_fee_known += 1

    kaibaru_fee += reservation_application_fee_yen(
        paid_reservations_for_clubs(club_ids, year, month)
    )

    return {
        "stripe_member_invoice_base_yen": base,
        "stripe_member_invoice_count": len(invoices),
        "kaibaru_application_fee_yen": kaibaru_fee,
        "stripe_processing_fee_yen": stripe_fee if stripe_fee_known else None,
        "stripe_processing_fee_invoice_count": stripe_fee_known,
        "stripe_processing_fee_incomplete": (
            stripe_fee_known > 0 and stripe_fee_known < positive_count
        ),
    }


def _stripe_processing_fee_for_invoice(inv):
    """Stripe's fee on a paid connected-account invoice, or None if unknown."""
    stripe_invoice_id = getattr(inv, "stripe_invoice_id", None)
    if not stripe_invoice_id:
        return None
    club = getattr(inv, "club", None)
    stripe_account = getattr(club, "stripe_account_id", None) if club else None
    if not stripe_account:
        club_id = getattr(inv, "club_id", None)
        if club_id:
            stripe_account = (
                Club.objects.filter(id=club_id)
                .values_list("stripe_account_id", flat=True)
                .first()
            )
    if not stripe_account:
        return None
    try:
        stripe_inv = stripe.Invoice.retrieve(
            stripe_invoice_id,
            expand=["charge", "charge.balance_transaction", "payment_intent"],
            stripe_account=stripe_account,
        )
    except Exception:
        logger.exception(
            "[BILLING HISTORY] Failed retrieving Stripe invoice %s",
            stripe_invoice_id,
        )
        return None

    charge = stripe_inv.get("charge")
    if isinstance(charge, str):
        try:
            charge = stripe.Charge.retrieve(
                charge,
                expand=["balance_transaction"],
                stripe_account=stripe_account,
            )
        except Exception:
            return None
    if not charge:
        return None
    bt = charge.get("balance_transaction")
    if isinstance(bt, str):
        try:
            bt = stripe.BalanceTransaction.retrieve(
                bt,
                stripe_account=stripe_account,
            )
        except Exception:
            return None
    if not bt:
        return None
    try:
        return int(bt.get("fee") or 0)
    except (TypeError, ValueError):
        return None


def _line_amount(line):
    try:
        return int(line.get("amount") or 0)
    except (TypeError, ValueError):
        return 0


def _line_quantity(line):
    try:
        return int(line.get("quantity") or 0)
    except (TypeError, ValueError):
        return 0


def _price_id(line):
    price = line.get("price")
    if isinstance(price, dict):
        return price.get("id")
    if isinstance(price, str):
        return price
    details = (line.get("pricing") or {}).get("price_details") or {}
    return details.get("price")


def present_member_tiers(full, reduced, full_yen, reduced_yen):
    """First 100 members are ¥100, and every member after that is ¥50.

    Older invoices put the whole headcount on the ¥100 price. Split that
    so the receipt shows both tiers.
    """
    try:
        full = int(full or 0)
        reduced = int(reduced or 0)
        full_yen = int(full_yen or 0)
        reduced_yen = int(reduced_yen or 0)
    except (TypeError, ValueError):
        return 0, 0, 0, 0
    if full > MEMBER_FULL_CAP and reduced == 0:
        reduced = full - MEMBER_FULL_CAP
        full = MEMBER_FULL_CAP
        full_yen = full * MEMBER_FULL_YEN
        reduced_yen = reduced * MEMBER_REDUCED_YEN
    return full, reduced, full_yen, reduced_yen


def _parse_platform_invoice_lines(lines):
    """Turn Stripe invoice lines into the same buckets as 次回請求."""
    club_base_yen = 0
    club_extra_yen = 0
    extra_clubs = 0
    member_full_yen = 0
    member_reduced_yen = 0
    full_price_members = 0
    reduced_members = 0
    cash_fee_yen = 0
    cash_fee_year = None
    cash_fee_month = None
    other_yen = 0

    base_price = settings.STRIPE_BASE_PRICE_ID
    base_reduced = settings.STRIPE_BASE_PRICE_ID_REDUCED
    member_price = settings.STRIPE_MEMBER_PRICE_ID
    member_reduced = settings.STRIPE_MEMBER_PRICE_ID_REDUCED

    for line in lines or []:
        amount = _line_amount(line)
        qty = _line_quantity(line)
        price_id = _price_id(line)
        metadata = line.get("metadata") or {}
        description = line.get("description") or ""

        if metadata.get("type") == "cash_platform_fee" or "現金" in description:
            cash_fee_yen += amount
            try:
                cash_fee_year = int(metadata.get("year") or cash_fee_year or 0) or cash_fee_year
                cash_fee_month = int(metadata.get("month") or cash_fee_month or 0) or cash_fee_month
            except (TypeError, ValueError):
                pass
            continue

        if price_id == base_price:
            club_base_yen += amount
            continue
        if price_id == base_reduced:
            club_extra_yen += amount
            extra_clubs += qty
            continue
        if price_id == member_price:
            member_full_yen += amount
            full_price_members += qty
            continue
        if price_id == member_reduced:
            member_reduced_yen += amount
            reduced_members += qty
            continue

        # Fallback by unit amount when price ids differ (test / migrated).
        unit = amount // qty if qty else amount
        if unit == CLUB_BASE_YEN and qty == 1 and club_base_yen == 0:
            club_base_yen += amount
        elif unit == CLUB_EXTRA_YEN:
            club_extra_yen += amount
            extra_clubs += qty
        elif unit == MEMBER_FULL_YEN:
            member_full_yen += amount
            full_price_members += qty
        elif unit == MEMBER_REDUCED_YEN:
            member_reduced_yen += amount
            reduced_members += qty
        else:
            other_yen += amount

    club_count = 1 + extra_clubs if club_base_yen or extra_clubs else extra_clubs
    if club_base_yen and club_count < 1:
        club_count = 1

    full_price_members, reduced_members, member_full_yen, member_reduced_yen = (
        present_member_tiers(
            full_price_members,
            reduced_members,
            member_full_yen,
            member_reduced_yen,
        )
    )

    subscription_yen = (
        club_base_yen
        + club_extra_yen
        + member_full_yen
        + member_reduced_yen
    )
    return {
        "club_count": club_count,
        "extra_clubs": extra_clubs,
        "club_base_yen": club_base_yen,
        "club_extra_yen": club_extra_yen,
        "full_price_members": full_price_members,
        "reduced_members": reduced_members,
        "member_count": full_price_members + reduced_members,
        "member_full_yen": member_full_yen,
        "member_reduced_yen": member_reduced_yen,
        "subscription_yen": subscription_yen,
        "cash_fee_yen": cash_fee_yen,
        "cash_fee_year": cash_fee_year,
        "cash_fee_month": cash_fee_month,
        "other_yen": other_yen,
    }


def _invoice_lines(stripe_invoice):
    lines = stripe_invoice.get("lines") or {}
    if isinstance(lines, dict):
        return list(lines.get("data") or [])
    return []


def _build_history_entry(stripe_invoice, club_ids, *, include_stripe_processing=False):
    paid_at = _ts_to_local_date(
        stripe_invoice.get("status_transitions", {}).get("paid_at")
        or stripe_invoice.get("created")
    )
    period_start = _ts_to_local_date(stripe_invoice.get("period_start"))
    period_end = _ts_to_local_date(stripe_invoice.get("period_end"))

    # Fee source month = calendar month before the platform payment month.
    anchor = paid_at or period_start or timezone.localdate()
    fee_year, fee_month = previous_month(anchor)

    lines = _invoice_lines(stripe_invoice)
    parsed = _parse_platform_invoice_lines(lines)

    cash_local = _cash_totals_for_month(club_ids, fee_year, fee_month)
    # Prefer amounts actually on the Stripe invoice when present.
    cash_fee_yen = parsed["cash_fee_yen"] or cash_local["cash_fee_yen"]
    cash_base = cash_local["cash_invoice_base_yen"]
    cash_fee_year = parsed["cash_fee_year"] or fee_year
    cash_fee_month = parsed["cash_fee_month"] or fee_month

    if include_stripe_processing:
        stripe_fees = _stripe_member_fee_totals(club_ids, fee_year, fee_month)
    else:
        # Skip per-invoice Stripe API calls on the list; Kaibaru 1% is local.
        stripe_fees = _stripe_member_fee_totals_local_only(
            club_ids, fee_year, fee_month
        )

    try:
        total_yen = int(stripe_invoice.get("amount_paid") or stripe_invoice.get("amount_due") or 0)
    except (TypeError, ValueError):
        total_yen = (
            parsed["subscription_yen"]
            + cash_fee_yen
            + parsed["other_yen"]
        )

    return {
        "stripe_invoice_id": stripe_invoice.get("id"),
        "invoice_number": stripe_invoice.get("number"),
        "status": stripe_invoice.get("status"),
        "paid_on": paid_at.isoformat() if paid_at else None,
        "period_start": period_start.isoformat() if period_start else None,
        "period_end": period_end.isoformat() if period_end else None,
        "payment_year": anchor.year,
        "payment_month": anchor.month,
        "fee_source_year": fee_year,
        "fee_source_month": fee_month,
        "club_count": parsed["club_count"],
        "extra_clubs": parsed["extra_clubs"],
        "club_base_yen": parsed["club_base_yen"],
        "club_extra_yen": parsed["club_extra_yen"],
        "full_price_members": parsed["full_price_members"],
        "reduced_members": parsed["reduced_members"],
        "member_count": parsed["member_count"],
        "member_full_yen": parsed["member_full_yen"],
        "member_reduced_yen": parsed["member_reduced_yen"],
        "subscription_yen": parsed["subscription_yen"],
        "other_yen": parsed["other_yen"],
        "cash_fee_yen": cash_fee_yen,
        "cash_invoice_base_yen": cash_base,
        "cash_invoice_count": cash_local["cash_invoice_count"],
        "cash_fee_year": cash_fee_year,
        "cash_fee_month": cash_fee_month,
        "total_yen": total_yen,
        **stripe_fees,
        "hosted_invoice_url": stripe_invoice.get("hosted_invoice_url"),
        "invoice_pdf": stripe_invoice.get("invoice_pdf"),
    }


def _stripe_member_fee_totals_local_only(club_ids, year, month):
    start, end = month_bounds(year, month)
    invoices = list(
        Invoice.objects.filter(
            club_id__in=club_ids,
            status="paid",
            payment_method="stripe",
        )
        .annotate(fee_effective_at=Coalesce("converted_at", "issued_at"))
        .filter(
            fee_effective_at__gte=start,
            fee_effective_at__lt=end,
        )
        .values_list("amount_paid", "amount_due")
    )
    base = 0
    kaibaru_fee = 0
    for amount_paid, amount_due in invoices:
        try:
            amount = int(amount_paid or amount_due or 0)
        except (TypeError, ValueError):
            amount = 0
        if amount <= 0:
            continue
        base += amount
        kaibaru_fee += platform_fee_yen(amount)
    kaibaru_fee += reservation_application_fee_yen(
        paid_reservations_for_clubs(club_ids, year, month)
    )
    return {
        "stripe_member_invoice_base_yen": base,
        "stripe_member_invoice_count": len(invoices),
        "kaibaru_application_fee_yen": kaibaru_fee,
        "stripe_processing_fee_yen": None,
        "stripe_processing_fee_invoice_count": 0,
        "stripe_processing_fee_incomplete": False,
    }


def platform_amount_paid_in_month(user, year, month):
    """Yen the owner paid Kaibaru in this calendar month.

    Dated by the platform invoice's paid_at. That bill covers the owner's
    clubs together, and the cash 1% inside it is for the previous month.
    """
    if not user or not getattr(user, "id", None) or not year or not month:
        return 0
    year = int(year)
    month = int(month)
    platform_sub = PlatformSubscription.objects.filter(user_id=user.id).first()
    total = 0
    for invoice in _list_platform_stripe_invoices(platform_sub, expand_lines=False):
        paid_at = _ts_to_local_date(
            (invoice.get("status_transitions") or {}).get("paid_at")
            or invoice.get("created")
        )
        if not paid_at or paid_at.year != year or paid_at.month != month:
            continue
        try:
            total += int(invoice.get("amount_paid") or invoice.get("amount_due") or 0)
        except (TypeError, ValueError):
            continue
    return total


def _list_platform_stripe_invoices(platform_sub, *, expand_lines=True):
    _stripe()
    if not platform_sub or not platform_sub.stripe_subscription_id:
        return []
    invoices = []
    starting_after = None
    while len(invoices) < HISTORY_LIMIT:
        kwargs = {
            "subscription": platform_sub.stripe_subscription_id,
            "status": "paid",
            "limit": min(24, HISTORY_LIMIT - len(invoices)),
        }
        if expand_lines:
            kwargs["expand"] = ["data.lines"]
        if starting_after:
            kwargs["starting_after"] = starting_after
        page = stripe.Invoice.list(**kwargs)
        batch = list(page.get("data") or [])
        if not batch:
            break
        invoices.extend(batch)
        if not page.get("has_more"):
            break
        starting_after = batch[-1]["id"]
    return invoices[:HISTORY_LIMIT]


def platform_billing_history_for_owner(user, *, include_stripe_processing=False):
    if not user or not getattr(user, "id", None):
        return {"entries": []}

    platform_sub = (
        PlatformSubscription.objects.filter(user_id=user.id).first()
    )
    club_ids = _owner_club_ids(user.id)
    stripe_invoices = _list_platform_stripe_invoices(platform_sub)
    entries = [
        _build_history_entry(
            inv,
            club_ids,
            include_stripe_processing=include_stripe_processing,
        )
        for inv in stripe_invoices
    ]
    return {"entries": entries}


def _yen(amount):
    try:
        value = int(amount or 0)
    except (TypeError, ValueError):
        value = 0
    return f"¥{value:,}"


def _month_label(year, month):
    if not year or not month:
        return ""
    return f"{year}年{month}月"


def format_platform_cycle_receipt(entry):
    """Owner email body for a paid platform cycle. Invoice lines only.

    The Kaibaru 1% on member Stripe charges is not part of this invoice,
    so it is left out.
    """
    pay_month = _month_label(entry.get("payment_year"), entry.get("payment_month"))
    fee_month = _month_label(entry.get("fee_source_year"), entry.get("fee_source_month"))
    paid_on = entry.get("paid_on") or ""
    lines = []

    lines.append(f"{pay_month}の請求" if pay_month else "ご請求")
    total_line = f"合計 {_yen(entry.get('total_yen'))}"
    if paid_on:
        total_line += f" · 支払日 {paid_on}"
    lines.append(total_line)
    lines.append("")
    lines.append("Kaibaru プラットフォーム利用料")
    pay_line = f"お支払い月：{pay_month}" if pay_month else "お支払い月："
    if paid_on:
        pay_line += f"（支払日 {paid_on}）"
    lines.append(pay_line)
    if entry.get("invoice_number"):
        lines.append(f"請求書番号：{entry['invoice_number']}")
    lines.append("")

    full_members, reduced_members, full_yen, reduced_yen = present_member_tiers(
        entry.get("full_price_members"),
        entry.get("reduced_members"),
        entry.get("member_full_yen"),
        entry.get("member_reduced_yen"),
    )

    lines.append("クラブ基本料")
    lines.append(_yen(entry.get("club_base_yen")))
    if (entry.get("extra_clubs") or 0) > 0:
        lines.append("")
        lines.append(f"追加クラブ × {entry['extra_clubs']}")
        lines.append(_yen(entry.get("club_extra_yen")))
    if full_members > 0:
        lines.append("")
        lines.append(f"会員 {full_members}名 × ¥{MEMBER_FULL_YEN}")
        lines.append(_yen(full_yen))
    if reduced_members > 0:
        lines.append("")
        lines.append(f"会員 {reduced_members}名 × ¥{MEMBER_REDUCED_YEN}")
        lines.append(_yen(reduced_yen))
    if (entry.get("cash_fee_yen") or 0) > 0 or (entry.get("cash_invoice_base_yen") or 0) > 0:
        lines.append("")
        lines.append(f"現金・振込手数料1%（{fee_month}分の請求に対する手数料）")
        lines.append(_yen(entry.get("cash_fee_yen")))
    if (entry.get("other_yen") or 0) > 0:
        lines.append("")
        lines.append("その他")
        lines.append(_yen(entry.get("other_yen")))

    lines.append("")
    lines.append("Kaibaru請求 合計")
    lines.append(_yen(entry.get("total_yen")))
    lines.append("")
    note = (
        "※ 現金・振込の1%は、プラットフォーム請求月の前月"
        f"（{fee_month}）に発生した現金・振込請求が対象です。"
    )
    if (entry.get("cash_invoice_base_yen") or 0) > 0:
        note += f" 対象合計 {_yen(entry.get('cash_invoice_base_yen'))}"
        if entry.get("cash_invoice_count"):
            note += f"（{entry['cash_invoice_count']}件）"
        note += "。"
    lines.append(note)
    return "\n".join(lines)


def platform_billing_history_entry_for_owner(
    user,
    stripe_invoice_id,
    *,
    include_stripe_processing=True,
):
    """One past invoice with optional live Stripe processing fee lookup."""
    if not user or not stripe_invoice_id:
        return None
    platform_sub = PlatformSubscription.objects.filter(user_id=user.id).first()
    if not platform_sub or not platform_sub.stripe_subscription_id:
        return None
    _stripe()
    try:
        stripe_invoice = stripe.Invoice.retrieve(
            stripe_invoice_id,
            expand=["lines"],
        )
    except Exception:
        logger.exception(
            "[BILLING HISTORY] Failed retrieving platform invoice %s",
            stripe_invoice_id,
        )
        return None
    sub_id = stripe_invoice.get("subscription")
    if isinstance(sub_id, dict):
        sub_id = sub_id.get("id")
    if sub_id != platform_sub.stripe_subscription_id:
        return None
    club_ids = _owner_club_ids(user.id)
    return _build_history_entry(
        stripe_invoice,
        club_ids,
        include_stripe_processing=include_stripe_processing,
    )
