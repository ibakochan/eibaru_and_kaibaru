import random

from django.db import IntegrityError, transaction
from django.db.models import Q

from .models import (
    BattleMatch,
    BattleMember,
    BattleTeam,
    EventReservation,
    MemberRecord,
    TeamBattle,
)
from .service_member_record import (
    _is_owner,
    _paid_rows,
    _sync_member_record,
    _visible_paid_rows,
    test_payload,
)


def event_team_battle(event):
    try:
        return event.team_battle
    except TeamBattle.DoesNotExist:
        return None


def _person(reservation):
    return {
        "id": reservation.id,
        "full_name": reservation.full_name,
        "member": reservation.member_id,
        "price_name": reservation.price_name,
        "amount": reservation.amount,
    }


def _team_dict(team):
    return {
        "id": team.id,
        "name": team.name,
        "position": team.position,
        "members": [
            _person(member.reservation)
            for member in team.members.all()
        ],
    }


def _match_dict(match):
    return {
        "id": match.id,
        "position": match.position,
        "first": _team_dict(match.first),
        "second": _team_dict(match.second),
        "winner_id": match.winner_id,
        "drawn": match.drawn,
        "note": match.note,
    }


def _teams(battle):
    return battle.teams.prefetch_related("members__reservation").order_by("position", "id")


def _matches(battle):
    return (
        battle.matches
        .select_related("first", "second", "winner")
        .prefetch_related(
            "first__members__reservation",
            "second__members__reservation",
        )
        .order_by("position", "id")
    )


def _paired_team_ids(battle):
    paired = set()
    for first_id, second_id in battle.matches.values_list("first_id", "second_id"):
        paired.add(first_id)
        paired.add(second_id)
    return paired


def _visible_matches(battle, user):
    matches = list(_matches(battle))
    if battle.results_public or battle.event.archive_visibility or _is_owner(battle.event, user):
        return matches
    visible_ids = set(
        _visible_paid_rows(battle.event, False, user).values_list("id", flat=True)
    )
    if not visible_ids:
        return []
    visible = []
    for match in matches:
        member_ids = {
            member.reservation_id
            for member in list(match.first.members.all()) + list(match.second.members.all())
        }
        if member_ids & visible_ids:
            visible.append(match)
    return visible


def team_battle_payload(battle, user):
    payload = test_payload(battle)
    owner = _is_owner(battle.event, user)
    paired = _paired_team_ids(battle)
    payload["matches"] = [_match_dict(match) for match in _visible_matches(battle, user)]
    if not owner:
        payload["teams"] = []
        payload["pool"] = []
        return payload
    payload["teams"] = [
        _team_dict(team) | {"paired": team.id in paired}
        for team in _teams(battle)
    ]
    assigned = set(
        BattleMember.objects.filter(team__battle=battle).values_list("reservation_id", flat=True)
    )
    payload["pool"] = [
        _person(row)
        for row in _paid_rows(battle.event).exclude(id__in=assigned).order_by("full_name", "id")
    ]
    return payload


def _next_position(manager):
    current = manager.order_by("-position").values_list("position", flat=True).first()
    return (current or 0) + 1


def _clean_name(value, fallback):
    name = str(value or "").strip() or fallback
    if len(name) > 80:
        raise ValueError("チーム名は80文字以内にしてください。")
    return name


def _team(battle, team_id):
    try:
        key = int(team_id)
    except (TypeError, ValueError):
        raise ValueError("チームが見つかりません。")
    team = (
        BattleTeam.objects.select_for_update()
        .filter(battle=battle, id=key)
        .first()
    )
    if team is None:
        raise ValueError("チームが見つかりません。")
    return team


def _paid(event, reservation_id):
    try:
        key = int(reservation_id)
    except (TypeError, ValueError):
        raise ValueError("支払い済みの予約だけチームに入れられます。")
    row = EventReservation.objects.select_for_update().filter(id=key).first()
    if (
        row is None
        or row.event_id != event.id
        or row.is_deleted
        or row.canceled
        or row.status != EventReservation.Status.PAID
    ):
        raise ValueError("支払い済みの予約だけチームに入れられます。")
    return row


