"""Interval booking: store windows, worker availability, random assignment."""

from __future__ import annotations

import random
import uuid
from datetime import date, time
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    ServiceCourse,
    ServiceReservation,
    ServiceWindow,
    WorkerAvailability,
    WorkerProfile,
    WorkerTimeOff,
)

SLOT_STEP_MINUTES = 15
ACTIVE_STATUSES = (
    ServiceReservation.Status.UNPAID,
    ServiceReservation.Status.PAID,
    ServiceReservation.Status.NOT_STARTED,
)


def _tmin(value: time) -> int:
    return value.hour * 60 + value.minute


def _from_min(total: int) -> time:
    return time(hour=total // 60, minute=total % 60)


def _overlaps(a_start, a_end, b_start, b_end) -> bool:
    return a_start < b_end and b_start < a_end


def _weekday(day: date) -> int:
    return day.weekday()


def windows_for(club, section_id=None, day=None):
    qs = ServiceWindow.objects.filter(club=club)
    if section_id is not None:
        qs = qs.filter(Q(section_id=section_id) | Q(section_id__isnull=True))
    if day is not None:
        qs = qs.filter(weekday=_weekday(day))
    return list(qs.order_by("weekday", "position", "earliest_start", "id"))


def courses_for(club, *, active_only=True):
    qs = ServiceCourse.objects.filter(club=club)
    if active_only:
        qs = qs.filter(active=True)
    return list(qs.order_by("position", "id"))


def _worker_month_minutes(worker, year, month) -> int:
    start = date(year, month, 1)
    if month == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, month + 1, 1)
    rows = (
        ServiceReservation.objects.filter(
            worker=worker,
            canceled=False,
            is_deleted=False,
            status__in=ACTIVE_STATUSES,
            reservation_date__gte=start,
            reservation_date__lt=end,
        )
        .exclude(no_show=True)
        .values_list("start_time", "end_time")
    )
    total = 0
    for start_t, end_t in rows:
        total += max(0, _tmin(end_t) - _tmin(start_t))
    return total


def _under_hour_cap(worker, day: date, add_minutes: int) -> bool:
    profile = getattr(worker, "worker_profile", None)
    if profile is None or profile.monthly_hour_cap is None:
        return True
    used = _worker_month_minutes(worker, day.year, day.month)
    cap_minutes = int(profile.monthly_hour_cap) * 60
    return used + add_minutes <= cap_minutes


def _availability_ranges(worker, day: date):
    weekday = _weekday(day)
    ranges = [
        (_tmin(row.start_time), _tmin(row.end_time))
        for row in WorkerAvailability.objects.filter(worker=worker, weekday=weekday)
    ]
    offs = WorkerTimeOff.objects.filter(worker=worker, date=day)
    for off in offs:
        if off.start_time is None and off.end_time is None:
            return []
        off_start = _tmin(off.start_time)
        off_end = _tmin(off.end_time)
        trimmed = []
        for start, end in ranges:
            if not _overlaps(start, end, off_start, off_end):
                trimmed.append((start, end))
                continue
            if start < off_start:
                trimmed.append((start, min(end, off_start)))
            if end > off_end:
                trimmed.append((max(start, off_end), end))
        ranges = [(s, e) for s, e in trimmed if e > s]
    return ranges


def _busy_ranges(worker, day: date):
    rows = ServiceReservation.objects.filter(
        worker=worker,
        reservation_date=day,
        canceled=False,
        is_deleted=False,
        status__in=ACTIVE_STATUSES,
    ).values_list("start_time", "end_time")
    return [(_tmin(s), _tmin(e)) for s, e in rows]


def _free_ranges(available, busy):
    free = list(available)
    for b_start, b_end in busy:
        next_free = []
        for a_start, a_end in free:
            if not _overlaps(a_start, a_end, b_start, b_end):
                next_free.append((a_start, a_end))
                continue
            if a_start < b_start:
                next_free.append((a_start, min(a_end, b_start)))
            if a_end > b_end:
                next_free.append((max(a_start, b_end), a_end))
        free = [(s, e) for s, e in next_free if e > s]
    return free


def _qualified_workers(club, course):
    profiles = (
        WorkerProfile.objects.filter(
            worker__club=club,
            worker__is_worker=True,
            courses=course,
        )
        .select_related("worker")
        .prefetch_related("courses")
    )
    return [p.worker for p in profiles]


