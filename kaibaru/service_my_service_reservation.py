"""Logged-in booker: list and cancel own salon (ServiceReservation) bookings."""

from __future__ import annotations

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .cash_reservations import cancel_blocked_message
from .models import ServiceReservation
from .reservation_cancel import cancel_is_closed


class MyServiceReservationError(Exception):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _owned_q(user):
    email = (getattr(user, "email", None) or "").strip().lower()
    owned = Q(user=user)
    if email:
        owned |= Q(user__isnull=True, email=email)
    return owned


def _can_cancel(reservation, user):
    if reservation.status != ServiceReservation.Status.PAID:
        return False, "確定した予約のみキャンセルできます。"
    if reservation.canceled:
        return False, "この予約はすでにキャンセルされています。"
    if cancel_is_closed("service", reservation):
        return False, "この予約はすでに終了しているため、キャンセルできません。"
    owner = (
        getattr(user, "is_authenticated", False)
        and reservation.club.owner_id == getattr(user, "id", None)
    )
    if not owner:
        cash_block = cancel_blocked_message(reservation)
        if cash_block:
            return False, cash_block
    return True, ""


def my_reservation_payload(reservation, user):
    course = reservation.course
    worker = reservation.worker
    can_cancel, cancel_detail = _can_cancel(reservation, user)
    return {
        "id": reservation.id,
        "full_name": reservation.full_name,
        "email": reservation.email,
        "phone_number": reservation.phone_number or "",
        "reservation_date": reservation.reservation_date.isoformat(),
        "start_time": reservation.start_time.strftime("%H:%M"),
        "end_time": reservation.end_time.strftime("%H:%M"),
        "course_id": reservation.course_id,
        "course_name": (
            course.name if course is not None else (reservation.price_name or "コース")
        ),
        "worker_id": reservation.worker_id,
        "worker_name": worker.full_name if worker else "",
        "amount": reservation.amount or 0,
        "payment_method": reservation.payment_method,
        "status": reservation.status,
        "canceled": reservation.canceled,
        "can_cancel": can_cancel,
        "cancel_detail": cancel_detail if not can_cancel else "",
    }


def list_my_service_reservations(club, user):
    if user is None or not getattr(user, "is_authenticated", False):
        raise MyServiceReservationError("ログインが必要です。", 401)

    today = timezone.localdate()
    qs = (
        ServiceReservation.objects.filter(
            club=club,
            is_deleted=False,
            status=ServiceReservation.Status.PAID,
            canceled=False,
            no_show=False,
            reservation_date__gte=today,
        )
        .filter(_owned_q(user))
        .select_related("course", "worker", "club")
        .order_by("reservation_date", "start_time", "id")
    )
    return [my_reservation_payload(row, user) for row in qs]


def cancel_my_service_reservation(club, user, reservation_id):
    if user is None or not getattr(user, "is_authenticated", False):
        raise MyServiceReservationError("ログインが必要です。", 401)

    with transaction.atomic():
        reservation = (
            ServiceReservation.objects.select_for_update()
            .select_related("club", "course", "worker")
            .filter(
                id=reservation_id,
                club=club,
                is_deleted=False,
            )
            .filter(_owned_q(user))
            .first()
        )
        if reservation is None:
            raise MyServiceReservationError("予約が見つかりません。", 404)

        ok, detail = _can_cancel(reservation, user)
        if not ok:
            raise MyServiceReservationError(detail, 400)

        reservation.canceled = True
        reservation.save(update_fields=["canceled"])
        reservation_id = reservation.id

        def _send_canceled_email():
            from .tasks_emails import send_reservation_canceled_email

            send_reservation_canceled_email.delay("service", reservation_id)

        transaction.on_commit(_send_canceled_email)

    reservation = (
        ServiceReservation.objects.select_related("club", "course", "worker")
        .filter(id=reservation_id)
        .first()
    )
    return my_reservation_payload(reservation, user)
