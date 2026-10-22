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
from django.db import IntegrityError
from django.db.models import Q, Sum
from django.utils import timezone

from .models import ClubMonthlyIncome, EventReservation, ExpenseReceipt, Invoice, Reservation
from .service_cash_platform_fee import month_bounds, previous_month
from .service_platform_billing_history import platform_amount_paid_in_month
from .service_paid_reservations import (
    paid_reservation_counts_by_month,
    paid_reservations_for_clubs,
)

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


def income_remainder(total_yen, platform_fee_yen, expense_total_yen):
    """Cash left after the platform bill and expense receipts for that month."""
    return (
        int(total_yen or 0)
        - int(platform_fee_yen or 0)
        - int(expense_total_yen or 0)
    )


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


def _payment_object_fee(payment, stripe_account):
    """Fee on one InvoicePayment.payment object.

    Subscription cycle invoices no longer carry charge or payment_intent.
    The fee lives on the invoice payment's charge balance transaction.
    """
    if not payment:
        return None
    intent = payment.get("payment_intent")
    if isinstance(intent, str):
        if not stripe_account:
            return None
        return _fee_from_payment_intent(stripe_account, intent)
    if intent:
        fee = _fee_from_charge(intent.get("latest_charge"), stripe_account)
        if fee is not None:
            return fee
        intent_id = intent.get("id")
        if intent_id and stripe_account:
            return _fee_from_payment_intent(stripe_account, intent_id)
        return None
    charge = payment.get("charge")
    if isinstance(charge, str) and not stripe_account:
        return None
    return _fee_from_charge(charge, stripe_account)


def fee_from_invoice_payments(rows, stripe_account=None):
    """Sum Stripe fees on paid invoice payments.

    None means a payment was there but its fee could not be read, so the
    invoice stays at the billed amount instead of a partial net.
    """
    if not rows:
        return None
    total = 0
    saw_payment = False
    for row in rows:
        status = row.get("status")
        if status not in (None, "paid"):
            continue
        payment = row.get("payment") or {}
        if not payment:
            continue
        fee = _payment_object_fee(payment, stripe_account)
        if fee is None:
            return None
        saw_payment = True
        total += int(fee)
    if not saw_payment:
        return None
    return total


def _invoice_payment_rows(stripe_account, stripe_invoice_id, expand):
    kwargs = {
        "invoice": stripe_invoice_id,
        "status": "paid",
        "limit": 20,
        "stripe_account": stripe_account,
    }
    if expand:
        kwargs["expand"] = expand
    payments = stripe.InvoicePayment.list(**kwargs)
    return list(payments.get("data") or [])


def _fee_from_stripe_invoice(stripe_account, stripe_invoice_id):
    def load():
        stripe.api_key = settings.STRIPE_SECRET_KEY
        rows = []
        try:
            rows = _invoice_payment_rows(
                stripe_account,
                stripe_invoice_id,
                [
                    "data.payment.payment_intent.latest_charge.balance_transaction",
                    "data.payment.charge.balance_transaction",
                ],
            )
        except Exception:
            logger.exception(
                "[INCOME] Invoice payment expand failed for %s",
                stripe_invoice_id,
            )
        fee = fee_from_invoice_payments(rows, stripe_account)
        if fee is not None:
            return fee
        rows = _invoice_payment_rows(stripe_account, stripe_invoice_id, None)
        fee = fee_from_invoice_payments(rows, stripe_account)
        if fee is not None:
            return fee
        stripe_invoice = stripe.Invoice.retrieve(
            stripe_invoice_id,
            stripe_account=stripe_account,
        )
        legacy = _fee_from_charge(stripe_invoice.get("charge"), stripe_account)
        if legacy is not None:
            return legacy
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


def persist_invoice_net(invoice, stripe_account=None):
    """Store Stripe's net on a paid invoice. Cash and bank transfers are skipped.

    A missing fee is left empty so a later open can try again. Amounts of 0
    are left empty too.
    """
    if getattr(invoice, "payment_method", None) != "stripe":
        return invoice
    if invoice.net_yen is not None:
        return invoice
    gross = _amount(invoice)
    if gross <= 0:
        return invoice
    account = stripe_account
    if not account:
        club = getattr(invoice, "club", None)
        account = getattr(club, "stripe_account_id", None)
    fee = _stripe_fee_for_invoice(invoice, account)
    if fee is None:
        return invoice
    invoice.net_yen = settled_yen(gross, "stripe", fee)
    invoice.save(update_fields=["net_yen"])
    return invoice


def _present_invoice_net(invoice):
    """Expose fee_yen for the serializer from the stored net."""
    gross = _amount(invoice)
    if invoice.payment_method == "stripe" and invoice.net_yen is not None:
        invoice.fee_yen = gross - int(invoice.net_yen)
    else:
        invoice.fee_yen = None


def _ensure_invoice_nets(invoices, stripe_account):
    for invoice in invoices:
        if (
            invoice.payment_method == "stripe"
            and invoice.net_yen is None
            and _amount(invoice) > 0
        ):
            persist_invoice_net(invoice, stripe_account)
        _present_invoice_net(invoice)


