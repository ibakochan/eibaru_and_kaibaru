"""Interval booking: store windows, worker availability, random assignment."""

from __future__ import annotations

import json
import random
import uuid
from collections import defaultdict
from datetime import date, time, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    Member,
    ServiceCourse,
    ServiceReservation,
    ServiceWindow,
    WorkerAvailability,
    WorkerProfile,
    WorkerScheduleVersion,
    WorkerTimeOff,
)

SLOT_STEP_MINUTES = 15


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


def _apply_time_offs(ranges, offs):
    """Trim availability ranges by time-off rows. All-day off clears the day."""
    for start, end in offs:
        if start is None and end is None:
            return []
        off_start = _tmin(start)
        off_end = _tmin(end)
        trimmed = []
        for a_start, a_end in ranges:
            if not _overlaps(a_start, a_end, off_start, off_end):
                trimmed.append((a_start, a_end))
                continue
            if a_start < off_start:
                trimmed.append((a_start, min(a_end, off_start)))
            if a_end > off_end:
                trimmed.append((max(a_start, off_end), a_end))
        ranges = [(s, e) for s, e in trimmed if e > s]
    return ranges


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


def _month_bounds(day: date):
    start = date(day.year, day.month, 1)
    if day.month == 12:
        end = date(day.year + 1, 1, 1)
    else:
        end = date(day.year, day.month + 1, 1)
    return start, end


_LEGACY_SCHEDULE = "legacy"


def _load_schedule_index(worker_ids):
    """Versions and spans for workers.

    Workers with no versions use the legacy key for unversioned rows.
    """
    versions = defaultdict(list)
    for row in WorkerScheduleVersion.objects.filter(worker_id__in=worker_ids).order_by(
        "effective_on", "id"
    ):
        versions[row.worker_id].append(row)
    spans = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(list))))
    for worker_id, version_id, weekday, kind, start_t, end_t in (
        WorkerAvailability.objects.filter(worker_id__in=worker_ids).values_list(
            "worker_id", "version_id", "weekday", "kind", "start_time", "end_time"
        )
    ):
        key = version_id if version_id is not None else _LEGACY_SCHEDULE
        spans[worker_id][key][kind][weekday].append((_tmin(start_t), _tmin(end_t)))
    return versions, spans


def _version_key_for_day(version_list, day):
    if not version_list:
        return _LEGACY_SCHEDULE
    chosen = None
    for version in version_list:
        if version.effective_on <= day:
            chosen = version.id
        else:
            break
    return chosen


def _kind_spans(spans, worker_id, version_key, kind, weekday):
    if version_key is None:
        return []
    return list(spans[worker_id][version_key][kind][weekday])


def _availability_for_day(worker, day, versions, rows_by_version):
    key = _version_key_for_day(versions, day)
    if key is None:
        return []
    if key == _LEGACY_SCHEDULE:
        return rows_by_version.get(None, [])
    return rows_by_version.get(key, [])


def _schedule_bundle(worker):
    versions = list(
        WorkerScheduleVersion.objects.filter(worker=worker).order_by("effective_on", "id")
    )
    grouped = defaultdict(list)
    for row in WorkerAvailability.objects.filter(worker=worker).order_by(
        "weekday", "start_time", "id"
    ):
        grouped[row.version_id].append(row)
    return versions, grouped


def _prefetch_worker_days(workers, days, *, exclude_reservation_id=None):
    """Load avail / offs / busy / month minutes once for workers across days.

    Returns {day: {worker_id: {"free", "month_minutes", "hour_cap"}}}.
    """
    if not workers or not days:
        return {}

    from .service_service_reservation import holding_reservations_q

    worker_ids = [worker.id for worker in workers]
    unique_days = sorted(set(days))
    versions_by_worker, spans_by_worker = _load_schedule_index(worker_ids)

    off_by_day = defaultdict(lambda: defaultdict(list))
    for worker_id, off_day, start_t, end_t in WorkerTimeOff.objects.filter(
        worker_id__in=worker_ids,
        date__in=unique_days,
    ).values_list("worker_id", "date", "start_time", "end_time"):
        off_by_day[off_day][worker_id].append((start_t, end_t))

    busy_qs = ServiceReservation.objects.filter(
        holding_reservations_q(),
        worker_id__in=worker_ids,
        reservation_date__in=unique_days,
    ).exclude(no_show=True)
    if exclude_reservation_id is not None:
        busy_qs = busy_qs.exclude(id=exclude_reservation_id)
    busy_by_day = defaultdict(lambda: defaultdict(list))
    for worker_id, busy_day, start_t, end_t in busy_qs.values_list(
        "worker_id", "reservation_date", "start_time", "end_time"
    ):
        busy_by_day[busy_day][worker_id].append((_tmin(start_t), _tmin(end_t)))

    capped_ids = []
    hour_caps = {}
    for worker in workers:
        profile = getattr(worker, "worker_profile", None)
        if profile is None or profile.monthly_hour_cap is None:
            hour_caps[worker.id] = None
            continue
        hour_caps[worker.id] = int(profile.monthly_hour_cap)
        capped_ids.append(worker.id)

    # One range covering every month touched by the requested days.
    month_by_worker = {worker_id: defaultdict(int) for worker_id in capped_ids}
    if capped_ids:
        month_start = min(_month_bounds(day)[0] for day in unique_days)
        month_end = max(_month_bounds(day)[1] for day in unique_days)
        for worker_id, res_day, start_t, end_t in (
            ServiceReservation.objects.filter(
                holding_reservations_q(),
                worker_id__in=capped_ids,
                reservation_date__gte=month_start,
                reservation_date__lt=month_end,
            )
            .exclude(no_show=True)
            .values_list("worker_id", "reservation_date", "start_time", "end_time")
        ):
            month_key = (res_day.year, res_day.month)
            month_by_worker[worker_id][month_key] += max(
                0, _tmin(end_t) - _tmin(start_t)
            )

    by_day = {}
    for day in unique_days:
        weekday = _weekday(day)
        day_offs = off_by_day.get(day, {})
        day_busy = busy_by_day.get(day, {})
        month_key = (day.year, day.month)
        day_data = {}
        for worker in workers:
            version_key = _version_key_for_day(versions_by_worker.get(worker.id) or [], day)
            work_spans = _kind_spans(spans_by_worker, worker.id, version_key, WorkerAvailability.Kind.WORK, weekday)
            book_spans = _kind_spans(spans_by_worker, worker.id, version_key, WorkerAvailability.Kind.BOOKING, weekday)
            breaks = _kind_spans(spans_by_worker, worker.id, version_key, WorkerAvailability.Kind.BREAK, weekday)
            offs = day_offs.get(worker.id, [])
            busy = list(day_busy.get(worker.id, []))
            bookable = _bookable_ranges(work_spans, book_spans, offs, breaks)
            day_data[worker.id] = {
                "free": _free_ranges(bookable, busy),
                "busy": busy,
                "work": work_spans,
                "book": book_spans,
                "offs": offs,
                "month_minutes": (
                    month_by_worker[worker.id].get(month_key, 0)
                    if worker.id in month_by_worker
                    else None
                ),
                "hour_cap": hour_caps.get(worker.id),
            }
        by_day[day] = day_data
    return by_day


def _prefetch_worker_day(workers, day: date, *, exclude_reservation_id=None):
    """Load avail / offs / busy / month minutes once for all workers on day."""
    return _prefetch_worker_days(
        workers,
        [day],
        exclude_reservation_id=exclude_reservation_id,
    ).get(day, {})


