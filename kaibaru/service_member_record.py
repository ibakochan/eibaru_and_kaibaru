import random

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    Competition,
    Duel,
    EventReservation,
    MemberRecord,
    Pairing,
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


def _sync_member_record(reservation, score, kind=MemberRecord.Kind.TEST, details=None):
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
            details=details if details is not None else [],
        )
        return

    record.member = reservation.member
    record.event = reservation.event
    record.kind = kind
    record.result = score
    update_fields = ["member", "event", "kind", "result", "updated_at"]
    if details is not None:
        record.details = details
        update_fields.append("details")
    record.save(update_fields=update_fields)


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


def event_duel(event):
    try:
        return event.duel
    except Duel.DoesNotExist:
        return None


def _person(reservation):
    return {
        "id": reservation.id,
        "full_name": reservation.full_name,
        "member": reservation.member_id,
    }


def _match_payload(pairing):
    return {
        "id": pairing.id,
        "position": pairing.position,
        "first": _person(pairing.first),
        "second": _person(pairing.second),
        "winner_id": pairing.winner_id,
        "drawn": pairing.drawn,
        "note": pairing.note,
    }


def _is_owner(event, user):
    return (
        user is not None
        and getattr(user, "is_authenticated", False)
        and event.club.owner_id == user.id
    )


def visible_duel_matches(duel, user):
    pairings = duel.pairings.select_related("first", "second").order_by("position", "id")
    if duel.results_public or _is_owner(duel.event, user):
        return [_match_payload(pairing) for pairing in pairings]
    visible_ids = set(
        _visible_paid_rows(duel.event, False, user).values_list("id", flat=True)
    )
    return [
        _match_payload(pairing)
        for pairing in pairings
        if pairing.first_id in visible_ids or pairing.second_id in visible_ids
    ]


def unpaired_pool(duel):
    paired_ids = set()
    for first_id, second_id in duel.pairings.values_list("first_id", "second_id"):
        paired_ids.add(first_id)
        paired_ids.add(second_id)
    rows = _paid_rows(duel.event).exclude(id__in=paired_ids).order_by("full_name", "id")
    return [_person(row) for row in rows]


def duel_payload(duel, user):
    payload = test_payload(duel)
    payload["matches"] = visible_duel_matches(duel, user)
    payload["pool"] = unpaired_pool(duel) if _is_owner(duel.event, user) else []
    return payload


def _parse_pair_ids(first_id, second_id):
    try:
        first = int(first_id)
        second = int(second_id)
    except (TypeError, ValueError):
        raise ValueError("2人を選んでください。")
    if first == second:
        raise ValueError("同じ人同士では対戦できません。")
    return first, second


def _locked_paid(event, ids):
    rows = list(
        EventReservation.objects.select_for_update()
        .filter(id__in=ids)
        .order_by("id")
    )
    found = {row.id: row for row in rows}
    if len(found) != len(set(ids)):
        raise ValueError("支払い済みの予約だけ対戦にできます。")
    for row in rows:
        if (
            row.event_id != event.id
            or row.is_deleted
            or row.canceled
            or row.status != EventReservation.Status.PAID
        ):
            raise ValueError("支払い済みの予約だけ対戦にできます。")
    return found


def _ensure_free(ids):
    if Pairing.objects.filter(
        Q(first_id__in=ids) | Q(second_id__in=ids)
    ).exists():
        raise ValueError("すでに対戦が決まっている人がいます。")


def _next_position(duel):
    current = (
        duel.pairings.order_by("-position").values_list("position", flat=True).first()
    )
    return (current or 0) + 1


def _duel_label(outcome, _opponent_name):
    return outcome[:80]


def _write_duel_records(pairing):
    sides = (
        (pairing.first, pairing.second),
        (pairing.second, pairing.first),
    )
    for person, opponent in sides:
        if pairing.drawn:
            outcome = "引き分け"
        elif person.id == pairing.winner_id:
            outcome = "勝ち"
        else:
            outcome = "負け"
        note = (pairing.note or "").strip()
        details = [{"label": "勝因", "value": note}] if pairing.winner_id and note else []
        _sync_member_record(
            person,
            _duel_label(outcome, opponent.full_name),
            MemberRecord.Kind.DUEL,
            details,
        )


def _clear_duel_records(pairing):
    MemberRecord.objects.filter(
        reservation_id__in=[pairing.first_id, pairing.second_id]
    ).delete()


def _renumber_pairings(duel):
    for index, pairing in enumerate(
        list(duel.pairings.order_by("position", "id")),
        start=1,
    ):
        if pairing.position != index:
            pairing.position = index
            pairing.save(update_fields=["position"])


@transaction.atomic
def pair_duel(duel, first_id, second_id):
    first, second = _parse_pair_ids(first_id, second_id)
    _locked_paid(duel.event, [first, second])
    _ensure_free([first, second])
    try:
        Pairing.objects.create(
            duel=duel,
            position=_next_position(duel),
            first_id=first,
            second_id=second,
        )
    except IntegrityError:
        raise ValueError("すでに対戦が決まっている人がいます。")