def _reservation_ids(values):
    if isinstance(values, (str, int)):
        values = [values]
    if not isinstance(values, (list, tuple)):
        raise ValueError("参加者を選んでください。")
    ids = []
    seen = set()
    for value in values:
        try:
            key = int(value)
        except (TypeError, ValueError):
            raise ValueError("参加者が見つかりません。")
        if key in seen:
            continue
        seen.add(key)
        ids.append(key)
    if not ids:
        raise ValueError("参加者を選んでください。")
    return ids


def _person_label(reservation):
    name = str(reservation.full_name or "").strip()
    return f"{name}さん" if name else "この人"


def _paid_named(event, reservation_id):
    try:
        key = int(reservation_id)
    except (TypeError, ValueError):
        raise ValueError("参加者が見つかりません。")
    row = EventReservation.objects.select_for_update().filter(id=key).first()
    if row is None:
        raise ValueError("参加者が見つかりません。")
    if (
        row.event_id != event.id
        or row.is_deleted
        or row.canceled
        or row.status != EventReservation.Status.PAID
    ):
        raise ValueError(f"{_person_label(row)}は支払い済みの予約だけチームに入れられます。")
    return row


def _renumber(manager):
    for index, row in enumerate(list(manager.order_by("position", "id")), start=1):
        if row.position != index:
            row.position = index
            row.save(update_fields=["position"])


def _clear_reservation_record(reservation_id):
    MemberRecord.objects.filter(
        reservation_id=reservation_id,
        kind=MemberRecord.Kind.TEAM_COMPETITION,
        division__isnull=True,
        bracket__isnull=True,
    ).delete()


def _clear_match_records(match):
    reservation_ids = list(
        BattleMember.objects.filter(
            team_id__in=[match.first_id, match.second_id]
        ).values_list("reservation_id", flat=True)
    )
    if not reservation_ids:
        return
    MemberRecord.objects.filter(
        reservation_id__in=reservation_ids,
        kind=MemberRecord.Kind.TEAM_COMPETITION,
        division__isnull=True,
        bracket__isnull=True,
    ).delete()


def _write_match_records(match):
    sides = (
        (match.first, match.second),
        (match.second, match.first),
    )
    current_ids = []
    for team, opponent in sides:
        members = list(team.members.select_related("reservation"))
        for member in members:
            current_ids.append(member.reservation_id)
            if match.drawn:
                outcome = "引き分け"
            elif team.id == match.winner_id:
                outcome = "勝ち"
            else:
                outcome = "負け"
            note = (match.note or "").strip()
            details = [
                {"label": "チーム", "value": team.name},
                {"label": "相手", "value": opponent.name},
            ]
            if match.winner_id and note:
                details.append({"label": "勝因", "value": note})
            _sync_member_record(
                member.reservation,
                outcome,
                MemberRecord.Kind.TEAM_COMPETITION,
                details,
            )
    if current_ids:
        return
    _clear_match_records(match)


def _refresh_team_match(team):
    match = (
        BattleMatch.objects.select_related("first", "second")
        .filter(Q(first=team) | Q(second=team))
        .first()
    )
    if match is None:
        return
    if match.drawn or match.winner_id:
        _write_match_records(match)
    else:
        _clear_match_records(match)


@transaction.atomic
def create_team(battle, name):
    count = battle.teams.count() + 1
    BattleTeam.objects.create(
        battle=battle,
        name=_clean_name(name, f"チーム{count}"),
        position=_next_position(battle.teams),
    )


@transaction.atomic
def rename_team(battle, team_id, name):
    team = _team(battle, team_id)
    team.name = _clean_name(name, team.name)
    team.save(update_fields=["name"])
    _refresh_team_match(team)


