"""Reservation history lists months first, then one month of bookings."""
from datetime import date, time, timedelta

from django.db.models.signals import post_save
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import CustomUser
from kaibaru.cash_reservations import CashReservationError
from kaibaru.models import Club, ServiceCourse, ServiceReservation
from kaibaru.service_reservation_history import (
    service_reservation_history_index,
    service_reservation_history_month,
)
from kaibaru.service_reservation_manage import (
    _reopen,
    list_manageable_reservations,
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
            months = service_reservation_history_index(
                self.club, today=date(2026, 11, 18)
            )

        self.assertEqual(
            months,
            [
                {
                    "year": 2026,
                    "month": 11,
                    "reservation_count": 2,
                    "is_current": True,
                },
                {
                    "year": 2026,
                    "month": 10,
                    "reservation_count": 1,
                    "is_current": False,
                },
            ],
        )

    def test_index_includes_the_current_month_when_it_has_no_bookings(self):
        self.book(when=date(2026, 10, 4), key="only-oct")

        months = service_reservation_history_index(
            self.club, today=date(2026, 11, 18)
        )

        self.assertEqual(months[0]["year"], 2026)
        self.assertEqual(months[0]["month"], 11)
        self.assertEqual(months[0]["reservation_count"], 0)
        self.assertTrue(months[0]["is_current"])

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

    def test_current_month_includes_paid_bookings_on_a_later_date(self):
        later = date(2026, 11, 28)
        self.book(when=later, key="later-paid", amount=7000, full_name="来週")

        detail = service_reservation_history_month(
            self.club, 2026, 11, today=date(2026, 11, 18)
        )

        day = next(row for row in detail["days"] if row["date"] == "2026-11-28")
        self.assertEqual(day["paid"][0]["full_name"], "来週")
        self.assertEqual(day["earned_yen"], 7000)

    def test_manage_board_drops_cash_paid_and_canceled(self):
        today = timezone.localdate()
        open_row = self.book(
            when=today,
            key="board-open",
            payment_method="cash",
            paid_at=None,
            amount=3000,
        )
        self.book(
            when=today + timedelta(days=1),
            key="board-paid",
            payment_method="cash",
            paid_at=timezone.now(),
            amount=3000,
            start_time=time(12, 0),
            end_time=time(13, 0),
        )
        self.book(
            when=today + timedelta(days=2),
            key="board-canceled",
            canceled=True,
            start_time=time(14, 0),
            end_time=time(15, 0),
        )
        future_paid = self.book(
            when=today + timedelta(days=3),
            key="board-future-card",
            amount=4000,
            start_time=time(16, 0),
            end_time=time(17, 0),
        )

        ids = [
            row["id"]
            for row in list_manageable_reservations(self.club, self.owner)
        ]
        self.assertIn(open_row.id, ids)
        self.assertIn(future_paid.id, ids)
        self.assertEqual(len(ids), 2)

    def test_reopen_until_the_day_after_the_reservation(self):
        when = timezone.localdate() + timedelta(days=2)
        paid = self.book(
            when=when,
            key="reopen-paid",
            payment_method="cash",
            paid_at=timezone.now(),
            amount=3000,
        )
        canceled = self.book(
            when=when,
            key="reopen-canceled",
            canceled=True,
            start_time=time(12, 0),
            end_time=time(13, 0),
        )

        open_day = service_reservation_history_month(
            self.club, when.year, when.month, today=when
        )
        flags = {
            row["id"]: row["can_reopen"]
            for day in open_day["days"]
            for group in ("paid", "unpaid")
            for row in day[group]
        }
        self.assertTrue(flags[paid.id])
        self.assertTrue(flags[canceled.id])

        locked = service_reservation_history_month(
            self.club,
            when.year,
            when.month,
            today=when + timedelta(days=1),
        )
        locked_flags = {
            row["id"]: row["can_reopen"]
            for day in locked["days"]
            for group in ("paid", "unpaid")
            for row in day[group]
        }
        self.assertFalse(locked_flags[paid.id])
        self.assertFalse(locked_flags[canceled.id])

        _reopen(paid, today=when)
        paid.refresh_from_db()
        self.assertIsNone(paid.paid_at)
        self.assertIn(
            paid.id,
            [
                row["id"]
                for row in list_manageable_reservations(self.club, self.owner)
            ],
        )

        with self.assertRaises(CashReservationError):
            _reopen(canceled, today=when + timedelta(days=1))
        canceled.refresh_from_db()
        self.assertTrue(canceled.canceled)

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
