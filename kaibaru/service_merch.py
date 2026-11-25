"""Shop stock, checkout, cash receipt, and refunds.

A sale holds quantity the same way a reservation holds a spot: an unpaid
card checkout counts for 31 minutes. Cash is taken out of stock when the
order is placed and marked received at the start of the next date.
"""

from __future__ import annotations

import secrets
from datetime import datetime, time, timedelta

import stripe
from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .charge_amounts import normalize_paid_amount, persisted_charge_amount, platform_fee_kwargs
from .models import Member, MerchOrder, StockItem, Subscription

HOLD_MINUTES = 31
CHECKOUT_MINUTES = 30


class MerchError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def _picture_url(item):
    if not item.picture:
        return ""
    try:
        return item.picture.url
    except Exception:
        return ""


def effective_audience(item):
    return item.audience or item.club.merch_audience or "both"


def effective_payment(item):
    option = item.payment_options or item.club.merch_payment_options or "stripe"
    if not item.club.stripe_account_id and option != "cash":
        return "cash"
    return option


def clean_item_fields(*, quantity, quantity_for_sale, price, audience, payment_options):
    if quantity < 0 or quantity_for_sale < 0:
        raise MerchError("数量は0以上にしてください。")
    if quantity_for_sale > quantity:
        raise MerchError("販売数は在庫数より多くできません。")
    if audience not in ("", "both", "members", "visitors"):
        raise MerchError("販売対象の設定が正しくありません。")
    if payment_options not in ("", "stripe", "cash", "both"):
        raise MerchError("支払い方法の設定が正しくありません。")
    if quantity_for_sale == 0:
        return price if price not in ("", None) else None
    if price in ("", None):
        raise MerchError("販売するときは料金を入力してください。")
    try:
        price = int(price)
    except (TypeError, ValueError):
        raise MerchError("料金は0円、または50円以上にしてください。")
    if price != 0 and price < 50:
        raise MerchError("料金は0円、または50円以上にしてください。")
    if price < 0:
        raise MerchError("料金は0円、または50円以上にしてください。")
    return price


def _held_quantity(item, now):
    cutoff = now - timedelta(minutes=HOLD_MINUTES)
    held = MerchOrder.objects.filter(
        item=item,
        status=MerchOrder.Status.UNPAID,
        canceled=False,
        stock_taken=False,
        created_at__gte=cutoff,
    ).aggregate(total=Sum("quantity"))["total"]
    return int(held or 0)


def available_for_sale(item, now=None):
    now = now or timezone.now()
    return max(0, int(item.quantity_for_sale or 0) - _held_quantity(item, now))


def public_item(item, now=None):
    now = now or timezone.now()
    price = item.price
    listed = int(item.quantity_for_sale or 0) > 0 and price is not None
    return {
        "id": item.id,
        "name": item.name,
        "description": item.description,
        "picture": _picture_url(item),
        "category": item.category,
        "detail": item.detail,
        "price": price,
        "available": available_for_sale(item, now) if listed else 0,
        "audience": effective_audience(item),
        "payment_options": effective_payment(item),
        "for_sale": listed,
    }


def staff_item(item):
    payload = public_item(item)
    payload.update({
        "quantity": item.quantity,
        "quantity_for_sale": item.quantity_for_sale,
        "audience_override": item.audience,
        "payment_override": item.payment_options,
        "for_sale": int(item.quantity_for_sale or 0) > 0 and item.price is not None,
    })
    return payload


def _buyer_member(user, club):
    if not user or not user.is_authenticated:
        return None
    return (
        Member.objects.filter(club=club, is_worker=False)
        .filter(models_q(user))
        .order_by("id")
        .first()
    )


def models_q(user):
    from django.db.models import Q

    return Q(user=user) | Q(owner=user)


def _audience_ok(item, member):
    audience = effective_audience(item)
    if audience == "both":
        return True
    if audience == "members":
        return member is not None
    return member is None


def _payment_ok(item, method):
    option = effective_payment(item)
    if option == "both":
        return method in ("stripe", "cash")
    return method == option


def _take_stock(order):
    if order.stock_taken or order.item_id is None:
        return
    item = StockItem.objects.select_for_update().get(id=order.item_id)
    if item.quantity < order.quantity or item.quantity_for_sale < order.quantity:
        raise MerchError("在庫が足りなくなりました。")
    item.quantity -= order.quantity
    item.quantity_for_sale -= order.quantity
    item.save(update_fields=["quantity", "quantity_for_sale"])
    order.stock_taken = True


