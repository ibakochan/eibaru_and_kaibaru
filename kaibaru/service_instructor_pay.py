"""Teacher statements. The monthly task only covers a fixed salary."""
import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from .manager_permissions import SELF_PAYMENT_MESSAGE, manages_own_money
from .models import InstructorPay, Lesson, Member, TeacherInvoice, TeacherInvoiceLesson

logger = logging.getLogger(__name__)


def previous_month(day):
    last = day.replace(day=1) - timedelta(days=1)
    return last.year, last.month


def create_monthly_teacher_invoices(year=None, month=None):
    """One statement per fixed salary for that calendar month.

    Called with no arguments, it uses the previous month in Asia/Tokyo.
    Running it again does not create a second statement.
    """
    if year is None or month is None:
        year, month = previous_month(timezone.localdate())

    pays = (
        InstructorPay.objects
        .filter(
            active=True,
            monthly_amount__gt=0,
            club__is_deleted=False,
        )
        .filter(
            Q(instructor__is_instructor=True) | Q(instructor__is_manager=True)
        )
        .select_related("club", "instructor")
    )
    created = 0
    for pay in pays:
        try:
            with transaction.atomic():
                _, was_created = TeacherInvoice.objects.get_or_create(
                    instructor_pay=pay,
                    year=year,
                    month=month,
                    defaults={
                        "club": pay.club,
                        "instructor": pay.instructor,
                        "amount": pay.monthly_amount,
                    },
                )
        except IntegrityError:
            logger.info(
                "[TEACHER PAY] statement already exists pay=%s %s-%s",
                pay.id,
                year,
                month,
            )
            continue
        if was_created:
            created += 1
    logger.info(
        "[TEACHER PAY] monthly statements year=%s month=%s created=%s",
        year,
        month,
        created,
    )
    return {"year": year, "month": month, "created": created}


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


def _lesson_lines(club, raw):
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        return None, "レッスンの指定が正しくありません。"
    seen = set()
    parsed = []
    for item in raw:
        if not isinstance(item, dict):
            return None, "レッスンの指定が正しくありません。"
        lesson_id = item.get("lesson_id")
        times = item.get("times_taught")
        if lesson_id in (None, "") and times in (None, ""):
            continue
        title = item.get("lesson_title") or ""
        if not isinstance(title, str):
            title = ""
        if lesson_id in (None, ""):
            if not title.strip():
                return None, "レッスンを選択してください。"
            try:
                times = int(times)
            except (TypeError, ValueError):
                return None, "回数は1以上の整数で入力してください。"
            if times < 1:
                return None, "回数は1以上の整数で入力してください。"
            parsed.append((None, times, title.strip()))
            continue
        try:
            lesson_id = int(lesson_id)
        except (TypeError, ValueError):
            return None, "レッスンを選択してください。"
        try:
            times = int(times)
        except (TypeError, ValueError):
            return None, "回数は1以上の整数で入力してください。"
        if times < 1:
            return None, "回数は1以上の整数で入力してください。"
        if lesson_id in seen:
            return None, "同じレッスンが2回選ばれています。"
        seen.add(lesson_id)
        lesson = Lesson.objects.filter(club=club, id=lesson_id).first()
        if not lesson:
            return None, "レッスンが見つかりません。"
        parsed.append((lesson, times, lesson.title or ""))
    return parsed, None


def _payable_member(club, instructor_id):
    try:
        instructor_id = int(instructor_id)
    except (TypeError, ValueError):
        return None
    member = Member.objects.filter(club=club, id=instructor_id).first()
    if member is None:
        return None
    if not member.is_instructor and not member.is_manager:
        return None
    return member


def _self_pay_block(actor, member):
    if manages_own_money(actor, member):
        return SELF_PAYMENT_MESSAGE
    return None


def _line_payload(line):
    return {
        "id": line.id,
        "lesson_id": line.lesson_id,
        "lesson_title": line.lesson_title,
        "times_taught": line.times_taught,
    }


def _invoice_payload(invoice):
    return {
        "id": invoice.id,
        "instructor_id": invoice.instructor_id,
        "year": invoice.year,
        "month": invoice.month,
        "amount": invoice.amount,
        "note": invoice.note,
        "paid_at": invoice.paid_at.isoformat() if invoice.paid_at else None,
        "lessons": [_line_payload(line) for line in invoice.lessons.all()],
    }


def _pay_payload(pay):
    if pay is None:
        return None
    return {
        "id": pay.id,
        "monthly_amount": pay.monthly_amount,
        "active": pay.active,
    }


