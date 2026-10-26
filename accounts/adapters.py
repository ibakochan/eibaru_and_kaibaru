from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.contrib.sites.models import Site
from allauth.account.adapter import DefaultAccountAdapter
from django.shortcuts import resolve_url
import logging

from kaibaru.custom_domain_hosts import safe_return_url, session_handoff_url

logger = logging.getLogger(__name__)

class DynamicSocialAccountAdapter(DefaultSocialAccountAdapter):
    def get_login_redirect_url(self, request):
        next_url = request.session.pop("next_subdomain", None)
        safe = safe_return_url(next_url)
        if safe:
            return session_handoff_url(request.user, safe)
        next_subdomain = request.COOKIES.get("next_subdomain")
        if next_subdomain:
            return f"https://{next_subdomain}.kaibaru.jp"
        return "https://g.kaibaru.jp"

    def get_connect_redirect_url(self, request, socialaccount):
        next_url = request.session.get("next_subdomain")
        safe = safe_return_url(next_url)
        if safe:
            return session_handoff_url(request.user, safe)
        return "https://kaibaru.jp/"



class DynamicAccountAdapter(DefaultAccountAdapter):
    def get_current_site(self, request):
        if hasattr(request, "site"):
            return request.site
        return super().get_current_site(request)

    def get_login_redirect_url(self, request):
        # Pop the subdomain from session after login
        next_subdomain = request.session.pop('next_subdomain', None)
        safe = safe_return_url(next_subdomain)
        if safe:
            return session_handoff_url(request.user, safe)
        
        host = request.get_host()  # e.g. "eibaru.jp", "kaibaru.jp", "g.kaibaru.jp"
        
        # OPTIONAL: strip subdomains
        parts = host.split('.')
        if len(parts) > 2:
            # remove the left-most subdomain ("g")
            host = ".".join(parts[-2:])

        return f"https://{host}"