def _under_hour_cap_cached(worker_id, day_data, add_minutes: int) -> bool:
    row = day_data.get(worker_id) or {}
    hour_cap = row.get("hour_cap")
    if hour_cap is None:
        return True
    used = int(row.get("month_minutes") or 0)
    return used + add_minutes <= hour_cap * 60


def _subtract_ranges(ranges, blocks):
    out = list(ranges)
    for start, end in blocks:
        if end > start:
            out = _cuts(out, start, end)
    return out


def _bookable_ranges(work_spans, book_spans, offs, breaks=None):
    """Reservation windows, falling back to 出勤時間 when none are set.

    A set window is clipped to 出勤時間. Time off and fixed breaks are
    removed, so a service cannot start in or run into a break.
    """
    work = [(start, end) for start, end in work_spans if end > start]
    if book_spans:
        raw = [(start, end) for start, end in book_spans if end > start]
        chosen = _intersect_ranges(raw, work) if work else []
    else:
        chosen = work
    chosen = _apply_time_offs(chosen, offs)
    return _subtract_ranges(chosen, breaks or [])


def viewer_worker(club, user):
    """The salon worker tied to this login, if they have one."""
    if club is None or user is None or not getattr(user, "is_authenticated", False):
        return None
    return (
        Member.objects.filter(club=club, user=user, is_worker=True)
        .order_by("id")
        .first()
    )


def duty_ranges_by_day(worker, days):
    """出勤 for each date, with breaks and 休み removed."""
    if worker is None or not days:
        return {}
    unique_days = list(dict.fromkeys(days))
    versions, spans = _load_schedule_index([worker.id])
    version_list = versions.get(worker.id) or []
    offs_by_day = defaultdict(list)
    for off_day, start_t, end_t in WorkerTimeOff.objects.filter(
        worker=worker,
        date__in=unique_days,
    ).values_list("date", "start_time", "end_time"):
        offs_by_day[off_day].append((start_t, end_t))
    result = {}
    for day in unique_days:
        weekday = _weekday(day)
        version_key = _version_key_for_day(version_list, day)
        work = _kind_spans(
            spans, worker.id, version_key, WorkerAvailability.Kind.WORK, weekday
        )
        breaks = _kind_spans(
            spans, worker.id, version_key, WorkerAvailability.Kind.BREAK, weekday
        )
        result[day] = _bookable_ranges(work, [], offs_by_day.get(day, []), breaks)
    return result


def shift_blocks_own_booking(club, user, day, start, end):
    """True when this worker's own booking would run into their 出勤."""
    worker = viewer_worker(club, user)
    if worker is None:
        return False
    duty = duty_ranges_by_day(worker, [day]).get(day) or []
    start_min = _tmin(start)
    end_min = _tmin(end)
    return any(_overlaps(start_min, end_min, duty_start, duty_end) for duty_start, duty_end in duty)


def _drop_slots_on_duty(day_payload, duty, duration):
    if not duty:
        return
    for row in day_payload.get("slots") or []:
        if not row.get("available"):
            continue
        hour, minute = str(row.get("time") or "0:0").split(":")[:2]
        start = int(hour) * 60 + int(minute)
        if any(_overlaps(start, start + duration, duty_start, duty_end) for duty_start, duty_end in duty):
            row["available"] = False
            row.pop("workers", None)
    slots = day_payload.get("slots") or []
    day_payload["available_count"] = sum(1 for row in slots if row.get("available"))


def _range_covered(start, end, spans) -> bool:
    """True when [start, end) sits inside the union of spans."""
    cursor = start
    for span_start, span_end in sorted(spans):
        if span_end <= cursor:
            continue
        if span_start > cursor:
            return False
        cursor = span_end
        if cursor >= end:
            return True
    return cursor >= end


def _intersect_ranges(left, right):
    out = []
    for a0, a1 in left:
        for b0, b1 in right:
            start, end = max(a0, b0), min(a1, b1)
            if end > start:
                out.append((start, end))
    return out


def _worker_fits_slot(worker_id, day_data, start_min: int, end_min: int) -> bool:
    if not _under_hour_cap_cached(worker_id, day_data, end_min - start_min):
        return False
    free = (day_data.get(worker_id) or {}).get("free") or []
    return any(start <= start_min and end_min <= end for start, end in free)


def _qualified_workers(club, course):
    profiles = WorkerProfile.objects.filter(
        worker__club=club,
        worker__is_worker=True,
        courses=course,
    ).select_related("worker")
    workers = []
    for profile in profiles:
        worker = profile.worker
        # Avoid a reverse OneToOne lookup per worker in slot loops.
        worker.worker_profile = profile
        workers.append(worker)
    return workers


def _window_allows(windows, start_min: int, end_min: int) -> bool:
    for window in windows:
        earliest = _tmin(window.earliest_start)
        latest = _tmin(window.latest_start)
        finish = _tmin(window.max_finish)
        if earliest <= start_min <= latest and end_min <= finish:
            return True
    return False


def resolve_slot_step(club, step_minutes=None):
    if step_minutes in (15, 30, 60):
        return int(step_minutes)
    step = getattr(club, "service_slot_step_minutes", None)
    if step in (15, 30, 60):
        return int(step)
    return SLOT_STEP_MINUTES


def free_workers_for_slot(
    club,
    course,
    day: date,
    start: time,
    end: time,
    *,
    exclude_reservation_id=None,
):
    """Qualified workers who are free for [start, end) on day."""
    workers = _qualified_workers(club, course)
    day_data = _prefetch_worker_day(
        workers,
        day,
        exclude_reservation_id=exclude_reservation_id,
    )
    start_min = _tmin(start)
    end_min = _tmin(end)
    return [
        worker
        for worker in workers
        if _worker_fits_slot(worker.id, day_data, start_min, end_min)
    ]


def _empty_day_payload(step, include_workers):
    return {
        "slots": [],
        "step_minutes": step,
        "available_count": 0,
        "allow_choose_worker": include_workers,
    }


def _slot_candidates(windows, duration, step):
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
            cursor += step
    return candidates


def _build_day_slots(
    *,
    workers,
    day_data,
    windows,
    duration,
    step,
    include_workers,
    day: date,
    today: date,
    now_min,
):
    """Build slot rows for one day from already-prefetched worker data."""
    if not windows:
        return _empty_day_payload(step, include_workers)
    if day < today:
        return _empty_day_payload(step, include_workers)

    candidates = _slot_candidates(windows, duration, step)
    slots = []
    for start_min in sorted(candidates):
        end_min = start_min + duration
        if now_min is not None and start_min <= now_min:
            free_for_slot = []
        else:
            free_for_slot = [
                worker
                for worker in workers
                if _worker_fits_slot(worker.id, day_data, start_min, end_min)
            ]
        available = bool(free_for_slot)
        row = {
            "time": _from_min(start_min).strftime("%H:%M"),
            "available": available,
        }
        if include_workers:
            row["workers"] = [
                {"id": worker.id, "full_name": worker.full_name}
                for worker in free_for_slot
            ]
        slots.append(row)
    return {
        "slots": slots,
        "step_minutes": step,
        "available_count": sum(1 for row in slots if row["available"]),
        "allow_choose_worker": include_workers,
    }


