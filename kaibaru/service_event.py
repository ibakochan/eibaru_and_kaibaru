import hashlib
import logging
import re
import secrets
from datetime import datetime, timedelta

import stripe
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import Event, EventReservation, StripeCustomer


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


class EventStartError(Exception):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.message = message
        self.retryable = retryable


def price_for(event, kind):
    raw = event.member_price if kind == "member" else event.visitor_price
    if raw is None:
        return 0
    return raw


def audience_allows(event, kind):
    if event.audience == Event.Audience.BOTH:
        return True
    if kind == "member":
        return event.audience == Event.Audience.MEMBERS
    if kind == "visitor":
        return event.audience == Event.Audience.VISITORS
    return False


def hold_cutoff_at(now=None):
    now = now or timezone.now()
    return now - timedelta(minutes=RESERVATION_HOLD_MINUTES)


def unpaid_hold_q(hold_cutoff):
    started_recently = Q(checkout_started_at__gte=hold_cutoff) | Q(
        checkout_started_at__isnull=True,
        created_at__gte=hold_cutoff,
    )
    return Q(status=EventReservation.Status.UNPAID) & started_recently


def hold_is_active(reservation, hold_cutoff):
    started = reservation.checkout_started_at or reservation.created_at
    return started >= hold_cutoff


def request_is_fresh(reservation, now=None):
    now = now or timezone.now()
    if reservation.event.starts_at <= now:
        return False
    requested_at = reservation.requested_at or reservation.created_at
    return requested_at >= now - timedelta(hours=START_LINK_HOURS)


def held_reservations(event, hold_cutoff):
    return (
        EventReservation.objects
        .filter(event=event)
        .filter(
            Q(status=EventReservation.Status.PAID)
            | unpaid_hold_q(hold_cutoff)
        )
    )


def assert_event_has_spots(*, event, hold_cutoff, needed=1, exclude_ids=None):
    if event.reservation_limit is None:
        return

    spots = held_reservations(event, hold_cutoff)
    if exclude_ids:
        spots = spots.exclude(id__in=list(exclude_ids))

    if spots.count() + needed > event.reservation_limit:
        raise ValueError("このイベントは満員です。")


def format_event_when(event):
    local = timezone.localtime(event.starts_at)
    weekday = WEEKDAY_NAMES[local.weekday()]
    return local.strftime(f"%Y年%m月%d日（{weekday}） %H:%M")


def member_reservation_key(event, member):
    return hashlib.sha256(
        f"event-member:{event.id}:{member.id}".encode()
    ).hexdigest()


def visitor_reservation_key(event, email):
    return hashlib.sha256(
        f"event-visitor:{event.id}:{email}".encode()
    ).hexdigest()


def _require_upcoming(event):
    if event.starts_at <= timezone.now():
        raise ValueError("このイベントの予約受付は終了しています。")


def _require_stripe_for_price(club, amount):
    if amount > 0 and not club.stripe_account_id:
        raise ValueError(
            "このクラブではオンライン決済が設定されていません。"
        )


def _queue_confirmation(reservation_id):
    from .tasks_emails import send_event_reservation_confirmation_email

    def send(reservation_id=reservation_id):
        send_event_reservation_confirmation_email.delay(reservation_id)

    transaction.on_commit(send)


def _queue_start_email(start_token):
    from .tasks_emails import send_event_reservation_start_email

    transaction.on_commit(
        lambda token=start_token: (
            send_event_reservation_start_email.delay(token)
        )
    )


def _paid_result(reservation):
    return {
        "reservation_id": reservation.id,
        "success": True,
        "paid": True,
        "requires_checkout": False,
    }


def _email_sent_result(reservation):
    return {
        "reservation_id": reservation.id,
        "success": True,
        "email_sent": True,
        "paid": False,
        "requires_checkout": False,
    }


