"""Dated lesson sessions for instructor pay (fees, substitutes, cancels)."""

from __future__ import annotations

import calendar
from datetime import date

from django.db import transaction
from django.db.models import Prefetch, Q, Sum
from django.utils import timezone

from .manager_permissions import SELF_PAYMENT_MESSAGE, manages_own_money
from .models import (
    Lesson,
    LessonDateCancel,
    LessonDateStaff,
    LessonDateStaffMember,
    LessonInstructor,
    Member,
    TeacherInvoice,
    TeacherInvoiceLesson,
)


def lesson_fee_yen(lesson):
    """Effective fee for one instructor teaching this lesson once."""
    if lesson.instructor_fee is not None:
        return int(lesson.instructor_fee)
    club_fee = getattr(lesson.club, "default_instructor_lesson_fee", None)
    if club_fee is not None:
        return int(club_fee)
    return None


def default_instructors(lesson):
    links = list(
        LessonInstructor.objects.filter(lesson=lesson)
        .select_related("instructor")
        .order_by("position", "id")
    )
    if links:
        return [link.instructor for link in links if link.instructor_id]
    if lesson.instructor_id and lesson.instructor:
        return [lesson.instructor]
    return []


def is_lesson_canceled(lesson, day: date) -> bool:
    return LessonDateCancel.objects.filter(lesson=lesson, date=day).exists()


def assert_lesson_date_open(lesson, day: date):
    """Raise ValueError when this occurrence was marked 休講."""
    if is_lesson_canceled(lesson, day):
        raise ValueError("この日のレッスンは休講です。")


def instructors_for_date(lesson, day: date):
    """Who teaches on this date. None when canceled; else list (maybe empty)."""
    if is_lesson_canceled(lesson, day):
        return None
    staff = (
        LessonDateStaff.objects.filter(lesson=lesson, date=day)
        .prefetch_related(
            Prefetch(
                "members",
                queryset=LessonDateStaffMember.objects.select_related(
                    "instructor"
                ).order_by("position", "id"),
            )
        )
        .first()
    )
    if staff is not None:
        return [
            row.instructor
            for row in staff.members.all()
            if row.instructor_id and row.instructor.is_instructor
        ]
    return default_instructors(lesson)


def _month_dates(year, month, weekday):
    """All dates in the month that fall on weekday (0=Mon)."""
    days_in_month = calendar.monthrange(year, month)[1]
    return [
        date(year, month, day)
        for day in range(1, days_in_month + 1)
        if date(year, month, day).weekday() == weekday
    ]


def _applied_keys(instructor, year, month):
    rows = TeacherInvoiceLesson.objects.filter(
        invoice__instructor=instructor,
        invoice__year=year,
        invoice__month=month,
        occurrence_date__isnull=False,
        lesson_id__isnull=False,
    ).values_list("lesson_id", "occurrence_date")
    return {(lesson_id, occurrence) for lesson_id, occurrence in rows}


def sessions_for_instructor(club, instructor, year, month):
    """Lazy month detail: dated sessions this instructor taught / would teach."""
    year = int(year)
    month = int(month)
    today = timezone.localdate()
    lessons = list(
        Lesson.objects.filter(club=club)
        .select_related("club", "instructor")
        .prefetch_related("lesson_instructors__instructor")
    )
    applied = _applied_keys(instructor, year, month)
    sessions = []
    for lesson in lessons:
        fee = lesson_fee_yen(lesson)
        for day in _month_dates(year, month, lesson.weekday):
            if day > today:
                continue
            staff = instructors_for_date(lesson, day)
            if staff is None:
                continue
            if not any(member.id == instructor.id for member in staff):
                continue
            key = (lesson.id, day)
            sessions.append({
                "lesson_id": lesson.id,
                "lesson_title": lesson.title or "",
                "weekday": lesson.weekday,
                "start_time": lesson.start_time.strftime("%H:%M"),
                "end_time": lesson.end_time.strftime("%H:%M"),
                "date": day.isoformat(),
                "fee_yen": fee,
                "applied": key in applied,
                "canceled": False,
            })
    sessions.sort(key=lambda row: (row["date"], row["start_time"], row["lesson_id"]))
    billable = [row for row in sessions if row["fee_yen"] is not None and not row["applied"]]
    return {
        "year": year,
        "month": month,
        "instructor_id": instructor.id,
        "sessions": sessions,
        "unapplied_count": len(billable),
        "unapplied_total_yen": sum(int(row["fee_yen"]) for row in billable),
    }