def available_slots(club, course_id, day: date, section_id=None, step_minutes=None, user=None):
    """Return timed slots with availability for the day schedule UI.

    Closed weekdays (no windows) return an empty day payload instead of an error,
    so callers can safely probe any date without 400 spam.
    """
    course = ServiceCourse.objects.filter(club=club, id=course_id, active=True).first()
    if not course:
        return None, "コースが見つかりません。"

    include_workers = assignment_mode(club) == ASSIGNMENT_CHOOSE
    step = resolve_slot_step(club, step_minutes)
    today = timezone.localdate()
    max_days_ahead = getattr(club, "visitor_reservation_max_days_ahead", None)
    if max_days_ahead is not None and day > today + timedelta(days=int(max_days_ahead)):
        return _empty_day_payload(step, include_workers), None

    windows = windows_for(club, section_id, day=day)
    if not windows or day < today:
        return _empty_day_payload(step, include_workers), None

    workers = _qualified_workers(club, course)
    day_data = _prefetch_worker_day(workers, day)
    now_min = _tmin(timezone.localtime().time()) if day == today else None
    payload = _build_day_slots(
        workers=workers,
        day_data=day_data,
        windows=windows,
        duration=int(course.duration_minutes),
        step=step,
        include_workers=include_workers,
        day=day,
        today=today,
        now_min=now_min,
    )
    viewer = viewer_worker(club, user)
    if viewer is not None:
        duty = duty_ranges_by_day(viewer, [day]).get(day) or []
        _drop_slots_on_duty(payload, duty, int(course.duration_minutes))
        payload["hides_own_shift"] = True
    return payload, None


def available_week_slots(
    club, course_id, week_start: date, section_id=None, step_minutes=None, user=None
):
    """Return slots for Mon–Sun starting at week_start in one DB-prefetch pass.

    Closed / past / beyond-horizon days are included as empty payloads so the
    UI can map the full week without per-day fetches or 400s.
    """
    course = ServiceCourse.objects.filter(club=club, id=course_id, active=True).first()
    if not course:
        return None, "コースが見つかりません。"

    include_workers = assignment_mode(club) == ASSIGNMENT_CHOOSE
    step = resolve_slot_step(club, step_minutes)
    today = timezone.localdate()
    now_min = _tmin(timezone.localtime().time())
    max_days_ahead = getattr(club, "visitor_reservation_max_days_ahead", None)
    horizon = (
        today + timedelta(days=int(max_days_ahead))
        if max_days_ahead is not None
        else None
    )
    duration = int(course.duration_minutes)

    days = [week_start + timedelta(days=offset) for offset in range(7)]
    all_windows = windows_for(club, section_id)
    windows_by_weekday = defaultdict(list)
    for window in all_windows:
        windows_by_weekday[window.weekday].append(window)

    # Only prefetch days that can actually produce slots.
    active_days = []
    for day in days:
        if day < today:
            continue
        if horizon is not None and day > horizon:
            continue
        if not windows_by_weekday.get(_weekday(day)):
            continue
        active_days.append(day)

    workers = _qualified_workers(club, course) if active_days else []
    by_day = _prefetch_worker_days(workers, active_days) if active_days else {}

    days_payload = {}
    for day in days:
        key = day.isoformat()
        if day < today or (horizon is not None and day > horizon):
            days_payload[key] = _empty_day_payload(step, include_workers)
            continue
        day_windows = windows_by_weekday.get(_weekday(day), [])
        if not day_windows:
            days_payload[key] = _empty_day_payload(step, include_workers)
            continue
        days_payload[key] = _build_day_slots(
            workers=workers,
            day_data=by_day.get(day, {}),
            windows=day_windows,
            duration=duration,
            step=step,
            include_workers=include_workers,
            day=day,
            today=today,
            now_min=now_min if day == today else None,
        )

    viewer = viewer_worker(club, user)
    if viewer is not None:
        duty_by_day = duty_ranges_by_day(viewer, days)
        for day in days:
            payload = days_payload.get(day.isoformat())
            if not payload:
                continue
            _drop_slots_on_duty(
                payload,
                duty_by_day.get(day) or [],
                duration,
            )

    return {
        "week_start": week_start.isoformat(),
        "days": days_payload,
        "step_minutes": step,
        "allow_choose_worker": include_workers,
        "hides_own_shift": viewer is not None,
    }, None


ASSIGNMENT_RANDOM = "random"
ASSIGNMENT_CHOOSE = "choose"
ASSIGNMENT_BALANCED = "balanced"
ASSIGNMENT_MODES = (ASSIGNMENT_RANDOM, ASSIGNMENT_CHOOSE, ASSIGNMENT_BALANCED)


def assignment_mode(club):
    """How a service booking picks its worker.

    Clubs saved before the assignment field existed stay on customer
    choice when that boolean is still on.
    """
    mode = (getattr(club, "service_worker_assignment", None) or "").strip()
    if mode == ASSIGNMENT_BALANCED:
        return ASSIGNMENT_BALANCED
    if mode == ASSIGNMENT_CHOOSE or getattr(club, "service_allow_choose_worker", False):
        return ASSIGNMENT_CHOOSE
    return ASSIGNMENT_RANDOM


def _booked_minutes_by_worker(worker_ids, day, *, exclude_reservation_id=None):
    """Confirmed booking minutes already on each worker for that date."""
    totals = {worker_id: 0 for worker_id in worker_ids}
    if not totals:
        return totals
    rows = ServiceReservation.objects.filter(
        worker_id__in=list(totals),
        reservation_date=day,
        canceled=False,
        is_deleted=False,
        no_show=False,
        status=ServiceReservation.Status.PAID,
    )
    if exclude_reservation_id:
        rows = rows.exclude(id=exclude_reservation_id)
    for start, end, worker_id in rows.values_list(
        "start_time", "end_time", "worker_id"
    ):
        totals[worker_id] += max(0, _tmin(end) - _tmin(start))
    return totals


def pick_worker(
    club,
    course,
    day: date,
    start: time,
    end: time,
    *,
    exclude_reservation_id=None,
    preferred_worker=None,
):
    """Choose a free worker.

    A named worker is used only while they are free. Otherwise the club
    setting picks at random, or gives the slot to whoever has the fewest
    booked minutes that day. A tie is broken at random.
    """
    free_workers = free_workers_for_slot(
        club,
        course,
        day,
        start,
        end,
        exclude_reservation_id=exclude_reservation_id,
    )
    if not free_workers:
        return None
    if preferred_worker is not None:
        preferred_id = getattr(preferred_worker, "id", preferred_worker)
        for worker in free_workers:
            if worker.id == preferred_id:
                return worker
        return None
    if assignment_mode(club) == ASSIGNMENT_BALANCED:
        minutes = _booked_minutes_by_worker(
            [worker.id for worker in free_workers],
            day,
            exclude_reservation_id=exclude_reservation_id,
        )
        least = min(minutes[worker.id] for worker in free_workers)
        tied = [worker for worker in free_workers if minutes[worker.id] == least]
        return random.choice(tied)
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


def create_service_reservation(club, payload, user=None):
    """Visitor service booking (email link or instant finish)."""
    from .service_service_reservation import (
        create_service_reservation as _create,
    )

    return _create(club, payload, user=user)