def _reservation_from_income_row(row):
    from .models import EventReservation, Reservation

    kind, pk = str(row.get("id") or "").split("-", 1)
    model = EventReservation if kind == "event" else Reservation
    return model.objects.filter(pk=int(pk)).first()


def persist_reservation_net(reservation):
    """Store each row's share of the Stripe fee for one payment.

    Trial, visitor, member, and event rows keep their own net. Rows that
    shared a charge split that one fee. A missing fee is left empty.
    """
    try:
        _persist_reservation_net(reservation)
    except Exception:
        logger.exception(
            "[INCOME] Could not store reservation net for %s",
            getattr(reservation, "id", None),
        )


def _persist_reservation_net(reservation):
    if reservation is None or getattr(reservation, "net_yen", None) is not None:
        return
    if getattr(reservation, "payment_method", None) == "ticket":
        return
    if int(getattr(reservation, "amount", 0) or 0) <= 0:
        return
    intent_id = getattr(reservation, "stripe_payment_intent_id", None)
    if not intent_id:
        return
    model = reservation.__class__
    siblings = list(
        model.objects.filter(
            stripe_payment_intent_id=intent_id,
            status="paid",
            is_deleted=False,
        ).order_by("id")
    )
    if not any(row.pk == reservation.pk for row in siblings):
        siblings.append(reservation)
        siblings.sort(key=lambda row: row.pk or 0)
    if all(row.net_yen is not None for row in siblings):
        return
    club = getattr(reservation, "club", None)
    account = getattr(club, "stripe_account_id", None)
    if not account:
        return
    fee = _fee_from_payment_intent(account, intent_id)
    if fee is None:
        return
    shares = allocate_fee_shares([row.amount for row in siblings], fee)
    for row, share in zip(siblings, shares):
        gross = int(row.amount or 0)
        net = settled_yen(gross, "stripe", share)
        row.net_yen = net
        if row.pk:
            type(row).objects.filter(pk=row.pk).update(net_yen=net)
        if row.pk == reservation.pk:
            reservation.net_yen = net


def _ensure_reservation_nets(reservations, stripe_account):
    groups = {}
    for reservation in reservations:
        intent_id = reservation.get("payment_intent_id") or ""
        groups.setdefault(intent_id, []).append(reservation)
    for intent_id, rows in groups.items():
        missing = [
            row for row in rows
            if row.get("net_yen") is None and int(row.get("amount") or 0) > 0 and intent_id
        ]
        if missing and stripe_account:
            anchor = _reservation_from_income_row(missing[0])
            if anchor is not None:
                persist_reservation_net(anchor)
                saved = {
                    row.pk: row.net_yen
                    for row in anchor.__class__.objects.filter(
                        stripe_payment_intent_id=intent_id,
                    )
                }
                for row in rows:
                    _, pk = str(row.get("id") or "").split("-", 1)
                    net = saved.get(int(pk))
                    if net is not None:
                        row["net_yen"] = net
        for row in rows:
            gross = int(row.get("amount") or 0)
            net = row.get("net_yen")
            if net is not None:
                row["fee_yen"] = gross - int(net)
                row["net_yen"] = int(net)
            else:
                row["fee_yen"] = None
                row["net_yen"] = gross if not intent_id else None
            row.pop("payment_intent_id", None)


def _month_of(paid_at):
    local = timezone.localtime(paid_at) if paid_at else None
    if not local:
        return None
    return local.year, local.month


def _expense_total_for_month(club, year, month):
    total = ExpenseReceipt.objects.filter(
        club=club,
        purchased_on__year=year,
        purchased_on__month=month,
    ).aggregate(total=Sum("amount"))["total"]
    return int(total or 0)


def member_income_index(club):
    """Months that have income, with counts only. No Stripe calls."""
    counts = {}
    invoices = Invoice.objects.filter(club=club, status="paid").prefetch_related(
        "payment_set"
    )
    for invoice in invoices:
        key = _month_of(_income_at(invoice))
        if not key:
            continue
        bucket = counts.setdefault(key, {"invoice_count": 0, "reservation_count": 0})
        bucket["invoice_count"] += 1

    for key, count in paid_reservation_counts_by_month([club.id]).items():
        bucket = counts.setdefault(key, {"invoice_count": 0, "reservation_count": 0})
        bucket["reservation_count"] += count

    entries = []
    for year, month in sorted(counts, reverse=True)[:MONTH_LIMIT]:
        bucket = counts[(year, month)]
        entries.append({
            "year": year,
            "month": month,
            "invoice_count": bucket["invoice_count"],
            "reservation_count": bucket["reservation_count"],
        })
    return entries


