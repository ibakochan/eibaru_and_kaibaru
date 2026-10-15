from django.db import IntegrityError, transaction
from django.db.models import Prefetch, Q

from .models import (
    EventReservation,
    MemberRecord,
    TeamBracket,
    TeamBracketEntry,
    TeamBracketMatch,
    TeamTournament,
    TournamentTeam,
    TournamentTeamMember,
)
from .service_event_read import cached_related, load_related, paid_reservations
from .service_member_record import (
    _is_owner,
    _sync_member_record,
    _visible_paid_rows,
    test_payload,
)
from .service_tournament import (
    _later_result,
    _lookup,
    _open_label,
    _record_label,
    _round_title,
    _sources,
)


def event_team_tournament(event):
    try:
        return event.team_tournament
    except TeamTournament.DoesNotExist:
        return None


def _person(reservation):
    return {
        "id": reservation.id,
        "full_name": reservation.full_name,
        "member": reservation.member_id,
        "price_name": reservation.price_name,
        "amount": reservation.amount,
    }


def _squad(team):
    if team is None:
        return None
    return {
        "id": team.id,
        "name": team.name,
        "position": team.position,
        "members": [
            _person(member.reservation)
            for member in team.members.all()
        ],
    }


def _member_prefetch():
    return "members__reservation"


def _load(bracket):
    entries = load_related(
        bracket,
        "entries",
        bracket.entries.prefetch_related(
            Prefetch(
                "team",
                queryset=TournamentTeam.objects.prefetch_related("members__reservation"),
            )
        ).order_by("position", "id"),
    )
    matches = load_related(
        bracket,
        "matches",
        bracket.matches.select_related("first", "second", "winner")
        .prefetch_related("first__members__reservation", "second__members__reservation")
        .order_by("round", "position", "id"),
    )
    return entries, matches


def _tree_view(entries, matches):
    teams = [entry.team for entry in entries]
    structure = _sources(len(teams))
    by_key = {(match.round, match.position): match for match in matches}
    winners = {}
    rounds = []
    total = len(structure)
    for index, specs in enumerate(structure):
        label = _round_title(index, total)
        rows = []
        for spec in specs:
            bye = spec["right"] is None
            left = _lookup(spec["left"], teams, winners)
            right = None if bye else _lookup(spec["right"], teams, winners)
            key = (spec["round"], spec["position"])
            match = by_key.get(key)
            same = (
                match is not None
                and left is not None
                and match.first_id == left.id
                and (
                    (bye and match.second_id is None)
                    or (right is not None and match.second_id == right.id)
                )
            )
            winner = None
            note = ""
            match_id = match.id if same else None
            if bye and left is not None:
                winner = left
            elif same and match.winner_id:
                if left is not None and match.winner_id == left.id:
                    winner = left
                elif right is not None and match.winner_id == right.id:
                    winner = right
                if winner is not None and not bye:
                    note = match.note or ""
            if winner is not None:
                winners[key] = winner
            pending = left is None or (not bye and right is None)
            rows.append({
                "id": match_id,
                "round": spec["round"],
                "position": spec["position"],
                "label": label,
                "first": _squad(left),
                "second": None if bye else _squad(right),
                "winner_id": winner.id if winner is not None else None,
                "bye": bye,
                "pending": pending,
                "note": note,
                "from": spec.get("from") or [],
                "result_locked": False,
            })
        rounds.append({"label": label, "matches": rows})
    for round_index, rnd in enumerate(rounds):
        for match_index, row in enumerate(rnd["matches"]):
            row["result_locked"] = bool(
                row["winner_id"]
                and not row["bye"]
                and _later_result(rounds, round_index, match_index)
            )
    champion = None
    complete = False
    if structure:
        last = structure[-1][0]
        champ = winners.get((last["round"], last["position"]))
        if champ is not None and last["right"] is not None:
            champion = champ
            complete = True
    locked = any(
        row["winner_id"] and not row["bye"]
        for rnd in rounds
        for row in rnd["matches"]
    )
    return rounds, champion, complete, locked


