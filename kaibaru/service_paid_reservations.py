"""Paid lesson, trial, and event reservations charged through Stripe.

Ticket reservations are excluded. Refunded and deleted rows are excluded.
The month is paid_at, or created_at when paid_at was not stored.
"""

from django.db.models.functions import Coalesce
from django.utils import timezone

from .charge_amounts import platform_fee_yen
from .models import EventReservation, Reservation
from .service_cash_platform_fee import month_bounds


def _rows(model, club_ids, *, start, end, kind_for):
    if not club_ids:
        return []
    related = "lesson" if model is Reservation else "event"
    queryset = (
        model.objects.filter(
            club_id__in=list(club_ids),
            status="paid",
            is_deleted=False,
            refunded_at__isnull=True,
            amount__gt=0,
        )
        .exclude(payment_method="ticket")
        .select_related(related)
        .annotate(income_at=Coalesce("paid_at", "created_at"))
    )
    if start is not None and end is not None:
        queryset = queryset.filter(income_at__gte=start, income_at__lt=end)
    rows = []
    for reservation in queryset:
        income_at = reservation.income_at
        local = timezone.localtime(income_at) if income_at else None
        rows.append({
            "id": f"{kind_for(reservation)}-{reservation.id}",
            "kind": kind_for(reservation),
            "title": _title(reservation),
            "full_name": reservation.full_name,
            "amount": int(reservation.amount or 0),
            "paid_on": local.date().isoformat() if local else None,
            "income_at": income_at,
            "payment_intent_id": reservation.stripe_payment_intent_id or "",
        })
    return rows


def _lesson_kind(reservation):
    if reservation.reservation_type == Reservation.ReservationType.TRIAL:
        return "trial"
    return "lesson"


def _event_kind(_reservation):
    return "event"


def _title(reservation):
    lesson = getattr(reservation, "lesson", None)
    if lesson is not None:
        return lesson.title or ""
    event = getattr(reservation, "event", None)
    if event is not None:
        return event.title or ""
    return ""


def paid_reservation_counts_by_month(club_ids):
    """Paid reservation counts per calendar month, without building row payloads."""
    if not club_ids:
        return {}
    counts = {}
    for model in (Reservation, EventReservation):
        stamps = (
            model.objects.filter(
                club_id__in=list(club_ids),
                status="paid",
                is_deleted=False,
                refunded_at__isnull=True,
                amount__gt=0,
            )
            .exclude(payment_method="ticket")
            .annotate(income_at=Coalesce("paid_at", "created_at"))
            .values_list("income_at", flat=True)
        )
        for income_at in stamps:
            local = timezone.localtime(income_at) if income_at else None
            if not local:
                continue
            key = (local.year, local.month)
            counts[key] = counts.get(key, 0) + 1
    return counts


def paid_reservations_for_clubs(club_ids, year=None, month=None):
    start = end = None
    if year and month:
        start, end = month_bounds(year, month)
    return (
        _rows(Reservation, club_ids, start=start, end=end, kind_for=_lesson_kind)
        + _rows(EventReservation, club_ids, start=start, end=end, kind_for=_event_kind)
    )


def reservation_application_fee_yen(rows):
    """1% actually taken: one rounding per Stripe charge.

    Reservations that share a payment intent were one checkout, so their
    amounts are added before the 1% is rounded.
    """
    grouped = {}
    loose = []
    for row in rows:
        try:
            amount = int(row.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0
        if amount <= 0:
            continue
        payment_intent_id = row.get("payment_intent_id") or ""
        if payment_intent_id:
            grouped[payment_intent_id] = grouped.get(payment_intent_id, 0) + amount
        else:
            loose.append(amount)
    fee = 0
    for amount in list(grouped.values()) + loose:
        fee += platform_fee_yen(amount)
    return fee


def kaibaru_stripe_application_fee_yen(club_ids, year, month):
    """1% Kaibaru took on paid Stripe member invoices and reservations."""
    from .models import Invoice

    if not club_ids or not year or not month:
        return 0
    start, end = month_bounds(year, month)
    fee = 0
    amounts = (
        Invoice.objects.filter(
            club_id__in=list(club_ids),
            status="paid",
            payment_method="stripe",
        )
        .annotate(fee_effective_at=Coalesce("converted_at", "issued_at"))
        .filter(fee_effective_at__gte=start, fee_effective_at__lt=end)
        .values_list("amount_paid", "amount_due")
    )
    for amount_paid, amount_due in amounts:
        try:
            amount = int(amount_paid or amount_due or 0)
        except (TypeError, ValueError):
            amount = 0
        if amount > 0:
            fee += platform_fee_yen(amount)
    reservations = paid_reservations_for_clubs(club_ids, year, month)
    return fee + reservation_application_fee_yen(reservations)