@transaction.atomic
def remove_team(battle, team_id):
    team = _team(battle, team_id)
    if team.id in _paired_team_ids(battle):
        raise ValueError("対戦が決まっているチームは、先に試合を外してください。")
    reservation_ids = list(team.members.values_list("reservation_id", flat=True))
    team.delete()
    _renumber(battle.teams)
    if reservation_ids:
        MemberRecord.objects.filter(
            reservation_id__in=reservation_ids,
            kind=MemberRecord.Kind.TEAM_COMPETITION,
            division__isnull=True,
            bracket__isnull=True,
        ).delete()


@transaction.atomic
def add_member(battle, team_id, reservation_id):
    team = _team(battle, team_id)
    reservation = _paid(battle.event, reservation_id)
    if BattleMember.objects.filter(team=team, reservation=reservation).exists():
        raise ValueError("この人はすでにこのチームに入っています。")
    existing = (
        BattleMember.objects.select_for_update()
        .filter(reservation=reservation, team__battle=battle)
        .first()
    )
    if existing is not None:
        raise ValueError("この人はすでに別のチームに入っています。")
    try:
        BattleMember.objects.create(
            team=team,
            reservation=reservation,
            position=_next_position(team.members),
        )
    except IntegrityError:
        raise ValueError("この人はすでにチームに入っています。")
    _refresh_team_match(team)


@transaction.atomic
def add_members(battle, team_id, reservation_ids):
    team = _team(battle, team_id)
    ids = _reservation_ids(reservation_ids)
    already = set(
        BattleMember.objects.filter(team=team, reservation_id__in=ids).values_list(
            "reservation_id",
            flat=True,
        )
    )
    reservations = []
    for reservation_id in ids:
        if reservation_id in already:
            continue
        reservation = _paid_named(battle.event, reservation_id)
        existing = (
            BattleMember.objects.select_for_update()
            .filter(reservation=reservation, team__battle=battle)
            .first()
        )
        if existing is not None:
            raise ValueError(f"{_person_label(reservation)}はすでに別のチームに入っています。")
        reservations.append(reservation)
    if not reservations:
        return
    position = _next_position(team.members)
    for reservation in reservations:
        try:
            BattleMember.objects.create(
                team=team,
                reservation=reservation,
                position=position,
            )
        except IntegrityError:
            raise ValueError(f"{_person_label(reservation)}はすでにチームに入っています。")
        position += 1
    _refresh_team_match(team)


@transaction.atomic
def remove_member(battle, team_id, reservation_id):
    team = _team(battle, team_id)
    try:
        key = int(reservation_id)
    except (TypeError, ValueError):
        raise ValueError("参加者が見つかりません。")
    member = (
        BattleMember.objects.select_for_update()
        .filter(team=team, reservation_id=key)
        .first()
    )
    if member is None:
        raise ValueError("参加者が見つかりません。")
    member.delete()
    _renumber(team.members)
    _clear_reservation_record(key)
    _refresh_team_match(team)


def _ensure_free(team_ids):
    if BattleMatch.objects.filter(
        Q(first_id__in=team_ids) | Q(second_id__in=team_ids)
    ).exists():
        raise ValueError("すでに対戦が決まっているチームがあります。")


def _has_member(team_id):
    return BattleMember.objects.filter(team_id=team_id).exists()


@transaction.atomic
def pair_teams(battle, first_id, second_id):
    first = _team(battle, first_id)
    second = _team(battle, second_id)
    if first.id == second.id:
        raise ValueError("同じチーム同士では対戦できません。")
    if not _has_member(first.id) or not _has_member(second.id):
        raise ValueError("チームに人を入れてから対戦させてください。")
    _ensure_free([first.id, second.id])
    try:
        BattleMatch.objects.create(
            battle=battle,
            position=_next_position(battle.matches),
            first=first,
            second=second,
        )
    except IntegrityError:
        raise ValueError("すでに対戦が決まっているチームがあります。")


