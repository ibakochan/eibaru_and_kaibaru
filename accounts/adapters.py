from urllib.parse import urlparse

from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.contrib.sites.models import Site
from allauth.account.adapter import DefaultAccountAdapter
from django.shortcuts import resolve_url
import logging

logger = logging.getLogger(__name__)

class DynamicSocialAccountAdapter(DefaultSocialAccountAdapter):
    def get_login_redirect_url(self, request):
        # Check if the cookie exists
        next_subdomain = request.COOKIES.get("next_subdomain")
        if next_subdomain:
            # Clear the cookie if you want (optional)
            return f"https://{next_subdomain}.kaibaru.jp"
        # Default fallback
        return "https://g.kaibaru.jp"

    def get_connect_redirect_url(self, request, socialaccount):
        next_url = request.session.get("next_subdomain")
        if _is_kaibaru_url(next_url):
            return next_url
        return "https://kaibaru.jp/"


def _is_kaibaru_url(url):
    if not url:
        return False
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (
        host == "kaibaru.jp" or host.endswith(".kaibaru.jp")
    )



class DynamicAccountAdapter(DefaultAccountAdapter):
    def get_current_site(self, request):
        if hasattr(request, "site"):
            return request.site
        return super().get_current_site(request)

    def get_login_redirect_url(self, request):
        # Pop the subdomain from session after login
        next_subdomain = request.session.pop('next_subdomain', None)
        if next_subdomain:
            return next_subdomain
        
        host = request.get_host()  # e.g. "eibaru.jp", "kaibaru.jp", "g.kaibaru.jp"
        
        # OPTIONAL: strip subdomains
        parts = host.split('.')
        if len(parts) > 2:
            # remove the left-most subdomain ("g")
            host = ".".join(parts[-2:])

        return f"https://{host}"
