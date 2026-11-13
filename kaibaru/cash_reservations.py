"""Pay-at-the-venue (cash) reservations for lessons and events.

A cash reservation is confirmed the moment it is made, so it holds a spot,
gets a confirmation email, and can be cancelled from the emailed link like
any other reservation. Its status is "paid" (confirmed). The money side is
tracked separately:

* paid_at is empty until the owner marks the cash as received.
* no_show is set by the owner when the person never came.
* Only reservations that are marked paid, and neither cancelled nor a
  no-show, count toward the 1% platform fee. The fee is added to the next
  platform invoice together with the cash member invoices.
"""

from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone

from .models import EventReservation, Reservation


STRIPE = "stripe"
CASH = "cash"

OPTION_STRIPE = "stripe"
OPTION_CASH = "cash"
OPTION_BOTH = "both"
PAYMENT_OPTIONS = (OPTION_STRIPE, OPTION_CASH, OPTION_BOTH)
DEFAULT_PAYMENT_OPTION = OPTION_STRIPE

# Past cash reservations stay on the owner's list this long, so cash that
# came in after the lesson (or a no-show) can still be recorded.
CASH_ACTION_WINDOW_DAYS = 60

ACTION_PAID = "paid"
ACTION_UNPAID = "unpaid"
ACTION_NO_SHOW = "no_show"
ACTION_CLEAR_NO_SHOW = "clear_no_show"
ACTIONS = (ACTION_PAID, ACTION_UNPAID, ACTION_NO_SHOW, ACTION_CLEAR_NO_SHOW)

KIND_LESSON = "lesson"
KIND_EVENT = "event"


