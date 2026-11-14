"""Worker salary statements. Fixed monthly pay and/or hourly rate."""

import logging
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.db import IntegrityError, transaction
from django.utils import timezone

from .manager_permissions import SELF_PAYMENT_MESSAGE, manages_own_money
from .models import Member, WorkerInvoice, WorkerPay, WorkerProfile
from .service_instructor_pay import previous_month
from .service_worker_schedule import work_month_stats

logger = logging.getLogger(__name__)


def _yen(value, *, required):
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            return None, "金額を入力してください。"
        return None, None
    if isinstance(value, bool):
        return None, "金額は0以上の整数で入力してください。"
    if isinstance(value, str):
        text = value.strip()
        if not text.isdigit():
            return None, "金額は0以上の整数で入力してください。"
        value = int(text)
    elif isinstance(value, int):
        if value < 0:
            return None, "金額は0以上の整数で入力してください。"
    else:
        return None, "金額は0以上の整数で入力してください。"
    return value, None


def _year_month(payload):
    try:
        year = int(payload.get("year"))
        month = int(payload.get("month"))
    except (TypeError, ValueError):
        return None, None, "年月を入力してください。"
    if year < 2000 or year > 2100 or month < 1 or month > 12:
        return None, None, "年月が正しくありません。"
    return year, month, None


def _paid(value):
    return value in (True, 1, "1", "true", "True")


def _payable_worker(club, worker_id):
    try:
        worker_id = int(worker_id)
    except (TypeError, ValueError):
        return None
    member = Member.objects.filter(club=club, id=worker_id, is_worker=True).first()
    return member


def _self_pay_block(actor, member):
    if manages_own_money(actor, member):
        return SELF_PAYMENT_MESSAGE
    return None


def _invoice_payload(invoice):
    return {
        "id": invoice.id,
        "worker_id": invoice.worker_id,
        "worker_name": invoice.worker_name,
        "year": invoice.year,
        "month": invoice.month,
        "amount": invoice.amount,
        "hours_worked": str(invoice.hours_worked),
        "reservation_count": invoice.reservation_count,
        "note": invoice.note,
        "paid_at": invoice.paid_at.isoformat() if invoice.paid_at else None,
    }


def _pay_payload(pay):
    if pay is None:
        return None
    return {
        "id": pay.id,
        "monthly_amount": pay.monthly_amount,
        "active": pay.active,
        "note": pay.note,
    }


def _hourly_amount(worker, year, month):
    profile = WorkerProfile.objects.filter(worker=worker).first()
    if profile is None or profile.hourly_rate is None:
        return None, Decimal("0"), 0
    stats = work_month_stats(worker, year, month)
    hours = Decimal(stats["hours_worked"])
    amount = int(
        (hours * Decimal(profile.hourly_rate)).quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP,
        )
    )
    return amount, hours, stats["reservation_count"]


def create_monthly_worker_invoices(year=None, month=None):
    """One statement per active worker pay for that calendar month.

    Fixed monthly_amount wins. Otherwise hourly_rate * hours worked.
    """
    if year is None or month is None:
        year, month = previous_month(timezone.localdate())

    pays = (
        WorkerPay.objects
        .filter(active=True, club__is_deleted=False, worker__is_worker=True)
        .select_related("club", "worker")
    )
    created = 0
    for pay in pays:
        amount = pay.monthly_amount
        hours = Decimal("0")
        reservation_count = 0
        if amount is None or amount == 0:
            hourly, hours, reservation_count = _hourly_amount(pay.worker, year, month)
            if hourly is None or hourly <= 0:
                continue
            amount = hourly
        else:
            stats = work_month_stats(pay.worker, year, month)
            hours = Decimal(stats["hours_worked"])
            reservation_count = stats["reservation_count"]
        try:
            with transaction.atomic():
                _, was_created = WorkerInvoice.objects.get_or_create(
                    worker_pay=pay,
                    year=year,
                    month=month,
                    defaults={
                        "club": pay.club,
                        "worker": pay.worker,
                        "worker_name": pay.worker.full_name,
                        "amount": amount,
                        "hours_worked": hours,
                        "reservation_count": reservation_count,
                    },
                )
        except IntegrityError:
            logger.info(
                "[WORKER PAY] statement already exists pay=%s %s-%s",
                pay.id,
                year,
                month,
            )
            continue
        if was_created:
            created += 1
    logger.info(
        "[WORKER PAY] monthly statements year=%s month=%s created=%s",
        year,
        month,
        created,
    )
    return {"year": year, "month": month, "created": created}


