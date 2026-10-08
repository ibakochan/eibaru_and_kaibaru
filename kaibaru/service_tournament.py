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
    if reservation is None:
        return None
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


def _sources(count):
    """Pair people from the top. A leftover person byes and advances."""
    if count < 2:
        return []
    rounds = []
    current = [("player", index) for index in range(count)]
    round_no = 1
    while len(current) > 1:
        specs = []
        nxt = []
        row = list(current)
        bye = row.pop() if len(row) % 2 == 1 else None
        position = 1
        for index in range(0, len(row), 2):
            specs.append({
                "round": round_no,
                "position": position,
                "left": row[index],
                "right": row[index + 1],
            })
            nxt.append(("winner", round_no, position))
            position += 1
        if bye is not None:
            specs.append({
                "round": round_no,
                "position": position,
                "left": bye,
                "right": None,
            })
            nxt.append(bye)
        rounds.append(specs)
        current = nxt
        round_no += 1
        if round_no > 12:
            break
    return rounds


def _lookup(source, people, winners):
    if source is None:
        return None
    if source[0] == "player":
        index = source[1]
        if index >= len(people):
            return None
        return people[index]
    return winners.get((source[1], source[2]))


def _round_title(index, total):
    remaining = total - index
    if remaining == 1:
        return "決勝"
    if remaining == 2 and total >= 3:
        return "準決勝"
    if remaining == 3 and total >= 4:
        return "準々決勝"
    return f"{index + 1}回戦"


def _tree_view(entries, matches):
    people = [entry.reservation for entry in entries]
    structure = _sources(len(people))
    by_key = {(match.round, match.position): match for match in matches}
    winners = {}
    rounds = []
    total = len(structure)
    for index, specs in enumerate(structure):
        label = _round_title(index, total)
        rows = []
        for spec in specs:
            bye = spec["right"] is None
            left = _lookup(spec["left"], people, winners)
            right = None if bye else _lookup(spec["right"], people, winners)
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
                "first": _person(left),
                "second": None if bye else _person(right),
                "winner_id": winner.id if winner is not None else None,
                "bye": bye,
                "pending": pending,
                "note": note,
            })
        rounds.append({"label": label, "matches": rows})
    champion = None
    complete = False
    if structure:
        last = structure[-1][0]
        champ = winners.get((last["round"], last["position"]))
        if champ is not None and last["right"] is not None:
            champion = champ
            complete = True
    locked = any(row["winner_id"] and not row["bye"] for rnd in rounds for row in rnd["matches"])
    return rounds, champion, complete, locked