def _restore_stock(order):
    if not order.stock_taken or order.item_id is None:
        order.stock_taken = False
        return
    item = StockItem.objects.select_for_update().get(id=order.item_id)
    item.quantity += order.quantity
    item.quantity_for_sale += order.quantity
    item.save(update_fields=["quantity", "quantity_for_sale"])
    order.stock_taken = False


def _return_origin(club, origin):
    allowed = {f"https://{club.subdomain}.kaibaru.jp"}
    if club.custom_domain:
        allowed.add(f"https://{club.custom_domain}")
    if origin in allowed:
        return origin
    return f"https://{club.subdomain}.kaibaru.jp"


def place_order(*, club, item_id, quantity, method, user, full_name, email, origin):
    if not club.show_merch:
        raise MerchError("いまは物販を受け付けていません。")
    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        quantity = 0
    if quantity < 1:
        raise MerchError("数量を選んでください。")
    member = _buyer_member(user, club)
    now = timezone.now()
    with transaction.atomic():
        item = (
            StockItem.objects.select_for_update()
            .filter(id=item_id, club=club)
            .first()
        )
        if item is None or item.price is None or item.quantity_for_sale <= 0:
            raise MerchError("この商品は販売していません。", 404)
        if not _audience_ok(item, member):
            who = "会員" if effective_audience(item) == "members" else "会員以外"
            raise MerchError(f"この商品は{who}だけが購入できます。")
        if not _payment_ok(item, method):
            raise MerchError("この支払い方法は選べません。")
        if available_for_sale(item, now) < quantity:
            raise MerchError("販売中の数が足りません。")
        unit = int(item.price)
        amount = normalize_paid_amount(unit, allow_free=True) * quantity
        order = MerchOrder.objects.create(
            item=item,
            club=club,
            member=member,
            user=user if user and user.is_authenticated else None,
            item_name=item.name,
            quantity=quantity,
            unit_price=unit,
            amount=amount,
            full_name=(full_name or (member.full_name if member else "") or "")[:200],
            email=(email or (getattr(user, "email", "") if user and user.is_authenticated else "") or ""),
            buyer_kind="member" if member else "visitor",
            payment_method="cash" if amount == 0 else method,
            status=MerchOrder.Status.UNPAID,
        )
        if amount == 0 or method == "cash":
            _take_stock(order)
            order.status = MerchOrder.Status.PAID
            if amount == 0:
                order.paid_at = now
                order.payment_method = "cash"
            order.save()
            return {"order": _order_payload(order), "paid": True}

    charged = _charge_saved_card(order)
    if charged:
        return charged
    try:
        session = _start_checkout(order, origin)
    except Exception:
        cancel_open_order(order.id)
        raise MerchError("カード決済を開始できませんでした。")
    return {"order": _order_payload(order), "checkout_url": session.url, "paid": False}


def _charge_saved_card(order):
    member = order.member
    club = order.club
    if member is None or not member.owner_id or not club.stripe_account_id:
        return None
    if not Subscription.objects.filter(
        owner=member.owner,
        club=club,
        billing_method="stripe",
        status="active",
    ).exists():
        return None
    from .stripe_service import get_or_create_stripe_customer

    stripe.api_key = settings.STRIPE_SECRET_KEY
    try:
        customer = get_or_create_stripe_customer(member.owner, club)
        stripe_customer = stripe.Customer.retrieve(
            customer.stripe_customer_id,
            expand=["invoice_settings.default_payment_method"],
            stripe_account=club.stripe_account_id,
        )
        default_payment_method = (
            stripe_customer.get("invoice_settings", {}).get("default_payment_method")
        )
        if not default_payment_method:
            return None
        payment_method_id = (
            default_payment_method["id"]
            if isinstance(default_payment_method, dict)
            else default_payment_method
        )
        charge_amount = persisted_charge_amount(order)
        intent = stripe.PaymentIntent.create(
            amount=charge_amount,
            currency="jpy",
            **platform_fee_kwargs(charge_amount),
            customer=customer.stripe_customer_id,
            payment_method=payment_method_id,
            off_session=True,
            confirm=True,
            metadata={
                "merch_order_id": str(order.id),
                "club_id": str(club.id),
                "kind": "merch",
            },
            stripe_account=club.stripe_account_id,
            idempotency_key=f"merch_order_{order.id}",
        )
    except (stripe.error.CardError, stripe.error.InvalidRequestError, stripe.error.StripeError):
        return None
    if intent.status != "succeeded":
        return None
    _mark_stripe_paid(order.id, intent.id, "")
    order.refresh_from_db()
    return {"order": _order_payload(order), "paid": True}