def reservation_payload(reservation):
    worker = reservation.worker
    return {
        "id": reservation.id,
        "course_id": reservation.course_id,
        "course_name": reservation.course.name if reservation.course_id else reservation.price_name,
        "duration_minutes": reservation.course.duration_minutes if reservation.course_id else None,
        "worker_id": reservation.worker_id,
        "worker_name": worker.full_name if worker else None,
        "preferred_worker_id": reservation.preferred_worker_id,
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


def _parse_birth_date(raw):
    try:
        return date.fromisoformat(str(raw)[:10])
    except (TypeError, ValueError):
        return None


def _payload_get(payload, key, default=""):
    if hasattr(payload, "getlist"):
        value = payload.get(key, default)
    else:
        value = (payload or {}).get(key, default)
    if value is None:
        return default
    return value


def _apply_worker_picture(member, files=None):
    if not files:
        return
    picture = files.get("picture") if hasattr(files, "get") else None
    if picture in (None, "", False):
        return
    if hasattr(picture, "read"):
        member.picture = picture
        member.save(update_fields=["picture"])


def _json_list(payload, key):
    raw = _payload_get(payload, key, None)
    if raw in (None, ""):
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            raise ValueError("入力の形式が正しくありません。")
        if not isinstance(parsed, list):
            raise ValueError("入力の形式が正しくありません。")
        return parsed
    raise ValueError("入力の形式が正しくありません。")


def _apply_created_worker_setup(club, _actor, member, payload):
    """Courses, schedule, time off, and pay sent with the create form."""
    course_ids = _json_list(payload, "course_ids")
    if course_ids:
        _, message = set_worker_courses(member, course_ids)
        if message:
            raise ValueError(message)
    availability = _json_list(payload, "availability")
    booking_hours = _json_list(payload, "booking_hours")
    breaks = _json_list(payload, "breaks")
    if availability or booking_hours or breaks:
        _, message = replace_worker_availability(
            member, availability, booking_hours, breaks
        )
        if message:
            raise ValueError(message)
    for row in _json_list(payload, "time_offs"):
        _, message = add_worker_time_off(member, row if isinstance(row, dict) else {})
        if message:
            raise ValueError(message)
    profile_payload = {}
    for key in ("monthly_hour_cap", "hourly_rate", "shift_hourly_rate"):
        if _payload_get(payload, key, None) is not None or (
            hasattr(payload, "__contains__") and key in payload
        ):
            profile_payload[key] = _payload_get(payload, key, None)
    if profile_payload:
        _, message = update_worker_profile(member, profile_payload)
        if message:
            raise ValueError(message)
    monthly_raw = _payload_get(payload, "monthly_amount", None)
    if monthly_raw not in (None, ""):
        from .models import WorkerPay
        from .service_worker_pay import _yen

        amount, error = _yen(monthly_raw, required=False)
        if error:
            raise ValueError(error)
        WorkerPay.objects.update_or_create(
            club=club,
            worker=member,
            defaults={"monthly_amount": amount, "active": True},
        )


def create_owner_worker(club, actor, payload, files=None):
    """Club owner creates a managed worker, or marks themselves as a worker."""
    if not actor or not getattr(actor, "is_authenticated", False):
        return None, "ログインしてください。"
    if club.owner_id != actor.id:
        return None, "クラブのオーナーだけがワーカーを作成できます。"

    as_self = _payload_get(payload, "as_self") in (True, 1, "1", "true", "True")
    from .utils import sync_worker_quantity
    from django.db import IntegrityError

    full_name = str(_payload_get(payload, "full_name") or "").strip()
    furigana = str(_payload_get(payload, "furigana") or "").strip()
    birth = _parse_birth_date(_payload_get(payload, "birth_date"))
    gender = str(_payload_get(payload, "gender") or "female")
    if gender not in ("male", "female"):
        gender = "female"
    phone = str(_payload_get(payload, "phone_number") or "")[:20] or None
    introduction = str(_payload_get(payload, "introduction") or "")[:2000]
    login_email = str(_payload_get(payload, "login_email") or "").strip()

    if as_self:
        if not full_name:
            full_name = (actor.get_full_name() or "").strip() or (actor.email or "オーナー")
        if birth is None:
            birth = date(2000, 1, 1)
        existing = Member.objects.filter(club=club, user=actor).first()
        if existing is not None:
            if existing.is_worker:
                ensure_worker_profile(existing)
                return worker_detail(existing), None
            update_fields = ["is_worker"]
            existing.is_worker = True
            if full_name:
                existing.full_name = full_name[:200]
                update_fields.append("full_name")
            if furigana:
                existing.furigana = furigana[:200]
                update_fields.append("furigana")
            if birth:
                existing.birth_date = birth
                update_fields.append("birth_date")
            existing.gender = gender
            update_fields.append("gender")
            if phone is not None:
                existing.phone_number = phone
                update_fields.append("phone_number")
            if introduction:
                existing.introduction = introduction
                update_fields.append("introduction")
            try:
                with transaction.atomic():
                    existing.save(update_fields=update_fields)
                    _apply_worker_picture(existing, files)
                    ensure_worker_profile(existing)
                    _apply_created_worker_setup(club, actor, existing, payload)
            except ValueError as exc:
                return None, str(exc)
            sync_worker_quantity(club)
            return worker_detail(existing), None

        try:
            with transaction.atomic():
                member = Member.objects.create(
                    club=club,
                    user=actor,
                    owner=actor,
                    full_name=full_name[:200],
                    furigana=furigana[:200],
                    birth_date=birth,
                    gender=gender,
                    phone_number=phone,
                    introduction=introduction,
                    is_worker=True,
                    is_instructor=False,
                    is_manager=False,
                )
                _apply_worker_picture(member, files)
                ensure_worker_profile(member)
                _apply_created_worker_setup(club, actor, member, payload)
        except ValueError as exc:
            return None, str(exc)
        except IntegrityError:
            existing = Member.objects.filter(club=club, user=actor).first()
            if existing is None:
                return None, "ワーカーを作成できませんでした。"
            if not existing.is_worker:
                existing.is_worker = True
                existing.save(update_fields=["is_worker"])
                ensure_worker_profile(existing)
                sync_worker_quantity(club)
            return worker_detail(existing), None
        sync_worker_quantity(club)
        return worker_detail(member), None

    if not full_name:
        return None, "お名前を入力してください。"
    if birth is None:
        return None, "生年月日を入力してください。"

    if Member.objects.filter(
        club=club,
        owner=actor,
        full_name=full_name,
        birth_date=birth,
    ).exists():
        return None, "同じ名前・生年月日の会員がすでにあります。"

    try:
        with transaction.atomic():
            member = Member.objects.create(
                club=club,
                user=None,
                owner=actor,
                full_name=full_name[:200],
                furigana=furigana[:200],
                birth_date=birth,
                gender=gender,
                phone_number=phone,
                introduction=introduction,
                is_worker=True,
                is_instructor=False,
                is_manager=False,
            )
            _apply_worker_picture(member, files)
            ensure_worker_profile(member)
            if login_email:
                _, message = link_worker_account(club, actor, member, login_email)
                if message:
                    raise ValueError(message)
            _apply_created_worker_setup(club, actor, member, payload)
    except ValueError as exc:
        return None, str(exc)
    except IntegrityError:
        return None, "同じ名前・生年月日の会員がすでにあります。"
    sync_worker_quantity(club)
    return worker_detail(member, include_login=True), None


def worker_deletion_summary(member):
    """What removing this worker will discard (for the confirm modal)."""
    from .models import (
        ServiceReservation,
        WorkerAvailability,
        WorkerInvoice,
        WorkerPay,
        WorkerTimeOff,
    )

    profile = getattr(member, "worker_profile", None)
    avail_count = WorkerAvailability.objects.filter(worker=member).count()
    off_count = WorkerTimeOff.objects.filter(worker=member).count()
    course_count = profile.courses.count() if profile else 0
    pay_count = WorkerPay.objects.filter(worker=member).count()
    invoice_count = WorkerInvoice.objects.filter(worker=member).count()
    reservation_count = ServiceReservation.objects.filter(
        worker=member, canceled=False, is_deleted=False
    ).count()

    lost = [
        "ワーカーとしての登録",
        "出勤時間（曜日）",
        "休み（やすみ）",
        "対応コース設定",
        "時給・月の労働上限",
        "ワーカー給与設定・明細との紐づけ",
    ]
    if member.introduction:
        lost.append("紹介文")
    if member.picture:
        lost.append("プロフィール写真")

    return {
        "worker_id": member.id,
        "full_name": member.full_name,
        "mode": "hard_delete",
        "lost": lost,
        "counts": {
            "availability": avail_count,
            "time_offs": off_count,
            "courses": course_count,
            "pays": pay_count,
            "invoices": invoice_count,
            "reservations": reservation_count,
        },
        "fee_note": (
            "このワーカー分として支払済みのプラットフォーム料金（月額¥500・日割り含む）は返金されません。"
        ),
        "delete_note": "会員レコードを完全に削除します。元に戻せません。",
    }


def remove_owner_worker(club, actor, member):
    """Hard-delete a managed worker Member (including owner-as-worker)."""
    if not actor or club.owner_id != actor.id:
        return None, "クラブのオーナーだけがワーカーを削除できます。"
    if member.club_id != club.id or not member.is_worker:
        return None, "ワーカーが見つかりません。"

    from .utils import sync_worker_quantity
    from .models import WorkerAvailability, WorkerProfile, WorkerTimeOff, WorkerPay

    summary = worker_deletion_summary(member)
    with transaction.atomic():
        WorkerAvailability.objects.filter(worker=member).delete()
        WorkerTimeOff.objects.filter(worker=member).delete()
        WorkerPay.objects.filter(worker=member).delete()
        WorkerProfile.objects.filter(worker=member).delete()
        member.delete()
    sync_worker_quantity(club)
    return {"ok": True, "mode": summary["mode"], "summary": summary}, None


def update_worker_public_profile(member, payload, files=None):
    """Owner/self can edit introduction and optional picture."""
    if not member.is_worker:
        return None, "ワーカーではありません。"
    keys = set(getattr(payload, "keys", lambda: [])())
    update_fields = []
    if "introduction" in keys:
        member.introduction = str(_payload_get(payload, "introduction") or "")[:2000]
        update_fields.append("introduction")
    if update_fields:
        member.save(update_fields=update_fields)
    _apply_worker_picture(member, files)
    return worker_detail(member), None


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


def _parse_hour_rows(rows, *, kind):
    parsed = []
    by_day = defaultdict(list)
    labels = {
        WorkerAvailability.Kind.WORK: "出勤時間",
        WorkerAvailability.Kind.BREAK: "休憩",
        WorkerAvailability.Kind.BOOKING: "予約受付時間",
    }
    label = labels.get(kind, "時間")
    for row in rows or []:
        if not isinstance(row, dict):
            return None, f"{label}の指定が正しくありません。"
        try:
            weekday = int(row.get("weekday"))
        except (TypeError, ValueError):
            return None, "曜日を選択してください。"
        if weekday < 0 or weekday > 6:
            return None, "曜日を選択してください。"
        start = _parse_hhmm(row.get("start_time"))
        end = _parse_hhmm(row.get("end_time"))
        if not start or not end or start >= end:
            return None, f"{label}を正しく入力してください。"
        start_min, end_min = _tmin(start), _tmin(end)
        for other_start, other_end in by_day[weekday]:
            if _overlaps(start_min, end_min, other_start, other_end):
                return None, f"同じ曜日の{label}が重なっています。"
        by_day[weekday].append((start_min, end_min))
        parsed.append((weekday, start, end))
    return parsed, None


def _inside_work(weekday, start, end, work_by_day, empty_message, outside_message):
    if not work_by_day.get(weekday):
        return empty_message
    if not _range_covered(_tmin(start), _tmin(end), work_by_day[weekday]):
        return outside_message
    return None


def replace_worker_availability(member, rows, booking_rows=None, break_rows=None):
    profile = ensure_worker_profile(member)
    if profile is None:
        return None, "ワーカーではありません。"
    parsed, message = _parse_hour_rows(rows, kind=WorkerAvailability.Kind.WORK)
    if message:
        return None, message
    booking, message = _parse_hour_rows(
        booking_rows, kind=WorkerAvailability.Kind.BOOKING
    )
    if message:
        return None, message
    breaks, message = _parse_hour_rows(break_rows, kind=WorkerAvailability.Kind.BREAK)
    if message:
        return None, message
    work_by_day = defaultdict(list)
    for weekday, start, end in parsed:
        work_by_day[weekday].append((_tmin(start), _tmin(end)))
    for weekday, start, end in booking:
        message = _inside_work(
            weekday,
            start,
            end,
            work_by_day,
            "予約受付時間を入れる日は、先に出勤時間を入れてください。",
            "予約受付時間は出勤時間の中にしてください。",
        )
        if message:
            return None, message
    for weekday, start, end in breaks:
        message = _inside_work(
            weekday,
            start,
            end,
            work_by_day,
            "休憩を入れる日は、先に出勤時間を入れてください。",
            "休憩は出勤時間の中にしてください。",
        )
        if message:
            return None, message
    _save_schedule_version(member, profile, parsed, booking, breaks)
    return worker_detail(member)["availability"], None


def _row_signature(rows):
    signed = []
    for row in rows:
        if hasattr(row, "kind"):
            signed.append((
                row.kind,
                row.weekday,
                row.start_time.strftime("%H:%M"),
                row.end_time.strftime("%H:%M"),
            ))
        else:
            kind, weekday, start, end = row
            signed.append((kind, weekday, start.strftime("%H:%M"), end.strftime("%H:%M")))
    return tuple(sorted(signed))


def _planned_rows(parsed, booking, breaks):
    planned = []
    for kind, source in (
        (WorkerAvailability.Kind.WORK, parsed),
        (WorkerAvailability.Kind.BOOKING, booking),
        (WorkerAvailability.Kind.BREAK, breaks),
    ):
        for weekday, start, end in source:
            planned.append((kind, weekday, start, end))
    return planned


def _write_version_rows(member, version, planned):
    WorkerAvailability.objects.filter(version=version).delete()
    WorkerAvailability.objects.bulk_create([
        WorkerAvailability(
            worker=member,
            version=version,
            weekday=weekday,
            kind=kind,
            start_time=start,
            end_time=end,
            break_minutes=0,
        )
        for kind, weekday, start, end in planned
    ])


def _save_schedule_version(member, profile, parsed, booking, breaks):
    """Keep the previous schedule when a later day gets a new one.

    Saving again on the same date replaces that date's version.
    """
    today = timezone.localdate()
    planned = _planned_rows(parsed, booking, breaks)
    new_sig = _row_signature(planned)
    enrolled = profile.enrolled_at or today
    if enrolled > today:
        enrolled = today
    with transaction.atomic():
        versions = list(
            WorkerScheduleVersion.objects.filter(worker=member).order_by("effective_on", "id")
        )
        if not versions:
            existing = list(
                WorkerAvailability.objects.filter(worker=member, version__isnull=True)
            )
            old_sig = _row_signature(existing)
            if old_sig == new_sig:
                version = WorkerScheduleVersion.objects.create(
                    worker=member, effective_on=enrolled
                )
                WorkerAvailability.objects.filter(
                    worker=member, version__isnull=True
                ).update(version=version)
                return
            if existing and enrolled < today:
                past = WorkerScheduleVersion.objects.create(
                    worker=member, effective_on=enrolled
                )
                WorkerAvailability.objects.filter(
                    worker=member, version__isnull=True
                ).update(version=past)
                current = WorkerScheduleVersion.objects.create(
                    worker=member, effective_on=today
                )
                _write_version_rows(member, current, planned)
                return
            WorkerAvailability.objects.filter(worker=member, version__isnull=True).delete()
            current = WorkerScheduleVersion.objects.create(
                worker=member, effective_on=today
            )
            _write_version_rows(member, current, planned)
            return
        latest = versions[-1]
        latest_rows = list(WorkerAvailability.objects.filter(version=latest))
        if _row_signature(latest_rows) == new_sig:
            return
        if latest.effective_on == today:
            _write_version_rows(member, latest, planned)
            return
        current = WorkerScheduleVersion.objects.create(worker=member, effective_on=today)
        _write_version_rows(member, current, planned)


def _time_off_payload(off):
    return {
        "id": off.id,
        "date": off.date.isoformat(),
        "start_time": off.start_time.strftime("%H:%M") if off.start_time else None,
        "end_time": off.end_time.strftime("%H:%M") if off.end_time else None,
        "note": off.note,
        "paid": bool(off.paid),
    }


def _yasumi_overlaps_existing(member, day, start, end):
    """True when this yasumi crosses one already stored that day.

    Paid and unpaid block each other. A block that ends when the next
    one starts does not overlap. An all-day row covers the whole day.
    """
    rows = WorkerTimeOff.objects.filter(worker=member, date=day).only(
        "start_time", "end_time"
    )
    for row in rows:
        if start is None or row.start_time is None or row.end_time is None:
            return True
        if _overlaps(
            _tmin(start), _tmin(end), _tmin(row.start_time), _tmin(row.end_time)
        ):
            return True
    return False


def _yasumi_blocked_by_reservation(member, day, start, end):
    """A taken reservation on a past day blocks yasumi that overlaps it."""
    rows = ServiceReservation.objects.filter(
        worker=member,
        reservation_date=day,
        canceled=False,
        is_deleted=False,
        status=ServiceReservation.Status.PAID,
    ).only("start_time", "end_time")
    if start is None:
        return rows.exists()
    off_start = _tmin(start)
    off_end = _tmin(end)
    for row in rows:
        if _overlaps(off_start, off_end, _tmin(row.start_time), _tmin(row.end_time)):
            return True
    return False


def add_worker_time_off(member, payload):
    profile = ensure_worker_profile(member)
    if profile is None:
        return None, "ワーカーではありません。"
    raw_dates = payload.get("dates")
    if not isinstance(raw_dates, list) or not raw_dates:
        single = payload.get("date")
        raw_dates = [single] if single else []
    days = []
    seen = set()
    for raw in raw_dates:
        try:
            day = date.fromisoformat(str(raw)[:10])
        except (TypeError, ValueError):
            return None, "休みの日付を入力してください。"
        if day in seen:
            continue
        seen.add(day)
        days.append(day)
    if not days:
        return None, "休みの日を選択してください。"
    start_raw = payload.get("start_time")
    end_raw = payload.get("end_time")
    if start_raw in (None, "") and end_raw in (None, ""):
        start = end = None
    else:
        start = _parse_hhmm(start_raw)
        end = _parse_hhmm(end_raw)
        if not start or not end or start >= end:
            return None, "休みの時間を正しく入力してください。"
    today = timezone.localdate()
    month_start = today.replace(day=1)
    for day in days:
        label = f"{day.month}/{day.day}"
        if day < month_start:
            return None, f"{label}は過去の月なので、休みにできません。"
        if day < today and _yasumi_blocked_by_reservation(member, day, start, end):
            return None, f"{label}には予約があるので、休みにできません。"
        if _yasumi_overlaps_existing(member, day, start, end):
            return None, f"{label}は、入っている休みまたは有給と重なっています。"
    note = payload.get("note") or ""
    if not isinstance(note, str):
        note = ""
    paid = payload.get("paid") in (True, 1, "1", "true", "True")
    created = []
    with transaction.atomic():
        WorkerProfile.objects.select_for_update().filter(worker=member).first()
        for day in days:
            label = f"{day.month}/{day.day}"
            if _yasumi_overlaps_existing(member, day, start, end):
                return None, f"{label}は、入っている休みまたは有給と重なっています。"
        for day in days:
            off = WorkerTimeOff.objects.create(
                worker=member,
                date=day,
                start_time=start,
                end_time=end,
                note=note[:200],
                paid=paid,
            )
            created.append(_time_off_payload(off))
    if len(created) == 1:
        return created[0], None
    return created, None


def delete_worker_time_off(member, time_off_id):
    off = WorkerTimeOff.objects.filter(worker=member, id=time_off_id).first()
    if off is None:
        return False, "休みが見つかりません。"
    today = timezone.localdate()
    if off.date < today.replace(day=1):
        return False, "過去の月の休みは削除できません。"
    off.delete()
    return True, None


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
                return None, "施術時給は整数で入力してください。"
            if rate < 0:
                return None, "施術時給は0以上にしてください。"
            profile.hourly_rate = rate
    if "shift_hourly_rate" in payload:
        raw = payload.get("shift_hourly_rate")
        if raw in (None, ""):
            profile.shift_hourly_rate = None
        else:
            try:
                rate = int(raw)
            except (TypeError, ValueError):
                return None, "出勤時給は整数で入力してください。"
            if rate < 0:
                return None, "出勤時給は0以上にしてください。"
            profile.shift_hourly_rate = rate
    profile.save()
    return worker_detail(member), None


def _current_availability(member, day=None):
    day = day or timezone.localdate()
    versions, grouped = _schedule_bundle(member)
    if versions:
        chosen = None
        for version in versions:
            if version.effective_on <= day:
                chosen = version
            else:
                break
        if chosen is None:
            return []
        return grouped.get(chosen.id, [])
    return grouped.get(None, [])


def worker_detail(member, *, include_login=False):
    profile = ensure_worker_profile(member)
    avail = _current_availability(member)
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
        "user_id": member.user_id,
        "owner_id": member.owner_id,
        "managed": member.user_id is None,
        "user_email": (
            member.user.email
            if include_login and member.user_id and member.user
            else ""
        ),
        "is_self_owner": bool(
            member.user_id and member.club_id and member.user_id == member.club.owner_id
        ),
        "enrolled_at": profile.enrolled_at.isoformat() if profile else None,
        "monthly_hour_cap": profile.monthly_hour_cap if profile else None,
        "hourly_rate": profile.hourly_rate if profile else None,
        "shift_hourly_rate": profile.shift_hourly_rate if profile else None,
        "course_ids": list(profile.courses.values_list("id", flat=True)) if profile else [],
        "availability": [
            {
                "id": row.id,
                "weekday": row.weekday,
                "start_time": row.start_time.strftime("%H:%M"),
                "end_time": row.end_time.strftime("%H:%M"),
            }
            for row in avail
            if row.kind == WorkerAvailability.Kind.WORK
        ],
        "breaks": [
            {
                "id": row.id,
                "weekday": row.weekday,
                "start_time": row.start_time.strftime("%H:%M"),
                "end_time": row.end_time.strftime("%H:%M"),
            }
            for row in avail
            if row.kind == WorkerAvailability.Kind.BREAK
        ],
        "booking_hours": [
            {
                "id": row.id,
                "weekday": row.weekday,
                "start_time": row.start_time.strftime("%H:%M"),
                "end_time": row.end_time.strftime("%H:%M"),
            }
            for row in avail
            if row.kind == WorkerAvailability.Kind.BOOKING
        ],
        "time_offs": [
            {
                "id": row.id,
                "date": row.date.isoformat(),
                "start_time": row.start_time.strftime("%H:%M") if row.start_time else None,
                "end_time": row.end_time.strftime("%H:%M") if row.end_time else None,
                "note": row.note,
                "paid": bool(row.paid),
            }
            for row in offs
        ],
    }


