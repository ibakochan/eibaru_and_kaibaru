from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from .service_reservation_start import (
    ReservationStartError,
    ReservationStartService,
)


@require_http_methods(["GET", "POST"])
def start_reservation(request, token):
    if request.method == "GET":
        return _render(
            request,
            ReservationStartService.preview(token),
        )

    try:
        result = ReservationStartService.begin(token)
    except ReservationStartError as exc:
        state = ReservationStartService.preview(token)
        state["message"] = exc.message

        if not (
            exc.retryable and state.get("mode") == "ready"
        ):
            if state.get("mode") != "invalid":
                state["mode"] = "error"

        return _render(request, state, status=400)

    if result.get("redirect_url"):
        return redirect(result["redirect_url"])

    return _render(request, result)


def _render(request, context, status=200):
    response = render(
        request,
        "kaibaru/reservation_start.html",
        context,
        status=status,
    )
    # no-referrer makes the browser send Origin: null, which Django
    # rejects. strict-origin still hides the token path from Stripe.
    response["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response