def _bracket_visible(bracket, visible_ids):
    entries = cached_related(bracket, "entries")
    if entries is None:
        return None
    visible = False
    for entry in entries:
        members = cached_related(entry.team, "members")
        if members is None:
            return None
        if any(member.reservation_id in visible_ids for member in members):
            visible = True
    return visible


def _visible_brackets(tournament, user):
    brackets = load_related(
        tournament,
        "brackets",
        tournament.brackets.order_by("position", "id"),
    )
    if tournament.results_public or tournament.event.archive_visibility or _is_owner(tournament.event, user):
        return brackets
    visible_ids = {row.id for row in _visible_paid_rows(tournament.event, False, user)}
    if not visible_ids:
        return []
    chosen = []
    for bracket in brackets:
        visible = _bracket_visible(bracket, visible_ids)
        if visible is None:
            entered = set(
                TeamBracketEntry.objects.filter(
                    bracket__tournament=tournament,
                    team__members__reservation_id__in=visible_ids,
                ).values_list("bracket_id", flat=True)
            )
            return [item for item in brackets if item.id in entered]
        if visible:
            chosen.append(bracket)
    return chosen


def _assigned_reservation_ids(tournament):
    teams = cached_related(tournament, "teams")
    if teams is not None and all(cached_related(team, "members") is not None for team in teams):
        return {
            member.reservation_id
            for team in teams
            for member in cached_related(team, "members")
        }
    return set(
        TournamentTeamMember.objects.filter(team__tournament=tournament).values_list(
            "reservation_id",
            flat=True,
        )
    )


def _entry_by_team(tournament):
    brackets = cached_related(tournament, "brackets")
    if brackets is not None and all(cached_related(bracket, "entries") is not None for bracket in brackets):
        return {
            entry.team_id: entry.bracket_id
            for bracket in brackets
            for entry in cached_related(bracket, "entries")
        }
    return {
        entry.team_id: entry.bracket_id
        for entry in TeamBracketEntry.objects.filter(bracket__tournament=tournament)
    }


def team_tournament_payload(tournament, user):
    payload = test_payload(tournament)
    owner = _is_owner(tournament.event, user)
    assigned = _assigned_reservation_ids(tournament) if owner else set()
    payload["pool"] = [
        _person(row)
        for row in paid_reservations(tournament.event)
        if row.id not in assigned
    ] if owner else []
    entry_by_team = _entry_by_team(tournament) if owner else {}
    payload["teams"] = []
    if owner:
        teams = load_related(
            tournament,
            "teams",
            tournament.teams.prefetch_related(_member_prefetch()).order_by("position", "id"),
        )
        for team in teams:
            payload["teams"].append(
                _squad(team) | {"bracket_id": entry_by_team.get(team.id)}
            )
    brackets = []
    for bracket in _visible_brackets(tournament, user):
        entries, matches = _load(bracket)
        rounds, champion, complete, locked = _tree_view(entries, matches)
        brackets.append({
            "id": bracket.id,
            "name": bracket.name,
            "position": bracket.position,
            "started": locked,
            "complete": complete,
            "champion": _squad(champion),
            "round_label": _open_label(rounds, complete),
            "teams": [_squad(entry.team) for entry in entries],
            "rounds": rounds,
        })
    payload["brackets"] = brackets
    return payload


def _next_position(manager):
    current = manager.order_by("-position").values_list("position", flat=True).first()
    return (current or 0) + 1


def _clean_name(value, fallback):
    name = str(value or "").strip() or fallback
    if len(name) > 80:
        raise ValueError("名前は80文字以内にしてください。")
    return name


def _renumber(manager):
    for index, row in enumerate(list(manager.order_by("position", "id")), start=1):
        if row.position != index:
            row.position = index
            row.save(update_fields=["position"])


