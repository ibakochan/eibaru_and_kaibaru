from django.core.exceptions import FieldDoesNotExist, ObjectDoesNotExist
from django.db.models import Prefetch

from .models import (
    BattleMatch,
    BattleMember,
    BattleTeam,
    Bracket,
    BracketEntry,
    Competition,
    Division,
    DivisionEntry,
    Duel,
    EventReservation,
    Pairing,
    TeamBattle,
    TeamBracket,
    TeamBracketEntry,
    TeamBracketMatch,
    TeamTournament,
    Tournament,
    TournamentMatch,
    TournamentTeam,
    TournamentTeamMember,
)


_MISSING = object()


class _CacheMiss(Exception):
    pass


def cached_related(instance, name):
    if instance is None:
        return None
    cache = getattr(instance, "_prefetched_objects_cache", None)
    if cache is not None and name in cache:
        return list(cache[name])
    return None


def load_related(instance, name, fallback):
    cached = cached_related(instance, name)
    if cached is not None:
        return cached
    return list(fallback)


def cached_reverse_one(instance, name):
    try:
        field = instance._meta.get_field(name)
    except FieldDoesNotExist:
        return _MISSING
    if not field.is_cached(instance):
        return _MISSING
    try:
        return getattr(instance, name)
    except ObjectDoesNotExist:
        return None


def event_reservations(event):
    cached = getattr(event, "cached_reservations", None)
    if cached is not None:
        return cached
    cached = list(
        EventReservation.objects.filter(event=event, is_deleted=False)
        .select_related("member", "club")
        .order_by("-created_at", "-id")
    )
    event.cached_reservations = cached
    return cached


def paid_reservations(event):
    cached = getattr(event, "_paid_reservation_list", None)
    if cached is not None:
        return cached
    cached = sorted(
        (
            row
            for row in event_reservations(event)
            if not row.canceled and row.status == EventReservation.Status.PAID
        ),
        key=lambda row: (row.full_name or "", row.id),
    )
    event._paid_reservation_list = cached
    return cached


def held_count(event):
    cached = getattr(event, "_held_count", None)
    if cached is not None:
        return cached
    from .service_event import hold_cutoff_at, hold_is_active

    cutoff = hold_cutoff_at()
    total = 0
    for row in event_reservations(event):
        if row.canceled:
            continue
        if row.status == EventReservation.Status.PAID:
            total += 1
        elif row.status == EventReservation.Status.UNPAID and hold_is_active(row, cutoff):
            total += 1
    event._held_count = total
    return total


def owned_member_ids(club, user):
    if user is None or not getattr(user, "is_authenticated", False):
        return set()
    cache = getattr(club, "_owned_member_ids_cache", None)
    if cache is None:
        cache = {}
        club._owned_member_ids_cache = cache
    if user.id in cache:
        return cache[user.id]
    members = cached_related(club, "members")
    if members is None:
        ids = set(club.members.filter(owner=user).values_list("id", flat=True))
    else:
        ids = {member.id for member in members if member.owner_id == user.id}
    cache[user.id] = ids
    return ids


def _need(instance, name):
    rows = cached_related(instance, name)
    if rows is None:
        raise _CacheMiss
    return rows


def _placed_from_draw(event):
    ids = set()
    competition = cached_reverse_one(event, "competition")
    if competition not in (_MISSING, None):
        for division in _need(competition, "divisions"):
            for entry in _need(division, "entries"):
                ids.add(entry.reservation_id)
    duel = cached_reverse_one(event, "duel")
    if duel not in (_MISSING, None):
        for pairing in _need(duel, "pairings"):
            ids.add(pairing.first_id)
            ids.add(pairing.second_id)
    tournament = cached_reverse_one(event, "tournament")
    if tournament not in (_MISSING, None):
        for bracket in _need(tournament, "brackets"):
            for entry in _need(bracket, "entries"):
                ids.add(entry.reservation_id)
    battle = cached_reverse_one(event, "team_battle")
    if battle not in (_MISSING, None):
        for team in _need(battle, "teams"):
            for member in _need(team, "members"):
                ids.add(member.reservation_id)
    team_tournament = cached_reverse_one(event, "team_tournament")
    if team_tournament not in (_MISSING, None):
        for team in _need(team_tournament, "teams"):
            for member in _need(team, "members"):
                ids.add(member.reservation_id)
    return ids


