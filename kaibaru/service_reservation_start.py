import logging
from datetime import timedelta

import stripe
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Club, Lesson, Reservation
from .rules_eligibility import assert_visitor_eligible_for_lesson
from .rules_reservations import (
    assert_lesson_has_spots,
    assert_reservation_horizon,
    assert_visitor_reservation_caps,
    hold_cutoff_at,
    hold_is_active,
    normalize_reservation_name,
    request_is_fresh,
    resolve_max_days_ahead,
)
from .tasks_emails import (
    send_visitor_reservation_confirmation_email,
)


logger = logging.getLogger(__name__)

stripe.api_key = settings.STRIPE_SECRET_KEY

STRIPE_CHECKOUT_MINUTES = 30
CHECKOUT_CREATE_GRACE_SECONDS = 60

WEEKDAY_NAMES = [
    "月曜日",
    "火曜日",
    "水曜日",
    "木曜日",
    "金曜日",
    "土曜日",
    "日曜日",
]


class ReservationStartError(Exception):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.message = message
        self.retryable = retryable


def queue_reservation_start_email(start_token):
    from .tasks_emails import send_reservation_start_email

    transaction.on_commit(
        lambda token=start_token: send_reservation_start_email.delay(
            token
        )
    )


class ReservationStartService:

    @staticmethod
    def preview(token):
        reservations = _load(token)

        if not reservations:
            return {"mode": "invalid"}

        lesson = reservations[0].lesson
        club = reservations[0].club
        page = _page_context(reservations, lesson, club)

        if all(
            reservation.status == Reservation.Status.PAID
            for reservation in reservations
        ):
            return {"mode": "done", **page}

        if not _same_slot(reservations):
            return {
                "mode": "error",
                "message": "予約内容を確認できませんでした。",
                **page,
            }

        message = _unusable_reason(reservations, lesson, club)

        if message:
            return {
                "mode": "error",
                "message": message,
                **page,
            }

        return {"mode": "ready", **page}

    @staticmethod
    def begin(token):
        prepared = _prepare(token)

        if prepared["action"] == "done":
            return _public_result(prepared)

        if prepared["action"] == "reuse":
            checkout_url = _reusable_checkout_url(
                club=prepared["club"],
                session_id=prepared["session_id"],
            )

            if checkout_url:
                return {"redirect_url": checkout_url}

            prepared = _prepare(token, force_new=True)

            if prepared["action"] == "done":
                return _public_result(prepared)

        try:
            checkout_url = _open_new_checkout(prepared)
        except ReservationStartError:
            raise
        except Exception:
            logger.exception(
                "Failed to start reservation checkout token=%s",
                prepared.get("token"),
            )
            _expire_session(prepared)
            _restore(prepared.get("previous") or [])
            raise ReservationStartError(
                "決済ページを作成できませんでした。"
                "しばらくしてからもう一度お試しください。",
                retryable=True,
            )

        return {"redirect_url": checkout_url}


def complete_paid_checkout(
    *,
    reservation_ids,
    session_id,
    payment_intent_id,
    account_id,
):
    if not reservation_ids:
        return []

    newly_paid_ids = []

    with transaction.atomic():
        reservations = list(
            Reservation.objects
            .select_for_update()
            .filter(id__in=reservation_ids)
            .order_by("id")
        )

        if len(reservations) != len(set(reservation_ids)):
            logger.error(
                "[RESERVATION CHECKOUT] Missing reservations "
                "ids=%s session=%s",
                reservation_ids,
                session_id,
            )
            return []

        clubs = {
            club.id: club
            for club in Club.objects.filter(
                id__in={row.club_id for row in reservations}
            )
        }

        if any(
            clubs.get(row.club_id) is None
            or clubs[row.club_id].stripe_account_id != account_id
            for row in reservations
        ):
            logger.error(
                "[RESERVATION CHECKOUT] Account mismatch "
                "ids=%s session=%s account=%s",
                reservation_ids,
                session_id,
                account_id,
            )
            return []

        for reservation in reservations:
            if (
                reservation.stripe_checkout_session_id
                and reservation.stripe_checkout_session_id != session_id
            ):
                logger.error(
                    "[RESERVATION CHECKOUT] Session mismatch "
                    "reservation=%s expected=%s actual=%s",
                    reservation.id,
                    reservation.stripe_checkout_session_id,
                    session_id,
                )
                return []

        now = timezone.now()

        for reservation in reservations:
            if reservation.status == Reservation.Status.PAID:
                continue

            reservation.status = Reservation.Status.PAID
            reservation.paid_at = now
            update_fields = ["status", "paid_at"]

            if payment_intent_id:
                reservation.stripe_payment_intent_id = payment_intent_id
                update_fields.append("stripe_payment_intent_id")

            reservation.save(update_fields=update_fields)
            newly_paid_ids.append(reservation.id)

    for reservation_id in newly_paid_ids:
        send_visitor_reservation_confirmation_email.delay(
            reservation_id
        )

    return newly_paid_ids


