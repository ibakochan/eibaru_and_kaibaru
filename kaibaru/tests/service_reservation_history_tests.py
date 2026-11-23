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
    mark_stale_open_service_reservations_paid,
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
                    "reservation_count": 1,
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
        self.assertNotIn("午前", [row["full_name"] for row in day["paid"]])
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

    def test_day_lists_paid_then_canceled_then_no_shows(self):
        when = date(2026, 11, 10)
        self.book(when=when, key="group-paid", amount=2000, full_name="支払")
        self.book(
            when=when,
            key="group-cancel",
            canceled=True,
            start_time=time(11, 0),
            end_time=time(12, 0),
            full_name="取消",
        )
        self.book(
            when=when,
            key="group-noshow",
            payment_method="cash",
            paid_at=None,
            no_show=True,
            amount=1500,
            start_time=time(13, 0),
            end_time=time(14, 0),
            full_name="欠席者",
        )

        detail = service_reservation_history_month(
            self.club, 2026, 11, today=when
        )
        day = detail["days"][0]
        self.assertEqual([row["full_name"] for row in day["paid"]], ["支払"])
        self.assertEqual([row["full_name"] for row in day["canceled"]], ["取消"])
        self.assertEqual([row["full_name"] for row in day["no_shows"]], ["欠席者"])
        self.assertEqual(day["earned_yen"], 2000)

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
        self.book(
            when=today + timedelta(days=4),
            key="board-noshow",
            payment_method="cash",
            paid_at=None,
            no_show=True,
            amount=2500,
            start_time=time(18, 0),
            end_time=time(19, 0),
        )

        rows = list_manageable_reservations(self.club, self.owner)
        ids = [row["id"] for row in rows]
        self.assertIn(open_row.id, ids)
        self.assertNotIn(future_paid.id, ids)
        self.assertEqual(len(ids), 1)
        open_payload = next(row for row in rows if row["id"] == open_row.id)
        self.assertTrue(open_payload["can_mark_paid"])
        self.assertTrue(open_payload["can_cancel"])
        self.assertTrue(open_payload["can_mark_no_show"])

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
        missed = self.book(
            when=when,
            key="reopen-noshow",
            payment_method="cash",
            paid_at=None,
            no_show=True,
            amount=1500,
            start_time=time(14, 0),
            end_time=time(15, 0),
        )

        open_day = service_reservation_history_month(
            self.club, when.year, when.month, today=when
        )
        flags = {
            row["id"]: row["can_reopen"]
            for day in open_day["days"]
            for group in ("paid", "canceled", "no_shows")
            for row in day[group]
        }
        self.assertTrue(flags[paid.id])
        self.assertTrue(flags[canceled.id])
        self.assertTrue(flags[missed.id])

        locked = service_reservation_history_month(
            self.club,
            when.year,
            when.month,
            today=when + timedelta(days=1),
        )
        locked_flags = {
            row["id"]: row["can_reopen"]
            for day in locked["days"]
            for group in ("paid", "canceled", "no_shows")
            for row in day[group]
        }
        self.assertFalse(locked_flags[paid.id])
        self.assertFalse(locked_flags[canceled.id])
        self.assertFalse(locked_flags[missed.id])

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

        _reopen(missed, today=when)
        missed.refresh_from_db()
        self.assertFalse(missed.no_show)
        self.assertIn(
            missed.id,
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

    def test_open_cash_booking_is_marked_paid_on_the_next_date(self):
        now = timezone.now()
        today = timezone.localdate()
        stale_local = timezone.localtime(now).replace(
            year=(today - timedelta(days=1)).year,
            month=(today - timedelta(days=1)).month,
            day=(today - timedelta(days=1)).day,
            hour=23,
            minute=15,
            second=0,
            microsecond=0,
        )
        fresh_local = timezone.localtime(now).replace(
            hour=23,
            minute=15,
            second=0,
            microsecond=0,
        )

        def at(local, key, **overrides):
            end = time(23, 59) if local.time() < time(23, 59) else local.time()
            return self.book(
                when=local.date(),
                key=key,
                start_time=local.time(),
                end_time=end,
                **overrides,
            )

        stale = at(
            stale_local,
            "auto-stale",
            payment_method="cash",
            paid_at=None,
            amount=3000,
        )
        fresh = at(
            fresh_local,
            "auto-fresh",
            payment_method="cash",
            paid_at=None,
            amount=3000,
        )
        canceled = at(
            stale_local,
            "auto-canceled",
            payment_method="cash",
            paid_at=None,
            canceled=True,
            amount=3000,
        )
        missed = at(
            stale_local,
            "auto-noshow",
            payment_method="cash",
            paid_at=None,
            no_show=True,
            amount=3000,
        )
        already_paid_at = now - timedelta(days=3)
        already = at(
            stale_local,
            "auto-paid",
            payment_method="cash",
            paid_at=already_paid_at,
            amount=3000,
        )
        ticket = at(
            stale_local,
            "auto-ticket",
            payment_method="ticket",
            paid_at=None,
            amount=3000,
        )

        marked = mark_stale_open_service_reservations_paid(now=now)

        stale.refresh_from_db()
        fresh.refresh_from_db()
        canceled.refresh_from_db()
        missed.refresh_from_db()
        already.refresh_from_db()
        ticket.refresh_from_db()
        self.assertEqual(marked, 1)
        self.assertIsNotNone(stale.paid_at)
        self.assertIsNone(fresh.paid_at)
        self.assertIsNone(canceled.paid_at)
        self.assertIsNone(missed.paid_at)
        self.assertIsNotNone(already.paid_at)
        self.assertNotEqual(already.paid_at, stale.paid_at)
        self.assertIsNone(ticket.paid_at)
        self.assertFalse(stale.platform_fee_applied)

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