def _window_allows(windows, start_min: int, end_min: int) -> bool:
    for window in windows:
        earliest = _tmin(window.earliest_start)
        latest = _tmin(window.latest_start)
        finish = _tmin(window.max_finish)
        if earliest <= start_min <= latest and end_min <= finish:
            return True
    return False


def available_slots(club, course_id, day: date, section_id=None, step_minutes=SLOT_STEP_MINUTES):
    """Return start times that at least one qualified worker can take."""
    course = ServiceCourse.objects.filter(club=club, id=course_id, active=True).first()
    if not course:
        return [], "コースが見つかりません。"
    windows = windows_for(club, section_id, day=day)
    if not windows:
        return [], "この曜日の予約可能な時間帯が設定されていません。"
    workers = _qualified_workers(club, course)
    if not workers:
        return [], "このコースに対応できるワーカーがいません。"

    duration = int(course.duration_minutes)
    candidates = set()
    for window in windows:
        start = _tmin(window.earliest_start)
        latest = _tmin(window.latest_start)
        finish = _tmin(window.max_finish)
        cursor = start
        while cursor <= latest:
            end = cursor + duration
            if end <= finish:
                candidates.add(cursor)
            cursor += step_minutes

    open_starts = []
    for start_min in sorted(candidates):
        end_min = start_min + duration
        for worker in workers:
            if not _under_hour_cap(worker, day, duration):
                continue
            free = _free_ranges(
                _availability_ranges(worker, day),
                _busy_ranges(worker, day),
            )
            if any(s <= start_min and end_min <= e for s, e in free):
                open_starts.append(_from_min(start_min).strftime("%H:%M"))
                break
    return open_starts, None


def pick_worker(club, course, day: date, start: time, end: time):
    """Randomly choose one qualified free worker for the slot."""
    duration = _tmin(end) - _tmin(start)
    free_workers = []
    for worker in _qualified_workers(club, course):
        if not _under_hour_cap(worker, day, duration):
            continue
        free = _free_ranges(
            _availability_ranges(worker, day),
            _busy_ranges(worker, day),
        )
        start_min = _tmin(start)
        end_min = _tmin(end)
        if any(s <= start_min and end_min <= e for s, e in free):
            free_workers.append(worker)
    if not free_workers:
        return None
    return random.choice(free_workers)


def _parse_hhmm(value):
    if isinstance(value, time):
        return value
    text = str(value or "").strip()
    try:
        hour, minute = text.split(":")[:2]
        return time(int(hour), int(minute))
    except (TypeError, ValueError):
        return None


def create_service_reservation(club, payload):
    """Create a visitor/member interval booking and assign a random worker."""
    try:
        course_id = int(payload.get("course_id"))
    except (TypeError, ValueError):
        return None, "コースを選択してください。"
    course = ServiceCourse.objects.filter(club=club, id=course_id, active=True).first()
    if not course:
        return None, "コースが見つかりません。"

    day_raw = payload.get("reservation_date") or payload.get("date")
    try:
        if isinstance(day_raw, date):
            day = day_raw
        else:
            day = date.fromisoformat(str(day_raw)[:10])
    except (TypeError, ValueError):
        return None, "日付を選択してください。"

    start = _parse_hhmm(payload.get("start_time"))
    if not start:
        return None, "開始時刻を選択してください。"
    end_min = _tmin(start) + int(course.duration_minutes)
    if end_min > 24 * 60:
        return None, "このコースはこの開始時刻では終わりません。"
    end = _from_min(end_min)

    section_id = payload.get("section_id")
    try:
        section_id = int(section_id) if section_id not in (None, "") else None
    except (TypeError, ValueError):
        section_id = None

    windows = windows_for(club, section_id, day=day)
    if not _window_allows(windows, _tmin(start), end_min):
        return None, "この時間帯は予約できません。"

    full_name = (payload.get("full_name") or "").strip()
    email = (payload.get("email") or "").strip()
    if not full_name:
        return None, "お名前を入力してください。"
    if not email:
        return None, "メールアドレスを入力してください。"

    payment_method = payload.get("payment_method") or ServiceReservation.PaymentMethod.CASH
    if payment_method not in dict(ServiceReservation.PaymentMethod.choices):
        payment_method = ServiceReservation.PaymentMethod.CASH

    reservation_type = payload.get("reservation_type") or ServiceReservation.ReservationType.VISITOR
    if reservation_type not in dict(ServiceReservation.ReservationType.choices):
        reservation_type = ServiceReservation.ReservationType.VISITOR

    with transaction.atomic():
        worker = pick_worker(club, course, day, start, end)
        if worker is None:
            return None, "この時間に空いているワーカーがいません。"

        status = ServiceReservation.Status.PAID
        paid_at = None
        if payment_method == ServiceReservation.PaymentMethod.STRIPE:
            status = ServiceReservation.Status.UNPAID
        elif payment_method == ServiceReservation.PaymentMethod.CASH:
            status = ServiceReservation.Status.PAID
            paid_at = None

        key = f"svc-{club.id}-{uuid.uuid4().hex}"
        reservation = ServiceReservation.objects.create(
            club=club,
            course=course,
            worker=worker,
            section_id=section_id,
            reservation_type=reservation_type,
            status=status,
            payment_method=payment_method,
            full_name=full_name,
            email=email,
            phone_number=(payload.get("phone_number") or "")[:30],
            amount=int(course.amount or 0),
            price_name=course.name,
            reservation_date=day,
            start_time=start,
            end_time=end,
            reservation_key=key,
            paid_at=paid_at,
        )
    return reservation_payload(reservation), None


