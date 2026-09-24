from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from .service_event import EventStartError, EventStartService


@require_http_methods(["GET", "POST"])
def start_event_reservation(request, token):
    if request.method == "GET":
        return _render(request, EventStartService.preview(token))

    try:
        result = EventStartService.begin(token)
    except EventStartError as exc:
        state = EventStartService.preview(token)
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
        "kaibaru/event_start.html",
        context,
        status=status,
    )
    response["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response
