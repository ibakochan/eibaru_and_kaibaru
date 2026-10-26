import secrets
from urllib.parse import urlparse

from django.core.cache import cache


def is_platform_host(host):
    host = (host or "").lower().rstrip(".")
    if not host:
        return False
    return (
        host == "kaibaru.jp"
        or host.endswith(".kaibaru.jp")
        or host == "eibaru.jp"
        or host.endswith(".eibaru.jp")
    )


def handoff_cache_key(token):
    return f"domain-handoff:{token}"


def safe_return_url(url):
    """Allow a return only to Kaibaru or a club domain that is already live."""
    if not url:
        return None
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host:
        return None
    if is_platform_host(host):
        return url
    from .models import Club

    live = Club.objects.filter(
        custom_domain=host,
        custom_domain_status="active",
        is_deleted=False,
    ).exists()
    if live:
        return url
    return None


def session_handoff_url(user, url):
    """
    A .kaibaru.jp session cookie is not sent to a club's own domain.
    Send a one-time token to that host so the response can set a host-only cookie.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or is_platform_host(host):
        return url
    token = secrets.token_urlsafe(32)
    cache.set(handoff_cache_key(token), user.pk, timeout=60)
    return f"https://{host}/accounts/domain-session/{token}/"
