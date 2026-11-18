"""Owner/worker board for salon (ServiceReservation) bookings.

Lists confirmed reservations in the cash-action window and lets staff
mark cash received, no-show, cancel, or restore. Stripe refunds are not
handled here — cancel only frees the slot.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .cash_reservations import (
    ACTION_CLEAR_NO_SHOW,
    ACTION_NO_SHOW,
    ACTION_PAID,
    ACTION_UNPAID,
    ACTIONS,
    CashReservationError,
    action_window_start,
    cash_state,
    is_cash_reservation,
)
from .manager_permissions import has_permission
from .models import Member, ServiceReservation


MANAGE_ACTIONS = ACTIONS + ("cancel", "restore", "reopen")


def resolve_service_actor(user, club):
    """Return staff/worker context for the salon reservation board."""
    if not user or not getattr(user, "is_authenticated", False) or club is None:
        return None

    if has_permission(user, club, "lessons"):
        return {"role": "staff", "member": None}

    member = (
        Member.objects.filter(
            club=club,
            user=user,
            is_worker=True,
        )
        .order_by("id")
        .first()
    )
    if member:
        return {"role": "worker", "member": member}
    return None


def can_manage_service_reservation(reservation, user, *, actor=None):
    actor = actor or resolve_service_actor(user, reservation.club)
    if not actor:
        return False
    if actor["role"] == "staff":
        return True
    member = actor.get("member")
    return bool(member and reservation.worker_id == member.id)


def _live_confirmed(reservation):
    return (
        reservation.status == ServiceReservation.Status.PAID
        and not reservation.is_deleted
        and not getattr(reservation, "refunded_at", None)
    )


def manage_flags(reservation, user, *, actor=None):
    flags = {
        "can_mark_paid": False,
        "can_unmark_paid": False,
        "can_mark_no_show": False,
        "can_clear_no_show": False,
        "can_cancel": False,
        "can_restore": False,
    }
    if not can_manage_service_reservation(reservation, user, actor=actor):
        return flags
    if not _live_confirmed(reservation):
        return flags

    if reservation.canceled:
        flags["can_restore"] = True
        return flags

    flags["can_cancel"] = not reservation.no_show

    if not is_cash_reservation(reservation):
        return flags

    if reservation.no_show:
        flags["can_clear_no_show"] = True
        flags["can_cancel"] = False
        return flags

    if reservation.paid_at is not None:
        flags["can_unmark_paid"] = not reservation.platform_fee_applied
        return flags

    flags["can_mark_paid"] = True
    flags["can_mark_no_show"] = True
    return flags


def reservation_payload(reservation, user, *, actor=None):
    course = reservation.course
    worker = reservation.worker
    return {
        "id": reservation.id,
        "full_name": reservation.full_name,
        "email": reservation.email,
        "phone_number": reservation.phone_number or "",
        "reservation_date": reservation.reservation_date.isoformat(),
        "start_time": reservation.start_time.strftime("%H:%M"),
        "end_time": reservation.end_time.strftime("%H:%M"),
        "course_id": reservation.course_id,
        "course_name": course.name if course else reservation.price_name,
        "worker_id": reservation.worker_id,
        "worker_name": worker.full_name if worker else "",
        "amount": reservation.amount or 0,
        "payment_method": reservation.payment_method,
        "status": reservation.status,
        "canceled": reservation.canceled,
        "no_show": reservation.no_show,
        "paid_at": (
            reservation.paid_at.isoformat() if reservation.paid_at else None
        ),
        "cash_status": cash_state(reservation),
        "created_at": (
            reservation.created_at.isoformat() if reservation.created_at else None
        ),
        **manage_flags(reservation, user, actor=actor),
    }


def list_manageable_reservations(club, user, *, actor=None):
    actor = actor or resolve_service_actor(user, club)
    if not actor:
        raise CashReservationError("許可なし", 403)

    window_start = action_window_start()
    qs = (
        ServiceReservation.objects.filter(
            club=club,
            is_deleted=False,
            status=ServiceReservation.Status.PAID,
            reservation_date__gte=window_start,
            canceled=False,
            no_show=False,
        )
        .exclude(
            Q(payment_method=ServiceReservation.PaymentMethod.CASH)
            & Q(paid_at__isnull=False)
            & Q(amount__gt=0)
        )
        .select_related("course", "worker")
        .order_by("reservation_date", "start_time", "id")
    )
    if actor["role"] == "worker" and actor.get("member"):
        qs = qs.filter(worker_id=actor["member"].id)

    return [reservation_payload(row, user, actor=actor) for row in qs]


def apply_service_manage_action(*, reservation_id, action, user, club):
    if action not in MANAGE_ACTIONS:
        raise CashReservationError("この操作は利用できません。", 404)

    if user is None or not getattr(user, "is_authenticated", False):
        raise CashReservationError("ログインが必要です。", 401)

    actor = resolve_service_actor(user, club)
    if not actor:
        raise CashReservationError("許可なし", 403)

    with transaction.atomic():
        reservation = (
            ServiceReservation.objects.select_for_update()
            .select_related("club", "course", "worker")
            .filter(id=reservation_id, club=club, is_deleted=False)
            .first()
        )
        if reservation is None:
            raise CashReservationError("予約が見つかりません。", 404)

        if not can_manage_service_reservation(reservation, user, actor=actor):
            raise CashReservationError("この予約を操作する権限がありません。", 403)

        if reservation.status != ServiceReservation.Status.PAID:
            raise CashReservationError("確定した予約ではありません。")

        if action == "cancel":
            _cancel(reservation)
        elif action == "restore":
            _restore(reservation)
        elif action == "reopen":
            _reopen(reservation)
        else:
            if not is_cash_reservation(reservation):
                raise CashReservationError("現地払いの予約ではありません。")
            if reservation.canceled:
                raise CashReservationError(
                    "キャンセルされた予約は変更できません。"
                    "先にキャンセルを取り消してください。"
                )
            # has_started in cash_reservations understands ServiceReservation
            # after the model branch was added; no_show uses that check.
            _run_service_cash_action(reservation, action)

    reservation = (
        ServiceReservation.objects.select_related("club", "course", "worker")
        .filter(id=reservation_id)
        .first()
    )
    return reservation_payload(reservation, user, actor=actor)


def _cancel(reservation):
    if reservation.canceled:
        return
    if reservation.no_show:
        raise CashReservationError(
            "欠席として記録された予約はキャンセルできません。"
            "先に欠席を取り消してください。"
        )
    reservation.canceled = True
    reservation.save(update_fields=["canceled"])


def _restore(reservation):
    if not reservation.canceled:
        return
    # Restoring may collide with another booking on the same worker/slot.
    # Keep it simple: allow restore; slot checks happen on new bookings.
    reservation.canceled = False
    reservation.save(update_fields=["canceled"])


def reopen_still_allowed(reservation, today=None):
    """True through the reservation date. The next day is too late."""
    today = today or timezone.localdate()
    return today <= reservation.reservation_date


def _reopen(reservation, today=None):
    """Put a paid, canceled, or no-show booking back on the manage board.

    Cash that was marked received is unmarked. Cancel and no-show are
    cleared. Stripe charges are not undone.
    """
    if not reopen_still_allowed(reservation, today):
        raise CashReservationError(
            "予約日の翌日以降は、予約管理に戻せません。"
        )

    cash_paid = (
        is_cash_reservation(reservation) and reservation.paid_at is not None
    )
    if not reservation.canceled and not reservation.no_show and not cash_paid:
        raise CashReservationError("この予約は戻せません。")
    if cash_paid and reservation.platform_fee_applied:
        raise CashReservationError(
            "この支払いはすでにKaibaruの手数料に含まれているため、"
            "取り消せません。"
        )

    update_fields = []
    if reservation.canceled:
        reservation.canceled = False
        update_fields.append("canceled")
    if reservation.no_show:
        reservation.no_show = False
        update_fields.append("no_show")
    if cash_paid:
        reservation.paid_at = None
        update_fields.append("paid_at")
    reservation.save(update_fields=update_fields)


def _run_service_cash_action(reservation, action):
    """Same money rules as lessons. A cash booking can be marked no-show before it starts."""
    if action == ACTION_PAID:
        if reservation.no_show:
            raise CashReservationError(
                "欠席として記録されています。"
                "先に欠席を取り消してください。"
            )
        if reservation.paid_at is None:
            reservation.paid_at = timezone.now()
            reservation.save(update_fields=["paid_at"])
        return

    if action == ACTION_UNPAID:
        if reservation.paid_at is None:
            return
        if reservation.platform_fee_applied:
            raise CashReservationError(
                "この支払いはすでにKaibaruの手数料に含まれているため、"
                "取り消せません。"
            )
        reservation.paid_at = None
        reservation.save(update_fields=["paid_at"])
        return

    if action == ACTION_NO_SHOW:
        if reservation.paid_at is not None:
            raise CashReservationError(
                "お支払い済みの予約は欠席にできません。"
                "先に支払いを取り消してください。"
            )
        if not reservation.no_show:
            reservation.no_show = True
            reservation.save(update_fields=["no_show"])
        return

    if action == ACTION_CLEAR_NO_SHOW:
        if reservation.no_show:
            reservation.no_show = False
            reservation.save(update_fields=["no_show"])
        return

    raise CashReservationError("この操作は利用できません。", 404)