def worker_pay_overview(club):
    workers = list(
        club.members.filter(is_worker=True).order_by("full_name", "id")
    )
    pays = {pay.worker_id: pay for pay in WorkerPay.objects.filter(club=club)}
    profiles = {
        p.worker_id: p
        for p in WorkerProfile.objects.filter(worker__in=workers)
    }
    grouped = {}
    invoices = WorkerInvoice.objects.filter(club=club, worker__in=workers)
    for invoice in invoices:
        grouped.setdefault(invoice.worker_id, []).append(_invoice_payload(invoice))
    former = {}
    for invoice in WorkerInvoice.objects.filter(club=club, worker__isnull=True).order_by(
        "-year", "-month", "id"
    ):
        former.setdefault(invoice.worker_name or "削除されたワーカー", []).append(
            _invoice_payload(invoice)
        )
    return {
        "workers": [
            {
                "id": member.id,
                "full_name": member.full_name,
                "picture": member.picture.url if member.picture else None,
                "hourly_rate": profiles.get(member.id).hourly_rate if profiles.get(member.id) else None,
                "monthly_hour_cap": (
                    profiles.get(member.id).monthly_hour_cap
                    if profiles.get(member.id)
                    else None
                ),
                "enrolled_at": (
                    profiles.get(member.id).enrolled_at.isoformat()
                    if profiles.get(member.id)
                    else None
                ),
                "pay": _pay_payload(pays.get(member.id)),
                # Invoice list ids/months only — month detail is loaded on demand.
                "invoice_months": [
                    {"id": inv["id"], "year": inv["year"], "month": inv["month"], "amount": inv["amount"], "paid_at": inv["paid_at"]}
                    for inv in grouped.get(member.id, [])
                ],
            }
            for member in workers
        ],
        "former_workers": [
            {"full_name": name, "invoice_months": [
                {"id": inv["id"], "year": inv["year"], "month": inv["month"], "amount": inv["amount"], "paid_at": inv["paid_at"]}
                for inv in rows
            ]}
            for name, rows in former.items()
        ],
    }


def save_worker_pay(club, payload, actor=None):
    member = _payable_worker(club, payload.get("worker_id"))
    if not member:
        return None, "ワーカーが見つかりません。"
    blocked = _self_pay_block(actor, member)
    if blocked:
        return None, blocked
    amount, error = _yen(payload.get("monthly_amount"), required=False)
    if error:
        return None, error
    note = payload.get("note") or ""
    if not isinstance(note, str):
        return None, "メモの指定が正しくありません。"
    pay, _created = WorkerPay.objects.update_or_create(
        club=club,
        worker=member,
        defaults={"monthly_amount": amount, "active": True, "note": note},
    )
    return _pay_payload(pay), None


def create_worker_invoice(club, payload, actor=None):
    member = _payable_worker(club, payload.get("worker_id"))
    if not member:
        return None, "ワーカーが見つかりません。"
    blocked = _self_pay_block(actor, member)
    if blocked:
        return None, blocked
    year, month, error = _year_month(payload)
    if error:
        return None, error
    amount, error = _yen(payload.get("amount"), required=True)
    if error:
        return None, error
    stats = work_month_stats(member, year, month)
    note = payload.get("note") or ""
    if not isinstance(note, str):
        return None, "メモの指定が正しくありません。"
    invoice = WorkerInvoice.objects.create(
        club=club,
        worker=member,
        worker_name=member.full_name,
        year=year,
        month=month,
        amount=amount,
        hours_worked=Decimal(stats["hours_worked"]),
        reservation_count=stats["reservation_count"],
        note=note,
        paid_at=timezone.now() if _paid(payload.get("paid")) else None,
    )
    return _invoice_payload(invoice), None


def update_worker_invoice(club, invoice_id, payload, actor=None):
    invoice = WorkerInvoice.objects.filter(club=club, id=invoice_id).first()
    if not invoice:
        return None, None
    blocked = _self_pay_block(actor, invoice.worker)
    if blocked:
        return None, blocked
    if "amount" in payload:
        amount, error = _yen(payload.get("amount"), required=True)
        if error:
            return None, error
        invoice.amount = amount
    if "note" in payload:
        note = payload.get("note") or ""
        if not isinstance(note, str):
            return None, "メモの指定が正しくありません。"
        invoice.note = note
    if "paid" in payload:
        invoice.paid_at = timezone.now() if _paid(payload.get("paid")) else None
    invoice.save()
    return _invoice_payload(invoice), None


def delete_worker_invoice(club, invoice_id, actor=None):
    invoice = WorkerInvoice.objects.filter(club=club, id=invoice_id).select_related(
        "worker"
    ).first()
    if invoice is None:
        return False, None
    blocked = _self_pay_block(actor, invoice.worker)
    if blocked:
        return False, blocked
    invoice.delete()
    return True, None


def worker_history_months(member):
    """Month index since enrollment — labels only, no reservation payloads."""
    profile = WorkerProfile.objects.filter(worker=member).first()
    enrolled = profile.enrolled_at if profile else timezone.localdate()
    today = timezone.localdate()
    months = []
    year, month = enrolled.year, enrolled.month
    while (year, month) <= (today.year, today.month):
        months.append({"year": year, "month": month})
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
    months.reverse()
    return {
        "worker_id": member.id,
        "enrolled_at": enrolled.isoformat(),
        "months": months,
    }


def worker_history_month(member, year, month):
    """Lazy detail for one month: hours, bookings, invoice/pay for that month."""
    stats = work_month_stats(member, year, month)
    invoice = WorkerInvoice.objects.filter(
        worker=member,
        year=year,
        month=month,
    ).first()
    pay = WorkerPay.objects.filter(club=member.club, worker=member).first()
    profile = WorkerProfile.objects.filter(worker=member).first()
    suggested = None
    if pay and pay.monthly_amount:
        suggested = pay.monthly_amount
    elif profile and profile.hourly_rate is not None:
        suggested = int(
            (Decimal(stats["hours_worked"]) * Decimal(profile.hourly_rate)).quantize(
                Decimal("1"),
                rounding=ROUND_HALF_UP,
            )
        )
    return {
        **stats,
        "pay": _pay_payload(pay),
        "hourly_rate": profile.hourly_rate if profile else None,
        "suggested_amount": suggested,
        "invoice": _invoice_payload(invoice) if invoice else None,
    }