def _self_block(actor, member):
    if manages_own_money(actor, member):
        return SELF_PAYMENT_MESSAGE
    return None


def _ensure_invoice(club, instructor, year, month):
    invoice = (
        TeacherInvoice.objects.filter(
            club=club,
            instructor=instructor,
            year=year,
            month=month,
            instructor_pay__isnull=True,
        )
        .order_by("id")
        .first()
    )
    if invoice:
        return invoice
    return TeacherInvoice.objects.create(
        club=club,
        instructor=instructor,
        instructor_name=instructor.full_name,
        year=year,
        month=month,
        amount=0,
    )


def _recompute_invoice_amount(invoice):
    total = (
        TeacherInvoiceLesson.objects.filter(invoice=invoice)
        .aggregate(
            dated=Sum("fee_yen"),
        )
    )
    dated = int(total["dated"] or 0)
    # Keep hand-written lines without fee_yen out of auto sum: if any dated
    # fees exist, amount is their sum; otherwise leave amount as typed.
    if TeacherInvoiceLesson.objects.filter(
        invoice=invoice, fee_yen__isnull=False
    ).exists():
        invoice.amount = dated
        invoice.save(update_fields=["amount"])
    return invoice


def apply_sessions(club, instructor, payload, actor=None):
    """Apply selected dated sessions onto that month's teacher invoice."""
    blocked = _self_block(actor, instructor)
    if blocked:
        return None, blocked
    try:
        year = int(payload.get("year"))
        month = int(payload.get("month"))
    except (TypeError, ValueError):
        return None, "年月を入力してください。"
    if year < 2000 or year > 2100 or month < 1 or month > 12:
        return None, "年月が正しくありません。"

    raw = payload.get("sessions")
    apply_all = payload.get("apply_all") in (True, 1, "1", "true", "True")
    month_data = sessions_for_instructor(club, instructor, year, month)
    candidates = [
        row
        for row in month_data["sessions"]
        if row["fee_yen"] is not None and not row["applied"]
    ]
    if apply_all:
        selected = candidates
    else:
        if not isinstance(raw, list) or not raw:
            return None, "適用するレッスンを選んでください。"
        wanted = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                lesson_id = int(item.get("lesson_id"))
                day = date.fromisoformat(str(item.get("date"))[:10])
            except (TypeError, ValueError):
                return None, "レッスンの指定が正しくありません。"
            wanted.add((lesson_id, day.isoformat()))
        selected = [
            row
            for row in candidates
            if (row["lesson_id"], row["date"]) in wanted
        ]
        if len(selected) != len(wanted):
            return None, "選べないレッスンが含まれています。"

    if not selected:
        return None, "適用できるレッスンがありません。"

    with transaction.atomic():
        invoice = _ensure_invoice(club, instructor, year, month)
        for row in selected:
            day = date.fromisoformat(row["date"])
            lesson = Lesson.objects.filter(club=club, id=row["lesson_id"]).first()
            TeacherInvoiceLesson.objects.update_or_create(
                invoice=invoice,
                lesson=lesson,
                occurrence_date=day,
                defaults={
                    "lesson_title": row["lesson_title"] or (lesson.title if lesson else ""),
                    "times_taught": 1,
                    "fee_yen": int(row["fee_yen"]),
                },
            )
        invoice = _recompute_invoice_amount(invoice)

    from .service_instructor_pay import _invoice_payload

    invoice = TeacherInvoice.objects.prefetch_related("lessons").get(id=invoice.id)
    return {
        "invoice": _invoice_payload(invoice),
        "month": sessions_for_instructor(club, instructor, year, month),
    }, None