@transaction.atomic
def pair_rest(battle):
    paired = _paired_team_ids(battle)
    free = [
        team
        for team in BattleTeam.objects.select_for_update().filter(battle=battle).order_by("id")
        if team.id not in paired and _has_member(team.id)
    ]
    empty = BattleTeam.objects.filter(battle=battle).exclude(id__in=paired).exclude(
        id__in=[team.id for team in free]
    ).exists()
    if empty:
        raise ValueError("人が入っていないチームがあります。人を入れるか、チームを削除してください。")
    if len(free) < 2:
        raise ValueError("対戦できるチームが2つ未満です。")
    if len(free) % 2 != 0:
        raise ValueError("ランダムに組むには、チームの数を偶数にしてください。")
    _ensure_free([team.id for team in free])
    random.shuffle(free)
    position = _next_position(battle.matches) - 1
    try:
        for index in range(0, len(free) - 1, 2):
            position += 1
            BattleMatch.objects.create(
                battle=battle,
                position=position,
                first=free[index],
                second=free[index + 1],
            )
    except IntegrityError:
        raise ValueError("すでに対戦が決まっているチームがあります。")


@transaction.atomic
def unpair(battle, match_id):
    try:
        key = int(match_id)
    except (TypeError, ValueError):
        raise ValueError("試合が見つかりません。")
    match = (
        BattleMatch.objects.select_for_update()
        .filter(battle=battle, id=key)
        .first()
    )
    if match is None:
        raise ValueError("試合が見つかりません。")
    _clear_match_records(match)
    match.delete()
    _renumber(battle.matches)


@transaction.atomic
def set_winner(battle, match_id, winner_value):
    try:
        key = int(match_id)
    except (TypeError, ValueError):
        raise ValueError("試合が見つかりません。")
    match = (
        BattleMatch.objects.select_for_update()
        .select_related("first", "second")
        .filter(battle=battle, id=key)
        .first()
    )
    if match is None:
        raise ValueError("試合が見つかりません。")
    raw = "" if winner_value is None else str(winner_value).strip()
    if raw in {"", "clear"}:
        match.winner = None
        match.drawn = False
        match.note = ""
        match.save(update_fields=["winner", "drawn", "note"])
        _clear_match_records(match)
        return
    if raw == "draw":
        match.winner = None
        match.drawn = True
        match.note = ""
        match.save(update_fields=["winner", "drawn", "note"])
        _write_match_records(match)
        return
    try:
        winner_id = int(raw)
    except (TypeError, ValueError):
        raise ValueError("勝ったチームはこの試合の2チームから選んでください。")
    if winner_id not in {match.first_id, match.second_id}:
        raise ValueError("勝ったチームはこの試合の2チームから選んでください。")
    match.winner_id = winner_id
    match.drawn = False
    match.save(update_fields=["winner", "drawn"])
    _write_match_records(match)


@transaction.atomic
def set_note(battle, match_id, note):
    try:
        key = int(match_id)
    except (TypeError, ValueError):
        raise ValueError("試合が見つかりません。")
    match = (
        BattleMatch.objects.select_for_update()
        .select_related("first", "second")
        .filter(battle=battle, id=key)
        .first()
    )
    if match is None:
        raise ValueError("試合が見つかりません。")
    if match.winner_id is None:
        raise ValueError("勝ちが決まってから理由を書けます。")
    text = str(note or "").strip()
    if len(text) > 200:
        raise ValueError("理由は200文字以内にしてください。")
    match.note = text
    match.save(update_fields=["note"])
    _write_match_records(match)


def sync_team_battle_from_record(record):
    if not record.reservation_id:
        return
    membership = (
        BattleMember.objects.filter(reservation_id=record.reservation_id)
        .select_related("team__battle")
        .first()
    )
    if membership is None:
        return
    match = (
        BattleMatch.objects.filter(
            Q(first=membership.team) | Q(second=membership.team)
        )
        .select_related("battle")
        .first()
    )
    if match is None:
        return
    text = (record.result or "").strip()
    if text.startswith("勝ち"):
        set_winner(match.battle, match.id, membership.team_id)
        return
    if text.startswith("負け"):
        other = match.second_id if match.first_id == membership.team_id else match.first_id
        set_winner(match.battle, match.id, other)
        return
    if text.startswith("引き分け"):
        set_winner(match.battle, match.id, "draw")