class EventReservationService:

    @staticmethod
    def create_member_reservation(*, event, member):
        club = event.club

        if member.club_id != club.id or event.club_id != club.id:
            raise ValueError("このイベントは指定されたクラブに属していません。")

        if club.is_deleted:
            raise ValueError("このクラブは利用できません。")

        if not audience_allows(event, "member"):
            raise ValueError("このイベントは会員予約を受け付けていません。")

        _require_upcoming(event)

        amount = price_for(event, "member")
        _require_stripe_for_price(club, amount)

        if not member.full_name:
            raise ValueError("会員のお名前が登録されていません。")

        if not member.owner or not (member.owner.email or "").strip():
            raise ValueError(
                "会員アカウントにメールアドレスが登録されていません。"
            )

        email = member.owner.email.strip().lower()
        full_name = member.full_name.strip()
        phone_number = (member.phone_number or "").strip()
        reservation_key = member_reservation_key(event, member)
        now = timezone.now()
        hold_cutoff = hold_cutoff_at(now)

        with transaction.atomic():
            locked_event = (
                Event.objects
                .select_for_update()
                .get(id=event.id)
            )
            _require_upcoming(locked_event)

            if not audience_allows(locked_event, "member"):
                raise ValueError("このイベントは会員予約を受け付けていません。")

            amount = price_for(locked_event, "member")
            _require_stripe_for_price(club, amount)

            existing = (
                EventReservation.objects
                .select_for_update()
                .filter(reservation_key=reservation_key)
                .first()
            )

            if existing:
                if existing.status == EventReservation.Status.PAID:
                    raise ValueError("このイベントはすでに予約済みです。")

                raise ValueError(
                    "このイベントの予約処理がすでに開始されています。"
                )

            assert_event_has_spots(
                event=locked_event,
                hold_cutoff=hold_cutoff,
            )

            reservation = EventReservation.objects.create(
                event=locked_event,
                club=club,
                member=member,
                user=member.owner,
                reservation_type=EventReservation.ReservationType.MEMBER,
                payment_method="stripe" if amount > 0 else "",
                status=(
                    EventReservation.Status.PAID
                    if amount == 0
                    else EventReservation.Status.UNPAID
                ),
                full_name=full_name,
                email=email,
                phone_number=phone_number,
                amount=amount,
                currency="jpy",
                reservation_key=reservation_key,
                paid_at=now if amount == 0 else None,
                checkout_started_at=now if amount > 0 else None,
            )

        if amount == 0:
            _queue_confirmation(reservation.id)
            return _paid_result(reservation)

        try:
            charged = _charge_member_off_session(
                reservation=reservation,
                club=club,
                member=member,
                event=locked_event,
            )
            if charged:
                return charged

            session = _create_checkout_session(
                reservation=reservation,
                club=club,
                event=locked_event,
                customer_email=email,
                idempotency_key=f"event_member_{reservation.id}",
            )
        except Exception:
            reservation.delete()
            raise

        reservation.stripe_checkout_session_id = session.id
        reservation.save(update_fields=["stripe_checkout_session_id"])

        return {
            "reservation_id": reservation.id,
            "checkout_session_id": session.id,
            "checkout_url": session.url,
            "requires_checkout": True,
            "paid": False,
            "success": True,
        }

    @staticmethod
    def create_visitor_reservation(
        *,
        event,
        full_name,
        email,
        phone_number="",
        user=None,
    ):
        club = event.club

        if club.is_deleted:
            raise ValueError("このクラブは利用できません。")

        if not audience_allows(event, "visitor"):
            raise ValueError("このイベントは一般予約を受け付けていません。")

        _require_upcoming(event)

        full_name = " ".join(
            str(full_name or "").replace("\u3000", " ").strip().split()
        )
        phone_number = (phone_number or "").strip()

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

        amount = price_for(event, "visitor")
        _require_stripe_for_price(club, amount)
        reservation_key = visitor_reservation_key(event, email)

        try:
            with transaction.atomic():
                token, reservation = _save_visitor_reservation(
                    event=event,
                    club=club,
                    full_name=full_name,
                    email=email,
                    phone_number=phone_number,
                    user=user,
                    amount=amount,
                    reservation_key=reservation_key,
                )
                _queue_start_email(token)
        except IntegrityError:
            existing = (
                EventReservation.objects
                .filter(reservation_key=reservation_key)
                .first()
            )
            if existing and existing.start_token:
                _queue_start_email(existing.start_token)
                return _email_sent_result(existing)
            raise ValueError(
                "このイベントの予約処理がすでに開始されています。"
            )

        return _email_sent_result(reservation)