def _span_payload(rows, kind):
    return [
        {
            "weekday": row.weekday,
            "start_time": row.start_time.strftime("%H:%M"),
            "end_time": row.end_time.strftime("%H:%M"),
        }
        for row in rows
        if row.kind == kind
    ]


def worker_month_schedule(member, year, month):
    """Yasumi and the weekly schedule that applied during one calendar month.

    Includes the version already in effect on the 1st, plus any version that
    starts during the month. Days before the first version stay empty.
    """
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    versions, grouped = _schedule_bundle(member)
    chosen = []
    if versions:
        prior = None
        for version in versions:
            if version.effective_on < start:
                prior = version
            elif version.effective_on < end:
                chosen.append(version)
        if prior is not None:
            chosen.insert(0, prior)
        snapshots = []
        for version in chosen:
            rows = grouped.get(version.id, [])
            snapshots.append({
                "effective_on": version.effective_on.isoformat(),
                "availability": _span_payload(rows, WorkerAvailability.Kind.WORK),
                "breaks": _span_payload(rows, WorkerAvailability.Kind.BREAK),
                "booking_hours": _span_payload(rows, WorkerAvailability.Kind.BOOKING),
            })
    else:
        legacy = grouped.get(None, [])
        profile = WorkerProfile.objects.filter(worker=member).first()
        enrolled = profile.enrolled_at if profile and profile.enrolled_at else date(2000, 1, 1)
        snapshots = [{
            "effective_on": enrolled.isoformat(),
            "availability": _span_payload(legacy, WorkerAvailability.Kind.WORK),
            "breaks": _span_payload(legacy, WorkerAvailability.Kind.BREAK),
            "booking_hours": _span_payload(legacy, WorkerAvailability.Kind.BOOKING),
        }] if legacy else []
    offs = WorkerTimeOff.objects.filter(
        worker=member, date__gte=start, date__lt=end
    ).order_by("date", "start_time", "id")
    return {
        "year": year,
        "month": month,
        "time_offs": [_time_off_payload(row) for row in offs],
        "versions": snapshots,
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
            status=ServiceReservation.Status.PAID,
            reservation_date__gte=start,
            reservation_date__lt=end,
        )
        .exclude(no_show=True)
        .order_by("reservation_date", "start_time")
    )
    versions, grouped = _schedule_bundle(worker)
    minutes = 0
    bookings = []
    for row in rows:
        availability = _availability_for_day(worker, row.reservation_date, versions, grouped)
        pieces = [(_tmin(row.start_time), _tmin(row.end_time))]
        for span in availability:
            if (
                span.kind == WorkerAvailability.Kind.BREAK
                and span.weekday == row.reservation_date.weekday()
            ):
                pieces = _cuts(pieces, _tmin(span.start_time), _tmin(span.end_time))
        minutes += _range_minutes(pieces)
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


