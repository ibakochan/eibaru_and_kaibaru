from django.db import transaction
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from .reservation_cancel import cancel_is_closed, read_cancel_token


@require_http_methods(["GET", "POST"])
def cancel_reservation(request, token):
    loaded = read_cancel_token(token)
    if loaded is None or loaded["reservation"] is None:
        return _render(
            request,
            {
                "mode": "invalid",
                "message": (
                    "この予約はすでにキャンセルされているか、"
                    "リンクが無効です。"
                ),
            },
        )

    reservation = loaded["reservation"]
    kind = loaded["kind"]
    context = _page_context(kind, reservation)

    if cancel_is_closed(kind, reservation):
        context["mode"] = "closed"
        context["message"] = "この予約はすでに終了しているため、キャンセルできません。"
        return _render(request, context)

    if request.method == "GET":
        context["mode"] = "confirm"
        return _render(request, context)

    with transaction.atomic():
        if kind == "lesson":
            from .models import Reservation

            locked = (
                Reservation.objects
                .select_for_update()
                .filter(id=reservation.id)
                .first()
            )
        else:
            from .models import EventReservation

            locked = (
                EventReservation.objects
                .select_for_update()
                .filter(id=reservation.id)
                .first()
            )

        if locked is None:
            context["mode"] = "invalid"
            context["message"] = "この予約はすでにキャンセルされています。"
            return _render(request, context)

        if cancel_is_closed(kind, locked):
            context["mode"] = "closed"
            context["message"] = "この予約はすでに終了しているため、キャンセルできません。"
            return _render(request, context)

        locked.delete()

    context["mode"] = "done"
    return _render(request, context)


def _page_context(kind, reservation):
    club = reservation.club
    if kind == "lesson":
        title = reservation.lesson.title
        when_text = reservation.reservation_date.strftime("%Y年%m月%d日")
    else:
        from .service_event import format_event_when

        title = reservation.event.title
        when_text = format_event_when(reservation.event)

    return {
        "club_name": club.title or club.subdomain or "クラブ",
        "title": title,
        "when_text": when_text,
        "person_name": reservation.full_name,
        "amount": reservation.amount,
        "is_paid_amount": reservation.amount > 0,
        "home_url": f"https://{club.subdomain}.kaibaru.jp/",
    }


def _render(request, context, status=200):
    if context.get("mode") == "invalid":
        status = 404
    response = render(
        request,
        "kaibaru/reservation_cancel.html",
        context,
        status=status,
    )
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response