class CashReservationError(Exception):
    def __init__(self, message, status_code=400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


# ---------------------------------------------------------------------
# Which payment methods a lesson or event offers
# ---------------------------------------------------------------------


def clean_payment_option(value, *, default=DEFAULT_PAYMENT_OPTION):
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text not in PAYMENT_OPTIONS:
        raise ValueError("支払い方法の設定が正しくありません。")
    return text


def allowed_payment_methods(club, option):
    """Methods a visitor may pick. Without Stripe the venue is the only way."""
    if not getattr(club, "stripe_account_id", None):
        return {CASH}
    if option not in PAYMENT_OPTIONS:
        option = DEFAULT_PAYMENT_OPTION
    if option == OPTION_BOTH:
        return {STRIPE, CASH}
    return {option}


def resolve_payment_method(*, club, option, requested, amount, subject):
    """Pick "stripe" or "cash" for a priced reservation.

    Free reservations never need a payment method, so None comes back and
    the caller keeps its existing free-reservation behaviour. A request that
    names no method gets card payment when the lesson or event offers it,
    which is what older clients expect.
    """
    if not amount or amount <= 0:
        return None

    allowed = allowed_payment_methods(club, option)
    method = str(requested or "").strip().lower()

    if not method:
        return STRIPE if STRIPE in allowed else CASH

    if method not in (STRIPE, CASH):
        raise ValueError("無効な支払い方法です。")

    if method in allowed:
        return method

    if method == CASH:
        raise ValueError(
            f"この{subject}では、現地でのお支払いを利用できません。"
        )

    if not getattr(club, "stripe_account_id", None):
        raise ValueError(
            "このクラブではオンライン決済が設定されていません。"
        )

    raise ValueError(
        f"この{subject}では、カード払いを利用できません。"
    )


def is_cash_reservation(reservation):
    return (
        (getattr(reservation, "payment_method", "") or "") == CASH
        and (reservation.amount or 0) > 0
    )


def cash_state(reservation):
    """None for non-cash rows, else "unpaid", "paid" or "no_show"."""
    if not is_cash_reservation(reservation):
        return None
    if reservation.no_show:
        return "no_show"
    if reservation.paid_at is not None:
        return "paid"
    return "unpaid"


# ---------------------------------------------------------------------
# Owner actions: mark paid, undo, no-show
# ---------------------------------------------------------------------


def has_started(reservation, *, now=None):
    """True once the lesson or event time has come."""
    now = now or timezone.now()

    if isinstance(reservation, EventReservation):
        return reservation.event.starts_at <= now

    start = datetime.combine(
        reservation.reservation_date,
        reservation.lesson.start_time,
    )
    start = timezone.make_aware(start, timezone.get_current_timezone())
    return start <= now


def _is_owner(reservation, user):
    return (
        user is not None
        and getattr(user, "is_authenticated", False)
        and reservation.club.owner_id == user.id
    )


def _live_cash_row(reservation):
    return (
        is_cash_reservation(reservation)
        and reservation.status == "paid"
        and not reservation.canceled
        and not reservation.is_deleted
        and not getattr(reservation, "refunded_at", None)
    )


def owner_cash_flags(reservation, user, *, now=None):
    """Which cash buttons the owner may use on this reservation."""
    flags = {
        "can_mark_paid": False,
        "can_unmark_paid": False,
        "can_mark_no_show": False,
        "can_clear_no_show": False,
    }

    if not _is_owner(reservation, user) or not _live_cash_row(reservation):
        return flags

    if reservation.no_show:
        flags["can_clear_no_show"] = True
        return flags

    if reservation.paid_at is not None:
        flags["can_unmark_paid"] = not reservation.platform_fee_applied
        return flags

    flags["can_mark_paid"] = True
    flags["can_mark_no_show"] = has_started(reservation, now=now)
    return flags


def cancel_blocked_message(reservation):
    """Why a participant cannot cancel a cash reservation, or None."""
    if not is_cash_reservation(reservation):
        return None
    if reservation.no_show:
        return (
            "欠席として記録された予約はキャンセルできません。"
            "ご不明な点はクラブにお問い合わせください。"
        )
    if reservation.paid_at is not None:
        return (
            "お支払い済みの予約はキャンセルできません。"
            "ご不明な点はクラブにお問い合わせください。"
        )
    return None


def _load_locked(kind, reservation_id):
    if kind == KIND_EVENT:
        return (
            EventReservation.objects
            .select_for_update()
            .select_related("club", "event")
            .filter(id=reservation_id, is_deleted=False)
            .first()
        )
    return (
        Reservation.objects
        .select_for_update()
        .select_related("club", "lesson")
        .filter(id=reservation_id, is_deleted=False)
        .first()
    )


def apply_cash_action(*, kind, reservation_id, action, user):
    """Run one owner action on a cash reservation and return its new state."""
    if action not in ACTIONS:
        raise CashReservationError("この操作は利用できません。", 404)

    if user is None or not getattr(user, "is_authenticated", False):
        raise CashReservationError("ログインが必要です。", 401)

    with transaction.atomic():
        reservation = _load_locked(kind, reservation_id)

        if reservation is None:
            raise CashReservationError("予約が見つかりません。", 404)

        if not _is_owner(reservation, user):
            raise CashReservationError(
                "操作できるのはクラブのオーナーだけです。",
                403,
            )

        if not is_cash_reservation(reservation):
            raise CashReservationError("現地払いの予約ではありません。")

        if reservation.canceled:
            raise CashReservationError(
                "キャンセルされた予約は変更できません。"
                "先にキャンセルを取り消してください。"
            )

        if reservation.status != "paid":
            raise CashReservationError("確定した予約ではありません。")

        _run_action(reservation, action)

    reservation = _load_locked(kind, reservation_id)
    return payload_for(reservation, user)


def _run_action(reservation, action):
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
        if not has_started(reservation):
            raise CashReservationError(
                "開始前の予約は欠席にできません。"
            )
        if not reservation.no_show:
            reservation.no_show = True
            reservation.save(update_fields=["no_show"])
        return

    if reservation.no_show:
        reservation.no_show = False
        reservation.save(update_fields=["no_show"])


def payload_for(reservation, user):
    return {
        "reservation_id": reservation.id,
        "payment_method": reservation.payment_method,
        "cash_status": cash_state(reservation),
        "paid_at": (
            reservation.paid_at.isoformat() if reservation.paid_at else None
        ),
        "no_show": reservation.no_show,
        "canceled": reservation.canceled,
        **owner_cash_flags(reservation, user),
    }


def action_window_start(today=None):
    today = today or timezone.localdate()
    return today - timedelta(days=CASH_ACTION_WINDOW_DAYS)
