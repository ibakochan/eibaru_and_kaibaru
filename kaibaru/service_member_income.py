"""Paid member invoices for one club, grouped by the month they were paid.

The month is the latest succeeded payment time. Cash payments often have no
paid_at, so the payment row's issued_at is used. Invoices with no payment
row fall back to issued_at.

Stripe charges are counted after Stripe's fee (Kaibaru 1%, processing, and
tax). Cash and bank transfers stay at the billed amount. That sum is what
reached the club.
"""

import logging

import stripe
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from .models import Invoice
from .service_paid_reservations import paid_reservations_for_clubs

logger = logging.getLogger(__name__)

MONTH_LIMIT = 36
_FEE_CACHE_SECONDS = 60 * 60 * 24


def _income_at(invoice):
    stamps = []
    for payment in invoice.payment_set.all():
        if payment.status != "succeeded":
            continue
        stamp = payment.paid_at or payment.issued_at
        if stamp:
            stamps.append(stamp)
    if stamps:
        return max(stamps)
    return invoice.issued_at


def _amount(invoice):
    try:
        return int(invoice.amount_paid or invoice.amount_due or 0)
    except (TypeError, ValueError):
        return 0


def allocate_fee_shares(amounts, fee):
    """Split one Stripe fee across charges that shared a payment.

    The last positive amount receives the remainder so the shares add up
    to the fee.
    """
    try:
        fee = int(fee or 0)
    except (TypeError, ValueError):
        fee = 0
    cleaned = []
    for amount in amounts:
        try:
            cleaned.append(int(amount or 0))
        except (TypeError, ValueError):
            cleaned.append(0)
    if fee <= 0 or not cleaned:
        return [0] * len(cleaned)
    total = sum(amount for amount in cleaned if amount > 0)
    if total <= 0:
        return [0] * len(cleaned)
    last = max(index for index, amount in enumerate(cleaned) if amount > 0)
    shares = []
    remaining = fee
    for index, amount in enumerate(cleaned):
        if amount <= 0:
            shares.append(0)
            continue
        if index == last:
            share = remaining
        else:
            share = amount * fee // total
            remaining -= share
        shares.append(share)
    return shares


def settled_yen(gross, payment_method, fee_yen):
    """Cash and bank transfers stay whole. Stripe is gross minus Stripe's fee."""
    try:
        gross = int(gross or 0)
    except (TypeError, ValueError):
        gross = 0
    if payment_method != "stripe" or fee_yen is None:
        return gross
    try:
        fee = int(fee_yen)
    except (TypeError, ValueError):
        return gross
    return gross - fee


def _cached_fee(account, key, load):
    cache_key = f"stripe-income-fee:{account}:{key}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached if cached >= 0 else None
    try:
        fee = load()
    except Exception:
        logger.exception("[INCOME] Stripe fee lookup failed for %s", key)
        return None
    if fee is None:
        return None
    cache.set(cache_key, int(fee), _FEE_CACHE_SECONDS)
    return int(fee)


def _fee_from_balance_transaction(balance_transaction, stripe_account):
    if isinstance(balance_transaction, str):
        balance_transaction = stripe.BalanceTransaction.retrieve(
            balance_transaction,
            stripe_account=stripe_account,
        )
    if not balance_transaction:
        return None
    return int(balance_transaction.get("fee") or 0)


def _fee_from_charge(charge, stripe_account):
    if isinstance(charge, str):
        charge = stripe.Charge.retrieve(
            charge,
            expand=["balance_transaction"],
            stripe_account=stripe_account,
        )
    if not charge:
        return None
    return _fee_from_balance_transaction(
        charge.get("balance_transaction"),
        stripe_account,
    )


def _fee_from_payment_intent(stripe_account, payment_intent_id):
    def load():
        stripe.api_key = settings.STRIPE_SECRET_KEY
        intent = stripe.PaymentIntent.retrieve(
            payment_intent_id,
            expand=["latest_charge.balance_transaction"],
            stripe_account=stripe_account,
        )
        return _fee_from_charge(intent.get("latest_charge"), stripe_account)

    return _cached_fee(stripe_account, f"pi:{payment_intent_id}", load)