def link_worker_account(club, actor, member, email):
    """Point a worker's login at an existing account. The salon stays the owner."""
    if not actor or not getattr(actor, "is_authenticated", False):
        return None, "ログインしてください。"
    if club.owner_id != actor.id:
        return None, "クラブのオーナーだけがログインを紐づけできます。"
    if member.club_id != club.id or not member.is_worker:
        return None, "ワーカーが見つかりません。"

    from accounts.models import CustomUser
    from django.db import IntegrityError

    cleaned = str(email or "").strip()
    if not cleaned:
        member.user = None
        member.save(update_fields=["user"])
        return worker_detail(member, include_login=True), None

    user = CustomUser.objects.filter(email__iexact=cleaned).first()
    if user is None:
        return None, "そのメールのアカウントはありません。"
    if Member.objects.filter(club=club, user=user).exclude(pk=member.pk).exists():
        return None, "このアカウントはすでにこのサロンの別の会員に紐づいています。"

    member.user = user
    try:
        member.save(update_fields=["user"])
    except IntegrityError:
        return None, "このアカウントはすでにこのサロンの別の会員に紐づいています。"
    member.user = user
    return worker_detail(member, include_login=True), None


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _cuts(intervals, start, end):
    """Remove [start, end) from a list of minute ranges."""
    next_ranges = []
    for block_start, block_end in intervals:
        if end <= block_start or start >= block_end:
            next_ranges.append((block_start, block_end))
            continue
        if start > block_start:
            next_ranges.append((block_start, start))
        if end < block_end:
            next_ranges.append((end, block_end))
    return next_ranges


