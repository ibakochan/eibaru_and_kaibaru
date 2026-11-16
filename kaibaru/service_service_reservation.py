"""Hair/nail interval booking: email start flow, Stripe checkout, repair."""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import date, time, timedelta

import stripe
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from .cash_reservations import (
    allowed_payment_methods,
    clean_payment_option,
    resolve_payment_method,
)
from .charge_amounts import persisted_charge_amount, platform_fee_kwargs
from .models import ServiceCourse, ServiceReservation
from .reservation_checkout_recovery import (
    read_stripe_payment,
    soft_delete_reservation,
)
from .stripe_service import get_or_create_stripe_customer


logger = logging.getLogger(__name__)

stripe.api_key = settings.STRIPE_SECRET_KEY

STRIPE_CHECKOUT_MINUTES = 30
RESERVATION_HOLD_MINUTES = 31
START_LINK_HOURS = 72
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


class ServiceStartError(Exception):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.message = message
        self.retryable = retryable


def hold_cutoff_at(now=None):
    now = now or timezone.now()
    return now - timedelta(minutes=RESERVATION_HOLD_MINUTES)


def unpaid_hold_q(hold_cutoff):
    """Unpaid rows hold a slot only after checkout started, within the hold window."""
    started_recently = Q(checkout_started_at__gte=hold_cutoff) | Q(
        checkout_started_at__isnull=True,
        created_at__gte=hold_cutoff,
    )
    return Q(
        status=ServiceReservation.Status.UNPAID,
        canceled=False,
        is_deleted=False,
    ) & started_recently


def open_course_reservations_q(today=None):
    """Reservations that keep an inactive course visible on the public menu.

    Open = still unsettled: not paid, not canceled, not no-show, not
    soft-deleted, and on today or later. Once every reservation is paid /
    canceled / no-show (or in the past), an inactive course can leave the
    customer UI.
    """
    today = today or timezone.localdate()
    return Q(
        canceled=False,
        is_deleted=False,
        no_show=False,
        reservation_date__gte=today,
    ) & ~Q(status=ServiceReservation.Status.PAID)


def course_has_open_reservations(course, today=None):
    return course.reservations.filter(open_course_reservations_q(today)).exists()


def holding_reservations_q(hold_cutoff=None):
    hold_cutoff = hold_cutoff or hold_cutoff_at()
    return Q(
        status=ServiceReservation.Status.PAID,
        canceled=False,
        is_deleted=False,
    ) | unpaid_hold_q(hold_cutoff)


def hold_is_active(reservation, hold_cutoff):
    started = reservation.checkout_started_at or reservation.created_at
    return started >= hold_cutoff


def request_is_fresh(reservation, now=None):
    now = now or timezone.now()
    if reservation.reservation_date < timezone.localdate():
        return False
    requested_at = reservation.requested_at or reservation.created_at
    return requested_at >= now - timedelta(hours=START_LINK_HOURS)


def queue_service_reservation_start_email(start_token):
    from .tasks_emails import send_service_reservation_start_email

    transaction.on_commit(
        lambda token=start_token: send_service_reservation_start_email.delay(
            token
        )
    )


def queue_service_reservation_confirmation(reservation_id):
    from .tasks_emails import send_service_reservation_confirmation_email

    transaction.on_commit(
        lambda rid=reservation_id: (
            send_service_reservation_confirmation_email.delay(rid)
        )
    )


def _parse_hhmm(raw):
    text = str(raw or "").strip()
    if not text:
        return None
    parts = text.split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except (TypeError, ValueError, IndexError):
        return None
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None
    return time(hour=hour, minute=minute)