def _save_visitor_reservation(
    *,
    event,
    club,
    full_name,
    email,
    phone_number,
    user,
    amount,
    reservation_key,
):
    now = timezone.now()
    hold_cutoff = hold_cutoff_at(now)

    locked_event = Event.objects.select_for_update().get(id=event.id)
    _require_upcoming(locked_event)

    if not audience_allows(locked_event, "visitor"):
        raise ValueError("このイベントは一般予約を受け付けていません。")

    amount = price_for(locked_event, "visitor")
    _require_stripe_for_price(club, amount)

    existing = (
        EventReservation.objects
        .select_for_update()
        .filter(reservation_key=reservation_key)
        .first()
    )

    if existing:
        if existing.status == EventReservation.Status.PAID:
            raise ValueError("このイベントはすでに予約済みです。")

        if (
            existing.status == EventReservation.Status.UNPAID
            and hold_is_active(existing, hold_cutoff)
        ):
            raise ValueError(
                "このイベントの予約はお支払い手続き中です。"
                "メールのリンクからお支払いを続けてください。"
            )

        if (
            existing.status == EventReservation.Status.NOT_STARTED
            and existing.start_token
            and request_is_fresh(existing, now)
        ):
            existing.full_name = full_name
            existing.phone_number = phone_number
            existing.amount = amount
            existing.user = user
            existing.requested_at = now
            existing.event = locked_event
            existing.save(
                update_fields=[
                    "full_name",
                    "phone_number",
                    "amount",
                    "user",
                    "requested_at",
                    "event",
                ]
            )
            return existing.start_token, existing

        assert_event_has_spots(
            event=locked_event,
            hold_cutoff=hold_cutoff,
            exclude_ids=[existing.id],
        )

        token = secrets.token_urlsafe(32)
        existing.event = locked_event
        existing.club = club
        existing.user = user
        existing.member = None
        existing.payment_method = "stripe" if amount > 0 else ""
        existing.reservation_type = EventReservation.ReservationType.VISITOR
        existing.status = EventReservation.Status.NOT_STARTED
        existing.full_name = full_name
        existing.email = email
        existing.phone_number = phone_number
        existing.amount = amount
        existing.currency = "jpy"
        existing.start_token = token
        existing.requested_at = now
        existing.checkout_started_at = None
        existing.stripe_checkout_session_id = None
        existing.stripe_payment_intent_id = None
        existing.paid_at = None
        existing.confirmation_email_sent = False
        existing.save()
        return token, existing

    assert_event_has_spots(
        event=locked_event,
        hold_cutoff=hold_cutoff,
    )

    token = secrets.token_urlsafe(32)
    reservation = EventReservation.objects.create(
        event=locked_event,
        club=club,
        member=None,
        user=user,
        payment_method="stripe" if amount > 0 else "",
        reservation_type=EventReservation.ReservationType.VISITOR,
        status=EventReservation.Status.NOT_STARTED,
        full_name=full_name,
        email=email,
        phone_number=phone_number,
        amount=amount,
        currency="jpy",
        reservation_key=reservation_key,
        start_token=token,
        requested_at=now,
    )
    return token, reservation