def _start_checkout(order, origin):
    stripe.api_key = settings.STRIPE_SECRET_KEY
    club = order.club
    base = _return_origin(club, origin)
    charge_amount = persisted_charge_amount(order)
    kwargs = {
        "mode": "payment",
        "payment_method_types": ["card"],
        "expires_at": int((timezone.now() + timedelta(minutes=CHECKOUT_MINUTES)).timestamp()),
        "line_items": [{
            "price_data": {
                "currency": "jpy",
                "product_data": {"name": order.item_name[:250]},
                "unit_amount": order.unit_price if order.quantity else charge_amount,
            },
            "quantity": order.quantity,
        }],
        "metadata": {
            "merch_order_id": str(order.id),
            "club_id": str(club.id),
            "kind": "merch",
        },
        "success_url": f"{base}/?merch_order=success&merch_order_id={order.id}",
        "cancel_url": f"{base}/?merch_order=cancel&merch_order_id={order.id}",
        "stripe_account": club.stripe_account_id,
        "idempotency_key": f"merch_checkout_{order.id}_{secrets.token_hex(4)}",
    }
    fee = platform_fee_kwargs(charge_amount)
    if fee:
        kwargs["payment_intent_data"] = fee
    if order.user_id:
        from .stripe_service import get_or_create_stripe_customer

        customer = get_or_create_stripe_customer(order.user, club)
        kwargs["customer"] = customer.stripe_customer_id
    elif order.email:
        kwargs["customer_email"] = order.email
    session = stripe.checkout.Session.create(**kwargs)
    order.stripe_checkout_session_id = session.id
    order.save(update_fields=["stripe_checkout_session_id"])
    return session


def _mark_stripe_paid(order_id, payment_intent_id, session_id):
    with transaction.atomic():
        order = (
            MerchOrder.objects.select_for_update()
            .select_related("item")
            .filter(id=order_id, is_deleted=False)
            .first()
        )
        if order is None or order.canceled or order.refunded_at:
            return None
        if order.status != MerchOrder.Status.PAID:
            _take_stock(order)
            order.status = MerchOrder.Status.PAID
            order.paid_at = timezone.now()
        if payment_intent_id:
            order.stripe_payment_intent_id = payment_intent_id
        if session_id:
            order.stripe_checkout_session_id = session_id
        order.payment_method = "stripe"
        order.save()
    from .service_member_income import persist_reservation_net

    order.refresh_from_db()
    persist_reservation_net(order)
    return order


def complete_paid_merch_checkout(*, order_id, session_id, payment_intent_id):
    return _mark_stripe_paid(order_id, payment_intent_id or "", session_id or "")


def cancel_open_order(order_id):
    with transaction.atomic():
        order = MerchOrder.objects.select_for_update().filter(id=order_id).first()
        if order is None or order.status == MerchOrder.Status.PAID or order.stock_taken:
            return
        order.canceled = True
        order.save(update_fields=["canceled"])


def reconcile_open_merch_orders(now=None):
    now = now or timezone.now()
    cutoff = now - timedelta(minutes=HOLD_MINUTES)
    stripe.api_key = settings.STRIPE_SECRET_KEY
    orders = MerchOrder.objects.filter(
        status=MerchOrder.Status.UNPAID,
        canceled=False,
        payment_method="stripe",
        created_at__lt=cutoff,
    ).select_related("club")
    completed = 0
    for order in orders:
        session = None
        if order.stripe_checkout_session_id and order.club.stripe_account_id:
            try:
                session = stripe.checkout.Session.retrieve(
                    order.stripe_checkout_session_id,
                    stripe_account=order.club.stripe_account_id,
                )
            except stripe.error.StripeError:
                session = None
        if session and session.get("payment_status") == "paid":
            _mark_stripe_paid(
                order.id,
                session.get("payment_intent") or "",
                session.get("id") or "",
            )
            completed += 1
        else:
            cancel_open_order(order.id)
    return completed