def _open_label(rounds, complete):
    if complete:
        return "終了"
    for rnd in rounds:
        for match in rnd["matches"]:
            if match["bye"]:
                continue
            if match["pending"] or not match["winner_id"]:
                return rnd["label"]
    if rounds:
        return rounds[-1]["label"]
    return "1回戦"


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
        rounds, champion, complete, locked = _tree_view(entries, matches)
        label = _open_label(rounds, complete)
        flat = [match for rnd in rounds for match in rnd["matches"]]
        brackets.append({
            "id": bracket.id,
            "name": bracket.name,
            "position": bracket.position,
            "started": locked,
            "complete": complete,
            "champion": _person(champion),
            "round": 1,
            "round_label": label,
            "members": [_person(entry.reservation) for entry in entries],
            "pool": [],
            "matches": flat,
            "rounds": rounds,
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


def _locked(bracket):
    entries, matches = _load(bracket)
    _rounds, _champion, _complete, locked = _tree_view(entries, matches)
    return locked


def _materialize(bracket):
    entries, matches = _load(bracket)
    people = [entry.reservation for entry in entries]
    structure = _sources(len(people))
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
        created = TournamentMatch.objects.create(
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
            left = _lookup(spec["left"], people, winners)
            right = None if bye else _lookup(spec["right"], people, winners)
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


def _label_for(match, entry_count):
    total = len(_sources(entry_count))
    if total < 1:
        return f"{match.round}回戦"
    return _round_title(match.round - 1, total)


def _refresh_all(bracket):
    entries, matches = _load(bracket)
    _rounds, champion, _complete, _locked_now = _tree_view(entries, matches)
    champion_id = champion.id if champion is not None else None
    for entry in entries:
        _write_person(entry.reservation, matches, champion_id, len(entries))


def _write_person(reservation, matches, champion_id, entry_count):
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
    title = _label_for(latest, entry_count)
    champion = champion_id == reservation.id
    if latest.second_id is None:
        label = "優勝" if champion else f"{title} 不戦勝"
    else:
        opponent = latest.second if latest.first_id == reservation.id else latest.first
        if champion and latest.winner_id == reservation.id:
            label = f"優勝（{opponent.full_name}）"
        elif latest.winner_id == reservation.id:
            label = f"{title} 勝ち（{opponent.full_name}）"
        else:
            label = f"{title} 負け（{opponent.full_name}）"
    note = (latest.note or "").strip()
    details = [{"label": "勝因", "value": note}] if note and latest.second_id else []
    _sync_member_record(
        reservation,
        label[:80],
        MemberRecord.Kind.INDIVIDUAL_TOURNAMENT,
        details,
    )


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
    if _locked(bracket):
        raise ValueError("勝敗が決まっているので、このブラケットは削除できません。")
    reservation_ids = list(bracket.entries.values_list("reservation_id", flat=True))
    bracket.delete()
    if reservation_ids:
        MemberRecord.objects.filter(reservation_id__in=reservation_ids).delete()


@transaction.atomic
def enter_bracket(tournament, bracket_id, reservation_id):
    bracket = _bracket(tournament, bracket_id)
    if _locked(bracket):
        raise ValueError("勝敗が決まっているので、人の入れ替えはできません。")
    reservation = _paid(tournament.event, reservation_id)
    if BracketEntry.objects.filter(reservation=reservation).exists():
        raise ValueError("この人はすでにブラケットに入っています。")
    BracketEntry.objects.create(
        bracket=bracket,
        reservation=reservation,
        position=_next_position(bracket.entries),
    )
    _materialize(bracket)
    _refresh_all(bracket)


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
    if _locked(entry.bracket):
        raise ValueError("勝敗が決まっているので、人の入れ替えはできません。")
    bracket = entry.bracket
    entry.delete()
    _materialize(bracket)
    _refresh_all(bracket)
    MemberRecord.objects.filter(reservation_id=key).delete()


@transaction.atomic
def move_entry(tournament, bracket_id, reservation_id, direction):
    way = str(direction or "").strip()
    if way not in {"up", "down"}:
        raise ValueError("上か下を指定してください。")
    bracket = _bracket(tournament, bracket_id)
    if _locked(bracket):
        raise ValueError("勝敗が決まっているので、順番は変えられません。")
    try:
        key = int(reservation_id)
    except (TypeError, ValueError):
        raise ValueError("参加者が見つかりません。")
    entries = list(bracket.entries.select_related("reservation").order_by("position", "id"))
    index = next((i for i, entry in enumerate(entries) if entry.reservation_id == key), None)
    if index is None:
        raise ValueError("参加者が見つかりません。")
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


def _ordered_error(*_args, **_kwargs):
    raise ValueError("組み合わせは並び順で決まります。上へ・下へで順番を変えてください。")


pair_bracket = _ordered_error
pair_bracket_rest = _ordered_error
bye_bracket = _ordered_error
unpair_tournament = _ordered_error


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


def _optional_int(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _apply_winner(match, winner_value, note):
    raw = "" if winner_value is None else str(winner_value).strip()
    if raw in {"", "clear"}:
        match.winner = None
        match.note = ""
        match.save(update_fields=["winner", "note"])
        return
    if raw == "draw":
        raise ValueError("トーナメントに引き分けはありません。")
    try:
        winner_id = int(raw)
    except (TypeError, ValueError):
        raise ValueError("勝った人はこの試合の2人から選んでください。")
    if winner_id not in {match.first_id, match.second_id}:
        raise ValueError("勝った人はこの試合の2人から選んでください。")
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
def set_tournament_winner(
    tournament,
    match_id,
    winner_value,
    note=None,
    bracket_id=None,
    round_no=None,
    position=None,
):
    bracket = None
    if bracket_id not in (None, ""):
        bracket = _bracket(tournament, bracket_id)
    if bracket is None and match_id not in (None, ""):
        current = _match(tournament, match_id)
        bracket = (
            Bracket.objects.select_for_update()
            .filter(id=current.bracket_id, tournament=tournament)
            .first()
        )
    if bracket is None:
        raise ValueError("ブラケットが見つかりません。")
    _materialize(bracket)
    found = None
    match_key = _optional_int(match_id)
    if match_key is not None:
        found = (
            TournamentMatch.objects.select_for_update()
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
            TournamentMatch.objects.select_for_update()
            .select_related("first", "second", "winner")
            .filter(bracket=bracket, round=rnd, position=pos)
            .first()
        )
    if found is None:
        raise ValueError("対戦する2人が揃ってから決められます。")
    if found.second_id is None:
        raise ValueError("不戦勝の相手は選べません。")
    _apply_winner(found, winner_value, note)
    _materialize(bracket)
    _refresh_all(bracket)


@transaction.atomic
def set_tournament_note(tournament, match_id, note):
    match = _match(tournament, match_id)
    if match.winner_id is None or match.second_id is None:
        raise ValueError("勝ちが決まってから理由を書けます。")
    text = str(note or "").strip()
    if len(text) > 200:
        raise ValueError("理由は200文字以内にしてください。")
    match.note = text
    match.save(update_fields=["note"])
    _refresh_all(match.bracket)


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
        set_tournament_winner(
            match.bracket.tournament,
            match.id,
            record.reservation_id,
            None,
            match.bracket_id,
            match.round,
            match.position,
        )
        return
    if "負け" in text:
        other = (
            match.second_id
            if match.first_id == record.reservation_id
            else match.first_id
        )
        if other is None:
            return
        set_tournament_winner(
            match.bracket.tournament,
            match.id,
            other,
            None,
            match.bracket_id,
            match.round,
            match.position,
        )
        return
    if text == "":
        set_tournament_winner(
            match.bracket.tournament,
            match.id,
            "",
            None,
            match.bracket_id,
            match.round,
            match.position,
        )
        return
    raise ValueError("トーナメントの結果は「勝ち」か「負け」で入力してください。")
