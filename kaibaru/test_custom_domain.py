from types import SimpleNamespace
from unittest.mock import patch

from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase

from kaibaru.custom_domain_middleware import CustomDomainSessionCookieMiddleware
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
        self.assertEqual(run.call_args.args[0][0:2], ["sudo", "-n"])
        self.assertNotIn("-S", run.call_args.args[0])
        message.return_value.send.assert_called_once()
        self.assertEqual(club.custom_domain_status, "active")
        self.assertFalse(club.scheduled_for_domain_cert)