def _charge_member_off_session(*, reservation, club, member, event):
    stripe_customer = (
        StripeCustomer.objects
        .filter(user=member.owner, club=club)
        .first()
    )
    stripe_subscription = (
        member.subscription_items
        .filter(
            subscription__billing_method="stripe",
            subscription__status="active",
        )
        .select_related("subscription")
        .first()
    )

    if not stripe_customer or not stripe_subscription:
        return None

    try:
        stripe_customer_obj = stripe.Customer.retrieve(
            stripe_customer.stripe_customer_id,
            expand=["invoice_settings.default_payment_method"],
            stripe_account=club.stripe_account_id,
        )
        default_payment_method = (
            stripe_customer_obj
            .get("invoice_settings", {})
            .get("default_payment_method")
        )
        if not default_payment_method:
            return None

        payment_method_id = (
            default_payment_method["id"]
            if isinstance(default_payment_method, dict)
            else default_payment_method
        )
        payment_intent = stripe.PaymentIntent.create(
            amount=reservation.amount,
            currency=reservation.currency,
            customer=stripe_customer.stripe_customer_id,
            payment_method=payment_method_id,
            off_session=True,
            confirm=True,
            metadata={
                "event_reservation_id": str(reservation.id),
                "club_id": str(club.id),
                "event_id": str(event.id),
                "member_id": str(member.id),
                "reservation_type": "member",
                "kind": "event",
            },
            stripe_account=club.stripe_account_id,
            idempotency_key=f"event_member_{reservation.id}",
        )
    except (stripe.error.CardError, stripe.error.InvalidRequestError):
        return None

    if payment_intent.status != "succeeded":
        return None

    reservation.status = EventReservation.Status.PAID
    reservation.paid_at = timezone.now()
    reservation.stripe_payment_intent_id = payment_intent.id
    reservation.save(
        update_fields=["status", "paid_at", "stripe_payment_intent_id"]
    )
    _queue_confirmation(reservation.id)
    result = _paid_result(reservation)
    result["payment_intent_id"] = payment_intent.id
    return result


def _create_checkout_session(
    *,
    reservation,
    club,
    event,
    customer_email,
    idempotency_key,
):
    stripe_customer = None
    if reservation.user_id:
        stripe_customer = (
            StripeCustomer.objects
            .filter(user_id=reservation.user_id, club=club)
            .first()
        )

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
                        "name": (event.title or "イベント")[:250],
                    },
                    "unit_amount": reservation.amount,
                },
                "quantity": 1,
            }
        ],
        "metadata": {
            "event_reservation_id": str(reservation.id),
            "club_id": str(club.id),
            "event_id": str(event.id),
            "reservation_type": reservation.reservation_type,
            "kind": "event",
        },
        "success_url": (
            f"https://{club.subdomain}.kaibaru.jp/"
            f"?event_reservation=success"
            f"&event_reservation_id={reservation.id}"
        ),
        "cancel_url": (
            f"https://{club.subdomain}.kaibaru.jp/"
            f"?event_reservation=cancel"
            f"&event_reservation_id={reservation.id}"
        ),
        "stripe_account": club.stripe_account_id,
        "idempotency_key": idempotency_key[:255],
    }

    if stripe_customer:
        session_kwargs["customer"] = stripe_customer.stripe_customer_id
    else:
        session_kwargs["customer_email"] = customer_email

    return stripe.checkout.Session.create(**session_kwargs)


def complete_paid_event_checkout(
    *,
    reservation_id,
    session_id,
    payment_intent_id,
    account_id,
):
    with transaction.atomic():
        reservation = (
            EventReservation.objects
            .select_for_update()
            .select_related("club", "event")
            .filter(id=reservation_id)
            .first()
        )

        if reservation is None:
            logger.error(
                "[EVENT CHECKOUT] Missing reservation=%s session=%s",
                reservation_id,
                session_id,
            )
            return

        if reservation.club.stripe_account_id != account_id:
            logger.error(
                "[EVENT CHECKOUT] Account mismatch reservation=%s "
                "session=%s account=%s",
                reservation.id,
                session_id,
                account_id,
            )
            return

        if (
            reservation.stripe_checkout_session_id
            and reservation.stripe_checkout_session_id != session_id
        ):
            logger.error(
                "[EVENT CHECKOUT] Session mismatch reservation=%s "
                "expected=%s actual=%s",
                reservation.id,
                reservation.stripe_checkout_session_id,
                session_id,
            )
            return

        if reservation.status == EventReservation.Status.PAID:
            return

        reservation.status = EventReservation.Status.PAID
        reservation.paid_at = timezone.now()
        reservation.stripe_checkout_session_id = session_id
        update_fields = [
            "status",
            "paid_at",
            "stripe_checkout_session_id",
        ]
        if payment_intent_id:
            reservation.stripe_payment_intent_id = payment_intent_id
            update_fields.append("stripe_payment_intent_id")
        reservation.save(update_fields=update_fields)

    _queue_confirmation(reservation.id)


