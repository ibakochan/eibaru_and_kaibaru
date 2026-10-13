import hashlib
import logging
from datetime import datetime

import stripe
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .charge_amounts import platform_fee_yen
from .models import Invoice


logger = logging.getLogger(__name__)


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
    return sum(platform_fee_yen(amount) for amount in amounts)


def apply_cash_platform_fees_for_month(year, month):
    """Add 1% of one month's non-Stripe invoices to each club's Kaibaru bill.

    Each invoice is rounded on its own, then the yen fees are summed.
    Invoices stay unmarked when the club cannot be billed yet.
    """
    stripe.api_key = settings.STRIPE_SECRET_KEY
    start, end = month_bounds(year, month)

    club_ids = (
        Invoice.objects
        .filter(
            issued_at__gte=start,
            issued_at__lt=end,
            platform_fee_applied=False,
        )
        .exclude(payment_method="stripe")
        .exclude(status__in=["void", "draft"])
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
            Invoice.objects
            .select_for_update()
            .select_related("club")
            .filter(
                club_id=club_id,
                issued_at__gte=start,
                issued_at__lt=end,
                platform_fee_applied=False,
            )
            .exclude(payment_method="stripe")
            .exclude(status__in=["void", "draft"])
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
            stripe.InvoiceItem.create(
                customer=club.stripe_customer_id,
                amount=fee,
                currency="jpy",
                description=f"現金・振込の手数料（{year}年{month}月）",
                metadata={
                    "type": "cash_platform_fee",
                    "club_id": str(club_id),
                    "year": str(year),
                    "month": str(month),
                },
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


def _idempotency_key(club_id, year, month, invoice_ids):
    digest = hashlib.sha256(
        ",".join(str(invoice_id) for invoice_id in invoice_ids).encode()
    ).hexdigest()[:16]
    return f"cash_platform_fee_{club_id}_{year}{month:02d}_{digest}"
