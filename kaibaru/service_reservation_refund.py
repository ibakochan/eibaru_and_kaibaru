import stripe
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Reservation, TicketUsage


class RefundError(Exception):
    def __init__(self, message, status_code=400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def reservation_is_refundable(reservation, user):
    if user is None or not getattr(user, "is_authenticated", False):
        return False

    if reservation.club.owner_id != user.id:
        return False

    if reservation.refunded_at:
        return False

    if reservation.status != Reservation.Status.PAID:
        return False

    if reservation.payment_method == Reservation.PaymentMethod.TICKET:
        usage = _ticket_usage(reservation)
        return (
            usage is not None
            and usage.refunded_at is None
            and usage.quantity > 0
        )

    if reservation.payment_method == Reservation.PaymentMethod.STRIPE:
        return (
            reservation.amount > 0
            and bool(reservation.stripe_payment_intent_id)
            and bool(reservation.club.stripe_account_id)
        )

    return False


def refund_preview(*, reservation_id, user):
    reservation = _load(reservation_id)
    _assert_owner(reservation, user)

    if reservation.refunded_at:
        return _payload(reservation, already_refunded=True)

    _assert_refundable(reservation)
    return _payload(reservation, already_refunded=False)


def refund_reservation(*, reservation_id, user):
    with transaction.atomic():
        reservation = _lock(reservation_id)
        _assert_owner(reservation, user)

        if reservation.refunded_at:
            return _payload(reservation, already_refunded=True)

        _assert_refundable(reservation)

        if reservation.payment_method == Reservation.PaymentMethod.TICKET:
            _refund_ticket(reservation)
        else:
            _refund_stripe(reservation)

        now = timezone.now()
        reservation.refunded_at = now
        reservation.canceled = True
        reservation.save(
            update_fields=[
                "refunded_at",
                "canceled",
                "stripe_refund_id",
            ]
        )

    reservation.refresh_from_db()
    return _payload(reservation, already_refunded=False)


def _load(reservation_id):
    reservation = (
        Reservation.objects
        .select_related("club", "lesson")
        .filter(id=reservation_id)
        .first()
    )

    if reservation is None:
        raise RefundError("予約が見つかりません。", 404)

    return reservation


def _lock(reservation_id):
    reservation = (
        Reservation.objects
        .select_for_update()
        .select_related("club", "lesson")
        .filter(id=reservation_id)
        .first()
    )

    if reservation is None:
        raise RefundError("予約が見つかりません。", 404)

    return reservation


def _assert_owner(reservation, user):
    if user is None or not getattr(user, "is_authenticated", False):
        raise RefundError("ログインが必要です。", 401)

    if reservation.club.owner_id != user.id:
        raise RefundError(
            "返金できるのはクラブのオーナーだけです。",
            403,
        )


def _assert_refundable(reservation):
    if reservation.status != Reservation.Status.PAID:
        raise RefundError("支払い済みの予約だけ返金できます。")

    if reservation.payment_method == Reservation.PaymentMethod.TICKET:
        usage = _ticket_usage(reservation)
        if usage is None or usage.refunded_at or usage.quantity <= 0:
            raise RefundError("戻せるチケットがありません。")
        return

    if reservation.payment_method != Reservation.PaymentMethod.STRIPE:
        raise RefundError("この予約は返金できません。")

    if reservation.amount <= 0 or not reservation.stripe_payment_intent_id:
        raise RefundError("Stripeの支払い情報がないため、返金できません。")

    if not reservation.club.stripe_account_id:
        raise RefundError("Stripeの支払い情報がないため、返金できません。")


def _ticket_usage(reservation):
    try:
        return reservation.ticket_usage
    except TicketUsage.DoesNotExist:
        return None


def _refund_ticket(reservation):
    usage = (
        TicketUsage.objects
        .select_for_update()
        .select_related("grant", "grant__ticket_type")
        .filter(reservation=reservation)
        .first()
    )

    if usage is None or usage.quantity <= 0:
        raise RefundError("戻せるチケットがありません。")

    if usage.refunded_at is None:
        usage.refunded_at = timezone.now()
        usage.save(update_fields=["refunded_at"])


def _refund_stripe(reservation):
    stripe.api_key = settings.STRIPE_SECRET_KEY
    existing = _find_stripe_refund(reservation)

    if existing is not None:
        reservation.stripe_refund_id = existing.id
        return

    try:
        refund = stripe.Refund.create(
            payment_intent=reservation.stripe_payment_intent_id,
            amount=reservation.amount,
            metadata={
                "reservation_id": str(reservation.id),
            },
            stripe_account=reservation.club.stripe_account_id,
            idempotency_key=f"reservation_refund_{reservation.id}",
        )
    except stripe.error.InvalidRequestError as exc:
        existing = _find_stripe_refund(reservation)
        if existing is not None:
            reservation.stripe_refund_id = existing.id
            return
        if getattr(exc, "code", None) in (
            "charge_already_refunded",
            "amount_too_large",
        ):
            raise RefundError(
                "Stripeでは、これ以上返金できる金額が残っていません。"
            )
        raise RefundError("Stripeでの返金に失敗しました。")
    except stripe.error.StripeError:
        raise RefundError("Stripeでの返金に失敗しました。")

    status = getattr(refund, "status", None) or refund.get("status")
    if status in ("failed", "canceled"):
        raise RefundError("Stripeでの返金に失敗しました。")

    reservation.stripe_refund_id = refund.id if hasattr(refund, "id") else refund["id"]


def _find_stripe_refund(reservation):
    page = stripe.Refund.list(
        payment_intent=reservation.stripe_payment_intent_id,
        limit=100,
        stripe_account=reservation.club.stripe_account_id,
    )
    refunds = page.data if hasattr(page, "data") else page["data"]

    for refund in refunds:
        metadata = getattr(refund, "metadata", None)
        if metadata is None and isinstance(refund, dict):
            metadata = refund.get("metadata") or {}
        metadata = metadata or {}
        status = getattr(refund, "status", None)
        if status is None and isinstance(refund, dict):
            status = refund.get("status")

        if status in ("failed", "canceled"):
            continue

        if str(metadata.get("reservation_id") or "") == str(reservation.id):
            return refund

    return None


def _payload(reservation, *, already_refunded):
    return {
        "reservation_id": reservation.id,
        "already_refunded": already_refunded,
        "payment_method": reservation.payment_method,
        "canceled": reservation.canceled,
        "refunded_at": (
            reservation.refunded_at.isoformat()
            if reservation.refunded_at
            else None
        ),
        "explanation": _explanation(reservation, already_refunded),
    }


def _explanation(reservation, already_refunded):
    if already_refunded:
        return "この予約はすでに返金済みです。"

    name = reservation.full_name or "お客様"
    lines = [
        f"{name}様の予約を返金します。この予約はキャンセルされます。",
        "",
    ]

    if reservation.payment_method == Reservation.PaymentMethod.TICKET:
        usage = _ticket_usage(reservation)
        ticket_name = "チケット"
        quantity = 1
        expired = False

        if usage is not None:
            quantity = usage.quantity
            ticket_name = usage.grant.ticket_type.name or ticket_name
            if (
                usage.grant.expires_at
                and usage.grant.expires_at < timezone.now()
            ):
                expired = True

        lines.append(
            f"使った「{ticket_name}」を{quantity}枚、同じチケットに戻します。"
        )

        if expired:
            lines.append(
                "このチケットの有効期限はすでに過ぎています。"
                "枚数は戻りますが、期限を過ぎた分は使えません。"
            )

        return "\n".join(lines)

    amount = f"¥{reservation.amount:,}"
    lines.append(
        f"お支払いの{amount}は、Stripeから{name}様へ返金されます。"
    )
    lines.append(
        "Stripeが引いた手数料と、Kaibaruが受け取った1%は戻りません。"
    )

    return "\n".join(lines)