def _load(token):
    if not token:
        return []

    return list(
        Reservation.objects
        .select_related("lesson", "club", "club__owner")
        .filter(start_token=token)
        .order_by("id")
    )


def _prepare(token, force_new=False):
    preview_rows = _load(token)

    if not preview_rows:
        raise ReservationStartError("このリンクは無効です。")

    with transaction.atomic():
        lesson = (
            Lesson.objects
            .select_for_update()
            .get(id=preview_rows[0].lesson_id)
        )
        reservations = list(
            Reservation.objects
            .select_for_update()
            .filter(start_token=token)
            .order_by("id")
        )

        if not reservations:
            raise ReservationStartError("このリンクは無効です。")

        club = (
            Club.objects
            .select_related("owner")
            .get(id=reservations[0].club_id)
        )

        for reservation in reservations:
            reservation.lesson = lesson
            reservation.club = club

        page = _page_context(reservations, lesson, club)

        if all(
            reservation.status == Reservation.Status.PAID
            for reservation in reservations
        ):
            return {"action": "done", **page}

        message = _unusable_reason(reservations, lesson, club)

        if message:
            raise ReservationStartError(message)

        if not _same_slot(reservations):
            raise ReservationStartError(
                "予約内容を確認できませんでした。"
            )

        open_rows = [
            reservation
            for reservation in reservations
            if reservation.status != Reservation.Status.PAID
        ]
        _revalidate(open_rows, lesson, club)

        now = timezone.now()
        hold_cutoff = hold_cutoff_at(now)
        open_ids = [reservation.id for reservation in open_rows]

        try:
            assert_lesson_has_spots(
                lesson=lesson,
                reservation_date=open_rows[0].reservation_date,
                hold_cutoff=hold_cutoff,
                needed=len(open_rows),
                exclude_ids=open_ids,
            )

            if (
                open_rows[0].reservation_type
                == Reservation.ReservationType.VISITOR
            ):
                assert_visitor_reservation_caps(
                    club=club,
                    lesson=lesson,
                    email=open_rows[0].email,
                    hold_cutoff=hold_cutoff,
                    exclude_ids=open_ids,
                    additional=len(open_rows),
                )
        except ValueError as exc:
            raise ReservationStartError(
                str(exc),
                retryable=True,
            ) from exc

        _assert_trials_available(open_rows)

        if sum(reservation.amount for reservation in open_rows) == 0:
            _mark_paid(open_rows, now)
            return {"action": "done", **page}

        if not club.stripe_account_id:
            raise ReservationStartError(
                "このクラブではオンライン決済が設定されていません。"
            )

        if not force_new and _can_reuse_session(open_rows, hold_cutoff):
            return {
                "action": "reuse",
                "session_id": open_rows[0].stripe_checkout_session_id,
                "club": club,
                "token": token,
            }

        if not force_new and _checkout_in_progress(
            open_rows,
            hold_cutoff,
            now,
        ):
            raise ReservationStartError(
                "決済ページを準備しています。"
                "数秒後にもう一度開いてください。",
                retryable=True,
            )

        previous = _snapshot(open_rows)
        _mark_unpaid(open_rows, now)

        return {
            "action": "create",
            "previous": previous,
            "reservation_ids": open_ids,
            "club": club,
            "lesson": lesson,
            "started_at": now,
            "token": token,
        }


def _public_result(prepared):
    return {
        key: value
        for key, value in prepared.items()
        if key != "action"
    } | {"mode": "done"}