def _team(tournament, team_id):
    try:
        key = int(team_id)
    except (TypeError, ValueError):
        raise ValueError("チームが見つかりません。")
    team = (
        TournamentTeam.objects.select_for_update()
        .filter(tournament=tournament, id=key)
        .first()
    )
    if team is None:
        raise ValueError("チームが見つかりません。")
    return team


def _bracket(tournament, bracket_id):
    try:
        key = int(bracket_id)
    except (TypeError, ValueError):
        raise ValueError("ブラケットが見つかりません。")
    bracket = (
        TeamBracket.objects.select_for_update()
        .filter(tournament=tournament, id=key)
        .first()
    )
    if bracket is None:
        raise ValueError("ブラケットが見つかりません。")
    return bracket


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


def _team_ids(values):
    if isinstance(values, (str, int)):
        values = [values]
    if not isinstance(values, (list, tuple)):
        raise ValueError("チームを選んでください。")
    ids = []
    seen = set()
    for value in values:
        try:
            key = int(value)
        except (TypeError, ValueError):
            raise ValueError("チームが見つかりません。")
        if key in seen:
            continue
        seen.add(key)
        ids.append(key)
    if not ids:
        raise ValueError("チームを選んでください。")
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


def _clear_reservation_record(reservation_id):
    MemberRecord.objects.filter(
        reservation_id=reservation_id,
        kind=MemberRecord.Kind.TEAM_TOURNAMENT,
    ).delete()


def _locked(bracket):
    entries, matches = _load(bracket)
    _rounds, _champion, _complete, locked = _tree_view(entries, matches)
    return locked


def _materialize(bracket):
    entries, matches = _load(bracket)
    teams = [entry.team for entry in entries]
    structure = _sources(len(teams))
    existing = {(match.round, match.position): match for match in matches}
    winners = {}
    keep = []

    def take(spec, first, second):
        key = (spec["round"], spec["position"])
        match = existing.get(key)
        first_id = first.id
        second_id = second.id if second is not None else None
        if (
            match is not None
            and match.first_id == first_id
            and match.second_id == second_id
        ):
            if second is None and match.winner_id != first_id:
                match.winner = first
                match.note = ""
                match.save(update_fields=["winner", "note"])
            elif match.winner_id and match.winner_id not in {first_id, second_id}:
                match.winner = None
                match.note = ""
                match.save(update_fields=["winner", "note"])
            if match.winner_id == first_id:
                winners[key] = first
            elif second is not None and match.winner_id == second_id:
                winners[key] = second
            keep.append(match.id)
            return
        if match is not None:
            match.delete()
            existing.pop(key, None)
        created = TeamBracketMatch.objects.create(
            bracket=bracket,
            round=spec["round"],
            position=spec["position"],
            first=first,
            second=second,
            winner=first if second is None else None,
        )
        if created.winner_id:
            winners[key] = first
        keep.append(created.id)
        existing[key] = created

    for specs in structure:
        for spec in specs:
            bye = spec["right"] is None
            left = _lookup(spec["left"], teams, winners)
            right = None if bye else _lookup(spec["right"], teams, winners)
            key = (spec["round"], spec["position"])
            if bye:
                if left is None:
                    match = existing.get(key)
                    if match is not None:
                        match.delete()
                        existing.pop(key, None)
                    continue
                take(spec, left, None)
                continue
            if left is None or right is None:
                match = existing.get(key)
                if match is not None:
                    match.delete()
                    existing.pop(key, None)
                continue
            take(spec, left, right)
    if keep:
        bracket.matches.exclude(id__in=keep).delete()
    else:
        bracket.matches.all().delete()


def _record_name(reservation, bracket):
    title = (reservation.event.title or "").strip()
    return f"{title} {bracket.name}".strip()[:200] or "記録"