class EventStartService:

    @staticmethod
    def preview(token):
        reservation = _load_start(token)
        if reservation is None:
            return {"mode": "invalid"}

        page = _start_page(reservation)
        if reservation.status == EventReservation.Status.PAID:
            return {"mode": "done", **page}

        message = _start_unusable_reason(reservation)
        if message:
            return {"mode": "error", "message": message, **page}

        return {"mode": "ready", **page}

    @staticmethod
    def begin(token):
        prepared = _prepare_start(token)

        if prepared["action"] == "done":
            return _public_start_result(prepared)

        if prepared["action"] == "reuse":
            checkout_url = _reusable_checkout_url(
                club=prepared["club"],
                session_id=prepared["session_id"],
            )
            if checkout_url:
                return {"redirect_url": checkout_url}
            prepared = _prepare_start(token, force_new=True)
            if prepared["action"] == "done":
                return _public_start_result(prepared)

        try:
            checkout_url = _open_event_checkout(prepared)
        except EventStartError:
            raise
        except Exception:
            logger.exception(
                "Failed to start event checkout token=%s",
                prepared.get("token"),
            )
            _expire_event_session(prepared)
            _restore_event_reservation(prepared.get("previous"))
            raise EventStartError(
                "決済ページを作成できませんでした。"
                "しばらくしてからもう一度お試しください。",
                retryable=True,
            )

        return {"redirect_url": checkout_url}


def _load_start(token):
    if not token:
        return None
    return (
        EventReservation.objects
        .select_related("event", "club", "club__owner")
        .filter(start_token=token)
        .first()
    )


def _start_page(reservation):
    event = reservation.event
    club = reservation.club
    is_free = reservation.amount == 0
    return {
        "club_name": club.title or club.subdomain or "クラブ",
        "event_title": event.title,
        "when_text": format_event_when(event),
        "person_name": reservation.full_name,
        "total_amount": reservation.amount,
        "is_free": is_free,
        "home_url": f"https://{club.subdomain}.kaibaru.jp/",
        "continue_label": "予約を確定する" if is_free else "支払いへ進む",
    }


def _public_start_result(prepared):
    return {
        key: value
        for key, value in prepared.items()
        if key not in {"action", "club", "reservation", "previous", "token"}
    } | {"mode": "done"}


def _start_unusable_reason(reservation):
    now = timezone.now()
    event = reservation.event

    if event.starts_at <= now:
        return "このイベントの予約受付は終了しています。"

    if not audience_allows(event, "visitor"):
        return "このイベントは一般予約を受け付けていません。"

    if (
        reservation.status == EventReservation.Status.NOT_STARTED
        and not request_is_fresh(reservation, now)
    ):
        return (
            "このリンクの有効期限が切れています。"
            "もう一度お申し込みください。"
        )

    return None


