from datetime import timedelta
from unittest.mock import patch

from django.db.models.signals import post_save
from django.test import Client, TestCase
from django.utils import timezone
from freezegun import freeze_time

from accounts.models import CustomUser
from kaibaru.models import (
    Club,
    Invoice,
    Member,
    MembershipPlan,
    Subscription,
    SubscriptionItem,
    TicketGrant,
    TicketType,
)
from kaibaru.service_cycle_invoice_create import (
    CashSubscriptionCycleInvoiceService,
)
from kaibaru.signals import club_created_signal


class CycleTicketGrantTests(TestCase):
    def setUp(self):
        post_save.disconnect(club_created_signal, sender=Club)
        self.addCleanup(
            lambda: post_save.connect(club_created_signal, sender=Club)
        )

        self.owner = CustomUser.objects.create_user(
            username="cycle-ticket-owner",
            email="cycle-ticket@example.com",
            password="pass123",
        )
        self.club = Club.objects.create(
            owner=self.owner,
            subdomain="cycle-ticket-club",
            stripe_account_id="acct_cycle_ticket",
        )
        self.member = Member.objects.create(
            owner=self.owner,
            club=self.club,
            full_name="Cycle Ticket Member",
        )
        self.ticket_type = TicketType.objects.create(
            club=self.club,
            name="Lesson ticket",
        )
        self.plan = MembershipPlan.objects.create(
            club=self.club,
            name="Monthly tickets",
            plan_type=MembershipPlan.PlanType.TICKET_PLAN,
            ticket_type=self.ticket_type,
            ticket_quantity=6,
            ticket_expiration_mode=(
                MembershipPlan.TicketExpirationMode.DAYS_AFTER_GRANT
            ),
            ticket_expiration_days=10,
            price=6000,
            interval="month",
            stripe_price_id="price_cycle_ticket",
        )

    def make_subscription(self, *, billing_method, stripe_id=None):
        subscription = Subscription.objects.create(
            owner=self.owner,
            club=self.club,
            billing_method=billing_method,
            billing_mode="regular",
            billing_anchor_day=20,
            status="active",
            stripe_subscription_id=stripe_id,
            current_period_end=timezone.now() - timedelta(days=1),
            access_until=timezone.now() - timedelta(days=1),
        )
        SubscriptionItem.objects.create(
            subscription=subscription,
            member=self.member,
            plan=self.plan,
            price_at_subscription=self.plan.price,
            stripe_price_id_at_subscription=self.plan.stripe_price_id,
            stripe_subscription_item_id=(
                "si_cycle_ticket" if stripe_id else None
            ),
        )
        return subscription

    @freeze_time("2026-09-21 12:00:00")
    def test_cash_cycle_invoice_grants_full_cycle_tickets(self):
        subscription = self.make_subscription(billing_method="cash")
        original_period_end = subscription.current_period_end

        result = CashSubscriptionCycleInvoiceService.create(
            subscription_id=subscription.id,
        )

        grant = TicketGrant.objects.get(
            member=self.member,
            source=TicketGrant.Source.SUBSCRIPTION,
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["ticket_grant_count"], 1)
        self.assertEqual(grant.ticket_type, self.ticket_type)
        self.assertEqual(grant.quantity, 6)
        self.assertEqual(
            grant.expires_at,
            timezone.now() + timedelta(days=10),
        )

        # Retrying the task returns the existing invoice and cannot
        # grant a second batch for the same billing cycle.
        subscription.current_period_end = original_period_end
        subscription.save(update_fields=["current_period_end"])
        retry = CashSubscriptionCycleInvoiceService.create(
            subscription_id=subscription.id,
        )
        self.assertTrue(retry["already_exists"])
        self.assertEqual(TicketGrant.objects.count(), 1)

    @freeze_time("2026-09-21 12:00:00")
    @patch("kaibaru.stripe_webhooks.stripe.Invoice.retrieve")
    @patch("kaibaru.stripe_webhooks.stripe.Webhook.construct_event")
    def test_stripe_invoice_created_grants_full_cycle_tickets(
        self,
        mock_construct_event,
        mock_invoice_retrieve,
    ):
        subscription = self.make_subscription(
            billing_method="stripe",
            stripe_id="sub_cycle_ticket",
        )
        period_end = int(
            (timezone.now() + timedelta(days=30)).timestamp()
        )
        stripe_invoice = {
            "id": "in_cycle_ticket",
            "subscription": "sub_cycle_ticket",
            "customer": "cus_cycle_ticket",
            "billing_reason": "subscription_cycle",
            "amount_due": 6000,
            "amount_paid": 0,
            "currency": "jpy",
            "lines": {
                "data": [
                    {
                        "description": "Monthly tickets",
                        "amount": 6000,
                        "quantity": 1,
                        "metadata": {"member_id": str(self.member.id)},
                        "period": {"end": period_end},
                    }
                ]
            },
        }
        event = {
            "id": "evt_cycle_ticket",
            "type": "invoice.created",
            "account": self.club.stripe_account_id,
            "data": {"object": stripe_invoice},
        }
        mock_construct_event.return_value = event
        mock_invoice_retrieve.return_value = stripe_invoice

        response = Client().post(
            "/stripe_webhook/connected/",
            data="{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="testsig",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            Invoice.objects.filter(
                subscription=subscription,
                stripe_invoice_id="in_cycle_ticket",
            ).exists()
        )
        grant = TicketGrant.objects.get(
            member=self.member,
            source=TicketGrant.Source.SUBSCRIPTION,
        )
        self.assertEqual(grant.ticket_type, self.ticket_type)
        self.assertEqual(grant.quantity, 6)
        self.assertEqual(
            grant.expires_at,
            timezone.now() + timedelta(days=10),
        )

        # A Stripe redelivery of the same event is idempotent.
        response = Client().post(
            "/stripe_webhook/connected/",
            data="{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="testsig",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(TicketGrant.objects.count(), 1)
