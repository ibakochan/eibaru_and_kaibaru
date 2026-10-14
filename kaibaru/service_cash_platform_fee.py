import hashlib
import logging
from datetime import datetime

import stripe
from django.conf import settings
from django.db import transaction
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import Invoice


logger = logging.getLogger(__name__)

CASH_FEE_STATUSES = ("open", "paid")
CASH_FEE_PAYMENT_METHODS = ("cash", "bank_transfer")


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
