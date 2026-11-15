from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from .service_service_reservation import (
    ServiceReservationStartService,
    ServiceStartError,
)


@require_http_methods(["GET", "POST"])
def start_service_reservation(request, token):
    if request.method == "GET":
        return _render(request, ServiceReservationStartService.preview(token))

    try:
        result = ServiceReservationStartService.begin(token)
    except ServiceStartError as exc:
        state = ServiceReservationStartService.preview(token)
        state["message"] = exc.message
        if not (exc.retryable and state.get("mode") == "ready"):
            if state.get("mode") != "invalid":
                state["mode"] = "error"
        return _render(request, state, status=400)

    if result.get("redirect_url"):
        return redirect(result["redirect_url"])

    return _render(request, result)


def _render(request, context, status=200):
    response = render(
        request,
        "kaibaru/service_start.html",
        context,
        status=status,
    )
    response["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response
