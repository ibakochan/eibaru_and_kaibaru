from django.db import transaction
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from .models import EventReservation, Reservation
from .reservation_cancel import (
    cancel_is_closed,
    read_cancel_token,
    restore_url,
)


@require_http_methods(["GET", "POST"])
def cancel_reservation(request, token):
    loaded = read_cancel_token(token)
    if loaded is None or loaded["reservation"] is None:
        return _render(
            request,
            {
                "mode": "invalid",
                "message": "この予約は見つからないか、リンクが無効です。",
            },
        )

    reservation = loaded["reservation"]
    kind = loaded["kind"]
    context = _page_context(kind, reservation)

    if reservation.status != _paid_status(kind):
        context["mode"] = "closed"
        context["message"] = "確定した予約のみキャンセルできます。"
        return _render(request, context)

    if reservation.canceled:
        context["mode"] = "already"
        context["message"] = "この予約はすでにキャンセルされています。"
        return _render(request, context)

    if cancel_is_closed(kind, reservation):
        context["mode"] = "closed"
        context["message"] = "この予約はすでに終了しているため、キャンセルできません。"
        return _render(request, context)

    if request.method == "GET":
        context["mode"] = "confirm"
        return _render(request, context)

    with transaction.atomic():
        locked = _lock(kind, reservation.id)
        if locked is None:
            context["mode"] = "invalid"
            context["message"] = "この予約は見つかりません。"
            return _render(request, context)

        if locked.status != _paid_status(kind):
            context["mode"] = "closed"
            context["message"] = "確定した予約のみキャンセルできます。"
            return _render(request, context)

        if locked.canceled:
            context["mode"] = "already"
            context["message"] = "この予約はすでにキャンセルされています。"
            return _render(request, context)

        if cancel_is_closed(kind, locked):
            context["mode"] = "closed"
            context["message"] = "この予約はすでに終了しているため、キャンセルできません。"
            return _render(request, context)

        locked.canceled = True
        locked.save(update_fields=["canceled"])
        reservation_id = locked.id

        def _send_canceled_email():
            from .tasks_emails import send_reservation_canceled_email

            send_reservation_canceled_email.delay(kind, reservation_id)

        transaction.on_commit(_send_canceled_email)

    context["mode"] = "done"
    return _render(request, context)


@require_http_methods(["GET", "POST"])
def restore_reservation(request, token):
    loaded = read_cancel_token(token)
    if loaded is None or loaded["reservation"] is None:
        return _render(
            request,
            {
                "mode": "invalid",
                "message": "この予約は見つからないか、リンクが無効です。",
            },
        )

    reservation = loaded["reservation"]
    kind = loaded["kind"]
    context = _page_context(kind, reservation)

    if not reservation.canceled:
        context["mode"] = "closed"
        context["message"] = "この予約はキャンセルされていません。"
        return _render(request, context)

    blocked = _restore_block_reason(kind, reservation)
    if blocked:
        context["mode"] = "closed"
        context["message"] = blocked
        return _render(request, context)

    if request.method == "GET":
        context["mode"] = "restore_confirm"
        return _render(request, context)

    with transaction.atomic():
        locked = _lock(kind, reservation.id)
        if locked is None or not locked.canceled:
            context["mode"] = "closed"
            context["message"] = "この予約はキャンセルされていません。"
            return _render(request, context)

        blocked = _restore_block_reason(kind, locked)
        if blocked:
            context["mode"] = "closed"
            context["message"] = blocked
            return _render(request, context)

        locked.canceled = False
        locked.save(update_fields=["canceled"])

    context["mode"] = "restored"
    return _render(request, context)


def _paid_status(kind):
    if kind == "lesson":
        return Reservation.Status.PAID
    return EventReservation.Status.PAID


def _lock(kind, reservation_id):
    if kind == "lesson":
        return (
            Reservation.objects
            .select_for_update()
            .select_related("club", "lesson")
            .filter(id=reservation_id)
            .first()
        )
    return (
        EventReservation.objects
        .select_for_update()
        .select_related("club", "event")
        .filter(id=reservation_id)
        .first()
    )


def _restore_block_reason(kind, reservation):
    if cancel_is_closed(kind, reservation):
        return "この予約はすでに終了しているため、キャンセルを取り消せません。"

    try:
        if kind == "lesson":
            from .rules_reservations import assert_lesson_has_spots, hold_cutoff_at

            assert_lesson_has_spots(
                lesson=reservation.lesson,
                reservation_date=reservation.reservation_date,
                hold_cutoff=hold_cutoff_at(),
                needed=1,
                exclude_ids=[reservation.id],
            )
        else:
            from .service_event import assert_event_has_spots, hold_cutoff_at

            assert_event_has_spots(
                event=reservation.event,
                hold_cutoff=hold_cutoff_at(),
                needed=1,
                exclude_ids=[reservation.id],
            )
    except ValueError as exc:
        return str(exc) or "定員に達しているため、キャンセルを取り消せません。"

    return None


def _page_context(kind, reservation):
    club = reservation.club
    if kind == "lesson":
        title = reservation.lesson.title
        when_text = reservation.reservation_date.strftime("%Y年%m月%d日")
    else:
        from .service_event import format_event_when

        title = reservation.event.title
        when_text = format_event_when(reservation.event)

    uses_ticket = getattr(reservation, "payment_method", "") == "ticket"
    return {
        "club_name": club.title or club.subdomain or "クラブ",
        "title": title,
        "when_text": when_text,
        "person_name": reservation.full_name,
        "amount": reservation.amount,
        "is_paid_amount": reservation.amount > 0 and not uses_ticket,
        "uses_ticket": uses_ticket,
        "home_url": f"https://{club.subdomain}.kaibaru.jp/",
        "restore_url": restore_url(club, kind, reservation.id),
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