def _open_new_checkout(prepared):
    reservations = list(
        Reservation.objects
        .filter(id__in=prepared["reservation_ids"])
        .order_by("id")
    )
    club = prepared["club"]
    lesson = prepared["lesson"]
    started_at = prepared["started_at"]

    session = stripe.checkout.Session.create(
        mode="payment",
        payment_method_types=["card"],
        customer_email=reservations[0].email,
        expires_at=int(
            (
                timezone.now()
                + timedelta(minutes=STRIPE_CHECKOUT_MINUTES)
            ).timestamp()
        ),
        line_items=[
            {
                "price_data": {
                    "currency": reservation.currency or "jpy",
                    "product_data": {
                        "name": (
                            f"{lesson.title}（{reservation.full_name}）"
                        )[:250],
                    },
                    "unit_amount": reservation.amount,
                },
                "quantity": 1,
            }
            for reservation in reservations
        ],
        metadata={
            "reservation_ids": ",".join(
                str(reservation.id) for reservation in reservations
            ),
            "reservation_id": str(reservations[0].id),
            "club_id": str(club.id),
            "lesson_id": str(lesson.id),
            "reservation_type": reservations[0].reservation_type,
        },
        success_url=(
            f"https://{club.subdomain}.kaibaru.jp/"
            f"?reservation=success"
            f"&reservation_id={reservations[0].id}"
        ),
        cancel_url=(
            f"https://{club.subdomain}.kaibaru.jp/"
            f"?reservation=cancel"
            f"&reservation_id={reservations[0].id}"
        ),
        stripe_account=club.stripe_account_id,
        idempotency_key=(
            f"reservation_start_{prepared['token']}_"
            f"{int(started_at.timestamp())}"
        )[:255],
    )

    prepared["session"] = session

    updated = (
        Reservation.objects
        .filter(
            id__in=prepared["reservation_ids"],
            status=Reservation.Status.UNPAID,
        )
        .update(stripe_checkout_session_id=session.id)
    )

    if updated != len(prepared["reservation_ids"]):
        raise RuntimeError(
            "Reservation changed before checkout was saved."
        )

    return session.url


def _expire_session(prepared):
    session = prepared.get("session")

    if session is None:
        return

    club = prepared.get("club")

    if club is None or not club.stripe_account_id:
        return

    try:
        stripe.checkout.Session.expire(
            session.id,
            stripe_account=club.stripe_account_id,
        )
    except Exception:
        logger.exception(
            "Failed to expire checkout session %s",
            session.id,
        )


def _reusable_checkout_url(*, club, session_id):
    session = stripe.checkout.Session.retrieve(
        session_id,
        stripe_account=club.stripe_account_id,
    )

    if session.status == "open" and session.url:
        return session.url

    return None


def _page_context(reservations, lesson, club):
    total = sum(reservation.amount for reservation in reservations)
    is_free = total == 0
    kind = (
        "体験予約"
        if reservations[0].reservation_type
        == Reservation.ReservationType.TRIAL
        else "ビジター予約"
    )

    return {
        "club_name": club.title or club.subdomain or "クラブ",
        "lesson_title": lesson.title,
        "when_text": _format_when(
            lesson,
            reservations[0].reservation_date,
        ),
        "people": [
            {
                "name": reservation.full_name,
                "age": reservation.age,
            }
            for reservation in reservations
        ],
        "total_amount": total,
        "is_free": is_free,
        "kind": kind,
        "home_url": f"https://{club.subdomain}.kaibaru.jp/",
        "continue_label": (
            "予約を確定する" if is_free else "支払いへ進む"
        ),
    }


def _format_when(lesson, reservation_date):
    weekday = WEEKDAY_NAMES[reservation_date.weekday()]
    date_text = reservation_date.strftime("%Y年%m月%d日")
    start = lesson.start_time.strftime("%H:%M")
    end = lesson.end_time.strftime("%H:%M")
    return f"{date_text}（{weekday}） {start}〜{end}"


def _same_slot(reservations):
    return (
        len({reservation.lesson_id for reservation in reservations}) == 1
        and len({
            reservation.reservation_date for reservation in reservations
        }) == 1
        and len({
            reservation.reservation_type for reservation in reservations
        }) == 1
        and len({
            reservation.email.lower() for reservation in reservations
        }) == 1
    )


def _unusable_reason(reservations, lesson, club):
    if reservations[0].reservation_date < timezone.localdate():
        return (
            "この予約日はすでに過ぎています。"
            "もう一度お申し込みください。"
        )

    reservation_type = reservations[0].reservation_type

    if reservation_type == Reservation.ReservationType.TRIAL and (
        club.trials_disabled or lesson.trial_disabled
    ):
        return "現在、体験予約を受け付けていません。"

    if reservation_type == Reservation.ReservationType.VISITOR and (
        club.visitor_reservations_disabled
        or lesson.visitor_reservation_disabled
    ):
        return "現在、ビジター予約を受け付けていません。"

    pending = [
        reservation
        for reservation in reservations
        if reservation.status == Reservation.Status.NOT_STARTED
    ]

    if pending and any(
        not request_is_fresh(reservation) for reservation in pending
    ):
        return (
            "このリンクの有効期限が切れています。"
            "もう一度お申し込みください。"
        )

    return None