def _invoices_for_month(club, year, month):
    start, end = month_bounds(year, month)
    candidates = (
        Invoice.objects.filter(club=club, status="paid")
        .filter(
            Q(
                payment__status="succeeded",
                payment__paid_at__gte=start,
                payment__paid_at__lt=end,
            )
            | Q(
                payment__status="succeeded",
                payment__paid_at__isnull=True,
                payment__issued_at__gte=start,
                payment__issued_at__lt=end,
            )
            | Q(issued_at__gte=start, issued_at__lt=end)
        )
        .distinct()
        .select_related("subscription", "payer", "club")
        .prefetch_related("items", "payment_set")
    )
    rows = []
    for invoice in candidates:
        paid_at = _income_at(invoice)
        if _month_of(paid_at) != (year, month):
            continue
        invoice.income_at = paid_at
        rows.append(invoice)
    rows.sort(
        key=lambda invoice: (invoice.income_at or invoice.issued_at, invoice.id),
        reverse=True,
    )
    return rows


def _invoice_settled(invoice):
    gross = _amount(invoice)
    if invoice.payment_method == "stripe" and invoice.net_yen is not None:
        return int(invoice.net_yen)
    return gross


def _reservation_ids(reservations):
    lesson_ids = []
    event_ids = []
    for row in reservations:
        parts = str(row.get("id") or "").split("-", 1)
        if len(parts) != 2:
            continue
        kind, pk = parts
        try:
            pk_n = int(pk)
        except (TypeError, ValueError):
            continue
        if kind == "event":
            event_ids.append(pk_n)
        else:
            lesson_ids.append(pk_n)
    return lesson_ids, event_ids


def _income_changed_since(calculated_at, invoices, reservations):
    """True when this month has an invoice or reservation newer than the snapshot."""
    if calculated_at is None:
        return True
    for invoice in invoices:
        issued_at = getattr(invoice, "issued_at", None)
        if issued_at and issued_at > calculated_at:
            return True
    lesson_ids, event_ids = _reservation_ids(reservations)
    if lesson_ids and Reservation.objects.filter(
        pk__in=lesson_ids,
        created_at__gt=calculated_at,
    ).exists():
        return True
    if event_ids and EventReservation.objects.filter(
        pk__in=event_ids,
        created_at__gt=calculated_at,
    ).exists():
        return True
    return False


def _month_figures(club, user, year, month, start, invoices, reservations):
    invoice_total = sum(_invoice_settled(invoice) for invoice in invoices)
    reservation_total = sum(
        int(row.get("net_yen") if row.get("net_yen") is not None else row.get("amount") or 0)
        for row in reservations
    )
    prev_year, prev_month = previous_month(start)
    return (
        invoice_total + reservation_total,
        platform_amount_paid_in_month(user, prev_year, prev_month),
        _expense_total_for_month(club, year, month),
    )


def member_income_month(club, user, year, month):
    """One month of 収納歴.

    The first open stores the income total, the previous month's Kaibaru
    bill, and that month's receipt total. A later open recalculates those
    when an invoice issued after calculated_at, or a reservation created
    after it, is part of this month. Stripe rows missing a net are filled in.
    """
    year = int(year)
    month = int(month)
    start, _end = month_bounds(year, month)
    rows = _invoices_for_month(club, year, month)
    reservations = paid_reservations_for_clubs([club.id], year, month)
    for reservation in reservations:
        reservation.pop("income_at", None)
    reservations.sort(
        key=lambda row: (row.get("paid_on") or "", row["id"]),
        reverse=True,
    )
    stripe_account = getattr(club, "stripe_account_id", None)
    _ensure_invoice_nets(rows, stripe_account)
    _ensure_reservation_nets(reservations, stripe_account)

    record = ClubMonthlyIncome.objects.filter(club=club, month=start).first()
    if record is None or _income_changed_since(record.calculated_at, rows, reservations):
        total, platform_fee, expense_total = _month_figures(
            club, user, year, month, start, rows, reservations,
        )
        calculated_at = timezone.now()
        if record is None:
            try:
                record = ClubMonthlyIncome.objects.create(
                    club=club,
                    month=start,
                    total_yen=total,
                    platform_fee_yen=platform_fee,
                    receipt_total_yen=expense_total,
                    calculated_at=calculated_at,
                )
            except IntegrityError:
                record = ClubMonthlyIncome.objects.get(club=club, month=start)
        else:
            record.total_yen = total
            record.platform_fee_yen = platform_fee
            record.receipt_total_yen = expense_total
            record.calculated_at = calculated_at
            record.save(update_fields=[
                "total_yen",
                "platform_fee_yen",
                "receipt_total_yen",
                "calculated_at",
            ])

    total = int(record.total_yen or 0)
    platform_fee = int(record.platform_fee_yen or 0)
    expense_total = int(record.receipt_total_yen or 0)
    return {
        "year": year,
        "month": month,
        "invoice_count": len(rows),
        "reservation_count": len(reservations),
        "invoice_total_yen": sum(_invoice_settled(invoice) for invoice in rows),
        "reservation_total_yen": sum(
            int(row.get("net_yen") if row.get("net_yen") is not None else row.get("amount") or 0)
            for row in reservations
        ),
        "total_yen": total,
        "platform_fee_yen": platform_fee,
        "expense_total_yen": expense_total,
        "remainder_yen": income_remainder(total, platform_fee, expense_total),
        "invoices": rows,
        "reservations": reservations,
    }