def reservation_payload(reservation):
    worker = reservation.worker
    return {
        "id": reservation.id,
        "course_id": reservation.course_id,
        "course_name": reservation.course.name if reservation.course_id else reservation.price_name,
        "duration_minutes": reservation.course.duration_minutes if reservation.course_id else None,
        "worker_id": reservation.worker_id,
        "worker_name": worker.full_name if worker else None,
        "section_id": reservation.section_id,
        "reservation_date": reservation.reservation_date.isoformat(),
        "start_time": reservation.start_time.strftime("%H:%M"),
        "end_time": reservation.end_time.strftime("%H:%M"),
        "full_name": reservation.full_name,
        "email": reservation.email,
        "phone_number": reservation.phone_number,
        "amount": reservation.amount,
        "price_name": reservation.price_name,
        "status": reservation.status,
        "payment_method": reservation.payment_method,
        "reservation_type": reservation.reservation_type,
        "canceled": reservation.canceled,
    }


def ensure_worker_profile(member):
    if not member.is_worker:
        return None
    profile, _created = WorkerProfile.objects.get_or_create(worker=member)
    return profile


def set_worker_courses(member, course_ids):
    profile = ensure_worker_profile(member)
    if profile is None:
        return None, "ワーカーではありません。"
    ids = []
    for raw in course_ids or []:
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            return None, "コースの指定が正しくありません。"
    courses = list(
        ServiceCourse.objects.filter(club=member.club, id__in=ids, active=True)
    )
    if len(courses) != len(set(ids)):
        return None, "コースが見つかりません。"
    profile.courses.set(courses)
    return [c.id for c in courses], None


def replace_worker_availability(member, rows):
    profile = ensure_worker_profile(member)
    if profile is None:
        return None, "ワーカーではありません。"
    parsed = []
    for row in rows or []:
        if not isinstance(row, dict):
            return None, "出勤時間の指定が正しくありません。"
        try:
            weekday = int(row.get("weekday"))
        except (TypeError, ValueError):
            return None, "曜日を選択してください。"
        if weekday < 0 or weekday > 6:
            return None, "曜日を選択してください。"
        start = _parse_hhmm(row.get("start_time"))
        end = _parse_hhmm(row.get("end_time"))
        if not start or not end or start >= end:
            return None, "出勤時間を正しく入力してください。"
        parsed.append((weekday, start, end))
    with transaction.atomic():
        WorkerAvailability.objects.filter(worker=member).delete()
        WorkerAvailability.objects.bulk_create([
            WorkerAvailability(
                worker=member,
                weekday=weekday,
                start_time=start,
                end_time=end,
            )
            for weekday, start, end in parsed
        ])
    return [
        {
            "id": row.id,
            "weekday": row.weekday,
            "start_time": row.start_time.strftime("%H:%M"),
            "end_time": row.end_time.strftime("%H:%M"),
        }
        for row in WorkerAvailability.objects.filter(worker=member).order_by(
            "weekday", "start_time", "id"
        )
    ], None