def set_default_instructors(lesson, instructor_ids):
    ids = []
    for raw in instructor_ids or []:
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            return None, "指導者の指定が正しくありません。"
    # Preserve order, drop dupes
    seen = set()
    ordered = []
    for pk in ids:
        if pk in seen:
            continue
        seen.add(pk)
        ordered.append(pk)
    members = list(
        Member.objects.filter(
            club=lesson.club,
            id__in=ordered,
            is_instructor=True,
        )
    )
    by_id = {member.id: member for member in members}
    if len(by_id) != len(ordered):
        return None, "指導者が見つかりません。"
    with transaction.atomic():
        LessonInstructor.objects.filter(lesson=lesson).delete()
        LessonInstructor.objects.bulk_create([
            LessonInstructor(
                lesson=lesson,
                instructor=by_id[pk],
                position=index,
            )
            for index, pk in enumerate(ordered)
        ])
        lesson.instructor = by_id[ordered[0]] if ordered else None
        lesson.save(update_fields=["instructor"])
    return [
        {"id": by_id[pk].id, "full_name": by_id[pk].full_name}
        for pk in ordered
    ], None


def cancel_lesson_date(lesson, day: date, note=""):
    cancel, _created = LessonDateCancel.objects.update_or_create(
        lesson=lesson,
        date=day,
        defaults={"note": (note or "")[:200]},
    )
    # A canceled date should not keep a staff replacement row.
    LessonDateStaff.objects.filter(lesson=lesson, date=day).delete()
    return {
        "lesson_id": lesson.id,
        "date": cancel.date.isoformat(),
        "canceled": True,
        "note": cancel.note,
    }


def uncancel_lesson_date(lesson, day: date):
    deleted, _ = LessonDateCancel.objects.filter(lesson=lesson, date=day).delete()
    return deleted > 0


def set_lesson_date_staff(lesson, day: date, instructor_ids, note=""):
    if is_lesson_canceled(lesson, day):
        return None, "休講の日には担当を設定できません。休講を解除してください。"
    ids = []
    for raw in instructor_ids or []:
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            return None, "指導者の指定が正しくありません。"
    seen = set()
    ordered = []
    for pk in ids:
        if pk in seen:
            continue
        seen.add(pk)
        ordered.append(pk)
    members = list(
        Member.objects.filter(club=lesson.club, id__in=ordered, is_instructor=True)
    )
    by_id = {member.id: member for member in members}
    if len(by_id) != len(ordered):
        return None, "指導者が見つかりません。"
    with transaction.atomic():
        staff, _created = LessonDateStaff.objects.update_or_create(
            lesson=lesson,
            date=day,
            defaults={"note": (note or "")[:200]},
        )
        LessonDateStaffMember.objects.filter(staff=staff).delete()
        LessonDateStaffMember.objects.bulk_create([
            LessonDateStaffMember(
                staff=staff,
                instructor=by_id[pk],
                position=index,
            )
            for index, pk in enumerate(ordered)
        ])
    return {
        "lesson_id": lesson.id,
        "date": day.isoformat(),
        "canceled": False,
        "note": staff.note,
        "instructors": [
            {"id": by_id[pk].id, "full_name": by_id[pk].full_name}
            for pk in ordered
        ],
    }, None


def clear_lesson_date_staff(lesson, day: date):
    deleted, _ = LessonDateStaff.objects.filter(lesson=lesson, date=day).delete()
    return deleted > 0


def lesson_date_status(lesson, day: date):
    canceled = is_lesson_canceled(lesson, day)
    staff_row = LessonDateStaff.objects.filter(lesson=lesson, date=day).first()
    if canceled:
        instructors = []
        replaced = False
    elif staff_row is not None:
        instructors = [
            {"id": m.instructor_id, "full_name": m.instructor.full_name}
            for m in LessonDateStaffMember.objects.filter(staff=staff_row)
            .select_related("instructor")
            .order_by("position", "id")
        ]
        replaced = True
    else:
        instructors = [
            {"id": member.id, "full_name": member.full_name}
            for member in default_instructors(lesson)
        ]
        replaced = False
    return {
        "lesson_id": lesson.id,
        "date": day.isoformat(),
        "canceled": canceled,
        "replaced": replaced,
        "instructors": instructors,
        "fee_yen": lesson_fee_yen(lesson),
    }


def sync_legacy_instructor_links(club=None):
    """Copy Lesson.instructor into LessonInstructor where missing."""
    qs = Lesson.objects.filter(instructor__isnull=False)
    if club is not None:
        qs = qs.filter(club=club)
    created = 0
    for lesson in qs.iterator():
        if LessonInstructor.objects.filter(lesson=lesson).exists():
            continue
        LessonInstructor.objects.create(
            lesson=lesson,
            instructor=lesson.instructor,
            position=0,
        )
        created += 1
    return created