def _range_minutes(ranges) -> int:
    return sum(max(0, end - start) for start, end in ranges)


def _hm(total: int) -> str:
    return _from_min(total).strftime("%H:%M")


def _active_reservations(worker, start: date, end: date):
    return list(
        ServiceReservation.objects.filter(
            worker=worker,
            is_deleted=False,
            canceled=False,
            no_show=False,
            status=ServiceReservation.Status.PAID,
            reservation_date__gte=start,
            reservation_date__lt=end,
        )
        .select_related("course")
        .order_by("reservation_date", "start_time", "id")
    )


def _off_slices(offs, paid):
    """All-day flag and timed ranges for paid or unpaid yasumi."""
    all_day = False
    ranges = []
    for off in offs:
        if bool(off.paid) != paid:
            continue
        if off.start_time is None or off.end_time is None:
            all_day = True
        else:
            ranges.append((_tmin(off.start_time), _tmin(off.end_time)))
    return all_day, ranges


def _take_cover(base, all_day, ranges):
    """Minutes of base covered by these ranges, and the base that remains.

    Overlapping ranges are counted once. An all-day row covers the whole base.
    """
    if all_day:
        return list(base), []
    covered = []
    remaining = list(base)
    for start, end in ranges:
        if end <= start:
            continue
        for block_start, block_end in remaining:
            lo = max(start, block_start)
            hi = min(end, block_end)
            if hi > lo:
                covered.append((lo, hi))
        remaining = _cuts(remaining, start, end)
    return covered, remaining


def _day_blocks(worker, day, reservations, offs, availability):
    weekday = day.weekday()
    work_rows = []
    book_rows = []
    break_rows = []
    for row in availability:
        if row.weekday != weekday or not (row.start_time < row.end_time):
            continue
        if row.kind == WorkerAvailability.Kind.BOOKING:
            book_rows.append(row)
        elif row.kind == WorkerAvailability.Kind.BREAK:
            break_rows.append(row)
        else:
            work_rows.append(row)
    day_offs = [off for off in offs if off.date == day]
    all_day = any(off.start_time is None or off.end_time is None for off in day_offs)
    day_breaks = [
        (_tmin(row.start_time), _tmin(row.end_time)) for row in break_rows
    ]
    payable = []
    for row in work_rows:
        pieces = [(_tmin(row.start_time), _tmin(row.end_time))]
        payable.extend(_subtract_ranges(pieces, day_breaks))
    paid_all, paid_ranges = _off_slices(day_offs, True)
    unpaid_all, unpaid_ranges = _off_slices(day_offs, False)
    paid_cover, after_paid = _take_cover(payable, paid_all, paid_ranges)
    unpaid_cover, shift = _take_cover(after_paid, unpaid_all, unpaid_ranges)
    shift_minutes = _range_minutes(shift)
    off_minutes = _range_minutes(unpaid_cover)
    paid_off_minutes = _range_minutes(paid_cover)
    work_spans = [
        (_tmin(row.start_time), _tmin(row.end_time)) for row in work_rows
    ]
    book_spans = [
        (_tmin(row.start_time), _tmin(row.end_time)) for row in book_rows
    ]
    bookable = _bookable_ranges(
        work_spans,
        book_spans,
        [(off.start_time, off.end_time) for off in day_offs],
        [] if all_day else day_breaks,
    )
    day_rows = [row for row in reservations if row.reservation_date == day]
    open_ranges = list(bookable)
    for row in day_rows:
        open_ranges = _cuts(open_ranges, _tmin(row.start_time), _tmin(row.end_time))
    reservation_minutes = sum(
        max(0, _tmin(row.end_time) - _tmin(row.start_time)) for row in day_rows
    )
    return (
        shift,
        open_ranges,
        day_rows,
        reservation_minutes,
        shift_minutes,
        off_minutes,
        paid_off_minutes,
    )