def add_worker_time_off(member, payload):
    profile = ensure_worker_profile(member)
    if profile is None:
        return None, "ワーカーではありません。"
    try:
        day = date.fromisoformat(str(payload.get("date"))[:10])
    except (TypeError, ValueError):
        return None, "休みの日付を入力してください。"
    start_raw = payload.get("start_time")
    end_raw = payload.get("end_time")
    if start_raw in (None, "") and end_raw in (None, ""):
        start = end = None
    else:
        start = _parse_hhmm(start_raw)
        end = _parse_hhmm(end_raw)
        if not start or not end or start >= end:
            return None, "休みの時間を正しく入力してください。"
    note = payload.get("note") or ""
    if not isinstance(note, str):
        note = ""
    off = WorkerTimeOff.objects.create(
        worker=member,
        date=day,
        start_time=start,
        end_time=end,
        note=note[:200],
    )
    return {
        "id": off.id,
        "date": off.date.isoformat(),
        "start_time": off.start_time.strftime("%H:%M") if off.start_time else None,
        "end_time": off.end_time.strftime("%H:%M") if off.end_time else None,
        "note": off.note,
    }, None


def delete_worker_time_off(member, time_off_id):
    deleted, _ = WorkerTimeOff.objects.filter(worker=member, id=time_off_id).delete()
    return deleted > 0


def update_worker_profile(member, payload):
    profile = ensure_worker_profile(member)
    if profile is None:
        return None, "ワーカーではありません。"
    if "monthly_hour_cap" in payload:
        raw = payload.get("monthly_hour_cap")
        if raw in (None, ""):
            profile.monthly_hour_cap = None
        else:
            try:
                cap = int(raw)
            except (TypeError, ValueError):
                return None, "月の労働上限は整数で入力してください。"
            if cap < 1:
                return None, "月の労働上限は1時間以上にしてください。"
            profile.monthly_hour_cap = cap
    if "hourly_rate" in payload:
        raw = payload.get("hourly_rate")
        if raw in (None, ""):
            profile.hourly_rate = None
        else:
            try:
                rate = int(raw)
            except (TypeError, ValueError):
                return None, "時給は整数で入力してください。"
            if rate < 0:
                return None, "時給は0以上にしてください。"
            profile.hourly_rate = rate
    profile.save()
    return worker_detail(member), None


def worker_detail(member):
    profile = ensure_worker_profile(member)
    avail = WorkerAvailability.objects.filter(worker=member).order_by(
        "weekday", "start_time", "id"
    )
    # Upcoming yasumi only in the summary; history months are loaded on demand.
    today = timezone.localdate()
    offs = WorkerTimeOff.objects.filter(worker=member, date__gte=today).order_by(
        "date", "start_time", "id"
    )[:60]
    return {
        "id": member.id,
        "full_name": member.full_name,
        "picture": member.picture.url if member.picture else None,
        "introduction": member.introduction or "",
        "enrolled_at": profile.enrolled_at.isoformat() if profile else None,
        "monthly_hour_cap": profile.monthly_hour_cap if profile else None,
        "hourly_rate": profile.hourly_rate if profile else None,
        "course_ids": list(profile.courses.values_list("id", flat=True)) if profile else [],
        "availability": [
            {
                "id": row.id,
                "weekday": row.weekday,
                "start_time": row.start_time.strftime("%H:%M"),
                "end_time": row.end_time.strftime("%H:%M"),
            }
            for row in avail
        ],
        "time_offs": [
            {
                "id": row.id,
                "date": row.date.isoformat(),
                "start_time": row.start_time.strftime("%H:%M") if row.start_time else None,
                "end_time": row.end_time.strftime("%H:%M") if row.end_time else None,
                "note": row.note,
            }
            for row in offs
        ],
    }


def work_month_stats(worker, year, month):
    """Hours and reservation count for one month. Call only when that month is opened."""
    start = date(year, month, 1)
    if month == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, month + 1, 1)
    rows = list(
        ServiceReservation.objects.filter(
            worker=worker,
            canceled=False,
            is_deleted=False,
            status__in=ACTIVE_STATUSES,
            reservation_date__gte=start,
            reservation_date__lt=end,
        )
        .exclude(no_show=True)
        .order_by("reservation_date", "start_time")
    )
    minutes = 0
    bookings = []
    for row in rows:
        minutes += max(0, _tmin(row.end_time) - _tmin(row.start_time))
        bookings.append({
            "id": row.id,
            "reservation_date": row.reservation_date.isoformat(),
            "start_time": row.start_time.strftime("%H:%M"),
            "end_time": row.end_time.strftime("%H:%M"),
            "course_name": row.price_name or (row.course.name if row.course_id else ""),
            "amount": row.amount,
            "status": row.status,
        })
    hours = (Decimal(minutes) / Decimal(60)).quantize(Decimal("0.01"))
    return {
        "year": year,
        "month": month,
        "minutes_worked": minutes,
        "hours_worked": str(hours),
        "reservation_count": len(rows),
        "reservations": bookings,
    }