def _reservation_key(*, club_id, course_id, day, start, email):
    material = (
        f"svc|{club_id}|{course_id}|{day.isoformat()}|"
        f"{start.strftime('%H:%M')}|{email.strip().lower()}"
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _email_sent_result(reservation):
    return {
        "success": True,
        "email_sent": True,
        "requires_checkout": False,
        "paid": False,
        "reservation_id": reservation.id,
    }


def create_service_reservation(club, payload):
    """Create a not_started hold request and email the start link."""
    from .service_worker_schedule import (
        _from_min,
        _tmin,
        _window_allows,
        pick_worker,
        windows_for,
    )

    try:
        course_id = int(payload.get("course_id"))
    except (TypeError, ValueError):
        return None, "コースを選択してください。"
    course = ServiceCourse.objects.filter(
        club=club, id=course_id, active=True
    ).first()
    if not course:
        return None, "コースが見つかりません。"

    day_raw = payload.get("reservation_date") or payload.get("date")
    try:
        if isinstance(day_raw, date):
            day = day_raw
        else:
            day = date.fromisoformat(str(day_raw)[:10])
    except (TypeError, ValueError):
        return None, "日付を選択してください。"

    start = _parse_hhmm(payload.get("start_time"))
    if not start:
        return None, "開始時刻を選択してください。"
    end_min = _tmin(start) + int(course.duration_minutes)
    if end_min > 24 * 60:
        return None, "このコースはこの開始時刻では終わりません。"
    end = _from_min(end_min)

    today = timezone.localdate()
    if day < today:
        return None, "過去の日付には予約できません。"
    if day == today:
        now_min = _tmin(timezone.localtime().time())
        if _tmin(start) <= now_min:
            return None, "過去の時刻には予約できません。"

    section_id = payload.get("section_id")
    try:
        section_id = int(section_id) if section_id not in (None, "") else None
    except (TypeError, ValueError):
        section_id = None

    windows = windows_for(club, section_id, day=day)
    if not _window_allows(windows, _tmin(start), end_min):
        return None, "この時間帯は予約できません。"

    full_name = (payload.get("full_name") or "").strip()
    email = (payload.get("email") or "").strip().lower()
    if not full_name:
        return None, "お名前を入力してください。"
    if not email:
        return None, "メールアドレスを入力してください。"

    amount = int(course.amount or 0)
    option = clean_payment_option(
        getattr(club, "service_reservation_payment_options", None),
    )
    try:
        payment_method = resolve_payment_method(
            club=club,
            option=option,
            requested=payload.get("payment_method"),
            amount=amount,
            subject="予約",
        )
    except ValueError as exc:
        return None, str(exc)

    allow_choose = bool(getattr(club, "service_allow_choose_worker", False))
    preferred_worker = None
    raw_worker = payload.get("worker_id")
    if allow_choose and raw_worker not in (None, "", "random"):
        try:
            preferred_id = int(raw_worker)
        except (TypeError, ValueError):
            return None, "担当スタッフを正しく選んでください。"
        preferred_worker = pick_worker(
            club,
            course,
            day,
            start,
            end,
            preferred_worker=preferred_id,
        )
        if preferred_worker is None:
            return None, "選んだスタッフはこの時間に予約できません。"
    elif pick_worker(club, course, day, start, end) is None:
        # Soft availability check only — slot is not held until email confirm.
        return None, "この時間に空いているワーカーがいません。"

    reservation_key = _reservation_key(
        club_id=club.id,
        course_id=course.id,
        day=day,
        start=start,
        email=email,
    )
    now = timezone.now()
    token = secrets.token_urlsafe(32)

    try:
        with transaction.atomic():
            reservation = _save_not_started(
                club=club,
                course=course,
                section_id=section_id,
                day=day,
                start=start,
                end=end,
                full_name=full_name,
                email=email,
                phone_number=(payload.get("phone_number") or "")[:30],
                amount=amount,
                payment_method=payment_method or "",
                preferred_worker=preferred_worker,
                reservation_key=reservation_key,
                token=token,
                now=now,
            )
            queue_service_reservation_start_email(reservation.start_token)
    except IntegrityError:
        existing = (
            ServiceReservation.objects
            .filter(reservation_key=reservation_key)
            .first()
        )
        if existing and existing.start_token:
            queue_service_reservation_start_email(existing.start_token)
            return _email_sent_result(existing), None
        return None, "この予約処理がすでに開始されています。"
    except ValueError as exc:
        return None, str(exc)

    return _email_sent_result(reservation), None


def _save_not_started(
    *,
    club,
    course,
    section_id,
    day,
    start,
    end,
    full_name,
    email,
    phone_number,
    amount,
    payment_method,
    preferred_worker,
    reservation_key,
    token,
    now,
):
    existing = (
        ServiceReservation.objects
        .select_for_update()
        .filter(reservation_key=reservation_key)
        .first()
    )
    if existing:
        if (
            existing.status == ServiceReservation.Status.PAID
            and not existing.canceled
            and not existing.is_deleted
        ):
            raise ValueError("この予約はすでに確定しています。")

        if (
            existing.status == ServiceReservation.Status.UNPAID
            and not existing.is_deleted
            and hold_is_active(existing, hold_cutoff_at(now))
        ):
            raise ValueError("この予約の決済手続き中です。メールのリンクから続けてください。")

        if (
            existing.status == ServiceReservation.Status.NOT_STARTED
            and existing.start_token
            and request_is_fresh(existing, now)
        ):
            existing.full_name = full_name
            existing.phone_number = phone_number
            existing.payment_method = payment_method
            existing.amount = amount
            existing.price_name = course.name
            existing.section_id = section_id
            existing.preferred_worker = preferred_worker
            existing.requested_at = now
            existing.confirmation_email_sent = False
            existing.is_deleted = False
            existing.canceled = False
            existing.save(
                update_fields=[
                    "full_name",
                    "phone_number",
                    "payment_method",
                    "amount",
                    "price_name",
                    "section_id",
                    "preferred_worker",
                    "requested_at",
                    "confirmation_email_sent",
                    "is_deleted",
                    "canceled",
                ]
            )
            return existing

        existing.course = course
        existing.worker = None
        existing.preferred_worker = preferred_worker
        existing.section_id = section_id
        existing.reservation_type = ServiceReservation.ReservationType.VISITOR
        existing.status = ServiceReservation.Status.NOT_STARTED
        existing.payment_method = payment_method
        existing.full_name = full_name
        existing.email = email
        existing.phone_number = phone_number
        existing.amount = amount
        existing.price_name = course.name
        existing.reservation_date = day
        existing.start_time = start
        existing.end_time = end
        existing.start_token = token
        existing.requested_at = now
        existing.checkout_started_at = None
        existing.stripe_checkout_session_id = None
        existing.stripe_payment_intent_id = None
        existing.confirmation_email_sent = False
        existing.canceled = False
        existing.is_deleted = False
        existing.paid_at = None
        existing.save()
        return existing

    return ServiceReservation.objects.create(
        club=club,
        course=course,
        worker=None,
        preferred_worker=preferred_worker,
        section_id=section_id,
        reservation_type=ServiceReservation.ReservationType.VISITOR,
        status=ServiceReservation.Status.NOT_STARTED,
        payment_method=payment_method or None,
        full_name=full_name,
        email=email,
        phone_number=phone_number,
        amount=amount,
        price_name=course.name,
        reservation_date=day,
        start_time=start,
        end_time=end,
        reservation_key=reservation_key,
        start_token=token,
        requested_at=now,
    )


class ServiceReservationStartService:
    @staticmethod
    def preview(token):
        reservation = _load_start(token)
        if reservation is None:
            return {"mode": "invalid"}
        page = _start_page(reservation)
        if reservation.status == ServiceReservation.Status.PAID:
            return {"mode": "done", **page}
        message = _start_unusable_reason(reservation)
        if message:
            return {"mode": "error", "message": message, **page}
        return {"mode": "ready", **page}

    @staticmethod
    def begin(token):
        prepared = _prepare_start(token)
        if prepared["action"] == "done":
            return {
                key: value
                for key, value in prepared.items()
                if key not in {"action", "club", "reservation", "previous", "token", "course"}
            } | {"mode": "done"}

        if prepared["action"] == "reuse":
            checkout_url = _reusable_checkout_url(
                club=prepared["club"],
                session_id=prepared["session_id"],
            )
            if checkout_url:
                return {"redirect_url": checkout_url}
            prepared = _prepare_start(token, force_new=True)
            if prepared["action"] == "done":
                return {
                    key: value
                    for key, value in prepared.items()
                    if key not in {
                        "action", "club", "reservation", "previous", "token", "course"
                    }
                } | {"mode": "done"}

        try:
            checkout_url = _open_service_checkout(prepared)
        except ServiceStartError:
            raise
        except Exception:
            logger.exception(
                "Failed to start service checkout token=%s",
                prepared.get("token"),
            )
            _expire_service_session(prepared)
            _restore_service_reservation(prepared.get("previous"))
            raise ServiceStartError(
                "決済ページを作成できませんでした。"
                "しばらくしてからもう一度お試しください。",
                retryable=True,
            )
        return {"redirect_url": checkout_url}


def _load_start(token):
    if not token:
        return None
    return (
        ServiceReservation.objects
        .select_related("course", "club", "club__owner", "worker")
        .filter(start_token=token)
        .first()
    )


def _start_page(reservation):
    club = reservation.club
    course = reservation.course
    is_free = (reservation.amount or 0) == 0
    is_cash = not is_free and reservation.payment_method == "cash"
    weekday = WEEKDAY_NAMES[reservation.reservation_date.weekday()]
    date_text = reservation.reservation_date.strftime("%Y年%m月%d日")
    start_text = reservation.start_time.strftime("%H:%M")
    end_text = reservation.end_time.strftime("%H:%M")
    course_name = (
        course.name if course is not None else (reservation.price_name or "コース")
    )
    return {
        "club_name": club.title or club.subdomain or "サロン",
        "course_name": course_name,
        "when_text": f"{date_text}（{weekday}） {start_text}〜{end_text}",
        "person_name": reservation.full_name,
        "total_amount": reservation.amount,
        "is_free": is_free,
        "is_cash": is_cash,
        "home_url": f"https://{club.subdomain}.kaibaru.jp/",
        "continue_label": (
            "予約を確定する" if is_free or is_cash else "支払いへ進む"
        ),
    }


def _start_unusable_reason(reservation):
    now = timezone.now()
    today = timezone.localdate()
    if reservation.reservation_date < today:
        return "この予約の日時はすでに過ぎています。"
    if reservation.reservation_date == today:
        from .service_worker_schedule import _tmin

        if _tmin(reservation.start_time) <= _tmin(timezone.localtime().time()):
            return "この予約の開始時刻はすでに過ぎています。"
    if (
        reservation.status == ServiceReservation.Status.NOT_STARTED
        and not request_is_fresh(reservation, now)
    ):
        return (
            "このリンクの有効期限が切れています。"
            "もう一度お申し込みください。"
        )
    return None


def _prepare_start(token, force_new=False):
    from .service_worker_schedule import pick_worker

    preview = _load_start(token)
    if preview is None:
        raise ServiceStartError("このリンクは無効です。")

    with transaction.atomic():
        reservation = (
            ServiceReservation.objects
            .select_for_update()
            .select_related("course", "club", "preferred_worker")
            .get(start_token=token)
        )
        club = reservation.club
        course = reservation.course
        page = _start_page(reservation)

        if reservation.is_deleted and reservation.status != ServiceReservation.Status.PAID:
            reservation.is_deleted = False
            reservation.save(update_fields=["is_deleted"])

        if reservation.status == ServiceReservation.Status.PAID:
            return {"action": "done", **page}

        message = _start_unusable_reason(reservation)
        if message:
            raise ServiceStartError(message)

        now = timezone.now()
        hold_cutoff = hold_cutoff_at(now)

        preferred = reservation.preferred_worker
        worker = pick_worker(
            club,
            course,
            reservation.reservation_date,
            reservation.start_time,
            reservation.end_time,
            exclude_reservation_id=reservation.id,
            preferred_worker=preferred,
        )
        if worker is None:
            if preferred is not None:
                raise ServiceStartError(
                    "ご希望のスタッフはこの時間に予約できなくなりました。"
                    "もう一度お申し込みください。",
                    retryable=True,
                )
            raise ServiceStartError(
                "この時間に空いているワーカーがいません。",
                retryable=True,
            )

        if (reservation.amount or 0) == 0:
            reservation.worker = worker
            reservation.status = ServiceReservation.Status.PAID
            reservation.paid_at = now
            reservation.is_deleted = False
            reservation.save(
                update_fields=["worker", "status", "paid_at", "is_deleted"]
            )
            queue_service_reservation_confirmation(reservation.id)
            return {"action": "done", **_start_page(reservation)}

        if page["is_cash"]:
            option = clean_payment_option(
                getattr(club, "service_reservation_payment_options", None),
            )
            if "cash" not in allowed_payment_methods(club, option):
                raise ServiceStartError(
                    "この予約では現地でのお支払いを"
                    "利用できなくなりました。もう一度お申し込みください。"
                )
            reservation.worker = worker
            reservation.status = ServiceReservation.Status.PAID
            reservation.payment_method = "cash"
            reservation.is_deleted = False
            # paid_at stays empty until the owner marks cash received.
            reservation.save(
                update_fields=[
                    "worker",
                    "status",
                    "payment_method",
                    "is_deleted",
                ]
            )
            queue_service_reservation_confirmation(reservation.id)
            return {"action": "done", **_start_page(reservation)}

        if not club.stripe_account_id:
            raise ServiceStartError(
                "このクラブではオンライン決済が設定されていません。"
            )

        if (
            not force_new
            and reservation.status == ServiceReservation.Status.UNPAID
            and hold_is_active(reservation, hold_cutoff)
            and reservation.stripe_checkout_session_id
        ):
            return {
                "action": "reuse",
                "session_id": reservation.stripe_checkout_session_id,
                "club": club,
                "token": token,
                **page,
            }

        if (
            not force_new
            and reservation.status == ServiceReservation.Status.UNPAID
            and hold_is_active(reservation, hold_cutoff)
            and not reservation.stripe_checkout_session_id
            and now
            - (reservation.checkout_started_at or reservation.created_at)
            < timedelta(seconds=CHECKOUT_CREATE_GRACE_SECONDS)
        ):
            raise ServiceStartError(
                "決済ページを準備しています。数秒後にもう一度開いてください。",
                retryable=True,
            )

        previous = {
            "id": reservation.id,
            "status": reservation.status,
            "worker_id": reservation.worker_id,
            "checkout_started_at": reservation.checkout_started_at,
            "stripe_checkout_session_id": reservation.stripe_checkout_session_id,
        }
        reservation.worker = worker
        reservation.status = ServiceReservation.Status.UNPAID
        reservation.checkout_started_at = now
        reservation.stripe_checkout_session_id = None
        reservation.payment_method = "stripe"
        reservation.save(
            update_fields=[
                "worker",
                "status",
                "checkout_started_at",
                "stripe_checkout_session_id",
                "payment_method",
            ]
        )
        return {
            "action": "create",
            "previous": previous,
            "reservation_id": reservation.id,
            "club": club,
            "course": course,
            "started_at": now,
            "token": token,
            **page,
        }


def _open_service_checkout(prepared):
    reservation = ServiceReservation.objects.get(id=prepared["reservation_id"])
    club = prepared["club"]
    course = prepared["course"]
    charge_amount = persisted_charge_amount(reservation)
    course_name = course.name if course is not None else reservation.price_name
    session_kwargs = {
        "mode": "payment",
        "payment_method_types": ["card"],
        "expires_at": int(
            (
                timezone.now()
                + timedelta(minutes=STRIPE_CHECKOUT_MINUTES)
            ).timestamp()
        ),
        "line_items": [
            {
                "price_data": {
                    "currency": reservation.currency or "jpy",
                    "product_data": {
                        "name": (course_name or "予約")[:250],
                    },
                    "unit_amount": charge_amount,
                },
                "quantity": 1,
            }
        ],
        "metadata": {
            "service_reservation_id": str(reservation.id),
            "club_id": str(club.id),
            "course_id": str(reservation.course_id or ""),
            "reservation_type": reservation.reservation_type,
            "kind": "service",
        },
        "success_url": (
            f"https://{club.subdomain}.kaibaru.jp/"
            f"?service_reservation=success"
            f"&service_reservation_id={reservation.id}"
        ),
        "cancel_url": (
            f"https://{club.subdomain}.kaibaru.jp/"
            f"?service_reservation=cancel"
            f"&service_reservation_id={reservation.id}"
        ),
        "stripe_account": club.stripe_account_id,
        "idempotency_key": (
            f"svc_start_{prepared['token']}_"
            f"{int(prepared['started_at'].timestamp())}"
        )[:255],
        "customer_email": reservation.email,
    }
    fee = platform_fee_kwargs(charge_amount)
    if fee:
        session_kwargs["payment_intent_data"] = fee
    if reservation.user_id:
        stripe_customer = get_or_create_stripe_customer(reservation.user, club)
        session_kwargs.pop("customer_email", None)
        session_kwargs["customer"] = stripe_customer.stripe_customer_id

    session = stripe.checkout.Session.create(**session_kwargs)
    prepared["session"] = session
    updated = ServiceReservation.objects.filter(
        id=reservation.id,
        status=ServiceReservation.Status.UNPAID,
    ).update(stripe_checkout_session_id=session.id)
    if updated != 1:
        raise RuntimeError("Service reservation changed before checkout was saved.")
    return session.url


def _expire_service_session(prepared):
    session = prepared.get("session")
    club = prepared.get("club")
    if session is None or club is None or not club.stripe_account_id:
        return
    try:
        stripe.checkout.Session.expire(
            session.id,
            stripe_account=club.stripe_account_id,
        )
    except Exception:
        logger.exception(
            "Failed to expire service checkout session %s", session.id
        )


def _restore_service_reservation(previous):
    if not previous:
        return
    ServiceReservation.objects.filter(id=previous["id"]).update(
        status=previous["status"],
        worker_id=previous["worker_id"],
        checkout_started_at=previous["checkout_started_at"],
        stripe_checkout_session_id=previous["stripe_checkout_session_id"],
    )


def _reusable_checkout_url(*, club, session_id):
    session = stripe.checkout.Session.retrieve(
        session_id,
        stripe_account=club.stripe_account_id,
    )
    if session.status == "open" and session.url:
        return session.url
    return None


def complete_paid_service_checkout(
    *,
    reservation_id,
    session_id,
    payment_intent_id,
    account_id,
):
    with transaction.atomic():
        reservation = (
            ServiceReservation.objects
            .select_for_update()
            .select_related("club")
            .filter(id=reservation_id)
            .first()
        )
        if reservation is None:
            logger.error(
                "[SERVICE CHECKOUT] Missing reservation id=%s session=%s",
                reservation_id,
                session_id,
            )
            return None

        club = reservation.club
        if not club or club.stripe_account_id != account_id:
            logger.error(
                "[SERVICE CHECKOUT] Account mismatch reservation=%s",
                reservation_id,
            )
            return None

        if (
            reservation.stripe_checkout_session_id
            and reservation.stripe_checkout_session_id != session_id
            and not reservation.is_deleted
        ):
            logger.error(
                "[SERVICE CHECKOUT] Session mismatch reservation=%s",
                reservation_id,
            )
            return None

        if reservation.status == ServiceReservation.Status.PAID:
            if reservation.is_deleted:
                reservation.is_deleted = False
                reservation.stripe_checkout_session_id = session_id
                fields = ["is_deleted", "stripe_checkout_session_id"]
                if payment_intent_id:
                    reservation.stripe_payment_intent_id = payment_intent_id
                    fields.append("stripe_payment_intent_id")
                reservation.save(update_fields=fields)
            return reservation

        reservation.status = ServiceReservation.Status.PAID
        reservation.paid_at = timezone.now()
        reservation.is_deleted = False
        reservation.stripe_checkout_session_id = session_id
        fields = [
            "status",
            "paid_at",
            "is_deleted",
            "stripe_checkout_session_id",
        ]
        if payment_intent_id:
            reservation.stripe_payment_intent_id = payment_intent_id
            fields.append("stripe_payment_intent_id")
        reservation.save(update_fields=fields)

    from .service_member_income import persist_reservation_net

    persist_reservation_net(reservation)
    queue_service_reservation_confirmation(reservation.id)
    return reservation


class ServiceReservationPaymentReconciler:
    HOLD_MINUTES = RESERVATION_HOLD_MINUTES
    TERMINAL_PAYMENT_INTENT_FAILURE_STATUSES = {
        "requires_payment_method",
        "canceled",
    }
    NON_TERMINAL_PAYMENT_INTENT_STATUSES = {
        "requires_confirmation",
        "requires_action",
        "processing",
        "requires_capture",
    }
    CHECKOUT_EXPIRED_STATUS = "expired"

    @classmethod
    def reconcile_old_unpaid_reservations(cls):
        now = timezone.now()
        cutoff = now - timedelta(minutes=cls.HOLD_MINUTES)
        hold_expired = Q(checkout_started_at__lt=cutoff) | Q(
            checkout_started_at__isnull=True,
            created_at__lt=cutoff,
        )
        reservations = (
            ServiceReservation.objects
            .filter(
                status=ServiceReservation.Status.UNPAID,
                is_deleted=False,
                payment_method="stripe",
            )
            .filter(hold_expired)
            .select_related("club")
            .order_by("created_at", "id")
        )

        checked = paid = deleted = waiting = skipped = failed = 0
        logger.info(
            "[SERVICE RESERVATION RECONCILE] Starting scan cutoff=%s",
            cutoff,
        )
        for reservation in reservations:
            checked += 1
            try:
                result = cls.reconcile_reservation(reservation_id=reservation.id)
                if result == "paid":
                    paid += 1
                elif result == "deleted":
                    deleted += 1
                elif result == "waiting":
                    waiting += 1
                elif result == "skipped":
                    skipped += 1
                else:
                    failed += 1
            except ServiceReservation.DoesNotExist:
                skipped += 1
            except Exception:
                failed += 1
                logger.exception(
                    "[SERVICE RESERVATION RECONCILE] Unexpected error "
                    "reservation=%s",
                    reservation.id,
                )

        logger.info(
            "[SERVICE RESERVATION RECONCILE] Finished checked=%s paid=%s "
            "deleted=%s waiting=%s skipped=%s failed=%s",
            checked,
            paid,
            deleted,
            waiting,
            skipped,
            failed,
        )
        return {
            "checked": checked,
            "paid": paid,
            "deleted": deleted,
            "waiting": waiting,
            "skipped": skipped,
            "failed": failed,
        }

    @classmethod
    def reconcile_reservation(cls, *, reservation_id):
        with transaction.atomic():
            reservation = (
                ServiceReservation.objects
                .select_for_update()
                .select_related("club")
                .get(id=reservation_id)
            )
            if reservation.status == ServiceReservation.Status.PAID:
                return "skipped"
            if reservation.status != ServiceReservation.Status.UNPAID:
                return "skipped"
            if reservation.payment_method != "stripe":
                return "skipped"

            club = reservation.club
            if not club or not club.stripe_account_id:
                logger.warning(
                    "[SERVICE RESERVATION RECONCILE] Reservation=%s has no "
                    "Stripe account. Leaving unchanged.",
                    reservation.id,
                )
                return "failed"

            cutoff = timezone.now() - timedelta(minutes=cls.HOLD_MINUTES)
            started_at = reservation.checkout_started_at or reservation.created_at
            if started_at >= cutoff:
                return "skipped"

            return cls._reconcile_checkout(reservation=reservation, club=club)

    @classmethod
    def _abandon(cls, reservation, club):
        soft_delete_reservation(reservation)
        view = read_stripe_payment(
            reservation,
            club.stripe_account_id,
            metadata_key="service_reservation_id",
        )
        if view.paid:
            cls._mark_paid(
                reservation,
                session_id=view.session_id,
                payment_intent_id=view.payment_intent_id,
            )
            return "paid"
        return "deleted"

    @classmethod
    def _mark_paid(cls, reservation, *, session_id=None, payment_intent_id=None):
        if reservation.canceled:
            reservation.canceled = False
        reservation.status = ServiceReservation.Status.PAID
        reservation.paid_at = timezone.now()
        reservation.is_deleted = False
        fields = ["status", "paid_at", "is_deleted", "canceled"]
        if session_id:
            reservation.stripe_checkout_session_id = session_id
            fields.append("stripe_checkout_session_id")
        if payment_intent_id:
            reservation.stripe_payment_intent_id = payment_intent_id
            fields.append("stripe_payment_intent_id")
        reservation.save(update_fields=fields)
        from .service_member_income import persist_reservation_net

        persist_reservation_net(reservation)
        queue_service_reservation_confirmation(reservation.id)

    @classmethod
    def _reconcile_checkout(cls, *, reservation, club):
        checkout_session = None
        if reservation.stripe_checkout_session_id:
            try:
                checkout_session = stripe.checkout.Session.retrieve(
                    reservation.stripe_checkout_session_id,
                    stripe_account=club.stripe_account_id,
                )
            except stripe.error.InvalidRequestError:
                checkout_session = None
            except stripe.error.StripeError:
                logger.exception(
                    "[SERVICE RESERVATION RECONCILE] Stripe error retrieving "
                    "Checkout Session reservation=%s",
                    reservation.id,
                )
                return "failed"

        if checkout_session is None:
            view = read_stripe_payment(
                reservation,
                club.stripe_account_id,
                metadata_key="service_reservation_id",
            )
            if view.paid:
                cls._mark_paid(
                    reservation,
                    session_id=view.session_id,
                    payment_intent_id=view.payment_intent_id,
                )
                return "paid"
            if view.session_id:
                return "waiting"
            return cls._abandon(reservation, club)

        if checkout_session.payment_status == "paid":
            cls._mark_paid(
                reservation,
                session_id=checkout_session.id,
                payment_intent_id=checkout_session.payment_intent,
            )
            return "paid"

        if checkout_session.status == cls.CHECKOUT_EXPIRED_STATUS:
            return cls._abandon(reservation, club)

        if checkout_session.status in ("open", "complete"):
            return "waiting"

        logger.warning(
            "[SERVICE RESERVATION RECONCILE] Checkout=%s status=%s "
            "payment_status=%s reservation=%s",
            checkout_session.id,
            checkout_session.status,
            checkout_session.payment_status,
            reservation.id,
        )
        return "waiting"