def _replace_lines(invoice, parsed):
    invoice.lessons.all().delete()
    TeacherInvoiceLesson.objects.bulk_create([
        TeacherInvoiceLesson(
            invoice=invoice,
            lesson=lesson,
            lesson_title=title,
            times_taught=times,
        )
        for lesson, times, title in parsed
    ])


def teacher_pay_overview(club):
    instructors = list(
        club.members.filter(Q(is_instructor=True) | Q(is_manager=True)).order_by("full_name", "id")
    )
    pays = {
        pay.instructor_id: pay
        for pay in InstructorPay.objects.filter(club=club)
    }
    grouped = {}
    invoices = (
        TeacherInvoice.objects
        .filter(club=club, instructor__in=instructors)
        .prefetch_related("lessons")
    )
    for invoice in invoices:
        grouped.setdefault(invoice.instructor_id, []).append(_invoice_payload(invoice))
    lessons = club.lessons.all().order_by("weekday", "start_time", "id")
    return {
        "lessons": [
            {
                "id": lesson.id,
                "title": lesson.title,
                "weekday": lesson.weekday,
                "start_time": lesson.start_time.strftime("%H:%M"),
                "end_time": lesson.end_time.strftime("%H:%M"),
            }
            for lesson in lessons
        ],
        "instructors": [
            {
                "id": member.id,
                "full_name": member.full_name,
                "picture": member.picture.url if member.picture else None,
                "pay": _pay_payload(pays.get(member.id)),
                "invoices": grouped.get(member.id, []),
            }
            for member in instructors
        ],
    }


def save_instructor_pay(club, payload, actor=None):
    member = _payable_member(club, payload.get("instructor_id"))
    if not member:
        return None, "指導者が見つかりません。"
    blocked = _self_pay_block(actor, member)
    if blocked:
        return None, blocked
    amount, error = _yen(payload.get("monthly_amount"), required=False)
    if error:
        return None, error
    pay, _created = InstructorPay.objects.update_or_create(
        club=club,
        instructor=member,
        defaults={"monthly_amount": amount, "active": True},
    )
    return _pay_payload(pay), None


def create_teacher_invoice(club, payload, actor=None):
    member = _payable_member(club, payload.get("instructor_id"))
    if not member:
        return None, "指導者が見つかりません。"
    blocked = _self_pay_block(actor, member)
    if blocked:
        return None, blocked
    year, month, error = _year_month(payload)
    if error:
        return None, error
    amount, error = _yen(payload.get("amount"), required=True)
    if error:
        return None, error
    lines, error = _lesson_lines(club, payload.get("lessons"))
    if error:
        return None, error
    note = payload.get("note") or ""
    if not isinstance(note, str):
        return None, "メモの指定が正しくありません。"
    with transaction.atomic():
        invoice = TeacherInvoice.objects.create(
            club=club,
            instructor=member,
            year=year,
            month=month,
            amount=amount,
            note=note,
            paid_at=timezone.now() if _paid(payload.get("paid")) else None,
        )
        _replace_lines(invoice, lines)
    invoice = TeacherInvoice.objects.prefetch_related("lessons").get(id=invoice.id)
    return _invoice_payload(invoice), None


def update_teacher_invoice(club, invoice_id, payload, actor=None):
    invoice = (
        TeacherInvoice.objects
        .filter(club=club, id=invoice_id)
        .first()
    )
    if not invoice:
        return None, None
    if "instructor_id" in payload:
        member = _payable_member(club, payload.get("instructor_id"))
        if not member:
            return None, "指導者が見つかりません。"
        invoice.instructor = member
    blocked = _self_pay_block(actor, invoice.instructor)
    if blocked:
        return None, blocked
    if "year" in payload or "month" in payload:
        year, month, error = _year_month({
            "year": payload.get("year", invoice.year),
            "month": payload.get("month", invoice.month),
        })
        if error:
            return None, error
        invoice.year = year
        invoice.month = month
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
    lines = None
    if "lessons" in payload:
        lines, error = _lesson_lines(club, payload.get("lessons"))
        if error:
            return None, error
    try:
        with transaction.atomic():
            invoice.save()
            if lines is not None:
                _replace_lines(invoice, lines)
    except IntegrityError:
        return None, "この月の自動明細はすでにあります。"
    invoice = TeacherInvoice.objects.prefetch_related("lessons").get(id=invoice.id)
    return _invoice_payload(invoice), None


def delete_teacher_invoice(club, invoice_id, actor=None):
    invoice = TeacherInvoice.objects.filter(club=club, id=invoice_id).select_related("instructor").first()
    if invoice is None:
        return False, None
    blocked = _self_pay_block(actor, invoice.instructor)
    if blocked:
        return False, blocked
    invoice.delete()
    return True, None
