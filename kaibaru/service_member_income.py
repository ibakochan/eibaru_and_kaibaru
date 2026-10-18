"""Paid member invoices for one club, grouped by the month they were paid.

The month is the latest succeeded payment time. Cash payments often have no
paid_at, so the payment row's issued_at is used. Invoices with no payment
row fall back to issued_at.
"""

from django.utils import timezone

from .models import Invoice

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

    entries = []
    for year, month in sorted(months, reverse=True)[:MONTH_LIMIT]:
        rows = months[(year, month)]
        rows.sort(
            key=lambda invoice: (invoice.income_at or invoice.issued_at, invoice.id),
            reverse=True,
        )
        entries.append({
            "year": year,
            "month": month,
            "invoice_count": len(rows),
            "total_yen": sum(_amount(invoice) for invoice in rows),
            "invoices": rows,
        })
    return entries
