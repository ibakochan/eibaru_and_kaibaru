import random

from django.db import transaction
from django.db.models import Q

from .models import Bracket, BracketEntry, EventReservation, MemberRecord, Tournament, TournamentMatch
from .service_member_record import (
    _is_owner,
    _paid_rows,
    _sync_member_record,
    _visible_paid_rows,
    test_payload,
)


def event_tournament(event):
    try:
        return event.tournament
    except Tournament.DoesNotExist:
        return None


def _person(reservation):
    return {
        "id": reservation.id,
        "full_name": reservation.full_name,
        "member": reservation.member_id,
    }


def _load(bracket):
    entries = list(
        bracket.entries.select_related("reservation").order_by("position", "id")
    )
    matches = list(
        bracket.matches.select_related("first", "second", "winner").order_by(
            "round", "position", "id"
        )
    )
    return entries, matches


def _people_index(entries, matches):
    people = {entry.reservation_id: entry.reservation for entry in entries}
    for match in matches:
        people[match.first_id] = match.first
        if match.second_id and match.second:
            people[match.second_id] = match.second
        if match.winner_id and match.winner:
            people[match.winner_id] = match.winner
    return people


def _candidates(entries, matches, people, round_no):
    if round_no <= 1:
        return [entry.reservation for entry in entries]
    ids = [
        match.winner_id
        for match in matches
        if match.round == round_no - 1 and match.winner_id
    ]
    return [people[item] for item in ids if item in people]


def _round_view(entries, matches):
    people = _people_index(entries, matches)
    round_no = 1
    while round_no < 40:
        group = _candidates(entries, matches, people, round_no)
        rows = [match for match in matches if match.round == round_no]
        placed = set()
        for match in rows:
            placed.add(match.first_id)
            if match.second_id:
                placed.add(match.second_id)
        unplaced = [person for person in group if person.id not in placed]
        unfinished = [match for match in rows if match.winner_id is None]
        if not rows:
            if len(group) <= 1:
                played = bool(matches)
                return {
                    "round": max(round_no - 1, 1),
                    "pool": [],
                    "complete": played and len(group) == 1,
                    "champion": group[0] if played and len(group) == 1 else None,
                    "round_size": len(group),
                }
            return {
                "round": round_no,
                "pool": group,
                "complete": False,
                "champion": None,
                "round_size": len(group),
            }
        if unplaced or unfinished:
            return {
                "round": round_no,
                "pool": unplaced,
                "complete": False,
                "champion": None,
                "round_size": len(group),
            }
        round_no += 1
    raise ValueError("ラウンドが多すぎます。")


def _match_label(match, matches):
    ids = set()
    real = False
    for row in matches:
        if row.round != match.round:
            continue
        ids.add(row.first_id)
        if row.second_id:
            ids.add(row.second_id)
            real = True
    if real and len(ids) == 2:
        return "決勝"
    return f"{match.round}回戦"


def _match_payload(match, matches):
    return {
        "id": match.id,
        "round": match.round,
        "position": match.position,
        "label": _match_label(match, matches),
        "first": _person(match.first),
        "second": _person(match.second) if match.second_id else None,
        "winner_id": match.winner_id,
        "bye": match.second_id is None,
        "note": match.note,
    }


def _visible_brackets(tournament, user):
    brackets = list(tournament.brackets.all())
    if tournament.results_public or _is_owner(tournament.event, user):
        return brackets
    visible_ids = set(
        _visible_paid_rows(tournament.event, False, user).values_list("id", flat=True)
    )
    if not visible_ids:
        return []
    entered = set(
        BracketEntry.objects.filter(
            bracket__tournament=tournament,
            reservation_id__in=visible_ids,
        ).values_list("bracket_id", flat=True)
    )
    return [bracket for bracket in brackets if bracket.id in entered]


