from django.core.signing import BadSignature, Signer
from django.utils import timezone

from .models import EventReservation, Reservation


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


def cancel_is_closed(kind, reservation):
    now = timezone.now()
    if kind == "lesson":
        return reservation.reservation_date < timezone.localdate()
    return reservation.event.starts_at <= now
