from django.conf import settings
from django.http import HttpResponseBadRequest, HttpResponseRedirect

from .custom_domain_hosts import custom_domain_redirect_url, is_platform_host

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "testserver", "[::1]"}
_ORIGINAL_ALLOWED_HOSTS = None


def original_allowed_hosts():
    global _ORIGINAL_ALLOWED_HOSTS
    if _ORIGINAL_ALLOWED_HOSTS is None:
        _ORIGINAL_ALLOWED_HOSTS = tuple(settings.ALLOWED_HOSTS)
    return _ORIGINAL_ALLOWED_HOSTS


def raw_host(request):
    host = request._get_raw_host()
    if host.startswith("["):
        host = host[1:].split("]", 1)[0]
    else:
        host = host.split(":", 1)[0]
    return host.lower().rstrip(".")


def host_is_known(host):
    if host in _LOCAL_HOSTS or is_platform_host(host):
        return True
    for allowed in original_allowed_hosts():
        if allowed == "*":
            return True
        allowed = allowed.lower()
        if allowed.startswith("."):
            suffix = allowed[1:]
            if host == suffix or host.endswith("." + suffix):
                return True
        elif host == allowed:
            return True
    return False


class CustomDomainHostMiddleware:
    """
    Allow a request only when the host is Kaibaru or a club domain that is live.
    A domain that is only saved, and not yet checked, is not allowed.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        host = raw_host(request)
        target = custom_domain_redirect_url(host, request.get_full_path())
        if target:
            return HttpResponseRedirect(target)
        if not host or host_is_known(host):
            return self.get_response(request)

        from django.db import OperationalError, ProgrammingError

        from .models import Club

        try:
            live = Club.objects.filter(
                custom_domain=host,
                custom_domain_status="active",
                is_deleted=False,
            ).exists()
        except (OperationalError, ProgrammingError):
            live = False
        if not live:
            allowed = settings.ALLOWED_HOSTS
            if (
                not isinstance(allowed, tuple)
                and host in allowed
                and host not in original_allowed_hosts()
            ):
                allowed.remove(host)
            return HttpResponseBadRequest("このドメインは利用できません。")
        allowed = settings.ALLOWED_HOSTS
        if isinstance(allowed, tuple):
            settings.ALLOWED_HOSTS = list(allowed) + [host]
        elif host not in allowed:
            allowed.append(host)
        return self.get_response(request)


class CustomDomainSessionCookieMiddleware:
    """
    Session cookies for Kaibaru hosts stay on .kaibaru.jp.
    A club's own domain gets a cookie for that host only.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        host = raw_host(request)
        if not host or is_platform_host(host):
            return response
        morsel = response.cookies.get(settings.SESSION_COOKIE_NAME)
        if morsel is not None:
            morsel["domain"] = ""
        return response