def _fee_from_stripe_invoice(stripe_account, stripe_invoice_id):
    def load():
        stripe.api_key = settings.STRIPE_SECRET_KEY
        stripe_invoice = stripe.Invoice.retrieve(
            stripe_invoice_id,
            expand=[
                "charge.balance_transaction",
                "payment_intent.latest_charge.balance_transaction",
            ],
            stripe_account=stripe_account,
        )
        fee = _fee_from_charge(stripe_invoice.get("charge"), stripe_account)
        if fee is not None:
            return fee
        intent = stripe_invoice.get("payment_intent")
        if isinstance(intent, str):
            return _fee_from_payment_intent(stripe_account, intent)
        if intent:
            return _fee_from_charge(intent.get("latest_charge"), stripe_account)
        return None

    return _cached_fee(stripe_account, f"in:{stripe_invoice_id}", load)


def _stripe_fee_for_invoice(invoice, stripe_account):
    if getattr(invoice, "payment_method", None) != "stripe" or not stripe_account:
        return None
    intent_ids = []
    for payment in invoice.payment_set.all():
        if (
            payment.status == "succeeded"
            and payment.method == "stripe"
            and payment.stripe_payment_intent_id
        ):
            intent_ids.append(payment.stripe_payment_intent_id)
    if intent_ids:
        fees = []
        for intent_id in dict.fromkeys(intent_ids):
            fee = _fee_from_payment_intent(stripe_account, intent_id)
            if fee is None:
                return None
            fees.append(fee)
        return sum(fees)
    stripe_invoice_id = getattr(invoice, "stripe_invoice_id", None)
    if not stripe_invoice_id:
        return None
    return _fee_from_stripe_invoice(stripe_account, stripe_invoice_id)


def _apply_invoice_fees(invoices, stripe_account):
    for invoice in invoices:
        gross = _amount(invoice)
        fee = _stripe_fee_for_invoice(invoice, stripe_account)
        invoice.fee_yen = fee
        invoice.net_yen = settled_yen(gross, invoice.payment_method, fee)


def _apply_reservation_fees(reservations, stripe_account):
    groups = {}
    for reservation in reservations:
        intent_id = reservation.get("payment_intent_id") or ""
        groups.setdefault(intent_id, []).append(reservation)
    for intent_id, rows in groups.items():
        fee = None
        if intent_id and stripe_account:
            fee = _fee_from_payment_intent(stripe_account, intent_id)
        shares = (
            allocate_fee_shares([row.get("amount") for row in rows], fee)
            if fee is not None
            else [None] * len(rows)
        )
        for reservation, share in zip(rows, shares):
            gross = int(reservation.get("amount") or 0)
            reservation["fee_yen"] = share
            reservation["net_yen"] = settled_yen(
                gross,
                "stripe" if intent_id else None,
                share,
            )
            reservation.pop("payment_intent_id", None)


def member_income_for_club(club):
    invoices = (
        Invoice.objects.filter(club=club, status="paid")
        .select_related("subscription", "payer", "club")
        .prefetch_related("items", "payment_set")
    )
    months = {}
    for invoice in invoices:
        paid_at = _income_at(invoice)
        invoice.income_at = paid_at
        local = timezone.localtime(paid_at) if paid_at else None
        if not local:
            continue
        months.setdefault((local.year, local.month), []).append(invoice)

    reservation_months = {}
    for reservation in paid_reservations_for_clubs([club.id]):
        income_at = reservation.get("income_at")
        local = timezone.localtime(income_at) if income_at else None
        if not local:
            continue
        reservation.pop("income_at", None)
        reservation_months.setdefault((local.year, local.month), []).append(reservation)

    entries = []
    keys = sorted(set(months) | set(reservation_months), reverse=True)[:MONTH_LIMIT]
    for year, month in keys:
        rows = months.get((year, month), [])
        reservations = reservation_months.get((year, month), [])
        rows.sort(
            key=lambda invoice: (invoice.income_at or invoice.issued_at, invoice.id),
            reverse=True,
        )
        reservations.sort(key=lambda row: (row.get("paid_on") or "", row["id"]), reverse=True)
        stripe_account = getattr(club, "stripe_account_id", None)
        _apply_invoice_fees(rows, stripe_account)
        _apply_reservation_fees(reservations, stripe_account)
        invoice_total = sum(
            settled_yen(_amount(invoice), invoice.payment_method, invoice.fee_yen)
            for invoice in rows
        )
        reservation_total = sum(int(row.get("net_yen") or 0) for row in reservations)
        entries.append({
            "year": year,
            "month": month,
            "invoice_count": len(rows),
            "reservation_count": len(reservations),
            "invoice_total_yen": invoice_total,
            "reservation_total_yen": reservation_total,
            "total_yen": invoice_total + reservation_total,
            "invoices": rows,
            "reservations": reservations,
        })
    return entries