def mark_cash_merch_paid(now=None):
    """Mark pay-in-person merch received at the start of the next date."""
    now = now or timezone.now()
    if timezone.is_naive(now):
        now = timezone.make_aware(now, timezone.get_current_timezone())
    today = timezone.localtime(now).date()
    start = timezone.make_aware(
        datetime.combine(today, time.min),
        timezone.get_current_timezone(),
    )
    return MerchOrder.objects.filter(
        payment_method="cash",
        status=MerchOrder.Status.PAID,
        canceled=False,
        is_deleted=False,
        paid_at__isnull=True,
        refunded_at__isnull=True,
        amount__gt=0,
        created_at__lt=start,
    ).update(paid_at=now)


def void_cash_order(*, order_id, user):
    with transaction.atomic():
        order = (
            MerchOrder.objects.select_for_update()
            .select_related("club")
            .filter(id=order_id, is_deleted=False)
            .first()
        )
        if order is None:
            raise MerchError("注文が見つかりません。", 404)
        _assert_staff(order, user)
        if order.payment_method != "cash" or order.refunded_at or order.canceled:
            raise MerchError("現地払いの未処理の注文だけ取り消せます。")
        if order.platform_fee_applied:
            raise MerchError("利用料の計算が終わった注文は取り消せません。")
        _restore_stock(order)
        order.canceled = True
        order.paid_at = None
        order.save(update_fields=["canceled", "paid_at", "stock_taken"])
    return _order_payload(order)


def refund_merch_order(*, order_id, user):
    with transaction.atomic():
        order = (
            MerchOrder.objects.select_for_update()
            .select_related("club", "item")
            .filter(id=order_id, is_deleted=False)
            .first()
        )
        if order is None:
            raise MerchError("注文が見つかりません。", 404)
        _assert_staff(order, user)
        if order.refunded_at:
            return _order_payload(order)
        if (
            order.payment_method != "stripe"
            or order.status != MerchOrder.Status.PAID
            or not order.stripe_payment_intent_id
            or not order.club.stripe_account_id
            or order.amount <= 0
        ):
            raise MerchError("Stripeで支払われた注文だけ返金できます。")
        order.refund_started_at = timezone.now()
        order.save(update_fields=["refund_started_at"])

    stripe.api_key = settings.STRIPE_SECRET_KEY
    try:
        refund = stripe.Refund.create(
            payment_intent=order.stripe_payment_intent_id,
            amount=order.amount,
            metadata={"merch_order_id": str(order.id)},
            stripe_account=order.club.stripe_account_id,
            idempotency_key=f"merch_refund_{order.id}",
        )
    except stripe.error.StripeError:
        raise MerchError("Stripeでの返金に失敗しました。")
    refund_id = refund.id if hasattr(refund, "id") else refund.get("id")

    with transaction.atomic():
        order = MerchOrder.objects.select_for_update().select_related("item").get(id=order_id)
        if not order.refunded_at:
            _restore_stock(order)
            order.refunded_at = timezone.now()
            order.stripe_refund_id = refund_id or ""
            order.canceled = True
            order.save(update_fields=["refunded_at", "stripe_refund_id", "canceled", "stock_taken"])
    order.refresh_from_db()
    return _order_payload(order)


def _assert_staff(order, user):
    from .manager_permissions import has_permission

    if not user or not user.is_authenticated:
        raise MerchError("許可なし", 403)
    if order.club.owner_id == user.id or has_permission(user, order.club, "stock"):
        return
    raise MerchError("許可なし", 403)


def _order_payload(order):
    return {
        "id": order.id,
        "item_id": order.item_id,
        "item_name": order.item_name,
        "quantity": order.quantity,
        "amount": order.amount,
        "full_name": order.full_name,
        "payment_method": order.payment_method,
        "status": order.status,
        "canceled": order.canceled,
        "paid_at": order.paid_at.isoformat() if order.paid_at else None,
        "refunded_at": order.refunded_at.isoformat() if order.refunded_at else None,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "can_refund": (
            order.payment_method == "stripe"
            and order.status == MerchOrder.Status.PAID
            and not order.refunded_at
            and not order.canceled
            and bool(order.stripe_payment_intent_id)
        ),
        "can_void": (
            order.payment_method == "cash"
            and not order.canceled
            and not order.platform_fee_applied
            and order.amount > 0
        ),
    }
