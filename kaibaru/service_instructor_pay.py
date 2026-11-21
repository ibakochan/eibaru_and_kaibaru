"""Teacher statements. A club creates them from monthly salary plus lessons taught."""
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


def _default_teachers(lesson):
    links = list(lesson.lesson_instructors.all())
    if links:
        return [
            link.instructor
            for link in links
            if link.instructor_id and link.instructor.is_instructor
        ]
    if lesson.instructor_id and lesson.instructor and lesson.instructor.is_instructor:
        return [lesson.instructor]
    return []


def _club_taught_sessions(club, year, month):
    """Lessons already held this month, with whoever taught that date.

    A LessonDateStaff row replaces the default instructors for that date.
    Canceled dates and future dates are left out.
    """
    from datetime import date as date_cls

    from .models import Lesson, LessonDateCancel, LessonDateStaff
    from .service_instructor_sessions import _month_dates, lesson_fee_yen

    today = timezone.localdate()
    start = date_cls(year, month, 1)
    end = date_cls(year + 1, 1, 1) if month == 12 else date_cls(year, month + 1, 1)
    lessons = list(
        Lesson.objects.filter(club=club)
        .select_related("instructor")
        .prefetch_related("lesson_instructors__instructor")
    )
    canceled = set(
        LessonDateCancel.objects.filter(
            lesson__club=club, date__gte=start, date__lt=end
        ).values_list("lesson_id", "date")
    )
    staff_rows = (
        LessonDateStaff.objects.filter(lesson__club=club, date__gte=start, date__lt=end)
        .prefetch_related("members__instructor")
    )
    staff_by = {(row.lesson_id, row.date): row for row in staff_rows}
    applied = set(
        TeacherInvoiceLesson.objects.filter(
            invoice__club=club,
            invoice__year=year,
            invoice__month=month,
            occurrence_date__isnull=False,
            lesson_id__isnull=False,
        ).values_list("lesson_id", "occurrence_date", "invoice__instructor_id")
    )
    sessions = []
    for lesson in lessons:
        fee = lesson_fee_yen(lesson)
        for day in _month_dates(year, month, lesson.weekday):
            if day > today or (lesson.id, day) in canceled:
                continue
            staff = staff_by.get((lesson.id, day))
            if staff is not None:
                people = [
                    link.instructor
                    for link in staff.members.all()
                    if link.instructor_id and link.instructor.is_instructor
                ]
                replaced = True
            else:
                people = _default_teachers(lesson)
                replaced = False
            sessions.append({
                "lesson_id": lesson.id,
                "lesson_title": lesson.title or "",
                "date": day.isoformat(),
                "start_time": lesson.start_time.strftime("%H:%M"),
                "fee_yen": fee,
                "replaced": replaced,
                "instructors": [
                    {"id": person.id, "name": person.full_name}
                    for person in people
                ],
                "applied_ids": [
                    person.id
                    for person in people
                    if (lesson.id, day, person.id) in applied
                ],
            })
    sessions.sort(key=lambda row: (row["date"], row["start_time"], row["lesson_id"]))
    return sessions


def preview_club_instructor_payslips(club, year, month, actor=None):
    """Monthly salary plus one fee per lesson taught. Nothing is written."""
    sessions = _club_taught_sessions(club, year, month)
    pays = {
        row.instructor_id: row
        for row in InstructorPay.objects.filter(club=club, active=True).select_related("instructor")
    }
    existing = {}
    for invoice in TeacherInvoice.objects.filter(club=club, year=year, month=month).select_related("instructor"):
        if invoice.instructor_id and invoice.instructor_id not in existing:
            existing[invoice.instructor_id] = invoice
    counts = {}
    for session in sessions:
        fee = session["fee_yen"]
        for person in session["instructors"]:
            bucket = counts.setdefault(person["id"], {
                "name": person["name"],
                "lesson_count": 0,
                "lesson_amount": 0,
            })
            bucket["lesson_count"] += 1
            if fee is not None and person["id"] not in session["applied_ids"]:
                bucket["lesson_amount"] += int(fee)
    people = {}
    for instructor_id, pay in pays.items():
        people[instructor_id] = pay.instructor.full_name
    for instructor_id, bucket in counts.items():
        people.setdefault(instructor_id, bucket["name"])
    members = {
        member.id: member
        for member in Member.objects.filter(club=club, id__in=list(people))
    }
    rows = []
    for instructor_id, name in people.items():
        pay = pays.get(instructor_id)
        monthly = int(pay.monthly_amount or 0) if pay and pay.monthly_amount else 0
        taught = counts.get(instructor_id) or {"lesson_count": 0, "lesson_amount": 0}
        invoice = existing.get(instructor_id)
        member = members.get(instructor_id)
        rows.append({
            "instructor_id": instructor_id,
            "name": name,
            "monthly_amount": monthly,
            "lesson_count": taught["lesson_count"],
            "lesson_amount": taught["lesson_amount"],
            "total_amount": monthly + taught["lesson_amount"],
            "created": invoice is not None,
            "invoice_id": invoice.id if invoice else None,
            "stored_amount": invoice.amount if invoice else None,
            "paid_at": invoice.paid_at.isoformat() if invoice and invoice.paid_at else None,
            "blocked": _self_pay_block(actor, member) if member is not None else None,
        })
    rows.sort(key=lambda row: row["name"])
    return {
        "year": year,
        "month": month,
        "rows": rows,
        "sessions": sessions,
    }