def _prepare_start(token, force_new=False):
    preview = _load_start(token)
    if preview is None:
        raise EventStartError("このリンクは無効です。")

    with transaction.atomic():
        event = Event.objects.select_for_update().get(id=preview.event_id)
        reservation = (
            EventReservation.objects
            .select_for_update()
            .get(start_token=token)
        )
        club = reservation.club
        reservation.event = event
        page = _start_page(reservation)

        if reservation.status == EventReservation.Status.PAID:
            return {"action": "done", **page}

        message = _start_unusable_reason(reservation)
        if message:
            raise EventStartError(message)

        now = timezone.now()
        hold_cutoff = hold_cutoff_at(now)

        try:
            assert_event_has_spots(
                event=event,
                hold_cutoff=hold_cutoff,
                exclude_ids=[reservation.id],
            )
        except ValueError as exc:
            raise EventStartError(str(exc), retryable=True) from exc

        if reservation.amount == 0:
            reservation.status = EventReservation.Status.PAID
            reservation.paid_at = now
            reservation.save(update_fields=["status", "paid_at"])
            _queue_confirmation(reservation.id)
            return {"action": "done", **page}

        if not club.stripe_account_id:
            raise EventStartError(
                "このクラブではオンライン決済が設定されていません。"
            )

        if (
            not force_new
            and reservation.status == EventReservation.Status.UNPAID
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
            and reservation.status == EventReservation.Status.UNPAID
            and hold_is_active(reservation, hold_cutoff)
            and not reservation.stripe_checkout_session_id
            and now - (
                reservation.checkout_started_at or reservation.created_at
            ) < timedelta(seconds=CHECKOUT_CREATE_GRACE_SECONDS)
        ):
            raise EventStartError(
                "決済ページを準備しています。数秒後にもう一度開いてください。",
                retryable=True,
            )

        previous = {
            "id": reservation.id,
            "status": reservation.status,
            "checkout_started_at": reservation.checkout_started_at,
            "stripe_checkout_session_id": (
                reservation.stripe_checkout_session_id
            ),
        }
        reservation.status = EventReservation.Status.UNPAID
        reservation.checkout_started_at = now
        reservation.stripe_checkout_session_id = None
        reservation.payment_method = "stripe"
        reservation.save(
            update_fields=[
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
            "event": event,
            "started_at": now,
            "token": token,
            **page,
        }


def _open_event_checkout(prepared):
    reservation = EventReservation.objects.get(id=prepared["reservation_id"])
    session = _create_checkout_session(
        reservation=reservation,
        club=prepared["club"],
        event=prepared["event"],
        customer_email=reservation.email,
        idempotency_key=(
            f"event_start_{prepared['token']}_"
            f"{int(prepared['started_at'].timestamp())}"
        ),
    )
    prepared["session"] = session
    updated = EventReservation.objects.filter(
        id=reservation.id,
        status=EventReservation.Status.UNPAID,
    ).update(stripe_checkout_session_id=session.id)

    if updated != 1:
        raise RuntimeError("Event reservation changed before checkout was saved.")

    return session.url


def _expire_event_session(prepared):
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
        logger.exception("Failed to expire event checkout session %s", session.id)


def _restore_event_reservation(previous):
    if not previous:
        return
    EventReservation.objects.filter(id=previous["id"]).update(
        status=previous["status"],
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


class EventReservationPaymentReconciler:
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
            EventReservation.objects
            .filter(
                status=EventReservation.Status.UNPAID,
                payment_method="stripe",
                reservation_type__in=[
                    EventReservation.ReservationType.MEMBER,
                    EventReservation.ReservationType.VISITOR,
                ],
            )
            .filter(hold_expired)
            .select_related("club")
            .order_by("created_at", "id")
        )

        checked = paid = deleted = waiting = skipped = failed = 0
        logger.info("[EVENT RESERVATION RECONCILE] Starting scan cutoff=%s", cutoff)

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
            except EventReservation.DoesNotExist:
                skipped += 1
            except Exception:
                failed += 1
                logger.exception(
                    "[EVENT RESERVATION RECONCILE] Unexpected error reservation=%s",
                    reservation.id,
                )

        logger.info(
            "[EVENT RESERVATION RECONCILE] Finished checked=%s paid=%s "
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
                EventReservation.objects
                .select_for_update()
                .select_related("club")
                .get(id=reservation_id)
            )

            if reservation.status == EventReservation.Status.PAID:
                return "skipped"

            if reservation.status != EventReservation.Status.UNPAID:
                return "skipped"

            if reservation.payment_method != "stripe":
                return "skipped"

            club = reservation.club
            if not club or not club.stripe_account_id:
                logger.warning(
                    "[EVENT RESERVATION RECONCILE] Reservation=%s has no "
                    "Stripe account. Leaving unchanged.",
                    reservation.id,
                )
                return "failed"

            cutoff = timezone.now() - timedelta(minutes=cls.HOLD_MINUTES)
            started_at = reservation.checkout_started_at or reservation.created_at
            if started_at >= cutoff:
                return "skipped"

            if (
                reservation.reservation_type
                == EventReservation.ReservationType.VISITOR
                or reservation.stripe_checkout_session_id
            ):
                return cls._reconcile_checkout(reservation=reservation, club=club)

            return cls._reconcile_payment_intent(
                reservation=reservation,
                club=club,
            )

    @classmethod
    def _mark_paid(cls, reservation, *, session_id=None, payment_intent_id=None):
        reservation.status = EventReservation.Status.PAID
        reservation.paid_at = timezone.now()
        update_fields = ["status", "paid_at"]
        if session_id:
            reservation.stripe_checkout_session_id = session_id
            update_fields.append("stripe_checkout_session_id")
        if payment_intent_id:
            reservation.stripe_payment_intent_id = payment_intent_id
            update_fields.append("stripe_payment_intent_id")
        reservation.save(update_fields=update_fields)
        _queue_confirmation(reservation.id)

    @classmethod
    def _reconcile_payment_intent(cls, *, reservation, club):
        payment_intent = None

        if reservation.stripe_payment_intent_id:
            try:
                payment_intent = stripe.PaymentIntent.retrieve(
                    reservation.stripe_payment_intent_id,
                    stripe_account=club.stripe_account_id,
                )
            except stripe.error.InvalidRequestError:
                payment_intent = None
            except stripe.error.StripeError:
                logger.exception(
                    "[EVENT RESERVATION RECONCILE] Stripe error retrieving "
                    "PaymentIntent reservation=%s",
                    reservation.id,
                )
                return "failed"

        if payment_intent is None:
            try:
                search_result = stripe.PaymentIntent.search(
                    query=(
                        "metadata['event_reservation_id']:"
                        f"'{reservation.id}'"
                    ),
                    limit=10,
                    stripe_account=club.stripe_account_id,
                )
            except stripe.error.StripeError:
                logger.exception(
                    "[EVENT RESERVATION RECONCILE] Could not search "
                    "PaymentIntents reservation=%s",
                    reservation.id,
                )
                return "failed"

            matches = list(search_result.data)
            paid = [item for item in matches if item.status == "succeeded"]
            chosen = paid or matches
            if chosen:
                payment_intent = max(chosen, key=lambda item: item.created)

        if payment_intent is None:
            logger.warning(
                "[EVENT RESERVATION RECONCILE] Reservation=%s has no "
                "PaymentIntent. Leaving unchanged.",
                reservation.id,
            )
            return "waiting"

        if payment_intent.status == "succeeded":
            cls._mark_paid(
                reservation,
                payment_intent_id=payment_intent.id,
            )
            return "paid"

        if payment_intent.status in cls.TERMINAL_PAYMENT_INTENT_FAILURE_STATUSES:
            reservation.delete()
            return "deleted"

        if payment_intent.status in cls.NON_TERMINAL_PAYMENT_INTENT_STATUSES:
            if reservation.stripe_payment_intent_id != payment_intent.id:
                reservation.stripe_payment_intent_id = payment_intent.id
                reservation.save(update_fields=["stripe_payment_intent_id"])
            return "waiting"

        logger.warning(
            "[EVENT RESERVATION RECONCILE] PaymentIntent=%s unknown status=%s "
            "reservation=%s",
            payment_intent.id,
            payment_intent.status,
            reservation.id,
        )
        return "waiting"

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
                    "[EVENT RESERVATION RECONCILE] Stripe error retrieving "
                    "Checkout Session reservation=%s",
                    reservation.id,
                )
                return "failed"

        if checkout_session is None:
            try:
                sessions = stripe.checkout.Session.list(
                    limit=100,
                    stripe_account=club.stripe_account_id,
                )
            except stripe.error.StripeError:
                logger.exception(
                    "[EVENT RESERVATION RECONCILE] Could not list Checkout "
                    "Sessions reservation=%s",
                    reservation.id,
                )
                return "failed"

            matching = []
            for session in sessions.auto_paging_iter():
                metadata = session.get("metadata", {}) or {}
                if str(metadata.get("event_reservation_id")) == str(reservation.id):
                    matching.append(session)

            paid_sessions = [
                session
                for session in matching
                if session.get("payment_status") == "paid"
            ]
            pool = paid_sessions or matching
            if pool:
                checkout_session = max(
                    pool,
                    key=lambda session: session.get("created", 0),
                )

        if checkout_session is None:
            logger.warning(
                "[EVENT RESERVATION RECONCILE] Reservation=%s has no matching "
                "Checkout Session. Leaving unchanged.",
                reservation.id,
            )
            return "waiting"

        session_id = checkout_session.id
        session_status = checkout_session.get("status")
        payment_status = checkout_session.get("payment_status")

        if payment_status == "paid":
            cls._mark_paid(
                reservation,
                session_id=session_id,
                payment_intent_id=checkout_session.get("payment_intent"),
            )
            return "paid"

        if session_status == cls.CHECKOUT_EXPIRED_STATUS:
            reservation.delete()
            return "deleted"

        if reservation.stripe_checkout_session_id != session_id:
            reservation.stripe_checkout_session_id = session_id
            reservation.save(update_fields=["stripe_checkout_session_id"])

        return "waiting"