def _write_team(team, matches, champion_id, entry_count, bracket):
    members = list(team.members.select_related("reservation", "reservation__event"))
    if not members:
        return
    mine = [
        match
        for match in matches
        if match.winner_id and (match.first_id == team.id or match.second_id == team.id)
    ]
    for member in members:
        if not mine:
            _clear_reservation_record(member.reservation_id)
            continue
        latest = max(mine, key=lambda match: (match.round, match.position, match.id))
        label = _record_label(team, latest, champion_id, entry_count)
        _sync_member_record(
            member.reservation,
            label[:80],
            MemberRecord.Kind.TEAM_TOURNAMENT,
            [{"label": "チーム", "value": team.name}],
            record_name=_record_name(member.reservation, bracket),
        )


def _refresh_all(bracket):
    entries, matches = _load(bracket)
    _rounds, champion, _complete, _locked_now = _tree_view(entries, matches)
    champion_id = champion.id if champion is not None else None
    for entry in entries:
        _write_team(entry.team, matches, champion_id, len(entries), bracket)


def _entry_for(team):
    return (
        TeamBracketEntry.objects.select_related("bracket")
        .filter(team=team)
        .first()
    )


@transaction.atomic
def create_team(tournament, name):
    count = tournament.teams.count() + 1
    TournamentTeam.objects.create(
        tournament=tournament,
        name=_clean_name(name, f"チーム{count}"),
        position=_next_position(tournament.teams),
    )


@transaction.atomic
def rename_team(tournament, team_id, name):
    team = _team(tournament, team_id)
    team.name = _clean_name(name, team.name)
    team.save(update_fields=["name"])
    entry = _entry_for(team)
    if entry is not None:
        _refresh_all(entry.bracket)


@transaction.atomic
def remove_team(tournament, team_id):
    team = _team(tournament, team_id)
    entry = _entry_for(team)
    if entry is not None and _locked(entry.bracket):
        raise ValueError("勝敗が決まっているので、このチームは削除できません。")
    reservation_ids = list(team.members.values_list("reservation_id", flat=True))
    bracket = entry.bracket if entry is not None else None
    team.delete()
    _renumber(tournament.teams)
    if reservation_ids:
        MemberRecord.objects.filter(
            reservation_id__in=reservation_ids,
            kind=MemberRecord.Kind.TEAM_TOURNAMENT,
        ).delete()
    if bracket is not None:
        _materialize(bracket)
        _refresh_all(bracket)


@transaction.atomic
def add_member(tournament, team_id, reservation_id):
    team = _team(tournament, team_id)
    reservation = _paid(tournament.event, reservation_id)
    if TournamentTeamMember.objects.filter(team=team, reservation=reservation).exists():
        raise ValueError("この人はすでにこのチームに入っています。")
    if TournamentTeamMember.objects.filter(
        reservation=reservation,
        team__tournament=tournament,
    ).exists():
        raise ValueError("この人はすでに別のチームに入っています。")
    try:
        TournamentTeamMember.objects.create(
            team=team,
            reservation=reservation,
            position=_next_position(team.members),
        )
    except IntegrityError:
        raise ValueError("この人はすでにチームに入っています。")
    entry = _entry_for(team)
    if entry is not None:
        _refresh_all(entry.bracket)


@transaction.atomic
def add_members(tournament, team_id, reservation_ids):
    team = _team(tournament, team_id)
    ids = _reservation_ids(reservation_ids)
    already = set(
        TournamentTeamMember.objects.filter(team=team, reservation_id__in=ids).values_list(
            "reservation_id",
            flat=True,
        )
    )
    reservations = []
    for reservation_id in ids:
        if reservation_id in already:
            continue
        reservation = _paid_named(tournament.event, reservation_id)
        if TournamentTeamMember.objects.filter(
            reservation=reservation,
            team__tournament=tournament,
        ).exists():
            raise ValueError(f"{_person_label(reservation)}はすでに別のチームに入っています。")
        reservations.append(reservation)
    if not reservations:
        return
    position = _next_position(team.members)
    for reservation in reservations:
        try:
            TournamentTeamMember.objects.create(
                team=team,
                reservation=reservation,
                position=position,
            )
        except IntegrityError:
            raise ValueError(f"{_person_label(reservation)}はすでにチームに入っています。")
        position += 1
    entry = _entry_for(team)
    if entry is not None:
        _refresh_all(entry.bracket)


