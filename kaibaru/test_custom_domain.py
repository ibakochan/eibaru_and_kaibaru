from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase

from kaibaru.custom_domain_middleware import CustomDomainSessionCookieMiddleware
from kaibaru.service_custom_domain import CustomDomainError, normalize_domain


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
