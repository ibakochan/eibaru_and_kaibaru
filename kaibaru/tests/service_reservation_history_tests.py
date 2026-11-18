"""Reservation history lists months first, then one month of bookings."""
from datetime import date, time

from django.db.models.signals import post_save
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import CustomUser
from kaibaru.models import Club, ServiceCourse, ServiceReservation
from kaibaru.service_reservation_history import (
    service_reservation_history_index,
    service_reservation_history_month,
)
from kaibaru.signals import club_created_signal


class ServiceReservationHistoryTests(TestCase):
    def setUp(self):
        post_save.disconnect(club_created_signal, sender=Club)
        self.addCleanup(lambda: post_save.connect(club_created_signal, sender=Club))
        self.owner = CustomUser.objects.create_user(
            username="historyowner",
            email="history@example.com",
            password="pass123",
        )
        self.club = Club.objects.create(
            subdomain="historyclub",
            owner=self.owner,
            expiration_date=timezone.now(),
        )
        self.course = ServiceCourse.objects.create(
            club=self.club,
            name="カット",
            duration_minutes=60,
            amount=5000,
        )
        self.client = APIClient()

    def book(self, *, when, key, **overrides):
        fields = {
            "club": self.club,
            "course": self.course,
            "full_name": "山田",
            "email": "guest@example.com",
            "reservation_date": when,
            "start_time": time(10, 0),
            "end_time": time(11, 0),
            "reservation_key": key,
            "status": ServiceReservation.Status.PAID,
            "amount": 5000,
            "payment_method": "stripe",
            "paid_at": timezone.now(),
        }
        fields.update(overrides)
        return ServiceReservation.objects.create(**fields)

    def test_index_is_one_query_and_skips_unconfirmed(self):
        self.book(when=date(2026, 11, 2), key="paid-nov")
        self.book(
            when=date(2026, 11, 3),
            key="cash-nov",
            payment_method="cash",
            paid_at=None,
            start_time=time(12, 0),
        )
        self.book(when=date(2026, 10, 4), key="paid-oct")
        self.book(
            when=date(2026, 11, 5),
            key="draft",
            status=ServiceReservation.Status.NOT_STARTED,
            paid_at=None,
        )
        self.book(when=date(2026, 11, 6), key="gone", is_deleted=True)

        with self.assertNumQueries(1):
            months = service_reservation_history_index(self.club)

        self.assertEqual(
            months,
            [
                {"year": 2026, "month": 11, "reservation_count": 2},
                {"year": 2026, "month": 10, "reservation_count": 1},
            ],
        )

    def test_month_puts_paid_before_unpaid_and_sums_the_day(self):
        self.book(
            when=date(2026, 11, 18),
            key="late-paid",
            start_time=time(15, 0),
            end_time=time(16, 0),
            amount=8000,
            full_name="午後",
        )
        self.book(
            when=date(2026, 11, 18),
            key="morning-unpaid",
            start_time=time(9, 0),
            end_time=time(10, 0),
            payment_method="cash",
            paid_at=None,
            amount=3000,
            full_name="午前",
        )
        self.book(
            when=date(2026, 11, 18),
            key="refunded",
            start_time=time(11, 0),
            end_time=time(12, 0),
            amount=4000,
            refunded_at=timezone.now(),
            full_name="返金",
        )
        self.book(when=date(2026, 11, 2), key="other-day", amount=1000)

        with self.assertNumQueries(1):
            detail = service_reservation_history_month(self.club, 2026, 11)

        self.assertEqual(detail["earned_yen"], 9000)
        self.assertEqual(
            [day["date"] for day in detail["days"]],
            ["2026-11-18", "2026-11-02"],
        )
        day = detail["days"][0]
        self.assertEqual(day["weekday"], "水")
        self.assertEqual(day["earned_yen"], 8000)
        self.assertEqual(
            [row["full_name"] for row in day["paid"]],
            ["返金", "午後"],
        )
        self.assertEqual(
            [row["full_name"] for row in day["unpaid"]],
            ["午前"],
        )
        self.assertTrue(day["paid"][0]["paid"])
        self.assertTrue(day["paid"][0]["refunded"])

    def test_owner_loads_months_then_one_month(self):
        self.book(when=date(2026, 11, 18), key="api-paid", amount=5000)
        self.client.force_authenticate(self.owner)

        index = self.client.get(
            "/api/clubs/by-subdomain/historyclub/service-reservation-history/"
        )
        self.assertEqual(index.status_code, 200)
        self.assertEqual(index.json()["months"][0]["reservation_count"], 1)

        month = self.client.get(
            "/api/clubs/by-subdomain/historyclub/service-reservation-history/",
            {"year": 2026, "month": 11},
        )
        self.assertEqual(month.status_code, 200)
        body = month.json()
        self.assertEqual(body["earned_yen"], 5000)
        self.assertEqual(len(body["days"][0]["paid"]), 1)

    def test_stranger_cannot_read_history(self):
        stranger = CustomUser.objects.create_user(
            username="historystranger",
            email="stranger@example.com",
            password="pass123",
        )
        self.client.force_authenticate(stranger)
        response = self.client.get(
            "/api/clubs/by-subdomain/historyclub/service-reservation-history/"
        )
        self.assertEqual(response.status_code, 403)
