import hashlib
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from .models import Reservation


START_LINK_HOURS = 72
RESERVATION_HOLD_MINUTES = 31


def unpaid_hold_q(hold_cutoff):
    """
    Unpaid rows hold a lesson spot only after checkout has started,
    and only for the hold window. not_started rows never hold a spot.
    """

    started_recently = Q(checkout_started_at__gte=hold_cutoff) | Q(
        checkout_started_at__isnull=True,
        created_at__gte=hold_cutoff,
    )

    return Q(status=Reservation.Status.UNPAID) & started_recently


def active_held_reservations_q(hold_cutoff, today=None):
    today = today or timezone.localdate()

    return Q(reservation_date__gte=today) & (
        Q(status=Reservation.Status.PAID)
        | unpaid_hold_q(hold_cutoff)
    )


def hold_cutoff_at(now=None):
    now = now or timezone.now()
    return now - timedelta(minutes=RESERVATION_HOLD_MINUTES)


def hold_is_active(reservation, hold_cutoff):
    started = reservation.checkout_started_at or reservation.created_at
    return started >= hold_cutoff


def request_is_fresh(reservation, now=None):
    now = now or timezone.now()

    if reservation.reservation_date < timezone.localdate():
        return False

    requested_at = reservation.requested_at or reservation.created_at
    cutoff = now - timedelta(hours=START_LINK_HOURS)
    return requested_at >= cutoff


def normalize_reservation_name(name):
    collapsed = " ".join(
        str(name or "").replace("\u3000", " ").strip().split()
    )
    return collapsed.casefold()


def display_reservation_name(name):
    return " ".join(
        str(name or "").replace("\u3000", " ").strip().split()
    )


def make_reservation_key(
    *,
    kind,
    lesson_id,
    reservation_date,
    email,
    normalized_name="",
):
    raw = (
        f"{kind}:{lesson_id}:"
        f"{reservation_date.isoformat()}:{email}"
    )

    if kind == "trial":
        raw = f"{raw}:{normalized_name}"

    return hashlib.sha256(raw.encode()).hexdigest()


def assert_lesson_has_spots(
    *,
    lesson,
    reservation_date,
    hold_cutoff,
    needed=1,
    exclude_ids=None,
):
    if lesson.reservation_limit is None:
        return

    spots = (
        Reservation.objects
        .filter(
            lesson=lesson,
            reservation_date=reservation_date,
        )
        .filter(
            Q(status=Reservation.Status.PAID)
            | unpaid_hold_q(hold_cutoff)
        )
    )

    if exclude_ids:
        spots = spots.exclude(id__in=list(exclude_ids))

    if spots.count() + needed > lesson.reservation_limit:
        raise ValueError("このレッスンは満員です。")


def resolve_lesson_or_club(lesson_value, club_value):
    if lesson_value is not None:
        return lesson_value

    return club_value


def resolve_max_days_ahead(lesson_value, club_value):
    return resolve_lesson_or_club(lesson_value, club_value)


def assert_reservation_horizon(*, reservation_date, max_days_ahead):
    today = timezone.localdate()

    if reservation_date < today:
        raise ValueError("過去の日付は予約できません。")

    if max_days_ahead is None:
        return

    latest = today + timedelta(days=max_days_ahead)

    if reservation_date > latest:
        if max_days_ahead == 0:
            raise ValueError("予約できるのは当日のみです。")

        raise ValueError(
            f"予約できるのは本日より{max_days_ahead}日先までです。"
        )


def count_active_reservations(*, hold_cutoff, **filters):
    return (
        Reservation.objects
        .filter(**filters)
        .filter(active_held_reservations_q(hold_cutoff))
        .count()
    )


def assert_member_reservation_caps(*, club, lesson, member, hold_cutoff):
    max_count = resolve_lesson_or_club(
        lesson.member_reservation_max_count,
        club.member_reservation_max_count,
    )

    if max_count is None:
        return

    count = count_active_reservations(
        hold_cutoff=hold_cutoff,
        lesson=lesson,
        member=member,
        reservation_type=Reservation.ReservationType.MEMBER,
    )

    if count >= max_count:
        raise ValueError(
            f"このレッスンの会員予約は{max_count}件までです。"
            "上限に達しています。"
        )


def assert_visitor_reservation_caps(
    *,
    club,
    lesson,
    email,
    hold_cutoff,
    exclude_ids=None,
    additional=1,
):
    max_count = resolve_lesson_or_club(
        lesson.visitor_reservation_max_count,
        club.visitor_reservation_max_count,
    )

    if max_count is None:
        return

    existing = (
        Reservation.objects
        .filter(
            lesson=lesson,
            email=email,
            reservation_type=Reservation.ReservationType.VISITOR,
        )
        .filter(active_held_reservations_q(hold_cutoff))
    )

    if exclude_ids:
        existing = existing.exclude(id__in=list(exclude_ids))

    if existing.count() + additional > max_count:
        raise ValueError(
            f"このレッスンの一般予約は{max_count}件までです。"
            "上限に達しています。"
        )