def _reservation_brief(row):
    course = row.course
    course_name = course.name if course else ""
    price_name = row.price_name or course_name
    return {
        "id": row.id,
        "full_name": row.full_name,
        "start_time": row.start_time.strftime("%H:%M"),
        "end_time": row.end_time.strftime("%H:%M"),
        "course_name": course_name,
        "price_name": price_name,
    }


WEEKDAY_LABELS = ("月", "火", "水", "木", "金", "土", "日")


def worker_week_board(worker, week_start=None):
    """One week: this worker's bookings and the gaps still open for booking."""
    start = _monday(week_start or timezone.localdate())
    end = start + timedelta(days=7)
    reservations = _active_reservations(worker, start, end)
    offs = list(
        WorkerTimeOff.objects.filter(
            worker=worker, date__gte=start, date__lt=end
        )
    )
    versions, grouped = _schedule_bundle(worker)
    days = []
    for offset in range(7):
        day = start + timedelta(days=offset)
        availability = _availability_for_day(worker, day, versions, grouped)
        (
            shift,
            open_ranges,
            day_rows,
            reservation_minutes,
            shift_minutes,
            _off_minutes,
            _paid_off_minutes,
        ) = _day_blocks(worker, day, reservations, offs, availability)
        days.append({
            "date": day.isoformat(),
            "weekday": WEEKDAY_LABELS[day.weekday()],
            "shift_minutes": shift_minutes,
            "reservation_count": len(day_rows),
            "reservation_minutes": reservation_minutes,
            "reservations": [_reservation_brief(row) for row in day_rows],
            "open": [
                {"start_time": _hm(begin), "end_time": _hm(stop)}
                for begin, stop in open_ranges
            ],
        })
    return {
        "worker_id": worker.id,
        "week_start": start.isoformat(),
        "days": days,
    }


def worker_work_months(worker):
    """Month labels only. Current month is included even with no bookings."""
    from django.db.models import Count
    from django.db.models.functions import ExtractMonth, ExtractYear

    today = timezone.localdate()
    profile = WorkerProfile.objects.filter(worker=worker).first()
    enrolled = profile.enrolled_at if profile else today
    rows = (
        ServiceReservation.objects.filter(
            worker=worker,
            is_deleted=False,
            status=ServiceReservation.Status.PAID,
        )
        .annotate(
            year=ExtractYear("reservation_date"),
            month=ExtractMonth("reservation_date"),
        )
        .values("year", "month")
        .annotate(reservation_count=Count("id"))
    )
    found = {
        (int(row["year"]), int(row["month"])): int(row["reservation_count"])
        for row in rows
    }
    year, month = enrolled.year, enrolled.month
    while (year, month) <= (today.year, today.month):
        found.setdefault((year, month), 0)
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
    found.setdefault((today.year, today.month), 0)
    current = (today.year, today.month)
    months = [
        {
            "year": key[0],
            "month": key[1],
            "reservation_count": count,
            "is_current": key == current,
        }
        for key, count in found.items()
    ]
    months.sort(key=lambda row: (row["year"], row["month"]), reverse=True)
    return {"worker_id": worker.id, "months": months}


def worker_work_month(worker, year, month):
    """One month: shift hours, booking count, and booking time by day and week."""
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    reservations = _active_reservations(worker, start, end)
    offs = list(
        WorkerTimeOff.objects.filter(worker=worker, date__gte=start, date__lt=end)
    )
    versions, grouped = _schedule_bundle(worker)
    days = []
    day = start
    while day < end:
        availability = _availability_for_day(worker, day, versions, grouped)
        (
            shift,
            _open,
            day_rows,
            reservation_minutes,
            shift_minutes,
            off_minutes,
            paid_off_minutes,
        ) = _day_blocks(worker, day, reservations, offs, availability)
        scheduled = [
            row for row in availability
            if row.weekday == day.weekday() and row.kind == WorkerAvailability.Kind.WORK
        ]
        day_breaks = [
            row for row in availability
            if row.weekday == day.weekday() and row.kind == WorkerAvailability.Kind.BREAK
        ]
        if shift or day_rows or scheduled or off_minutes or paid_off_minutes:
            days.append({
                "date": day.isoformat(),
                "weekday": WEEKDAY_LABELS[day.weekday()],
                "shift_minutes": shift_minutes,
                "shift_spans": [
                    {
                        "start_time": row.start_time.strftime("%H:%M"),
                        "end_time": row.end_time.strftime("%H:%M"),
                    }
                    for row in scheduled
                ],
                "breaks": [
                    {
                        "start_time": row.start_time.strftime("%H:%M"),
                        "end_time": row.end_time.strftime("%H:%M"),
                    }
                    for row in day_breaks
                ],
                "reservation_count": len(day_rows),
                "reservation_minutes": reservation_minutes,
                "off_minutes": off_minutes,
                "paid_off_minutes": paid_off_minutes,
                "reservations": [_reservation_brief(row) for row in day_rows],
            })
        day += timedelta(days=1)

    weeks = []
    for row in days:
        row_day = date.fromisoformat(row["date"])
        week_start = _monday(row_day)
        week_end = week_start + timedelta(days=6)
        if weeks and weeks[-1]["start"] == week_start.isoformat():
            bucket = weeks[-1]
        else:
            bucket = {
                "start": week_start.isoformat(),
                "end": week_end.isoformat(),
                "shift_minutes": 0,
                "reservation_count": 0,
                "reservation_minutes": 0,
                "off_minutes": 0,
                "paid_off_minutes": 0,
            }
            weeks.append(bucket)
        bucket["shift_minutes"] += row["shift_minutes"]
        bucket["reservation_count"] += row["reservation_count"]
        bucket["reservation_minutes"] += row["reservation_minutes"]
        bucket["off_minutes"] += row["off_minutes"]
        bucket["paid_off_minutes"] += row["paid_off_minutes"]

    shift_minutes = sum(row["shift_minutes"] for row in days)
    from .service_worker_pay import salary_breakdown

    return {
        "worker_id": worker.id,
        "year": year,
        "month": month,
        "shift_minutes": shift_minutes,
        "off_minutes": sum(row["off_minutes"] for row in days),
        "paid_off_minutes": sum(row["paid_off_minutes"] for row in days),
        "reservation_count": sum(row["reservation_count"] for row in days),
        "reservation_minutes": sum(row["reservation_minutes"] for row in days),
        "weeks": weeks,
        "days": days,
        "pay": salary_breakdown(worker, year, month, shift_minutes),
    }
