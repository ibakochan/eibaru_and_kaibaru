# service_reservation.py

import hashlib
import stripe

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import Lesson, Reservation

from datetime import datetime, timezone as dt_timezone

from .rules_eligibility import assert_visitor_eligible_for_lesson
from .rules_reservations import (
    assert_reservation_horizon,
    assert_visitor_reservation_caps,
    resolve_max_days_ahead,
)

STRIPE_CHECKOUT_MINUTES = 30
RESERVATION_HOLD_MINUTES = 31


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
        if lesson.club_id != club.id:
            raise ValueError(
                "このレッスンは指定されたクラブに属していません。"
            )

        # -------------------------
        # Reservation availability
        # -------------------------

        if club.visitor_reservations_disabled:
            raise ValueError(
                "現在、ビジター予約を受け付けていません。"
            )

        if lesson.visitor_reservation_disabled:
            raise ValueError(
                "このレッスンではビジター予約を受け付けていません。"
            )

        # -------------------------
        # Determine price
        # -------------------------

        if lesson.visitor_reservation_price is not None:
            reservation_price = (
                lesson.visitor_reservation_price
            )
        else:
            reservation_price = (
                club.visitor_reservation_price
            )

        if reservation_price is None:
            raise ValueError(
                "このレッスンはビジター予約を受け付けていません。"
            )

        # -------------------------
        # Validate date
        # -------------------------

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

        # -------------------------
        # Stripe
        # -------------------------

        if not club.stripe_account_id:
            raise ValueError(
                "このクラブではオンライン決済が設定されていません。"
            )

        # -------------------------
        # Customer information
        # -------------------------

        email = email.strip().lower()
        full_name = full_name.strip()
        phone_number = phone_number.strip()

        if not full_name:
            raise ValueError(
                "お名前を入力してください。"
            )

        if user is not None:
            email = user.email.strip().lower()

            if not email:
                raise ValueError(
                    "アカウントにメールアドレスが"
                    "登録されていません。"
                )
        else:
            email = email.strip().lower()

            if not email:
                raise ValueError(
                    "メールアドレスを入力してください。"
            )
        # -------------------------
        # Idempotency key
        # -------------------------

        reservation_key = hashlib.sha256(
            (
                f"visitor:"
                f"{lesson.id}:"
                f"{reservation_date.isoformat()}:"
                f"{email}"
            ).encode()
        ).hexdigest()

        now = timezone.now()

        hold_cutoff = (
            now
            - timezone.timedelta(
                minutes=RESERVATION_HOLD_MINUTES
            )
        )

        # -------------------------
        # Create reservation
        # -------------------------

        with transaction.atomic():

            locked_lesson = (
                Lesson.objects
                .select_for_update()
                .get(id=lesson.id)
            )

            # Prevent duplicate reservation attempts.
            existing = (
                Reservation.objects
                .filter(
                    reservation_key=reservation_key
                )
                .first()
            )

            if existing:

                if (
                    existing.status
                    == Reservation.Status.PAID
                ):
                    raise ValueError(
                        "このレッスンはすでに予約済みです。"
                    )

                raise ValueError(
                    "このレッスンの予約処理が"
                    "すでに開始されています。"
                )

            # -------------------------
            # Reservation limit
            # -------------------------

            if (
                locked_lesson.reservation_limit
                is not None
            ):

                active_reservations = (
                    Reservation.objects
                    .filter(
                        lesson=locked_lesson,
                        reservation_date=reservation_date,
                    )
                    .filter(
                        Q(
                            status=
                            Reservation.Status.PAID
                        )
                        |
                        Q(
                            status=
                            Reservation.Status.UNPAID,
                            created_at__gte=
                            hold_cutoff,
                        )
                    )
                    .count()
                )

                if (
                    active_reservations
                    >= locked_lesson.reservation_limit
                ):
                    raise ValueError(
                        "このレッスンは満員です。"
                    )

            assert_visitor_reservation_caps(
                club=club,
                lesson=locked_lesson,
                email=email,
                hold_cutoff=hold_cutoff,
            )

            # -------------------------
            # Create local reservation
            # -------------------------

            reservation = Reservation.objects.create(
                lesson=locked_lesson,
                club=club,
                member=None,
                user=user,
                payment_method="stripe",
                reservation_type=(
                    Reservation.ReservationType.VISITOR
                ),
                status=Reservation.Status.UNPAID,
                full_name=full_name,
                email=email,
                phone_number=phone_number,
                age=age,
                gender=gender or "",
                amount=reservation_price,
                currency="jpy",
                reservation_date=reservation_date,
                reservation_key=reservation_key,
            )

        # -------------------------
        # Stripe Checkout
        # -------------------------

        try:

            session = stripe.checkout.Session.create(
                mode="payment",
                payment_method_types=["card"],
                customer_email=email,
                expires_at=int(
                    (timezone.now() + timezone.timedelta(minutes=STRIPE_CHECKOUT_MINUTES))
                    .timestamp()
                ),
                line_items=[
                    {
                        "price_data": {
                            "currency":
                                reservation.currency,
                            "product_data": {
                                "name":
                                    locked_lesson.title,
                            },
                            "unit_amount":
                                reservation.amount,
                        },
                        "quantity": 1,
                    }
                ],
                metadata={
                    "reservation_id":
                        str(reservation.id),
                    "club_id":
                        str(club.id),
                    "lesson_id":
                        str(locked_lesson.id),
                    "reservation_type":
                        "visitor",
                },
                success_url=(
                    f"https://{club.subdomain}.kaibaru.jp/"
                    f"?reservation=success"
                    f"&reservation_id={reservation.id}"
                ),
                cancel_url=(
                    f"https://{club.subdomain}.kaibaru.jp/"
                    f"?reservation=cancel"
                    f"&reservation_id={reservation.id}"
                ),
                stripe_account=club.stripe_account_id,
                idempotency_key=(
                    f"visitor_reservation_"
                    f"{reservation.id}"
                ),
            )

        except Exception:
            reservation.delete()
            raise

        reservation.stripe_checkout_session_id = (
            session.id
        )

        reservation.save(
            update_fields=[
                "stripe_checkout_session_id"
            ]
        )

        return {
            "reservation_id": reservation.id,
            "checkout_session_id": session.id,
            "checkout_url": session.url,
        }