_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _optional_money(value, label):
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label}は数字で入力してください。")
    if number < 0:
        raise ValueError(f"{label}は0以上にしてください。")
    return number


def _optional_cap(value):
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError("定員は数字で入力してください。")
    if number < 1:
        raise ValueError("定員は1以上にするか、空欄で無制限にしてください。")
    return number


def _color(value, default):
    if value is None or value == "":
        return default
    text = str(value).strip()
    if not _HEX_COLOR.fullmatch(text):
        raise ValueError("色は #FFFFFF の形式で指定してください。")
    return text


def _parse_starts_at(value):
    if not value:
        raise ValueError("開催日時を入力してください。")

    parsed = parse_datetime(str(value))
    if parsed is None:
        try:
            parsed = datetime.strptime(str(value), "%Y-%m-%dT%H:%M")
        except ValueError:
            raise ValueError("開催日時の形式が正しくありません。")

    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(
            parsed,
            timezone.get_current_timezone(),
        )
    return parsed


def save_section_event(*, club, section_id, title, payload):
    if not isinstance(payload, dict):
        payload = {}

    clean_title = (title or "").strip()
    if not clean_title:
        raise ValueError("タイトルを入力してください。")

    audience = payload.get("audience") or Event.Audience.BOTH
    if audience not in Event.Audience.values:
        raise ValueError("予約できる人の設定が正しくありません。")

    description = str(payload.get("description") or "").strip()
    if len(description) > 4000:
        raise ValueError("説明は4000文字以内にしてください。")

    picture = str(payload.get("picture") or "").strip()
    if len(picture) > 1000:
        raise ValueError("画像のURLが長すぎます。")

    defaults = {
        "title": clean_title[:200],
        "description": description,
        "picture": picture,
        "starts_at": _parse_starts_at(payload.get("starts_at")),
        "member_price": _optional_money(payload.get("member_price"), "会員料金"),
        "visitor_price": _optional_money(payload.get("visitor_price"), "一般料金"),
        "reservation_limit": _optional_cap(payload.get("reservation_limit")),
        "audience": audience,
        "title_color": _color(payload.get("title_color"), "#1c1917"),
        "description_color": _color(payload.get("description_color"), "#57534e"),
        "detail_color": _color(payload.get("detail_color"), "#44403c"),
        "button_color": _color(payload.get("button_color"), "#1c1917"),
        "button_text_color": _color(payload.get("button_text_color"), "#faf6f1"),
    }

    event, _created = Event.objects.update_or_create(
        club=club,
        section_id=section_id,
        defaults=defaults,
    )
    return event
