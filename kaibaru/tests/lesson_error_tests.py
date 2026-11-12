"""The lesson API says which piece is missing instead of a bare "club" error."""
from django.db.models.signals import post_save
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from rest_framework import serializers
from rest_framework.test import APIClient

from accounts.models import CustomUser
from kaibaru.models import Club
from kaibaru.serializers import (
    validate_allowed_gender_value,
    validate_reservation_cap_attrs,
)
from kaibaru.signals import club_created_signal


class GenderRestrictionValidatorTests(SimpleTestCase):
    def test_blank_gender_means_no_restriction(self):
        self.assertIsNone(validate_allowed_gender_value(""))
        self.assertIsNone(validate_allowed_gender_value(None))

    def test_known_genders_pass(self):
        self.assertEqual(validate_allowed_gender_value("male"), "male")
        self.assertEqual(validate_allowed_gender_value("female"), "female")

    def test_unknown_gender_is_rejected(self):
        with self.assertRaises(serializers.ValidationError):
            validate_allowed_gender_value("other")

    def test_cap_validator_never_reports_a_gender_error(self):
        # The cap check used to fall through into the gender check and read
        # the last days-ahead value as if it were a gender.
        for days in (0, 1, 7, 30):
            with self.subTest(days=days):
                validate_reservation_cap_attrs(
                    {
                        "member_reservation_max_count": 2,
                        "visitor_reservation_max_count": 3,
                        "member_reservation_max_days_ahead": days,
                        "visitor_reservation_max_days_ahead": days,
                    }
                )

    def test_cap_validator_still_rejects_bad_caps(self):
        with self.assertRaises(serializers.ValidationError) as ctx:
            validate_reservation_cap_attrs({"member_reservation_max_count": 0})
        self.assertIn("member_reservation_max_count", ctx.exception.detail)
        with self.assertRaises(serializers.ValidationError) as ctx:
            validate_reservation_cap_attrs({"visitor_reservation_max_days_ahead": -1})
        self.assertIn("visitor_reservation_max_days_ahead", ctx.exception.detail)


class LessonCreateErrorTests(TestCase):
    URL = "/api/lessons/"

    def setUp(self):
        post_save.disconnect(club_created_signal, sender=Club)
        self.addCleanup(lambda: post_save.connect(club_created_signal, sender=Club))
        self.owner = CustomUser.objects.create_user(
            username="lessonowner", email="lesson@example.com", password="pass123"
        )
        self.club = Club.objects.create(
            subdomain="lessonclub", owner=self.owner, expiration_date=timezone.now()
        )
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def payload(self, **overrides):
        data = {
            "club_subdomain": "lessonclub",
            "section_id": "1",
            "title": "朝のクラス",
            "weekday": "0",
            "start_time": "09:00",
            "end_time": "10:00",
        }
        data.update(overrides)
        return {key: value for key, value in data.items() if value is not None}

    def test_missing_subdomain_names_the_club(self):
        response = self.client.post(
            self.URL, self.payload(club_subdomain=None), format="multipart"
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("club_subdomain", response.json())
        self.assertNotIn("club", response.json())

    def test_empty_subdomain_names_the_club(self):
        response = self.client.post(
            self.URL, self.payload(club_subdomain=""), format="multipart"
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("club_subdomain", response.json())

    def test_unknown_subdomain_says_which_one(self):
        response = self.client.post(
            self.URL, self.payload(club_subdomain="nosuchclub"), format="multipart"
        )
        self.assertEqual(response.status_code, 400, response.content)
        message = response.json()["club_subdomain"]
        self.assertIn("nosuchclub", message)

    def test_missing_section_is_named(self):
        response = self.client.post(
            self.URL, self.payload(section_id=None), format="multipart"
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("section_id", response.json())

    def test_valid_lesson_is_still_created(self):
        response = self.client.post(self.URL, self.payload(), format="multipart")
        self.assertEqual(response.status_code, 201, response.content)

    def test_no_gender_selected_creates_an_unrestricted_lesson(self):
        response = self.client.post(
            self.URL,
            self.payload(
                allowed_gender="",
                visitor_reservation_max_days_ahead="7",
                member_reservation_max_days_ahead="14",
            ),
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertFalse(response.json().get("allowed_gender"))

    def test_unknown_gender_is_reported_on_the_gender_field(self):
        response = self.client.post(
            self.URL, self.payload(allowed_gender="other"), format="multipart"
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("allowed_gender", response.json())
        self.assertNotIn("non_field_errors", response.json())

    def test_field_errors_still_name_the_field(self):
        response = self.client.post(
            self.URL,
            self.payload(start_time="11:00", end_time="10:00"),
            format="multipart",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("end_time", response.json())

    def test_stranger_gets_a_readable_permission_message(self):
        stranger = CustomUser.objects.create_user(
            username="lessonother", email="other-lesson@example.com", password="pass123"
        )
        self.client.force_authenticate(stranger)
        response = self.client.post(self.URL, self.payload(), format="multipart")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("権限", response.json()["detail"][0])
