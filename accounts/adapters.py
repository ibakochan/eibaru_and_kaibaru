from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.contrib.sites.models import Site
from allauth.account.adapter import DefaultAccountAdapter
from django.shortcuts import resolve_url
import logging

from accounts.models import CustomUser

from kaibaru.custom_domain_hosts import (
    login_return_url,
    prefer_login_return,
    safe_return_url,
)

logger = logging.getLogger(__name__)

class DynamicSocialAccountAdapter(DefaultSocialAccountAdapter):
    def pre_social_login(self, request, sociallogin):
        """Attach Google to the password account that already uses this email."""
        if sociallogin.is_existing:
            return
        email = ""
        for address in sociallogin.email_addresses:
            if address.verified and address.email:
                email = address.email.strip().lower()
                break
        if not email:
            email = (sociallogin.user.email or "").strip().lower()
        if not email:
            return
        matches = list(CustomUser.objects.filter(email__iexact=email)[:2])
        if len(matches) != 1:
            return
        user = matches[0]
        if not user.is_active:
            user.is_active = True
            user.save(update_fields=["is_active"])
        sociallogin.connect(request, user)

    def get_login_redirect_url(self, request):
        stored = request.session.pop("next_subdomain", None)
        target = login_return_url(request.user, stored)
        if target:
            return target
        next_subdomain = request.COOKIES.get("next_subdomain")
        if next_subdomain:
            return f"https://{next_subdomain}.kaibaru.jp"
        return "https://g.kaibaru.jp"

    def get_connect_redirect_url(self, request, socialaccount):
        stored = request.session.get("next_subdomain")
        target = login_return_url(request.user, stored)
        if target:
            return target
        return "https://kaibaru.jp/"



class DynamicAccountAdapter(DefaultAccountAdapter):
    def get_current_site(self, request):
        if hasattr(request, "site"):
            return request.site
        return super().get_current_site(request)

    def is_safe_url(self, url):
        if safe_return_url(url):
            return True
        return super().is_safe_url(url)

    def post_login(
        self,
        request,
        user,
        *,
        email_verification,
        signal_kwargs,
        email,
        signup,
        redirect_url,
    ):
        stored = request.session.pop("next_subdomain", None)
        target = prefer_login_return(stored, redirect_url)
        handed = login_return_url(user, target)
        if handed:
            redirect_url = handed
        return super().post_login(
            request,
            user,
            email_verification=email_verification,
            signal_kwargs=signal_kwargs,
            email=email,
            signup=signup,
            redirect_url=redirect_url,
        )

    def get_login_redirect_url(self, request):
        stored = request.session.pop("next_subdomain", None)
        target = login_return_url(request.user, stored)
        if target:
            return target

        host = request.get_host()  # e.g. "eibaru.jp", "kaibaru.jp", "g.kaibaru.jp"
        parts = host.split(".")
        if len(parts) > 2:
            host = ".".join(parts[-2:])
        return f"https://{host}"
