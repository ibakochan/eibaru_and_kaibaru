from types import SimpleNamespace
from unittest.mock import patch

from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase

from kaibaru.custom_domain_hosts import custom_domain_redirect_url, login_return_url
from kaibaru.custom_domain_middleware import (
    CustomDomainHostMiddleware,
    CustomDomainSessionCookieMiddleware,
)
from kaibaru.service_custom_domain import (
    CustomDomainError,
    check_domain,
    normalize_domain,
    publish_domain,
)


def _dns(ip):
    return [(2, 1, 6, "", (ip, 0))]


class _Club:
    def __init__(self):
        self.pk = 1
        self.custom_domain = "club.example.com"
        self.custom_domain_status = "pending"
        self.scheduled_for_domain_cert = True
        self.title = "道場"
        self.subdomain = "dojo"
        self.owner = SimpleNamespace(email="owner@example.com")
        self.saved = None

    def save(self, update_fields=None):
        self.saved = update_fields


class NormalizeDomainTests(SimpleTestCase):
    def test_strips_url(self):
        self.assertEqual(
            normalize_domain("https://Club.Example.com/path"),
            "club.example.com",
        )

    def test_rejects_platform_names(self):
        with self.assertRaises(CustomDomainError):
            normalize_domain("no.kaibaru.jp")
        with self.assertRaises(CustomDomainError):
            normalize_domain("eibaru.jp")


class SessionCookieTests(SimpleTestCase):
    def response_for(self, host):
        def app(request):
            response = HttpResponse("ok")
            response.set_cookie("sessionid", "abc", domain=".kaibaru.jp")
            return response

        request = RequestFactory().get("/", HTTP_HOST=host)
        return CustomDomainSessionCookieMiddleware(app)(request)

    def test_club_host_is_host_only(self):
        response = self.response_for("club.example.com")
        self.assertEqual(response.cookies["sessionid"]["domain"], "")

    def test_kaibaru_keeps_parent_domain(self):
        response = self.response_for("no.kaibaru.jp")
        self.assertEqual(response.cookies["sessionid"]["domain"], ".kaibaru.jp")


class CustomDomainRedirectTests(SimpleTestCase):
    def test_active_domain_redirects_with_the_same_path(self):
        club = SimpleNamespace(custom_domain="ibarubibaru.lol")
        with patch("kaibaru.models.Club") as club_model:
            club_model.objects.filter.return_value.exclude.return_value.exclude.return_value.only.return_value.first.return_value = club
            url = custom_domain_redirect_url("gy.kaibaru.jp", "/members/?tab=1")
            club_model.objects.filter.assert_called_once_with(
                subdomain="gy",
                custom_domain_status="active",
            )
        self.assertEqual(url, "https://ibarubibaru.lol/members/?tab=1")

    def test_other_hosts_are_not_redirected(self):
        self.assertIsNone(custom_domain_redirect_url("kaibaru.jp", "/"))
        self.assertIsNone(custom_domain_redirect_url("ibarubibaru.lol", "/"))

    def test_pending_domain_stays_on_the_subdomain(self):
        with patch("kaibaru.models.Club") as club_model:
            club_model.objects.filter.return_value.exclude.return_value.exclude.return_value.only.return_value.first.return_value = None
            url = custom_domain_redirect_url("gy.kaibaru.jp", "/")
        self.assertIsNone(url)

    def test_middleware_sends_a_temporary_redirect(self):
        def app(request):
            return HttpResponse("ok")

        request = RequestFactory().get("/members/", HTTP_HOST="gy.kaibaru.jp")
        with patch(
            "kaibaru.custom_domain_middleware.custom_domain_redirect_url",
            return_value="https://ibarubibaru.lol/members/",
        ):
            response = CustomDomainHostMiddleware(app)(request)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://ibarubibaru.lol/members/")


class LoginReturnTests(SimpleTestCase):
    def test_subdomain_with_a_live_domain_hands_off_to_that_host(self):
        user = SimpleNamespace(pk=5)
        with patch("kaibaru.custom_domain_hosts.active_custom_domain", return_value="ibarubibaru.lol"):
            with patch("kaibaru.custom_domain_hosts.cache.set") as saved:
                url = login_return_url(user, "https://gy.kaibaru.jp/members/?tab=1")
        self.assertTrue(url.startswith("https://ibarubibaru.lol/accounts/domain-session/"))
        self.assertEqual(saved.call_args.args[1]["user_id"], 5)
        self.assertEqual(saved.call_args.args[1]["path"], "/members/?tab=1")

    def test_subdomain_without_a_custom_domain_stays_on_kaibaru(self):
        user = SimpleNamespace(pk=5)
        with patch("kaibaru.custom_domain_hosts.active_custom_domain", return_value=None):
            url = login_return_url(user, "https://gy.kaibaru.jp/members/")
        self.assertEqual(url, "https://gy.kaibaru.jp/members/")

    def test_custom_domain_hands_off_and_keeps_the_path(self):
        user = SimpleNamespace(pk=5)
        with patch("kaibaru.custom_domain_hosts.safe_return_url", side_effect=lambda url: url):
            with patch("kaibaru.custom_domain_hosts.cache.set") as saved:
                url = login_return_url(user, "https://ibarubibaru.lol/members/?tab=1")
        self.assertTrue(url.startswith("https://ibarubibaru.lol/accounts/domain-session/"))
        self.assertEqual(saved.call_args.args[1]["path"], "/members/?tab=1")


class CheckDomainTests(SimpleTestCase):
    def test_check_does_not_issue_a_certificate(self):
        club = _Club()
        with patch("kaibaru.service_custom_domain.socket.getaddrinfo", return_value=_dns("13.158.230.76")):
            with patch("kaibaru.service_custom_domain.subprocess.run") as run:
                self.assertEqual(check_domain(club), "club.example.com")
        run.assert_not_called()
        self.assertEqual(club.custom_domain_status, "pending")

    def test_publish_issues_and_emails(self):
        club = _Club()
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch("kaibaru.service_custom_domain.socket.getaddrinfo", return_value=_dns("13.158.230.76")):
            with patch("kaibaru.service_custom_domain.subprocess.run", return_value=completed) as run:
                with patch("kaibaru.service_custom_domain.EmailMessage") as message:
                    publish_domain(club)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][0:2], ["/usr/bin/sudo", "-n"])
        self.assertNotIn("-S", run.call_args.args[0])
        message.return_value.send.assert_called_once()
        self.assertEqual(club.custom_domain_status, "active")
        self.assertFalse(club.scheduled_for_domain_cert)