def tournament_payload(tournament, user):
    payload = test_payload(tournament)
    owner = _is_owner(tournament.event, user)
    entered_ids = set(
        BracketEntry.objects.filter(bracket__tournament=tournament).values_list(
            "reservation_id",
            flat=True,
        )
    )
    payload["pool"] = [
        _person(row)
        for row in _paid_rows(tournament.event).exclude(id__in=entered_ids).order_by(
            "full_name", "id"
        )
    ] if owner else []
    brackets = []
    for bracket in _visible_brackets(tournament, user):
        entries, matches = _load(bracket)
        state = _round_view(entries, matches)
        label = "決勝" if state["round_size"] == 2 else f"{state['round']}回戦"
        brackets.append({
            "id": bracket.id,
            "name": bracket.name,
            "position": bracket.position,
            "started": bool(matches),
            "complete": state["complete"],
            "champion": _person(state["champion"]) if state["champion"] else None,
            "round": state["round"],
            "round_label": label,
            "members": [_person(entry.reservation) for entry in entries],
            "pool": [_person(person) for person in state["pool"]],
            "matches": [_match_payload(match, matches) for match in matches],
        })
    payload["brackets"] = brackets
    return payload


def _bracket(tournament, bracket_id):
    try:
        bracket_key = int(bracket_id)
    except (TypeError, ValueError):
        raise ValueError("ブラケットが見つかりません。")
    bracket = (
        Bracket.objects.select_for_update()
        .filter(tournament=tournament, id=bracket_key)
        .first()
    )
    if bracket is None:
        raise ValueError("ブラケットが見つかりません。")
    return bracket


def _clean_name(value, fallback):
    name = str(value or "").strip() or fallback
    if len(name) > 80:
        raise ValueError("ブラケット名は80文字以内にしてください。")
    return name


def _paid(event, reservation_id):
    try:
        key = int(reservation_id)
    except (TypeError, ValueError):
        raise ValueError("支払い済みの予約だけ入れられます。")
    row = (
        EventReservation.objects.select_for_update()
        .filter(id=key)
        .first()
    )
    if (
        row is None
        or row.event_id != event.id
        or row.is_deleted
        or row.canceled
        or row.status != EventReservation.Status.PAID
    ):
        raise ValueError("支払い済みの予約だけ入れられます。")
    return row


def _next_position(manager):
    current = manager.order_by("-position").values_list("position", flat=True).first()
    return (current or 0) + 1


@transaction.atomic
def create_bracket(tournament, name):
    position = _next_position(tournament.brackets)
    count = tournament.brackets.count() + 1
    Bracket.objects.create(
        tournament=tournament,
        name=_clean_name(name, f"ブラケット{count}"),
        position=position,
    )


@transaction.atomic
def rename_bracket(tournament, bracket_id, name):
    bracket = _bracket(tournament, bracket_id)
    bracket.name = _clean_name(name, bracket.name)
    bracket.save(update_fields=["name"])


@transaction.atomic
def remove_bracket(tournament, bracket_id):
    bracket = _bracket(tournament, bracket_id)
    if bracket.matches.exists():
        raise ValueError("試合があるブラケットは削除できません。")
    bracket.delete()


@transaction.atomic
def enter_bracket(tournament, bracket_id, reservation_id):
    bracket = _bracket(tournament, bracket_id)
    if bracket.matches.exists():
        raise ValueError("このブラケットは始まっているので、人の入れ替えはできません。")
    reservation = _paid(tournament.event, reservation_id)
    if BracketEntry.objects.filter(reservation=reservation).exists():
        raise ValueError("この人はすでにブラケットに入っています。")
    BracketEntry.objects.create(
        bracket=bracket,
        reservation=reservation,
        position=_next_position(bracket.entries),
    )


@transaction.atomic
def leave_bracket(tournament, reservation_id):
    try:
        key = int(reservation_id)
    except (TypeError, ValueError):
        raise ValueError("参加者が見つかりません。")
    entry = (
        BracketEntry.objects.select_for_update()
        .select_related("bracket")
        .filter(bracket__tournament=tournament, reservation_id=key)
        .first()
    )
    if entry is None:
        raise ValueError("参加者が見つかりません。")
    if entry.bracket.matches.exists():
        raise ValueError("このブラケットは始まっているので、人の入れ替えはできません。")
    entry.delete()