@transaction.atomic
def remove_member(tournament, team_id, reservation_id):
    team = _team(tournament, team_id)
    try:
        key = int(reservation_id)
    except (TypeError, ValueError):
        raise ValueError("参加者が見つかりません。")
    member = (
        TournamentTeamMember.objects.select_for_update()
        .filter(team=team, reservation_id=key)
        .first()
    )
    if member is None:
        raise ValueError("参加者が見つかりません。")
    member.delete()
    _renumber(team.members)
    _clear_reservation_record(key)
    entry = _entry_for(team)
    if entry is not None:
        _refresh_all(entry.bracket)


@transaction.atomic
def create_bracket(tournament, name):
    count = tournament.brackets.count() + 1
    TeamBracket.objects.create(
        tournament=tournament,
        name=_clean_name(name, f"ブラケット{count}"),
        position=_next_position(tournament.brackets),
    )


@transaction.atomic
def rename_bracket(tournament, bracket_id, name):
    bracket = _bracket(tournament, bracket_id)
    bracket.name = _clean_name(name, bracket.name)
    bracket.save(update_fields=["name"])
    _refresh_all(bracket)


@transaction.atomic
def remove_bracket(tournament, bracket_id):
    bracket = _bracket(tournament, bracket_id)
    if _locked(bracket):
        raise ValueError("勝敗が決まっているので、このブラケットは削除できません。")
    reservation_ids = list(
        TournamentTeamMember.objects.filter(team__bracket_entry__bracket=bracket).values_list(
            "reservation_id",
            flat=True,
        )
    )
    bracket.delete()
    if reservation_ids:
        MemberRecord.objects.filter(
            reservation_id__in=reservation_ids,
            kind=MemberRecord.Kind.TEAM_TOURNAMENT,
        ).delete()


@transaction.atomic
def enter_bracket(tournament, bracket_id, team_id):
    bracket = _bracket(tournament, bracket_id)
    if _locked(bracket):
        raise ValueError("勝敗が決まっているので、チームの入れ替えはできません。")
    team = _team(tournament, team_id)
    if not team.members.exists():
        raise ValueError("チームに人を入れてからブラケットに入れてください。")
    if TeamBracketEntry.objects.filter(team=team).exists():
        raise ValueError("このチームはすでにブラケットに入っています。")
    TeamBracketEntry.objects.create(
        bracket=bracket,
        team=team,
        position=_next_position(bracket.entries),
    )
    _materialize(bracket)
    _refresh_all(bracket)


@transaction.atomic
def enter_many(tournament, bracket_id, team_ids):
    bracket = _bracket(tournament, bracket_id)
    if _locked(bracket):
        raise ValueError("勝敗が決まっているので、チームの入れ替えはできません。")
    ids = _team_ids(team_ids)
    already = set(
        TeamBracketEntry.objects.filter(bracket=bracket, team_id__in=ids).values_list(
            "team_id",
            flat=True,
        )
    )
    teams = []
    for team_id in ids:
        if team_id in already:
            continue
        team = _team(tournament, team_id)
        label = str(team.name or "").strip() or "このチーム"
        if not team.members.exists():
            raise ValueError(f"{label}に人を入れてからブラケットに入れてください。")
        if TeamBracketEntry.objects.filter(team=team).exists():
            raise ValueError(f"{label}はすでにブラケットに入っています。")
        teams.append(team)
    if not teams:
        return
    position = _next_position(bracket.entries)
    for team in teams:
        label = str(team.name or "").strip() or "このチーム"
        try:
            TeamBracketEntry.objects.create(
                bracket=bracket,
                team=team,
                position=position,
            )
        except IntegrityError:
            raise ValueError(f"{label}はすでにブラケットに入っています。")
        position += 1
    _materialize(bracket)
    _refresh_all(bracket)


