"""Interval booking: store windows, worker availability, random assignment."""

from __future__ import annotations

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


def _prefetch_worker_days(workers, days, *, exclude_reservation_id=None):
    """Load avail / offs / busy / month minutes once for workers across days.

    Returns {day: {worker_id: {"free", "month_minutes", "hour_cap"}}}.
    """
    if not workers or not days:
        return {}

    from .service_service_reservation import holding_reservations_q

    worker_ids = [worker.id for worker in workers]
    unique_days = sorted(set(days))
    weekdays = {_weekday(day) for day in unique_days}

    avail_by_weekday = defaultdict(lambda: defaultdict(list))
    for worker_id, weekday, start_t, end_t in WorkerAvailability.objects.filter(
        worker_id__in=worker_ids,
        weekday__in=weekdays,
    ).values_list("worker_id", "weekday", "start_time", "end_time"):
        avail_by_weekday[weekday][worker_id].append((_tmin(start_t), _tmin(end_t)))

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
    )
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
        day_avail = avail_by_weekday.get(weekday, {})
        day_offs = off_by_day.get(day, {})
        day_busy = busy_by_day.get(day, {})
        month_key = (day.year, day.month)
        day_data = {}
        for worker in workers:
            available = _apply_time_offs(
                list(day_avail.get(worker.id, [])),
                day_offs.get(worker.id, []),
            )
            day_data[worker.id] = {
                "free": _free_ranges(available, day_busy.get(worker.id, [])),
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


def _worker_fits_slot(worker_id, day_data, start_min: int, end_min: int) -> bool:
    if not _under_hour_cap_cached(worker_id, day_data, end_min - start_min):
        return False
    free = (day_data.get(worker_id) or {}).get("free") or []
    return any(s <= start_min and end_min <= e for s, e in free)


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


def available_slots(club, course_id, day: date, section_id=None, step_minutes=None):
    """Return timed slots with availability for the day schedule UI.

    Closed weekdays (no windows) return an empty day payload instead of an error,
    so callers can safely probe any date without 400 spam.
    """
    course = ServiceCourse.objects.filter(club=club, id=course_id, active=True).first()
    if not course:
        return None, "コースが見つかりません。"

    include_workers = bool(getattr(club, "service_allow_choose_worker", False))
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
    return (
        _build_day_slots(
            workers=workers,
            day_data=day_data,
            windows=windows,
            duration=int(course.duration_minutes),
            step=step,
            include_workers=include_workers,
            day=day,
            today=today,
            now_min=now_min,
        ),
        None,
    )


def available_week_slots(
    club, course_id, week_start: date, section_id=None, step_minutes=None
):
    """Return slots for Mon–Sun starting at week_start in one DB-prefetch pass.

    Closed / past / beyond-horizon days are included as empty payloads so the
    UI can map the full week without per-day fetches or 400s.
    """
    course = ServiceCourse.objects.filter(club=club, id=course_id, active=True).first()
    if not course:
        return None, "コースが見つかりません。"

    include_workers = bool(getattr(club, "service_allow_choose_worker", False))
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

    return {
        "week_start": week_start.isoformat(),
        "days": days_payload,
        "step_minutes": step,
        "allow_choose_worker": include_workers,
    }, None


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
    """Choose a free worker: preferred if still free, otherwise random."""
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
            existing.save(update_fields=update_fields)
            _apply_worker_picture(existing, files)
            ensure_worker_profile(existing)
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
    except IntegrityError:
        return None, "同じ名前・生年月日の会員がすでにあります。"
    sync_worker_quantity(club)
    return worker_detail(member), None


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


def worker_detail(member, *, include_login=False):
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
            status=ServiceReservation.Status.PAID,
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


def _day_blocks(worker, day, reservations, offs, availability):
    weekday = day.weekday()
    shift = [
        (_tmin(row.start_time), _tmin(row.end_time))
        for row in availability
        if row.weekday == weekday and row.start_time < row.end_time
    ]
    for off in offs:
        if off.date != day:
            continue
        if off.start_time is None or off.end_time is None:
            shift = []
            break
        shift = _cuts(shift, _tmin(off.start_time), _tmin(off.end_time))
    open_ranges = list(shift)
    day_rows = [row for row in reservations if row.reservation_date == day]
    for row in day_rows:
        open_ranges = _cuts(open_ranges, _tmin(row.start_time), _tmin(row.end_time))
    reservation_minutes = sum(
        max(0, _tmin(row.end_time) - _tmin(row.start_time)) for row in day_rows
    )
    return shift, open_ranges, day_rows, reservation_minutes


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
    availability = list(
        WorkerAvailability.objects.filter(worker=worker).order_by(
            "weekday", "start_time", "id"
        )
    )
    days = []
    for offset in range(7):
        day = start + timedelta(days=offset)
        shift, open_ranges, day_rows, reservation_minutes = _day_blocks(
            worker, day, reservations, offs, availability
        )
        days.append({
            "date": day.isoformat(),
            "weekday": WEEKDAY_LABELS[day.weekday()],
            "shift_minutes": _range_minutes(shift),
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
    availability = list(WorkerAvailability.objects.filter(worker=worker))
    days = []
    day = start
    while day < end:
        shift, _open, day_rows, reservation_minutes = _day_blocks(
            worker, day, reservations, offs, availability
        )
        if shift or day_rows:
            days.append({
                "date": day.isoformat(),
                "weekday": WEEKDAY_LABELS[day.weekday()],
                "shift_minutes": _range_minutes(shift),
                "reservation_count": len(day_rows),
                "reservation_minutes": reservation_minutes,
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
            }
            weeks.append(bucket)
        bucket["shift_minutes"] += row["shift_minutes"]
        bucket["reservation_count"] += row["reservation_count"]
        bucket["reservation_minutes"] += row["reservation_minutes"]

    return {
        "worker_id": worker.id,
        "year": year,
        "month": month,
        "shift_minutes": sum(row["shift_minutes"] for row in days),
        "reservation_count": sum(row["reservation_count"] for row in days),
        "reservation_minutes": sum(row["reservation_minutes"] for row in days),
        "weeks": weeks,
        "days": days,
    }
