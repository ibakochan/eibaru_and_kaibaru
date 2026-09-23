from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from .models import Reservation


def active_held_reservations_q(hold_cutoff, today=None):
    today = today or timezone.localdate()

    return Q(reservation_date__gte=today) & (
        Q(status=Reservation.Status.PAID)
        | Q(
            status=Reservation.Status.UNPAID,
            created_at__gte=hold_cutoff,
        )
    )


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


def assert_visitor_reservation_caps(*, club, lesson, email, hold_cutoff):
    max_count = resolve_lesson_or_club(
        lesson.visitor_reservation_max_count,
        club.visitor_reservation_max_count,
    )

    if max_count is None:
        return

    count = count_active_reservations(
        hold_cutoff=hold_cutoff,
        lesson=lesson,
        email=email,
        reservation_type=Reservation.ReservationType.VISITOR,
    )

    if count >= max_count:
        raise ValueError(
            f"このレッスンの一般予約は{max_count}件までです。"
            "上限に達しています。"
        )
