"""Salon reservation history, one month at a time.

The index is a single grouped query: which months have confirmed
bookings, and how many. Opening a month loads that month's rows with
course and worker joined in, then splits each day into paid and unpaid.
"""

from django.db.models import Count
from django.db.models.functions import ExtractMonth, ExtractYear
from django.utils import timezone

from .cash_reservations import is_cash_reservation
from .manager_permissions import has_permission
from .models import Member, ServiceReservation
from .service_reservation_manage import reopen_still_allowed

WEEKDAYS = ("月", "火", "水", "木", "金", "土", "日")


def can_view_service_reservation_history(user, club):
    """Owners and managers. Same people who see 予約設定."""
    if has_permission(user, club, "lessons"):
        return True
    if not user or not getattr(user, "is_authenticated", False) or club is None:
        return False
    return Member.objects.filter(
        club=club,
        user=user,
        is_manager=True,
    ).exists()


def reservation_collected(reservation):
    """Money was taken, a ticket was used, or nothing was owed.

    Cash stays unpaid until the club marks it received. A refund still
    belongs with the paid rows; the amount is left out of the day's total.
    """
    if reservation.payment_method == ServiceReservation.PaymentMethod.TICKET:
        return True
    if int(reservation.amount or 0) == 0:
        return True
    return reservation.paid_at is not None


def can_reopen_reservation(reservation, today=None):
    """Canceled or cash-paid rows can return to 予約管理 through that date."""
    if not reopen_still_allowed(reservation, today):
        return False
    if reservation.refunded_at:
        return False
    cash_paid = (
        is_cash_reservation(reservation) and reservation.paid_at is not None
    )
    if cash_paid and reservation.platform_fee_applied:
        return False
    return bool(reservation.canceled or cash_paid)


def reservation_earned_yen(reservation):
    if not reservation_collected(reservation):
        return 0
    if reservation.refunded_at or reservation.canceled or reservation.no_show:
        return 0
    return int(reservation.amount or 0)


def _confirmed(club):
    return ServiceReservation.objects.filter(
        club=club,
        is_deleted=False,
        status=ServiceReservation.Status.PAID,
    )


def service_reservation_history_index(club, today=None):
    today = today or timezone.localdate()
    rows = (
        _confirmed(club)
        .annotate(
            year=ExtractYear("reservation_date"),
            month=ExtractMonth("reservation_date"),
        )
        .values("year", "month")
        .annotate(reservation_count=Count("id"))
        .order_by("-year", "-month")
    )
    months = [
        {
            "year": int(row["year"]),
            "month": int(row["month"]),
            "reservation_count": int(row["reservation_count"]),
        }
        for row in rows
    ]
    current = (today.year, today.month)
    if not any((row["year"], row["month"]) == current for row in months):
        months.append({
            "year": today.year,
            "month": today.month,
            "reservation_count": 0,
        })
    for row in months:
        row["is_current"] = (row["year"], row["month"]) == current
    months.sort(key=lambda row: (row["year"], row["month"]), reverse=True)
    return months


def _reservation_row(reservation, today=None):
    course = reservation.course
    worker = reservation.worker
    course_name = course.name if course else ""
    price_name = reservation.price_name or course_name
    return {
        "id": reservation.id,
        "full_name": reservation.full_name,
        "email": reservation.email,
        "phone_number": reservation.phone_number or "",
        "start_time": reservation.start_time.strftime("%H:%M"),
        "end_time": reservation.end_time.strftime("%H:%M"),
        "course_name": course_name,
        "price_name": price_name,
        "worker_name": worker.full_name if worker else "",
        "amount": int(reservation.amount or 0),
        "payment_method": reservation.payment_method,
        "paid": reservation_collected(reservation),
        "canceled": reservation.canceled,
        "no_show": reservation.no_show,
        "refunded": reservation.refunded_at is not None,
        "can_reopen": can_reopen_reservation(reservation, today),
    }


def service_reservation_history_month(club, year, month, today=None):
    today = today or timezone.localdate()
    start, end = _month_bounds(year, month)
    reservations = list(
        _confirmed(club)
        .filter(reservation_date__gte=start, reservation_date__lt=end)
        .select_related("course", "worker")
        .order_by("reservation_date", "start_time", "id")
    )

    by_date = {}
    for reservation in reservations:
        day = by_date.setdefault(
            reservation.reservation_date,
            {"paid": [], "unpaid": [], "earned_yen": 0},
        )
        row = _reservation_row(reservation, today)
        if row["paid"]:
            day["paid"].append(row)
        else:
            day["unpaid"].append(row)
        day["earned_yen"] += reservation_earned_yen(reservation)

    days = []
    for day, bucket in sorted(by_date.items(), reverse=True):
        days.append({
            "date": day.isoformat(),
            "weekday": WEEKDAYS[day.weekday()],
            "earned_yen": bucket["earned_yen"],
            "paid": bucket["paid"],
            "unpaid": bucket["unpaid"],
        })
    return {
        "year": year,
        "month": month,
        "earned_yen": sum(day["earned_yen"] for day in days),
        "days": days,
    }


def _month_bounds(year, month):
    from datetime import date

    start = date(year, month, 1)
    if month == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, month + 1, 1)
    return start, end