def _placed_from_query(event):
    from .reservation_cancel import placed_event_reservation_ids

    return placed_event_reservation_ids(row.id for row in event_reservations(event))


def placed_ids_for_event(event):
    cached = getattr(event, "_placed_ids", None)
    if cached is not None:
        return cached
    if getattr(event, "_draw_prefetched", False):
        try:
            ids = _placed_from_draw(event)
        except _CacheMiss:
            ids = _placed_from_query(event)
    else:
        ids = _placed_from_query(event)
    event._placed_ids = ids
    return ids


def _battle_members():
    return BattleMember.objects.select_related("reservation").order_by("position", "id")


def _battle_team_with_members():
    return BattleTeam.objects.prefetch_related(
        Prefetch("members", queryset=_battle_members()),
    )


def _tournament_team_members():
    return TournamentTeamMember.objects.select_related("reservation").order_by("position", "id")


def _tournament_team_with_members():
    return TournamentTeam.objects.prefetch_related(
        Prefetch("members", queryset=_tournament_team_members()),
    )


def event_draw_prefetches():
    return [
        Prefetch(
            "events__reservations",
            queryset=(
                EventReservation.objects.filter(is_deleted=False)
                .select_related("member", "club")
                .order_by("-created_at", "-id")
            ),
            to_attr="cached_reservations",
        ),
        Prefetch(
            "events__competition",
            queryset=Competition.objects.prefetch_related(
                Prefetch(
                    "divisions",
                    queryset=Division.objects.order_by("position", "id").prefetch_related(
                        Prefetch(
                            "entries",
                            queryset=DivisionEntry.objects.select_related("reservation").order_by(
                                "position", "id"
                            ),
                        )
                    ),
                )
            ),
        ),
        Prefetch(
            "events__duel",
            queryset=Duel.objects.prefetch_related(
                Prefetch(
                    "pairings",
                    queryset=Pairing.objects.select_related("first", "second").order_by(
                        "position", "id"
                    ),
                )
            ),
        ),
        Prefetch(
            "events__tournament",
            queryset=Tournament.objects.prefetch_related(
                Prefetch(
                    "brackets",
                    queryset=Bracket.objects.order_by("position", "id").prefetch_related(
                        Prefetch(
                            "entries",
                            queryset=BracketEntry.objects.select_related("reservation").order_by(
                                "position", "id"
                            ),
                        ),
                        Prefetch(
                            "matches",
                            queryset=TournamentMatch.objects.select_related(
                                "first", "second", "winner"
                            ).order_by("round", "position", "id"),
                        ),
                    ),
                )
            ),
        ),
        Prefetch(
            "events__team_battle",
            queryset=TeamBattle.objects.prefetch_related(
                Prefetch(
                    "teams",
                    queryset=BattleTeam.objects.order_by("position", "id").prefetch_related(
                        Prefetch("members", queryset=_battle_members()),
                    ),
                ),
                Prefetch(
                    "matches",
                    queryset=BattleMatch.objects.prefetch_related(
                        Prefetch("first", queryset=_battle_team_with_members()),
                        Prefetch("second", queryset=_battle_team_with_members()),
                    ).order_by("position", "id"),
                ),
            ),
        ),
        Prefetch(
            "events__team_tournament",
            queryset=TeamTournament.objects.prefetch_related(
                Prefetch(
                    "teams",
                    queryset=TournamentTeam.objects.order_by("position", "id").prefetch_related(
                        Prefetch("members", queryset=_tournament_team_members()),
                    ),
                ),
                Prefetch(
                    "brackets",
                    queryset=TeamBracket.objects.order_by("position", "id").prefetch_related(
                        Prefetch(
                            "entries",
                            queryset=TeamBracketEntry.objects.prefetch_related(
                                Prefetch("team", queryset=_tournament_team_with_members()),
                            ).order_by("position", "id"),
                        ),
                        Prefetch(
                            "matches",
                            queryset=TeamBracketMatch.objects.select_related(
                                "first", "second", "winner"
                            ).order_by("round", "position", "id"),
                        ),
                    ),
                ),
            ),
        ),
    ]
