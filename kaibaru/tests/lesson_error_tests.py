"""The lesson API says which piece is missing instead of a bare "club" error."""
from django.db.models.signals import post_save
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import CustomUser
from kaibaru.models import Club
from kaibaru.signals import club_created_signal


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