@transaction.atomic
def pair_duel_rest(duel):
    paired_ids = set()
    for first_id, second_id in duel.pairings.values_list("first_id", "second_id"):
        paired_ids.add(first_id)
        paired_ids.add(second_id)
    free_ids = list(
        _paid_rows(duel.event)
        .exclude(id__in=paired_ids)
        .order_by("id")
        .values_list("id", flat=True)
    )
    if len(free_ids) % 2 != 0:
        raise ValueError(
            "ランダムに組むには、予約をあと1件取るか、予約を1件キャンセルして偶数にしてください。"
        )
    if len(free_ids) < 2:
        raise ValueError("対戦できる人が2人未満です。")
    locked = _locked_paid(duel.event, free_ids)
    _ensure_free(free_ids)
    people = list(locked.values())
    if len(people) % 2 != 0:
        raise ValueError(
            "ランダムに組むには、予約をあと1件取るか、予約を1件キャンセルして偶数にしてください。"
        )
    random.shuffle(people)
    position = _next_position(duel) - 1
    try:
        for index in range(0, len(people) - 1, 2):
            position += 1
            Pairing.objects.create(
                duel=duel,
                position=position,
                first=people[index],
                second=people[index + 1],
            )
    except IntegrityError:
        raise ValueError("すでに対戦が決まっている人がいます。")


@transaction.atomic
def unpair_duel(duel, pairing_id):
    try:
        pairing_key = int(pairing_id)
    except (TypeError, ValueError):
        raise ValueError("試合が見つかりません。")
    pairing = (
        Pairing.objects.select_for_update()
        .filter(duel=duel, id=pairing_key)
        .first()
    )
    if pairing is None:
        raise ValueError("試合が見つかりません。")
    _clear_duel_records(pairing)
    pairing.delete()
    _renumber_pairings(duel)


@transaction.atomic
def set_duel_winner(duel, pairing_id, winner_value):
    try:
        pairing_key = int(pairing_id)
    except (TypeError, ValueError):
        raise ValueError("試合が見つかりません。")
    pairing = (
        Pairing.objects.select_for_update()
        .select_related("first", "second")
        .filter(duel=duel, id=pairing_key)
        .first()
    )
    if pairing is None:
        raise ValueError("試合が見つかりません。")
    raw = "" if winner_value is None else str(winner_value).strip()
    if raw == "" or raw == "clear":
        pairing.winner = None
        pairing.drawn = False
        pairing.note = ""
        pairing.save(update_fields=["winner", "drawn", "note"])
        _clear_duel_records(pairing)
        return
    if raw == "draw":
        pairing.winner = None
        pairing.drawn = True
        pairing.note = ""
        pairing.save(update_fields=["winner", "drawn", "note"])
        _write_duel_records(pairing)
        return
    try:
        winner_id = int(raw)
    except (TypeError, ValueError):
        raise ValueError("勝った人はこの試合の2人から選んでください。")
    if winner_id not in {pairing.first_id, pairing.second_id}:
        raise ValueError("勝った人はこの試合の2人から選んでください。")
    pairing.winner_id = winner_id
    pairing.drawn = False
    pairing.save(update_fields=["winner", "drawn"])
    _write_duel_records(pairing)


def _duel_pairing(duel, pairing_id):
    try:
        pairing_key = int(pairing_id)
    except (TypeError, ValueError):
        raise ValueError("試合が見つかりません。")
    pairing = (
        Pairing.objects.select_for_update()
        .select_related("first", "second")
        .filter(duel=duel, id=pairing_key)
        .first()
    )
    if pairing is None:
        raise ValueError("試合が見つかりません。")
    return pairing


@transaction.atomic
def set_duel_note(duel, pairing_id, note):
    pairing = _duel_pairing(duel, pairing_id)
    if pairing.winner_id is None:
        raise ValueError("勝ちが決まってから理由を書けます。")
    text = str(note or "").strip()
    if len(text) > 200:
        raise ValueError("理由は200文字以内にしてください。")
    pairing.note = text
    pairing.save(update_fields=["note"])
    _write_duel_records(pairing)


def sync_duel_winner_from_record(record):
    if not record.reservation_id:
        return
    pairing = (
        Pairing.objects.filter(
            Q(first_id=record.reservation_id) | Q(second_id=record.reservation_id)
        )
        .select_related("duel")
        .first()
    )
    if pairing is None:
        return
    text = (record.result or "").strip()
    if text.startswith("勝ち"):
        set_duel_winner(pairing.duel, pairing.id, record.reservation_id)
        return
    if text.startswith("負け"):
        other = (
            pairing.second_id
            if pairing.first_id == record.reservation_id
            else pairing.first_id
        )
        set_duel_winner(pairing.duel, pairing.id, other)
        return
    if text.startswith("引き分け"):
        set_duel_winner(pairing.duel, pairing.id, "draw")
        return
    if text == "":
        set_duel_winner(pairing.duel, pairing.id, "")
        return
    raise ValueError("対戦の結果は「勝ち」「負け」「引き分け」で入力してください。")