def _open_state(bracket):
    entries, matches = _load(bracket)
    return entries, matches, _round_view(entries, matches)


def _pool_ids(state):
    return {person.id for person in state["pool"]}


@transaction.atomic
def pair_bracket(tournament, bracket_id, first_id, second_id):
    bracket = _bracket(tournament, bracket_id)
    try:
        first = int(first_id)
        second = int(second_id)
    except (TypeError, ValueError):
        raise ValueError("2人を選んでください。")
    if first == second:
        raise ValueError("同じ人同士では対戦できません。")
    _paid(tournament.event, first)
    _paid(tournament.event, second)
    _entries, matches, state = _open_state(bracket)
    pool = _pool_ids(state)
    if first not in pool or second not in pool:
        raise ValueError("このラウンドに残っている人だけ対戦できます。")
    TournamentMatch.objects.create(
        bracket=bracket,
        round=state["round"],
        position=_next_position(bracket.matches.filter(round=state["round"])),
        first_id=first,
        second_id=second,
    )


@transaction.atomic
def pair_bracket_rest(tournament, bracket_id):
    bracket = _bracket(tournament, bracket_id)
    _entries, _matches, state = _open_state(bracket)
    people = list(state["pool"])
    if len(people) % 2 != 0:
        raise ValueError(
            "ランダムに組むには人数を偶数にしてください。1人を不戦勝で進めるか、始まる前なら1人入れ替えるか外してください。"
        )
    if len(people) < 2:
        raise ValueError("対戦できる人が2人未満です。")
    for person in people:
        _paid(tournament.event, person.id)
    random.shuffle(people)
    position = _next_position(bracket.matches.filter(round=state["round"])) - 1
    for index in range(0, len(people), 2):
        position += 1
        TournamentMatch.objects.create(
            bracket=bracket,
            round=state["round"],
            position=position,
            first=people[index],
            second=people[index + 1],
        )


@transaction.atomic
def bye_bracket(tournament, bracket_id, reservation_id):
    bracket = _bracket(tournament, bracket_id)
    reservation = _paid(tournament.event, reservation_id)
    entries, matches, state = _open_state(bracket)
    pool = state["pool"]
    if reservation.id not in _pool_ids(state):
        raise ValueError("このラウンドに残っている人だけ不戦勝にできます。")
    if len(pool) % 2 == 0 or (len(pool) == 1 and not matches):
        raise ValueError("人数が奇数のときだけ、1人を不戦勝で進められます。")
    match = TournamentMatch.objects.create(
        bracket=bracket,
        round=state["round"],
        position=_next_position(bracket.matches.filter(round=state["round"])),
        first=reservation,
        second=None,
        winner=reservation,
    )
    _refresh_bracket_records(bracket, focus=match)


def _latest_round(matches):
    return max((match.round for match in matches), default=0)


def _match(tournament, match_id):
    try:
        key = int(match_id)
    except (TypeError, ValueError):
        raise ValueError("試合が見つかりません。")
    match = (
        TournamentMatch.objects.select_for_update()
        .select_related("bracket", "first", "second", "winner")
        .filter(bracket__tournament=tournament, id=key)
        .first()
    )
    if match is None:
        raise ValueError("試合が見つかりません。")
    return match


def _require_latest(match):
    latest = _latest_round(match.bracket.matches.all())
    if match.round != latest:
        raise ValueError("次のラウンドが進んでいるので変更できません。")


@transaction.atomic
def unpair_tournament(tournament, match_id):
    match = _match(tournament, match_id)
    _require_latest(match)
    bracket = match.bracket
    first_id = match.first_id
    second_id = match.second_id
    match.delete()
    _refresh_people(bracket, [first_id, second_id])


