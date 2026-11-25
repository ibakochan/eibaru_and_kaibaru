"""Self check-in, expected rosters, absences, and one-time makeup lessons."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone

from .models import Lesson, LessonAbsence, LessonDateCancel, Member, Participation
from .service_instructor_sessions import is_lesson_canceled


def _ids(raw):
    if raw in (None, ""):
        return []
    if isinstance(raw, str):
        parts = [part for part in raw.split(",") if part.strip()]
    elif isinstance(raw, (list, tuple)):
        parts = list(raw)
    else:
        parts = [raw]
    parsed = []
    for value in parts:
        if value in (None, ""):
            continue
        parsed.append(int(value))
    return parsed


def sync_expected_members(lesson, raw_ids):
    ids = _ids(raw_ids)
    members = list(Member.objects.filter(id__in=ids, club_id=lesson.club_id, is_worker=False))
    if len(members) != len(set(ids)):
        raise ValueError("このクラブの会員から選んでください。")
    lesson.expected_members.set(members)
    return [member.id for member in members]


def _account_members(user, club):
    if not user or not user.is_authenticated:
        return Member.objects.none()
    return Member.objects.filter(club=club, is_worker=False).filter(
        models_q(user)
    )


def models_q(user):
    from django.db.models import Q

    return Q(user=user) | Q(owner=user)


def member_can_manage(user, member) -> bool:
    if not user or not user.is_authenticated:
        return False
    return member.user_id == user.id or member.owner_id == user.id


def lesson_is_open_for_checkin(lesson, now):
    if lesson.occurs_on and lesson.occurs_on != now.date():
        return False
    if not lesson.occurs_on and lesson.weekday != now.weekday():
        return False
    if is_lesson_canceled(lesson, now.date()):
        return False
    club = lesson.club
    start = datetime.combine(now.date(), lesson.start_time)
    if timezone.is_naive(now):
        current = now
    else:
        current = timezone.localtime(now).replace(tzinfo=None)
        start = datetime.combine(timezone.localtime(now).date(), lesson.start_time)
    before = int(club.attendance_qr_minutes_before or 0)
    after = int(club.attendance_qr_minutes_after or 0)
    return (start - timedelta(minutes=before)) <= current <= (start + timedelta(minutes=after))


def open_checkin_lessons(club, now=None):
    now = timezone.localtime(now or timezone.now())
    lessons = (
        Lesson.objects.filter(club=club)
        .prefetch_related("expected_members")
        .order_by("start_time", "id")
    )
    return [lesson for lesson in lessons if lesson_is_open_for_checkin(lesson, now)]


def lesson_accepts_member(lesson, member) -> bool:
    roster = list(lesson.expected_members.all())
    if roster and all(row.id != member.id for row in roster):
        return False
    return True


def member_is_absent(member, lesson, day):
    return LessonAbsence.objects.filter(member=member, date=day).filter(
        models_lesson_or_day(lesson)
    ).exists()


def models_lesson_or_day(lesson):
    from django.db.models import Q

    return Q(lesson__isnull=True) | Q(lesson=lesson)


def check_in_member(lesson, member):
    today = timezone.localdate()
    if member_is_absent(member, lesson, today):
        raise ValueError("この日は欠席の連絡が入っています。")
    if not lesson_accepts_member(lesson, member):
        raise ValueError("このレッスンの対象者だけが出席できます。")
    participation, _created = Participation.objects.get_or_create(
        member=member,
        lesson=lesson,
        defaults={"total_count": 0, "monthly_count": 0},
    )
    if participation.last_participation_date == today:
        return participation, False
    participation.total_count += 1
    participation.monthly_count += 1
    participation.last_participation_date = today
    participation.save()
    from .level_progress import record_level_progress

    record_level_progress(member, lesson)
    return participation, True


def generate_qr_token(club):
    club.attendance_qr_token = secrets.token_urlsafe(24)
    club.save(update_fields=["attendance_qr_token"])
    return club.attendance_qr_token


def _clock_time(value):
    text = str(value or "").strip()
    if len(text) >= 8 and text[5:6] == ":":
        return datetime.strptime(text[:8], "%H:%M:%S").time()
    return datetime.strptime(text[:5], "%H:%M").time()


def _copy_lesson_catalog(source, makeup):
    from .models import LessonPriceOffer, ReservationAddon, ReservationPrice

    addon_map = {}
    for addon in source.reservation_addons.all():
        clone = ReservationAddon.objects.create(
            club=addon.club,
            lesson=makeup,
            name=addon.name,
            amount=addon.amount,
            position=addon.position,
        )
        addon_map[addon.id] = clone
    for price in source.reservation_prices.all():
        clone = ReservationPrice.objects.create(
            club=price.club,
            lesson=makeup,
            name=price.name,
            amount=price.amount,
            position=price.position,
            audience=price.audience,
        )
        clone.addons.set(
            [addon_map.get(addon.id, addon) for addon in price.addons.all()]
        )
    for offer in source.price_offers.all():
        clone = LessonPriceOffer.objects.create(
            lesson=makeup,
            price_id=offer.price_id,
            addons_overridden=offer.addons_overridden,
        )
        if offer.addons_overridden:
            clone.addons.set(offer.addons.all())


def times_overlap(start_a, end_a, start_b, end_b) -> bool:
    return start_a < end_b and start_b < end_a


def _slot_taken(club, day, start, end, ignore_lesson_id=None):
    lessons = Lesson.objects.filter(club=club).exclude(id=ignore_lesson_id)
    for lesson in lessons:
        if lesson.occurs_on:
            if lesson.occurs_on != day:
                continue
        elif lesson.weekday != day.weekday():
            continue
        if is_lesson_canceled(lesson, day):
            continue
        if times_overlap(start, end, lesson.start_time, lesson.end_time):
            return True
    return False


@transaction.atomic
def create_makeup_lesson(source, day, start, end):
    if end <= start:
        raise ValueError("終了時刻は開始より後にしてください。")
    if _slot_taken(source.club, day, start, end, ignore_lesson_id=source.id):
        raise ValueError("その時間には別のレッスンがあります。")
    makeup = Lesson.objects.create(
        club=source.club,
        instructor=source.instructor,
        section_id=source.section_id,
        title=source.title,
        weekday=day.weekday(),
        start_time=start,
        end_time=end,
        description=source.description,
        background_color=source.background_color,
        reservation_only=source.reservation_only,
        reservation_limit=source.reservation_limit,
        trial_disabled=source.trial_disabled,
        reservation_payment_options=source.reservation_payment_options,
        visitor_reservation_disabled=source.visitor_reservation_disabled,
        member_reservation_disabled=source.member_reservation_disabled,
        member_reservation_max_count=source.member_reservation_max_count,
        member_reservation_max_days_ahead=source.member_reservation_max_days_ahead,
        visitor_reservation_max_count=source.visitor_reservation_max_count,
        visitor_reservation_max_days_ahead=source.visitor_reservation_max_days_ahead,
        age_min=source.age_min,
        age_max=source.age_max,
        allowed_gender=source.allowed_gender,
        instructor_fee=source.instructor_fee,
        occurs_on=day,
        makeup_source=source,
    )
    makeup.reservation_menu_customized = source.reservation_menu_customized
    makeup.save(update_fields=["reservation_menu_customized"])
    makeup.allowed_plans.set(source.allowed_plans.all())
    makeup.expected_members.set(source.expected_members.all())
    _copy_lesson_catalog(source, makeup)
    for system in source.level_systems.all():
        system.lessons.add(makeup)
    from .service_instructor_sessions import default_instructors, set_default_instructors

    instructor_ids = [row.id for row in default_instructors(source)]
    if instructor_ids:
        set_default_instructors(makeup, instructor_ids)
    return makeup


def cancel_club_day(club, day, note=""):
    lessons = Lesson.objects.filter(club=club)
    count = 0
    for lesson in lessons:
        if lesson.occurs_on and lesson.occurs_on != day:
            continue
        if not lesson.occurs_on and lesson.weekday != day.weekday():
            continue
        LessonDateCancel.objects.update_or_create(
            lesson=lesson,
            date=day,
            defaults={"note": (note or "終日休講")[:200]},
        )
        count += 1
    return count


def uncancel_club_day(club, day):
    return LessonDateCancel.objects.filter(lesson__club=club, date=day).delete()[0]


def save_absences(*, member, dates, lesson, reason):
    reason = (reason or "").strip()
    if len(reason) > 500:
        raise ValueError("理由は500文字以内で入力してください。")
    today = timezone.localdate()
    created = []
    for day in dates:
        if day < today:
            raise ValueError("過去の日は欠席にできません。")
        if lesson is not None:
            if lesson.occurs_on and lesson.occurs_on != day:
                raise ValueError("そのレッスンはその日にはありません。")
            if not lesson.occurs_on and lesson.weekday != day.weekday():
                raise ValueError("そのレッスンはその曜日にはありません。")
            if not lesson.expected_members.filter(id=member.id).exists():
                raise ValueError("このレッスンの対象者だけが欠席を出せます。")
        existing = LessonAbsence.objects.filter(member=member, lesson=lesson, date=day)
        row = existing.first()
        if row is None:
            row = LessonAbsence.objects.create(
                member=member,
                lesson=lesson,
                date=day,
                reason=reason,
            )
        elif row.reason != reason:
            row.reason = reason
            row.save(update_fields=["reason"])
        created.append(row)
    return created


def _expected_on(lessons, day):
    titles = []
    for lesson in lessons:
        if lesson.occurs_on:
            if lesson.occurs_on != day:
                continue
        elif lesson.weekday != day.weekday():
            continue
        titles.append(lesson.title or "レッスン")
    return titles


def absence_history(member):
    rows = list(
        LessonAbsence.objects.filter(member=member)
        .select_related("lesson")
        .order_by("-date", "id")
    )
    expected = list(member.expected_lessons.all().order_by("weekday", "start_time", "id"))
    months = []
    by_month = {}
    for row in rows:
        key = row.date.strftime("%Y-%m")
        bucket = by_month.get(key)
        if bucket is None:
            bucket = {
                "month": key,
                "label": f"{row.date.year}年{row.date.month}月",
                "lesson_absences": 0,
                "whole_days": 0,
                "rows": [],
            }
            by_month[key] = bucket
            months.append(bucket)
        whole_day = row.lesson_id is None
        if whole_day:
            bucket["whole_days"] += 1
        else:
            bucket["lesson_absences"] += 1
        bucket["rows"].append({
            "id": row.id,
            "date": row.date.isoformat(),
            "whole_day": whole_day,
            "lesson_title": "" if whole_day else (row.lesson.title or "レッスン"),
            "lessons": _expected_on(expected, row.date) if whole_day else [],
            "reason": row.reason,
        })
    return {
        "lesson_absences": sum(month["lesson_absences"] for month in months),
        "whole_days": sum(month["whole_days"] for month in months),
        "months": months,
    }
