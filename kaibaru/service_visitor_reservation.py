import secrets

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import Reservation
from .reservation_checkout_recovery import (
    read_stripe_payment,
    record_stripe_payment,
)
from .tasks_emails import send_visitor_reservation_confirmation_email
from .rules_eligibility import assert_visitor_eligible_for_lesson
from .rules_reservations import (
    assert_lesson_has_spots,
    assert_reservation_horizon,
    assert_visitor_reservation_caps,
    display_reservation_name,
    hold_cutoff_at,
    hold_is_active,
    make_reservation_key,
    request_is_fresh,
    resolve_max_days_ahead,
)
from .service_reservation_start import queue_reservation_start_email


class VisitorReservationService:

    @staticmethod
    def create_reservation(
        *,
        club,
        lesson,
        reservation_date,
        full_name,
        email,
        phone_number="",
        user=None,
        age=None,
        gender="",
    ):
        prepared = _prepare_visitor_request(
            club=club,
            lesson=lesson,
            reservation_date=reservation_date,
            full_name=full_name,
            email=email,
            phone_number=phone_number,
            user=user,
            age=age,
            gender=gender,
        )

        try:
            with transaction.atomic():
                token, reservation_ids = _save_visitor_reservation(
                    **prepared
                )
                queue_reservation_start_email(token)
        except IntegrityError:
            existing = (
                Reservation.objects
                .filter(reservation_key=prepared["reservation_key"])
                .first()
            )

            if existing and existing.start_token:
                from .tasks_emails import (
                    send_reservation_start_email,
                )

                send_reservation_start_email.delay(
                    existing.start_token
                )

                return _email_sent_result([existing.id])

            raise ValueError(
                "このレッスンの予約処理がすでに開始されています。"
            )

        return _email_sent_result(reservation_ids)


def _assert_visitor_room(
    *,
    lesson,
    club,
    reservation_date,
    email,
    hold_cutoff,
    exclude_ids=None,
):
    assert_lesson_has_spots(
        lesson=lesson,
        reservation_date=reservation_date,
        hold_cutoff=hold_cutoff,
        needed=1,
        exclude_ids=exclude_ids,
    )
    assert_visitor_reservation_caps(
        club=club,
        lesson=lesson,
        email=email,
        hold_cutoff=hold_cutoff,
        exclude_ids=exclude_ids,
        additional=1,
    )


def _email_sent_result(reservation_ids):
    return {
        "success": True,
        "email_sent": True,
        "requires_checkout": False,
        "paid": False,
        "reservation_id": reservation_ids[0],
        "reservation_ids": reservation_ids,
    }


def _prepare_visitor_request(
    *,
    club,
    lesson,
    reservation_date,
    full_name,
    email,
    phone_number,
    user,
    age,
    gender,
):
    if lesson.club_id != club.id:
        raise ValueError(
            "このレッスンは指定されたクラブに属していません。"
        )

    if club.visitor_reservations_disabled:
        raise ValueError(
            "現在、ビジター予約を受け付けていません。"
        )

    if lesson.visitor_reservation_disabled:
        raise ValueError(
            "このレッスンではビジター予約を受け付けていません。"
        )

    if lesson.visitor_reservation_price is not None:
        reservation_price = lesson.visitor_reservation_price
    else:
        reservation_price = club.visitor_reservation_price

    if reservation_price is None:
        raise ValueError(
            "このレッスンはビジター予約を受け付けていません。"
        )

    if lesson.weekday != reservation_date.weekday():
        raise ValueError(
            "選択した日付がレッスンの曜日と一致していません。"
        )

    assert_visitor_eligible_for_lesson(
        age=age,
        gender=gender,
        lesson=lesson,
    )

    assert_reservation_horizon(
        reservation_date=reservation_date,
        max_days_ahead=resolve_max_days_ahead(
            lesson.visitor_reservation_max_days_ahead,
            club.visitor_reservation_max_days_ahead,
        ),
    )

    if reservation_price > 0 and not club.stripe_account_id:
        raise ValueError(
            "このクラブではオンライン決済が設定されていません。"
        )

    full_name = display_reservation_name(full_name)
    phone_number = (phone_number or "").strip()
    gender = gender or ""

    if not full_name:
        raise ValueError("お名前を入力してください。")

    if user is not None:
        email = (user.email or "").strip().lower()

        if not email:
            raise ValueError(
                "アカウントにメールアドレスが登録されていません。"
            )
    else:
        email = (email or "").strip().lower()

        if not email:
            raise ValueError("メールアドレスを入力してください。")

    reservation_key = make_reservation_key(
        kind="visitor",
        lesson_id=lesson.id,
        reservation_date=reservation_date,
        email=email,
    )

    return {
        "club": club,
        "lesson": lesson,
        "reservation_date": reservation_date,
        "full_name": full_name,
        "email": email,
        "phone_number": phone_number,
        "user": user,
        "age": age,
        "gender": gender,
        "reservation_price": reservation_price,
        "reservation_key": reservation_key,
    }