@transaction.atomic
def leave_bracket(tournament, bracket_id, team_id):
    bracket = _bracket(tournament, bracket_id)
    team = _team(tournament, team_id)
    entry = (
        TeamBracketEntry.objects.select_for_update()
        .filter(bracket=bracket, team=team)
        .first()
    )
    if entry is None:
        raise ValueError("チームが見つかりません。")
    if _locked(bracket):
        raise ValueError("勝敗が決まっているので、チームの入れ替えはできません。")
    reservation_ids = list(team.members.values_list("reservation_id", flat=True))
    entry.delete()
    if reservation_ids:
        MemberRecord.objects.filter(
            reservation_id__in=reservation_ids,
            kind=MemberRecord.Kind.TEAM_TOURNAMENT,
        ).delete()
    _materialize(bracket)
    _refresh_all(bracket)


@transaction.atomic
def move_team(tournament, bracket_id, team_id, direction):
    way = str(direction or "").strip()
    if way not in {"up", "down"}:
        raise ValueError("上か下を指定してください。")
    bracket = _bracket(tournament, bracket_id)
    if _locked(bracket):
        raise ValueError("勝敗が決まっているので、順番は変えられません。")
    team = _team(tournament, team_id)
    entries = list(bracket.entries.select_related("team").order_by("position", "id"))
    index = next((i for i, entry in enumerate(entries) if entry.team_id == team.id), None)
    if index is None:
        raise ValueError("チームが見つかりません。")
    other = index - 1 if way == "up" else index + 1
    if other < 0 or other >= len(entries):
        return
    entries[index], entries[other] = entries[other], entries[index]
    for position, entry in enumerate(entries, start=1):
        if entry.position != position:
            entry.position = position
            entry.save(update_fields=["position"])
    _materialize(bracket)
    _refresh_all(bracket)