@transaction.atomic
def set_tournament_winner(tournament, match_id, winner_value):
    match = _match(tournament, match_id)
    if match.second_id is None:
        raise ValueError("不戦勝の相手は選べません。")
    _require_latest(match)
    raw = "" if winner_value is None else str(winner_value).strip()
    if raw == "" or raw == "clear":
        match.winner = None
        match.note = ""
        match.save(update_fields=["winner", "note"])
        _refresh_people(match.bracket, [match.first_id, match.second_id])
        return
    if raw == "draw":
        raise ValueError("トーナメントに引き分けはありません。")
    try:
        winner_id = int(raw)
    except (TypeError, ValueError):
        raise ValueError("勝った人はこの試合の2人から選んでください。")
    if winner_id not in {match.first_id, match.second_id}:
        raise ValueError("勝った人はこの試合の2人から選んでください。")
    match.winner_id = winner_id
    match.save(update_fields=["winner"])
    _refresh_people(match.bracket, [match.first_id, match.second_id])


@transaction.atomic
def set_tournament_note(tournament, match_id, note):
    match = _match(tournament, match_id)
    if match.winner_id is None or match.second_id is None:
        raise ValueError("勝ちが決まってから理由を書けます。")
    _require_latest(match)
    text = str(note or "").strip()
    if len(text) > 200:
        raise ValueError("理由は200文字以内にしてください。")
    match.note = text
    match.save(update_fields=["note"])
    _refresh_people(match.bracket, [match.first_id, match.second_id])


def _refresh_people(bracket, reservation_ids):
    entries, matches = _load(bracket)
    state = _round_view(entries, matches)
    people = _people_index(entries, matches)
    for reservation_id in reservation_ids:
        if not reservation_id:
            continue
        reservation = people.get(reservation_id)
        if reservation is not None:
            _write_person(reservation, matches, state)


def _refresh_bracket_records(bracket, focus=None):
    ids = []
    if focus is not None:
        ids.extend([focus.first_id, focus.second_id])
    _refresh_people(bracket, ids)


def _write_person(reservation, matches, state):
    mine = [
        match
        for match in matches
        if match.winner_id
        and (match.first_id == reservation.id or match.second_id == reservation.id)
    ]
    if not mine:
        MemberRecord.objects.filter(reservation=reservation).delete()
        return
    latest = max(mine, key=lambda match: (match.round, match.position, match.id))
    final = _match_label(latest, matches) == "決勝"
    prefix = "決勝" if final else f"{latest.round}回戦"
    champion = state["champion"] is not None and state["champion"].id == reservation.id
    if latest.second_id is None:
        label = "優勝" if champion else f"{prefix} 不戦勝"
    else:
        opponent = latest.second if latest.first_id == reservation.id else latest.first
        if champion and latest.winner_id == reservation.id:
            label = f"優勝（{opponent.full_name}）"
        elif latest.winner_id == reservation.id:
            label = f"{prefix} 勝ち（{opponent.full_name}）"
        else:
            label = f"{prefix} 負け（{opponent.full_name}）"
    note = (latest.note or "").strip()
    details = [{"label": "勝因", "value": note}] if note and latest.second_id else []
    _sync_member_record(
        reservation,
        label[:80],
        MemberRecord.Kind.INDIVIDUAL_TOURNAMENT,
        details,
    )


def sync_tournament_from_record(record):
    if not record.reservation_id:
        return
    match = (
        TournamentMatch.objects.filter(
            Q(first_id=record.reservation_id) | Q(second_id=record.reservation_id)
        )
        .select_related("bracket__tournament")
        .order_by("-round", "-id")
        .first()
    )
    if match is None:
        return
    text = (record.result or "").strip()
    if "引き分け" in text:
        raise ValueError("トーナメントに引き分けはありません。")
    if "不戦勝" in text:
        return
    if text.startswith("優勝") or "勝ち" in text:
        set_tournament_winner(match.bracket.tournament, match.id, record.reservation_id)
        return
    if "負け" in text:
        other = (
            match.second_id
            if match.first_id == record.reservation_id
            else match.first_id
        )
        if other is None:
            return
        set_tournament_winner(match.bracket.tournament, match.id, other)
        return
    if text == "":
        set_tournament_winner(match.bracket.tournament, match.id, "")
        return
    raise ValueError("トーナメントの結果は「勝ち」か「負け」で入力してください。")
