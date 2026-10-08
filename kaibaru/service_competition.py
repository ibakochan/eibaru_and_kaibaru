from django.db import transaction

from .models import Division, DivisionEntry, EventPrice, MemberRecord
from .service_member_record import (
    _is_owner,
    _paid_rows,
    _person,
    _sync_member_record,
    _visible_paid_rows,
    test_payload,
)


def _clean_name(value, fallback):
    name = str(value or "").strip() or fallback
    if len(name) > 80:
        raise ValueError("名前は80文字以内にしてください。")
    return name


def _division(competition, division_id):
    try:
        key = int(division_id)
    except (TypeError, ValueError):
        raise ValueError("種目が見つかりません。")
    division = competition.divisions.filter(id=key).first()
    if division is None:
        raise ValueError("種目が見つかりません。")
    return division


def _record_name(event, division_name):
    title = (event.title or "").strip()
    return f"{title} {division_name}".strip()[:200] or "記録"


def _parse_place(place_value):
    raw = str(place_value if place_value is not None else "").strip()
    if raw == "":
        return None
    digits = ""
    for char in raw:
        if char.isdigit():
            digits += char
        elif digits:
            break
    if not digits:
        raise ValueError("順位は数字で入力してください。")
    place = int(digits)
    if place < 1:
        raise ValueError("順位は1以上にしてください。")
    return place


def _refresh_division(division):
    total = division.entries.count()
    event = division.competition.event
    record_name = _record_name(event, division.name)
    for entry in division.entries.select_related("reservation"):
        MemberRecord.objects.filter(
            reservation=entry.reservation,
            kind=MemberRecord.Kind.INDIVIDUAL_COMPETITION,
            division__isnull=True,
        ).delete()
        label = f"{entry.place}位 / {total}人" if entry.place else ""
        _sync_member_record(
            entry.reservation,
            label,
            MemberRecord.Kind.INDIVIDUAL_COMPETITION,
            [],
            division=division,
            record_name=record_name,
        )


def competition_payload(competition, user):
    payload = test_payload(competition)
    owner = _is_owner(competition.event, user)
    event = competition.event
    divisions = list(
        competition.divisions.prefetch_related("entries__reservation").order_by("position", "id")
    )
    visible_ids = set()
    if not owner and not competition.results_public:
        visible_ids = set(
            _visible_paid_rows(event, False, user).values_list("id", flat=True)
        )
        divisions = [
            division
            for division in divisions
            if any(entry.reservation_id in visible_ids for entry in division.entries.all())
        ]
    payload["divisions"] = []
    for division in divisions:
        entries = []
        for entry in division.entries.all():
            if (
                not owner
                and not competition.results_public
                and entry.reservation_id not in visible_ids
            ):
                continue
            person = _person(entry.reservation)
            person["place"] = entry.place
            entries.append(person)
        entries.sort(key=lambda item: (item["place"] is None, item["place"] or 0, item["full_name"]))
        payload["divisions"].append({
            "id": division.id,
            "name": division.name,
            "position": division.position,
            "entries": entries,
        })
    payload["prices"] = []
    payload["pool"] = []
    if owner:
        for price in event.prices.prefetch_related("divisions").order_by("position", "id"):
            payload["prices"].append({
                "id": price.id,
                "name": price.name,
                "amount": price.amount,
                "division_ids": [item.id for item in price.divisions.all()],
            })
        payload["pool"] = [
            _person(row) for row in _paid_rows(event).order_by("full_name", "id")
        ]
    return payload


@transaction.atomic
def create_division(competition, name):
    count = competition.divisions.count()
    if count >= 30:
        raise ValueError("種目は30件までです。")
    position = (competition.divisions.order_by("-position").values_list("position", flat=True).first() or 0) + 1
    cleaned = _clean_name(name, f"種目{count + 1}")
    if competition.divisions.filter(name=cleaned).exists():
        raise ValueError("同じ名前の種目がすでにあります。")
    Division.objects.create(competition=competition, name=cleaned, position=position)


@transaction.atomic
def rename_division(competition, division_id, name):
    division = _division(competition, division_id)
    cleaned = _clean_name(name, division.name)
    if competition.divisions.filter(name=cleaned).exclude(id=division.id).exists():
        raise ValueError("同じ名前の種目がすでにあります。")
    division.name = cleaned
    division.save(update_fields=["name"])
    _refresh_division(division)


@transaction.atomic
def remove_division(competition, division_id):
    division = _division(competition, division_id)
    MemberRecord.objects.filter(division=division).delete()
    division.delete()


@transaction.atomic
def set_division_price(competition, price_id, division_id, included):
    division = _division(competition, division_id)
    try:
        key = int(price_id)
    except (TypeError, ValueError):
        raise ValueError("料金が見つかりません。")
    price = EventPrice.objects.filter(id=key, event_id=competition.event_id).first()
    if price is None:
        raise ValueError("料金が見つかりません。")
    if included is True or included == "true" or included == 1 or included == "1":
        price.divisions.add(division)
    else:
        price.divisions.remove(division)


@transaction.atomic
def enter_division(competition, division_id, reservation_id):
    division = _division(competition, division_id)
    try:
        key = int(reservation_id)
    except (TypeError, ValueError):
        raise ValueError("予約が見つかりません。")
    reservation = _paid_rows(competition.event).filter(id=key).first()
    if reservation is None:
        raise ValueError("支払い済みの予約だけ種目に入れられます。")
    if division.entries.filter(reservation=reservation).exists():
        raise ValueError("この人はすでにこの種目に入っています。")
    position = (division.entries.order_by("-position").values_list("position", flat=True).first() or 0) + 1
    DivisionEntry.objects.create(
        division=division,
        reservation=reservation,
        position=position,
    )
    _refresh_division(division)


@transaction.atomic
def leave_division(competition, division_id, reservation_id):
    division = _division(competition, division_id)
    try:
        key = int(reservation_id)
    except (TypeError, ValueError):
        raise ValueError("参加者が見つかりません。")
    entry = division.entries.filter(reservation_id=key).first()
    if entry is None:
        raise ValueError("参加者が見つかりません。")
    entry.delete()
    MemberRecord.objects.filter(reservation_id=key, division=division).delete()
    if not DivisionEntry.objects.filter(reservation_id=key, division__competition=competition).exists():
        MemberRecord.objects.filter(
            reservation_id=key,
            kind=MemberRecord.Kind.INDIVIDUAL_COMPETITION,
            division__isnull=True,
        ).delete()
    _refresh_division(division)


@transaction.atomic
def set_division_place(competition, division_id, reservation_id, place_value):
    division = _division(competition, division_id)
    try:
        key = int(reservation_id)
    except (TypeError, ValueError):
        raise ValueError("参加者が見つかりません。")
    entry = (
        DivisionEntry.objects.select_for_update()
        .filter(division=division, reservation_id=key)
        .first()
    )
    if entry is None:
        raise ValueError("この人はこの種目に入っていません。")
    place = _parse_place(place_value)
    total = division.entries.count()
    if place is not None and place > total:
        raise ValueError(f"順位は1から{total}までです。")
    if place is not None and division.entries.filter(place=place).exclude(id=entry.id).exists():
        raise ValueError("その順位はすでに使われています。")
    entry.place = place
    entry.save(update_fields=["place"])
    _refresh_division(division)
