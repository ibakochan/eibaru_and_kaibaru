from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    Competition,
    EventReservation,
    MemberRecord,
    Placement,
    Test,
    TestResult,
)


def event_test(event):
    try:
        return event.test
    except Test.DoesNotExist:
        return None


def test_payload(test):
    return {
        "id": test.id,
        "results_public": test.results_public,
        "reservations_open_until": test.reservations_open_until,
        "frozen_at": test.frozen_at,
    }


def event_competition(event):
    try:
        return event.competition
    except Competition.DoesNotExist:
        return None


def _paid_rows(event):
    return EventReservation.objects.filter(
        event=event,
        is_deleted=False,
        canceled=False,
        status=EventReservation.Status.PAID,
    )


def _visible_paid_rows(event, public, user):
    rows = _paid_rows(event).order_by("full_name", "id")
    if public:
        return rows
    if user is None or not getattr(user, "is_authenticated", False):
        return rows.none()
    if event.club.owner_id == user.id:
        return rows
    owned_member_ids = list(
        event.club.members.filter(owner=user).values_list("id", flat=True)
    )
    query = Q(member_id__in=owned_member_ids) | Q(user=user)
    email = (getattr(user, "email", "") or "").strip()
    if email:
        query |= Q(email__iexact=email)
    return rows.filter(query)


def visible_test_results(event, user):
    test = event_test(event)
    if test is None:
        return None

    rows = _visible_paid_rows(event, test.results_public, user)
    scores = dict(
        TestResult.objects.filter(test=test, reservation__in=rows).values_list(
            "reservation_id",
            "score",
        )
    )
    return [
        {
            "id": row.id,
            "full_name": row.full_name,
            "result": scores.get(row.id, ""),
            "place": None,
            "price_name": row.price_name,
            "member": row.member_id,
        }
        for row in rows
    ]


def visible_competition_results(event, user):
    competition = event_competition(event)
    if competition is None:
        return None

    rows = list(_visible_paid_rows(event, competition.results_public, user))
    places = dict(
        Placement.objects.filter(
            competition=competition,
            reservation__in=rows,
        ).values_list("reservation_id", "place")
    )
    total = _paid_rows(event).count()
    placed = []
    unplaced = []
    for row in rows:
        place = places.get(row.id)
        item = {
            "id": row.id,
            "full_name": row.full_name,
            "result": f"{place}位 / {total}人" if place else "",
            "place": place,
            "price_name": row.price_name,
            "member": row.member_id,
        }
        if place:
            placed.append(item)
        else:
            unplaced.append(item)
    placed.sort(key=lambda item: (item["place"], item["full_name"]))
    return placed + unplaced


def _sync_member_record(reservation, score, kind=MemberRecord.Kind.TEST):
    if not reservation.member_id:
        return

    occurred_on = timezone.localtime(reservation.event.starts_at).date()
    record = MemberRecord.objects.filter(reservation=reservation).first()
    if record is None:
        MemberRecord.objects.create(
            club=reservation.club,
            member=reservation.member,
            event=reservation.event,
            reservation=reservation,
            kind=kind,
            name=(reservation.event.title or "")[:200] or "記録",
            occurred_on=occurred_on,
            result=score,
        )
        return

    record.member = reservation.member
    record.event = reservation.event
    record.kind = kind
    record.result = score
    record.save(update_fields=["member", "event", "kind", "result", "updated_at"])


@transaction.atomic
def set_test_result(reservation, result_text):
    test = event_test(reservation.event)
    if test is None:
        raise ValueError("テストの結果だけ入力できます。")

    text = str(result_text or "").strip()
    if len(text) > 80:
        raise ValueError("結果は80文字以内にしてください。")

    TestResult.objects.update_or_create(
        reservation=reservation,
        defaults={"test": test, "score": text},
    )
    _sync_member_record(reservation, text, MemberRecord.Kind.TEST)
    return text


def sync_test_result_from_record(record):
    if not record.reservation_id:
        return
    reservation = record.reservation
    test = event_test(reservation.event)
    if test is None:
        return
    TestResult.objects.update_or_create(
        reservation=reservation,
        defaults={"test": test, "score": record.result},
    )


@transaction.atomic
def set_competition_place(reservation, place_value):
    competition = event_competition(reservation.event)
    if competition is None:
        raise ValueError("競技の順位だけ入力できます。")

    raw = str(place_value if place_value is not None else "").strip()
    if raw == "":
        Placement.objects.filter(reservation=reservation).delete()
        _sync_member_record(reservation, "", MemberRecord.Kind.INDIVIDUAL_COMPETITION)
        return None, ""

    try:
        place = int(raw)
    except (TypeError, ValueError):
        raise ValueError("順位は数字で入力してください。")
    if place < 1:
        raise ValueError("順位は1以上にしてください。")

    total = _paid_rows(reservation.event).count()
    if total < 1 or place > total:
        raise ValueError(f"順位は1から{total}までです。")
    if Placement.objects.filter(competition=competition, place=place).exclude(
        reservation=reservation
    ).exists():
        raise ValueError("その順位はすでに使われています。")

    Placement.objects.update_or_create(
        reservation=reservation,
        defaults={"competition": competition, "place": place},
    )
    label = f"{place}位 / {total}人"
    _sync_member_record(
        reservation,
        label,
        MemberRecord.Kind.INDIVIDUAL_COMPETITION,
    )
    return place, label


def sync_competition_place_from_record(record):
    if not record.reservation_id:
        return
    reservation = record.reservation
    if event_competition(reservation.event) is None:
        return
    text = (record.result or "").strip()
    if not text:
        set_competition_place(reservation, "")
        return
    digits = ""
    for char in text:
        if char.isdigit():
            digits += char
        elif digits:
            break
    if not digits:
        raise ValueError("順位は数字で入力してください。")
    set_competition_place(reservation, digits)
