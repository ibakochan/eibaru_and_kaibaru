from datetime import date, time
from types import SimpleNamespace
from unittest.mock import patch

from django.db.models.signals import post_save
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from kaibaru.models import (
    Club,
    Lesson,
    Member,
    Reservation,
    TicketGrant,
    TicketType,
    TicketUsage,
)
from kaibaru.signals import club_created_signal


class ReservationRefundTests(TestCase):
    def setUp(self):
        post_save.disconnect(club_created_signal, sender=Club)
        self.addCleanup(
            lambda: post_save.connect(club_created_signal, sender=Club)
        )

        self.owner = CustomUser.objects.create_user(
            username="refundowner",
            email="owner@example.com",
            password="pass123",
        )
        self.other = CustomUser.objects.create_user(
            username="refundother",
            email="other@example.com",
            password="pass123",
        )
        self.club = Club.objects.create(
            owner=self.owner,
            subdomain="refundclub",
            stripe_account_id="acct_refund",
            stripe_anchor_date=20,
        )
        self.lesson = Lesson.objects.create(
            club=self.club,
            title="Yoga",
            weekday=1,
            start_time=time(10, 0),
            end_time=time(11, 0),
        )
        self.member = Member.objects.create(
            owner=self.owner,
            club=self.club,
            full_name="会員",
            gender="female",
            birth_date=date(1990, 1, 1),
        )
        self.client = APIClient()

    def make_reservation(self, **kwargs):
        defaults = dict(
            lesson=self.lesson,
            club=self.club,
            member=self.member,
            reservation_type=Reservation.ReservationType.MEMBER,
            status=Reservation.Status.PAID,
            payment_method=Reservation.PaymentMethod.STRIPE,
            full_name="会員",
            email="member@example.com",
            amount=2000,
            reservation_date=date(2026, 9, 29),
            reservation_key="refund-stripe",
            stripe_payment_intent_id="pi_refund",
        )
        defaults.update(kwargs)
        return Reservation.objects.create(**defaults)

    def test_instructor_cannot_refund(self):
        reservation = self.make_reservation()
        self.client.force_authenticate(self.other)

        response = self.client.post(
            f"/api/reservations/refund/{reservation.id}/"
        )

        self.assertEqual(response.status_code, 403)
        reservation.refresh_from_db()
        self.assertIsNone(reservation.refunded_at)

    def test_unpaid_reservation_cannot_be_refunded(self):
        reservation = self.make_reservation(
            status=Reservation.Status.UNPAID,
            reservation_key="refund-unpaid",
            stripe_payment_intent_id="pi_unpaid",
        )
        self.client.force_authenticate(self.owner)

        response = self.client.post(
            f"/api/reservations/refund/{reservation.id}/"
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("支払い済み", response.data["detail"])

    @patch("kaibaru.service_reservation_refund.stripe.Refund.create")
    @patch("kaibaru.service_reservation_refund.stripe.Refund.list")
    def test_stripe_refund_is_idempotent(
        self,
        refund_list,
        refund_create,
    ):
        refund_list.return_value = SimpleNamespace(data=[])
        refund_create.return_value = SimpleNamespace(
            id="re_new",
            status="succeeded",
        )
        reservation = self.make_reservation()
        self.client.force_authenticate(self.owner)

        preview = self.client.get(
            f"/api/reservations/refund/{reservation.id}/"
        )
        self.assertEqual(preview.status_code, 200)
        self.assertIn(
            "Stripeが引いた手数料と、Kaibaruが受け取った1%は戻りません。",
            preview.data["explanation"],
        )

        first = self.client.post(
            f"/api/reservations/refund/{reservation.id}/"
        )
        second = self.client.post(
            f"/api/reservations/refund/{reservation.id}/"
        )

        self.assertEqual(first.status_code, 200)
        self.assertFalse(first.data["already_refunded"])
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.data["already_refunded"])
        self.assertEqual(refund_create.call_count, 1)
        refund_create.assert_called_with(
            payment_intent="pi_refund",
            amount=2000,
            metadata={"reservation_id": str(reservation.id)},
            stripe_account="acct_refund",
            idempotency_key=f"reservation_refund_{reservation.id}",
        )

        reservation.refresh_from_db()
        self.assertIsNotNone(reservation.refunded_at)
        self.assertTrue(reservation.canceled)
        self.assertEqual(reservation.stripe_refund_id, "re_new")

    @patch("kaibaru.service_reservation_refund.stripe.Refund.create")
    @patch("kaibaru.service_reservation_refund.stripe.Refund.list")
    def test_existing_stripe_refund_is_reused(
        self,
        refund_list,
        refund_create,
    ):
        reservation = self.make_reservation(
            reservation_key="refund-existing",
            stripe_payment_intent_id="pi_existing",
        )
        refund_list.return_value = SimpleNamespace(
            data=[
                SimpleNamespace(
                    id="re_existing",
                    status="succeeded",
                    metadata={"reservation_id": str(reservation.id)},
                )
            ]
        )
        self.client.force_authenticate(self.owner)

        response = self.client.post(
            f"/api/reservations/refund/{reservation.id}/"
        )

        self.assertEqual(response.status_code, 200)
        refund_create.assert_not_called()
        reservation.refresh_from_db()
        self.assertEqual(reservation.stripe_refund_id, "re_existing")

    def test_ticket_refund_returns_the_same_quantity_once(self):
        ticket_type = TicketType.objects.create(
            club=self.club,
            name="yoga",
        )
        grant = TicketGrant.objects.create(
            member=self.member,
            ticket_type=ticket_type,
            source=TicketGrant.Source.PURCHASE,
            quantity=3,
        )
        reservation = self.make_reservation(
            payment_method=Reservation.PaymentMethod.TICKET,
            amount=0,
            reservation_key="refund-ticket",
            stripe_payment_intent_id=None,
        )
        TicketUsage.objects.create(
            grant=grant,
            reservation=reservation,
            quantity=1,
        )
        self.client.force_authenticate(self.owner)

        preview = self.client.get(
            f"/api/reservations/refund/{reservation.id}/"
        )
        self.assertIn("「yoga」を1枚", preview.data["explanation"])

        first = self.client.post(
            f"/api/reservations/refund/{reservation.id}/"
        )
        second = self.client.post(
            f"/api/reservations/refund/{reservation.id}/"
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.data["already_refunded"])

        usage = TicketUsage.objects.get(reservation=reservation)
        self.assertIsNotNone(usage.refunded_at)
        self.assertEqual(usage.quantity, 1)
        self.assertEqual(
            TicketUsage.objects.filter(
                grant=grant,
                refunded_at__isnull=True,
            ).count(),
            0,
        )
        reservation.refresh_from_db()
        self.assertTrue(reservation.canceled)