def _optional_int(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _changes_winner(match, winner_value):
    raw = "" if winner_value is None else str(winner_value).strip()
    if raw in {"", "clear"}:
        return match.winner_id is not None
    try:
        winner_id = int(raw)
    except (TypeError, ValueError):
        return True
    return winner_id != match.winner_id


def _match_result_locked(bracket, match):
    entries, matches = _load(bracket)
    rounds, _champion, _complete, _locked_now = _tree_view(entries, matches)
    for rnd in rounds:
        for row in rnd["matches"]:
            if row["round"] == match.round and row["position"] == match.position:
                return bool(row["result_locked"])
    return False


def _apply_winner(match, winner_value, note):
    raw = "" if winner_value is None else str(winner_value).strip()
    if raw in {"", "clear"}:
        match.winner = None
        match.note = ""
        match.save(update_fields=["winner", "note"])
        return
    try:
        winner_id = int(raw)
    except (TypeError, ValueError):
        raise ValueError("勝ったチームはこの試合の2チームから選んでください。")
    if winner_id not in {match.first_id, match.second_id}:
        raise ValueError("勝ったチームはこの試合の2チームから選んでください。")
    if note is None:
        text = "" if match.winner_id != winner_id else (match.note or "")
    else:
        text = str(note).strip()
        if len(text) > 200:
            raise ValueError("理由は200文字以内にしてください。")
    match.winner_id = winner_id
    match.note = text
    match.save(update_fields=["winner", "note"])


@transaction.atomic
def set_winner(tournament, match_id, winner_value, note=None, bracket_id=None, round_no=None, position=None):
    bracket = None
    if bracket_id not in (None, ""):
        bracket = _bracket(tournament, bracket_id)
    match_key = _optional_int(match_id)
    current = None
    if bracket is None and match_key is not None:
        current = (
            TeamBracketMatch.objects.select_related("bracket")
            .filter(bracket__tournament=tournament, id=match_key)
            .first()
        )
        if current is not None:
            bracket = (
                TeamBracket.objects.select_for_update()
                .filter(id=current.bracket_id, tournament=tournament)
                .first()
            )
    if bracket is None:
        raise ValueError("ブラケットが見つかりません。")
    _materialize(bracket)
    found = None
    if match_key is not None:
        found = (
            TeamBracketMatch.objects.select_for_update()
            .select_related("first", "second", "winner")
            .filter(bracket=bracket, id=match_key)
            .first()
        )
    if found is None:
        rnd = _optional_int(round_no)
        pos = _optional_int(position)
        if rnd is None or pos is None:
            raise ValueError("試合が見つかりません。")
        found = (
            TeamBracketMatch.objects.select_for_update()
            .select_related("first", "second", "winner")
            .filter(bracket=bracket, round=rnd, position=pos)
            .first()
        )
    if found is None:
        raise ValueError("対戦する2チームが揃ってから決められます。")
    if found.second_id is None:
        raise ValueError("不戦勝の相手は選べません。")
    if _changes_winner(found, winner_value) and _match_result_locked(bracket, found):
        raise ValueError("この勝敗は次の試合に使われているので、先に次の試合の勝敗を取り消してください。")
    _apply_winner(found, winner_value, note)
    _materialize(bracket)
    _refresh_all(bracket)


@transaction.atomic
def set_note(tournament, match_id, note):
    try:
        key = int(match_id)
    except (TypeError, ValueError):
        raise ValueError("試合が見つかりません。")
    match = (
        TeamBracketMatch.objects.select_for_update()
        .select_related("bracket")
        .filter(bracket__tournament=tournament, id=key)
        .first()
    )
    if match is None:
        raise ValueError("試合が見つかりません。")
    if match.winner_id is None or match.second_id is None:
        raise ValueError("勝ちが決まってから理由を書けます。")
    text = str(note or "").strip()
    if len(text) > 200:
        raise ValueError("理由は200文字以内にしてください。")
    match.note = text
    match.save(update_fields=["note"])
    _refresh_all(match.bracket)


def sync_team_tournament_from_record(record):
    if not record.reservation_id:
        return
    membership = (
        TournamentTeamMember.objects.filter(reservation_id=record.reservation_id)
        .select_related("team__tournament")
        .first()
    )
    if membership is None:
        return
    match = (
        TeamBracketMatch.objects.filter(
            Q(first_id=membership.team_id) | Q(second_id=membership.team_id)
        )
        .select_related("bracket__tournament")
        .order_by("-round", "-id")
        .first()
    )
    if match is None or match.second_id is None:
        return
    text = (record.result or "").strip()
    if "引き分け" in text:
        raise ValueError("トーナメントに引き分けはありません。")
    if "不戦勝" in text:
        return
    lost = (
        text.startswith("準優勝")
        or text.startswith("３位")
        or text.startswith("3位")
        or "負け" in text
    )
    won = (
        not lost
        and (
            text.startswith("優勝")
            or "勝ち" in text
            or text in {"決勝", "準決勝", "準々決勝"}
            or text.endswith("回戦")
        )
    )
    if won:
        set_winner(
            match.bracket.tournament,
            match.id,
            membership.team_id,
            None,
            match.bracket_id,
            match.round,
            match.position,
        )
        return
    if lost:
        other = match.second_id if match.first_id == membership.team_id else match.first_id
        if other is None:
            return
        set_winner(
            match.bracket.tournament,
            match.id,
            other,
            None,
            match.bracket_id,
            match.round,
            match.position,
        )