def create_club_instructor_payslips(club, year, month, actor=None):
    """Freeze monthly salary plus taught-lesson fees for this club.

    A statement that already exists for that instructor and month is left as it is.
    Lessons already on a statement are not added again.
    """
    from datetime import date as date_cls

    preview = preview_club_instructor_payslips(club, year, month, actor)
    sessions = preview["sessions"]
    pays = {
        row.instructor_id: row
        for row in InstructorPay.objects.filter(club=club, active=True)
    }
    members = {
        member.id: member
        for member in Member.objects.filter(club=club, id__in=[row["instructor_id"] for row in preview["rows"]])
    }
    created = 0
    for row in preview["rows"]:
        if row["created"] or row["total_amount"] <= 0:
            continue
        member = members.get(row["instructor_id"])
        if member is None or _self_pay_block(actor, member):
            continue
        pay = pays.get(member.id)
        own_sessions = [
            session for session in sessions
            if member.id in [person["id"] for person in session["instructors"]]
            and member.id not in session["applied_ids"]
            and session["fee_yen"] is not None
        ]
        try:
            with transaction.atomic():
                if pay is not None:
                    invoice, was_created = TeacherInvoice.objects.get_or_create(
                        instructor_pay=pay,
                        year=year,
                        month=month,
                        defaults={
                            "club": club,
                            "instructor": member,
                            "instructor_name": member.full_name,
                            "amount": row["total_amount"],
                        },
                    )
                else:
                    invoice = TeacherInvoice.objects.create(
                        club=club,
                        instructor=member,
                        instructor_name=member.full_name,
                        year=year,
                        month=month,
                        amount=row["total_amount"],
                    )
                    was_created = True
                if not was_created:
                    continue
                lesson_ids = {session["lesson_id"] for session in own_sessions}
                lessons = {
                    lesson.id: lesson
                    for lesson in Lesson.objects.filter(club=club, id__in=lesson_ids)
                }
                TeacherInvoiceLesson.objects.bulk_create([
                    TeacherInvoiceLesson(
                        invoice=invoice,
                        lesson=lessons.get(session["lesson_id"]),
                        lesson_title=session["lesson_title"],
                        times_taught=1,
                        occurrence_date=date_cls.fromisoformat(session["date"]),
                        fee_yen=int(session["fee_yen"]),
                        session_key=TeacherInvoiceLesson.make_session_key(
                            session["lesson_id"], session["date"]
                        ),
                    )
                    for session in own_sessions
                ])
        except IntegrityError:
            logger.info(
                "[TEACHER PAY] statement already exists club=%s instructor=%s %s-%s",
                club.id,
                member.id,
                year,
                month,
            )
            continue
        created += 1
    fresh = preview_club_instructor_payslips(club, year, month, actor)
    fresh["created_count"] = created
    return fresh, None


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
        "occurrence_date": (
            line.occurrence_date.isoformat()
            if getattr(line, "occurrence_date", None)
            else None
        ),
        "fee_yen": getattr(line, "fee_yen", None),
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
            # Hand-written / aggregate lines: unique per row on MySQL.
            session_key=TeacherInvoiceLesson.make_session_key(None, None),
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
    former = {}
    former_invoices = (
        TeacherInvoice.objects
        .filter(club=club, instructor__isnull=True)
        .prefetch_related("lessons")
        .order_by("-year", "-month", "id")
    )
    for invoice in former_invoices:
        former.setdefault(invoice.instructor_name or "削除された会員", []).append(
            _invoice_payload(invoice)
        )
    lessons = club.lessons.select_related("club").order_by("weekday", "start_time", "id")
    from .service_instructor_sessions import lesson_fee_yen

    return {
        "lessons": [
            {
                "id": lesson.id,
                "title": lesson.title,
                "weekday": lesson.weekday,
                "start_time": lesson.start_time.strftime("%H:%M"),
                "end_time": lesson.end_time.strftime("%H:%M"),
                "instructor_fee": lesson_fee_yen(lesson),
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
        "former_instructors": [
            {"full_name": name, "invoices": rows}
            for name, rows in former.items()
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