def _revalidate(reservations, lesson, club):
    for reservation in reservations:
        try:
            assert_visitor_eligible_for_lesson(
                age=reservation.age,
                gender=reservation.gender,
                lesson=lesson,
            )
        except ValueError as exc:
            raise ReservationStartError(str(exc)) from exc

    if (
        reservations[0].reservation_type
        == Reservation.ReservationType.VISITOR
    ):
        try:
            assert_reservation_horizon(
                reservation_date=reservations[0].reservation_date,
                max_days_ahead=resolve_max_days_ahead(
                    lesson.visitor_reservation_max_days_ahead,
                    club.visitor_reservation_max_days_ahead,
                ),
            )
        except ValueError as exc:
            raise ReservationStartError(str(exc)) from exc


def _assert_trials_available(reservations):
    open_ids = [reservation.id for reservation in reservations]
    now_cutoff = hold_cutoff_at()

    for reservation in reservations:
        if (
            reservation.reservation_type
            != Reservation.ReservationType.TRIAL
        ):
            continue

        normalized = normalize_reservation_name(reservation.full_name)
        others = (
            Reservation.objects
            .filter(
                club_id=reservation.club_id,
                reservation_type=Reservation.ReservationType.TRIAL,
                email__iexact=reservation.email,
            )
            .exclude(id__in=open_ids)
        )

        for other in others:
            if (
                normalize_reservation_name(other.full_name)
                != normalized
            ):
                continue

            if other.status == Reservation.Status.PAID:
                raise ReservationStartError(
                    f"{reservation.full_name}さんは、このクラブで"
                    "体験予約をすでにご利用済みです。"
                )

            if (
                other.status == Reservation.Status.UNPAID
                and hold_is_active(other, now_cutoff)
            ):
                raise ReservationStartError(
                    f"{reservation.full_name}さんの体験予約は"
                    "お支払い手続き中です。"
                    "メールのリンクからお支払いを続けてください。"
                )


def _mark_paid(reservations, now):
    reservation_ids = []

    for reservation in reservations:
        reservation.status = Reservation.Status.PAID
        reservation.paid_at = now
        reservation.save(update_fields=["status", "paid_at"])
        reservation_ids.append(reservation.id)

    def send_confirmations(ids=tuple(reservation_ids)):
        for reservation_id in ids:
            send_visitor_reservation_confirmation_email.delay(
                reservation_id
            )

    transaction.on_commit(send_confirmations)


def _mark_unpaid(reservations, now):
    for reservation in reservations:
        reservation.status = Reservation.Status.UNPAID
        reservation.checkout_started_at = now
        reservation.stripe_checkout_session_id = None
        reservation.save(
            update_fields=[
                "status",
                "checkout_started_at",
                "stripe_checkout_session_id",
            ]
        )


def _can_reuse_session(reservations, hold_cutoff):
    if not all(
        reservation.status == Reservation.Status.UNPAID
        and hold_is_active(reservation, hold_cutoff)
        for reservation in reservations
    ):
        return False

    session_ids = [
        reservation.stripe_checkout_session_id
        for reservation in reservations
    ]

    return bool(session_ids) and all(session_ids) and (
        len(set(session_ids)) == 1
    )


def _checkout_in_progress(reservations, hold_cutoff, now):
    if not all(
        reservation.status == Reservation.Status.UNPAID
        and hold_is_active(reservation, hold_cutoff)
        and not reservation.stripe_checkout_session_id
        for reservation in reservations
    ):
        return False

    grace = timedelta(seconds=CHECKOUT_CREATE_GRACE_SECONDS)

    return all(
        now - (reservation.checkout_started_at or reservation.created_at)
        < grace
        for reservation in reservations
    )


def _snapshot(reservations):
    return [
        {
            "id": reservation.id,
            "status": reservation.status,
            "checkout_started_at": reservation.checkout_started_at,
            "stripe_checkout_session_id": (
                reservation.stripe_checkout_session_id
            ),
        }
        for reservation in reservations
    ]


def _restore(previous):
    for item in previous:
        Reservation.objects.filter(id=item["id"]).update(
            status=item["status"],
            checkout_started_at=item["checkout_started_at"],
            stripe_checkout_session_id=item[
                "stripe_checkout_session_id"
            ],
        )