def _save_visitor_reservation(
    *,
    club,
    lesson,
    reservation_date,
    full_name,
    email,
    phone_number,
    user,
    age,
    gender,
    reservation_price,
    reservation_key,
):
    now = timezone.now()
    hold_cutoff = hold_cutoff_at(now)

    existing = (
        Reservation.objects
        .select_for_update()
        .filter(reservation_key=reservation_key)
        .first()
    )

    if existing:
        if existing.status == Reservation.Status.PAID:
            if existing.is_deleted:
                existing.is_deleted = False
                existing.save(update_fields=["is_deleted"])

            if existing.canceled:
                from .reservation_cancel import canceled_rebook_message

                raise ValueError(canceled_rebook_message())

            raise ValueError("このレッスンはすでに予約済みです。")

        revived = existing.is_deleted
        if revived:
            payment_view = read_stripe_payment(
                existing,
                club.stripe_account_id,
                metadata_key="reservation_id",
            )
            if payment_view.paid:
                record_stripe_payment(existing, payment_view)
                send_visitor_reservation_confirmation_email.delay(
                    existing.id
                )
                raise ValueError("このレッスンはすでに予約済みです。")
            _assert_visitor_room(
                lesson=lesson,
                club=club,
                reservation_date=reservation_date,
                email=email,
                hold_cutoff=hold_cutoff,
            )
            if payment_view.open_url:
                existing.is_deleted = False
                existing.save(update_fields=["is_deleted"])
                raise ValueError(
                    "このレッスンの予約はお支払い手続き中です。"
                    "メールのリンクからお支払いを続けてください。"
                )
            if not payment_view.confirmed_unpaid:
                raise ValueError(
                    "決済の確認ができませんでした。"
                    "しばらくしてからもう一度お試しください。"
                )
            existing.is_deleted = False
        elif (
            existing.status == Reservation.Status.UNPAID
            and hold_is_active(existing, hold_cutoff)
        ):
            raise ValueError(
                "このレッスンの予約はお支払い手続き中です。"
                "メールのリンクからお支払いを続けてください。"
            )

        if (
            not revived
            and existing.status == Reservation.Status.NOT_STARTED
            and request_is_fresh(existing, now)
            and existing.start_token
        ):
            existing.full_name = full_name
            existing.phone_number = phone_number
            existing.age = age
            existing.gender = gender
            existing.amount = reservation_price
            existing.user = user
            existing.requested_at = now
            existing.save(
                update_fields=[
                    "full_name",
                    "phone_number",
                    "age",
                    "gender",
                    "amount",
                    "user",
                    "requested_at",
                ]
            )

            return existing.start_token, [existing.id]

        _assert_visitor_room(
            lesson=lesson,
            club=club,
            reservation_date=reservation_date,
            email=email,
            hold_cutoff=hold_cutoff,
        )

        token = secrets.token_urlsafe(32)
        existing.lesson = lesson
        existing.club = club
        existing.user = user
        existing.payment_method = "stripe"
        existing.reservation_type = Reservation.ReservationType.VISITOR
        existing.status = Reservation.Status.NOT_STARTED
        existing.full_name = full_name
        existing.email = email
        existing.phone_number = phone_number
        existing.age = age
        existing.gender = gender
        existing.amount = reservation_price
        existing.currency = "jpy"
        existing.reservation_date = reservation_date
        existing.start_token = token
        existing.requested_at = now
        existing.checkout_started_at = None
        existing.stripe_checkout_session_id = None
        existing.stripe_payment_intent_id = None
        existing.paid_at = None
        existing.confirmation_email_sent = False
        existing.save()

        return token, [existing.id]

    _assert_visitor_room(
        lesson=lesson,
        club=club,
        reservation_date=reservation_date,
        email=email,
        hold_cutoff=hold_cutoff,
    )

    token = secrets.token_urlsafe(32)

    reservation = Reservation.objects.create(
        lesson=lesson,
        club=club,
        member=None,
        user=user,
        payment_method="stripe",
        reservation_type=Reservation.ReservationType.VISITOR,
        status=Reservation.Status.NOT_STARTED,
        full_name=full_name,
        email=email,
        phone_number=phone_number,
        age=age,
        gender=gender,
        amount=reservation_price,
        currency="jpy",
        reservation_date=reservation_date,
        reservation_key=reservation_key,
        start_token=token,
        requested_at=now,
    )

    return token, [reservation.id]
