from django.core.signing import BadSignature, Signer
from django.utils import timezone

from .models import (
    BattleMember,
    BracketEntry,
    DivisionEntry,
    EventReservation,
    Pairing,
    Reservation,
    TournamentTeamMember,
)


OWNER_PLACEMENT_MESSAGE = (
    "この人はすでに大会の組み合わせに入っています。"
    "先にチーム、ブラケット、種目、または対戦から外してください。"
)
PARTICIPANT_PLACEMENT_MESSAGE = (
    "すでに大会の組み合わせに入っています。"
    "キャンセルするには、大会の担当者に連絡してください。"
)


_signer = Signer(salt="kaibaru-reservation-cancel")


def make_cancel_token(kind, reservation_id):
    return _signer.sign(f"{kind}:{reservation_id}")


def cancel_url(club, kind, reservation_id):
    token = make_cancel_token(kind, reservation_id)
    return (
        f"https://{club.subdomain}.kaibaru.jp/"
        f"cancel_reservation/{token}/"
    )


def restore_url(club, kind, reservation_id):
    token = make_cancel_token(kind, reservation_id)
    return (
        f"https://{club.subdomain}.kaibaru.jp/"
        f"restore_reservation/{token}/"
    )


def canceled_rebook_message():
    return (
        "この予約はキャンセル済みです。"
        "キャンセルの取り消しから、もう一度有効にしてください。"
    )


def owner_may_cancel_free(reservation, user):
    """Owner cancel for a confirmed reservation that took no money and no ticket."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if reservation.club.owner_id != user.id:
        return False
    if reservation.canceled or getattr(reservation, "refunded_at", None):
        return False
    if reservation.status != "paid":
        return False
    if reservation.amount != 0:
        return False
    if (reservation.payment_method or "") == "ticket":
        return False
    return True


def owner_may_restore_free(reservation, user):
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if reservation.club.owner_id != user.id:
        return False
    if not reservation.canceled or getattr(reservation, "refunded_at", None):
        return False
    if reservation.amount != 0:
        return False
    if (reservation.payment_method or "") == "ticket":
        return False
    return True


def read_cancel_token(token):
    try:
        raw = _signer.unsign(token)
    except BadSignature:
        return None

    kind, separator, reservation_id = raw.partition(":")
    if separator != ":" or kind not in {"lesson", "event"}:
        return None

    try:
        reservation_id = int(reservation_id)
    except ValueError:
        return None

    if kind == "lesson":
        reservation = (
            Reservation.objects
            .select_related("club", "lesson")
            .filter(id=reservation_id)
            .first()
        )
    else:
        reservation = (
            EventReservation.objects
            .select_related("club", "event")
            .filter(id=reservation_id)
            .first()
        )

    if reservation is None:
        return {"kind": kind, "reservation": None}

    return {"kind": kind, "reservation": reservation}


def placed_event_reservation_ids(reservation_ids):
    ids = [item for item in reservation_ids if item]
    if not ids:
        return set()
    placed = set(
        BracketEntry.objects.filter(reservation_id__in=ids).values_list(
            "reservation_id",
            flat=True,
        )
    )
    placed.update(
        DivisionEntry.objects.filter(reservation_id__in=ids).values_list(
            "reservation_id",
            flat=True,
        )
    )
    placed.update(
        Pairing.objects.filter(first_id__in=ids).values_list("first_id", flat=True)
    )
    placed.update(
        Pairing.objects.filter(second_id__in=ids).values_list("second_id", flat=True)
    )
    placed.update(
        TournamentTeamMember.objects.filter(reservation_id__in=ids).values_list(
            "reservation_id",
            flat=True,
        )
    )
    placed.update(
        BattleMember.objects.filter(reservation_id__in=ids).values_list(
            "reservation_id",
            flat=True,
        )
    )
    return placed


def placement_cancel_message(reservation, *, owner):
    if reservation.id not in placed_event_reservation_ids([reservation.id]):
        return None
    if owner:
        return OWNER_PLACEMENT_MESSAGE
    return PARTICIPANT_PLACEMENT_MESSAGE


def cancel_is_closed(kind, reservation):
    now = timezone.now()
    if kind == "lesson":
        return reservation.reservation_date < timezone.localdate()
    return reservation.event.starts_at <= now
