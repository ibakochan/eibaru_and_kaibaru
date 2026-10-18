"""Paid member invoices for one club, grouped by the month they were paid.

The month is the latest succeeded payment time. Cash payments often have no
paid_at, so the payment row's issued_at is used. Invoices with no payment
row fall back to issued_at.
"""

from django.utils import timezone

from .models import Invoice
from .service_paid_reservations import paid_reservations_for_clubs

MONTH_LIMIT = 36


def _income_at(invoice):
    stamps = []
    for payment in invoice.payment_set.all():
        if payment.status != "succeeded":
            continue
        stamp = payment.paid_at or payment.issued_at
        if stamp:
            stamps.append(stamp)
    if stamps:
        return max(stamps)
    return invoice.issued_at


def _amount(invoice):
    try:
        return int(invoice.amount_paid or invoice.amount_due or 0)
    except (TypeError, ValueError):
        return 0


def member_income_for_club(club):
    invoices = (
        Invoice.objects.filter(club=club, status="paid")
        .select_related("subscription", "payer", "club")
        .prefetch_related("items", "payment_set")
    )
    months = {}
    for invoice in invoices:
        paid_at = _income_at(invoice)
        invoice.income_at = paid_at
        local = timezone.localtime(paid_at) if paid_at else None
        if not local:
            continue
        months.setdefault((local.year, local.month), []).append(invoice)

    reservation_months = {}
    for reservation in paid_reservations_for_clubs([club.id]):
        income_at = reservation.get("income_at")
        local = timezone.localtime(income_at) if income_at else None
        if not local:
            continue
        reservation.pop("income_at", None)
        reservation.pop("payment_intent_id", None)
        reservation_months.setdefault((local.year, local.month), []).append(reservation)

    entries = []
    keys = sorted(set(months) | set(reservation_months), reverse=True)[:MONTH_LIMIT]
    for year, month in keys:
        rows = months.get((year, month), [])
        reservations = reservation_months.get((year, month), [])
        rows.sort(
            key=lambda invoice: (invoice.income_at or invoice.issued_at, invoice.id),
            reverse=True,
        )
        reservations.sort(key=lambda row: (row.get("paid_on") or "", row["id"]), reverse=True)
        invoice_total = sum(_amount(invoice) for invoice in rows)
        reservation_total = sum(int(row.get("amount") or 0) for row in reservations)
        entries.append({
            "year": year,
            "month": month,
            "invoice_count": len(rows),
            "reservation_count": len(reservations),
            "invoice_total_yen": invoice_total,
            "reservation_total_yen": reservation_total,
            "total_yen": invoice_total + reservation_total,
            "invoices": rows,
            "reservations": reservations,
        })
    return entries
