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


def club_subdomain(host):
    """The club label in gy.kaibaru.jp. Empty for the apex and for other hosts."""
    host = (host or "").lower().rstrip(".")
    suffix = ".kaibaru.jp"
    if not host.endswith(suffix):
        return None
    label = host[: -len(suffix)]
    if not label or "." in label or label == "www":
        return None
    return label


def active_custom_domain(subdomain):
    from django.db import OperationalError, ProgrammingError

    from .models import Club

    try:
        club = (
            Club.objects.filter(
                subdomain=subdomain,
                custom_domain_status="active",
            )
            .exclude(custom_domain__isnull=True)
            .exclude(custom_domain="")
            .only("custom_domain")
            .first()
        )
    except (OperationalError, ProgrammingError):
        return None
    if club is None:
        return None
    domain = (club.custom_domain or "").strip().lower().rstrip(".")
    if not domain or is_platform_host(domain) or "/" in domain or "\\" in domain:
        return None
    return domain


def custom_domain_redirect_url(host, path):
    """
    Where a Kaibaru subdomain should send the browser once its own domain is live.
    Pending domains stay on the subdomain. This does not look up DNS.
    """
    subdomain = club_subdomain(host)
    if not subdomain:
        return None
    domain = active_custom_domain(subdomain)
    if not domain:
        return None
    if not path.startswith("/"):
        path = "/" + path
    return f"https://{domain}{path}"


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


def _return_path(parsed):
    path = parsed.path or "/"
    if not path.startswith("/") or path.startswith("//"):
        path = "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return path


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
    cache.set(
        handoff_cache_key(token),
        {"user_id": user.pk, "path": _return_path(parsed)},
        timeout=60,
    )
    return f"https://{host}/accounts/domain-session/{token}/"


def login_return_url(user, url):
    """
    Finish login on the host the person should keep using.
    A club subdomain whose custom domain is active is sent there, through the handoff.
    A plain Kaibaru host is returned unchanged so its .kaibaru.jp cookie still works.
    """
    safe = safe_return_url(url)
    if not safe or user is None:
        return None
    parsed = urlparse(safe)
    host = (parsed.hostname or "").lower().rstrip(".")
    path = _return_path(parsed)
    label = club_subdomain(host)
    if label:
        custom = active_custom_domain(label)
        if custom:
            return session_handoff_url(user, f"https://{custom}{path}")
        return safe
    if is_platform_host(host):
        return safe
    return session_handoff_url(user, safe)


def prefer_login_return(stored, redirect_url):
    """Session destination wins over the apex fallback Google would otherwise use."""
    candidates = [stored, redirect_url]
    for candidate in candidates:
        host = (urlparse(candidate or "").hostname or "").lower().rstrip(".")
        if host and host not in {"kaibaru.jp", "www.kaibaru.jp"} and safe_return_url(candidate):
            return candidate
    for candidate in candidates:
        if safe_return_url(candidate):
            return candidate
    return None
