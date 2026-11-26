import json

from .club_language import is_english
from .custom_domain_middleware import raw_host
from .message_en import MESSAGE_EN


def _club(host):
    from .models import Club

    if host.endswith(".kaibaru.jp"):
        subdomain = host.split(".", 1)[0]
        if subdomain in {"www", "kaibaru"}:
            return None
        return (
            Club.objects.filter(subdomain=subdomain, is_deleted=False)
            .only("language")
            .first()
        )
    return (
        Club.objects.filter(custom_domain=host, is_deleted=False)
        .only("language")
        .first()
    )


def _translate(node):
    if isinstance(node, str):
        return MESSAGE_EN.get(node, node)
    if isinstance(node, list):
        return [_translate(item) for item in node]
    if isinstance(node, dict):
        return {key: _translate(value) for key, value in node.items()}
    return node


class EnglishClubMessageMiddleware:
    """Replace known system messages when the club is in English.

    Japanese clubs are untouched. Stored titles are left alone unless the
    whole value is one of the system messages in MESSAGE_EN.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        content_type = response.get("Content-Type", "")
        if "application/json" not in content_type:
            return response
        if getattr(response, "streaming", False):
            return response
        raw = response.content
        if not raw:
            return response
        try:
            club = _club(raw_host(request))
        except Exception:
            return response
        if not is_english(club):
            return response
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return response
        translated = _translate(data)
        if translated == data:
            return response
        response.content = json.dumps(translated, ensure_ascii=False).encode("utf-8")
        return response
