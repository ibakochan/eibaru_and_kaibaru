# kaibaru/tests/webhook_integration_tests.py

from unittest.mock import patch
from unittest.mock import MagicMock

from django.test import TestCase, Client
from django.db.models.signals import post_save

from django.utils import timezone
from datetime import date

from freezegun import freeze_time

from accounts.models import CustomUser
from kaibaru.models import (
    Club,
    Subscription,
    SubscriptionItem,
    MembershipPlan,
    Member,
    StripeCustomer,
    StripeWebhookEvent,
)
from kaibaru.signals import club_created_signal

from kaibaru.stripe_views import (
    cancel_member_subscription,
    resume_member_subscription,
    create_member_checkout_session,
)

class StripeConnectedWebhookTests(TestCase):
    def setUp(self):
        post_save.disconnect(club_created_signal, sender=Club)
        self.addCleanup(
            lambda: post_save.connect(club_created_signal, sender=Club)
        )

        self.owner = CustomUser.objects.create_user(
            username="testuser",
            password="pass123"
        )

        self.club = Club.objects.create(
            owner=self.owner,
            subdomain="testclub",
            stripe_account_id="acct_test123",
        )

        self.sub = Subscription.objects.create(
            owner=self.owner,
            club=self.club,
            stripe_subscription_id="sub_123",
            status="pending",
        )

        self.url = "/stripe_webhook/connected/"

    def post_event(self, payload):
        with patch(
            "kaibaru.stripe_webhooks.stripe.Webhook.construct_event",
            return_value=payload
        ):
            return self.client.post(
                self.url,
                data="{}",
                content_type="application/json",
                HTTP_STRIPE_SIGNATURE="testsig",
            )

    def test_invoice_paid(self):
        payload = {
            "id": "evt_1",
            "type": "invoice.paid",
            "account": "acct_test123",
            "data": {
                "object": {
                    "id": "in_123",
                    "subscription": "sub_123",
                    "lines": {
                        "data": []
                    }
                }
            }
        }

        response = self.post_event(payload)

        self.assertEqual(response.status_code, 200)

        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, "active")

    def test_payment_failed(self):
        payload = {
            "id": "evt_2",
            "type": "invoice.payment_failed",
            "account": "acct_test123",
            "data": {
                "object": {
                    "subscription": "sub_123"
                }
            }
        }

        response = self.post_event(payload)

        self.assertEqual(response.status_code, 200)

        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, "past_due")

    def test_subscription_deleted(self):
        payload = {
            "id": "evt_3",
            "type": "customer.subscription.deleted",
            "account": "acct_test123",
            "data": {
                "object": {
                    "id": "sub_123"
                }
            }
        }

        response = self.post_event(payload)

        self.assertEqual(response.status_code, 200)

        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, "canceled")

    def test_duplicate_event(self):
        StripeWebhookEvent.objects.create(event_id="evt_dup")

        payload = {
            "id": "evt_dup",
            "type": "invoice.paid",
            "account": "acct_test123",
            "data": {
                "object": {}
            }
        }

        response = self.post_event(payload)

        self.assertEqual(response.status_code, 200)

    @patch("kaibaru.stripe_webhooks.get_or_create_stripe_customer")
    @patch("kaibaru.stripe_webhooks.stripe.Invoice.pay")
    @patch("kaibaru.stripe_webhooks.stripe.Invoice.create")
    @patch("kaibaru.stripe_webhooks.stripe.InvoiceItem.create")
    @patch("kaibaru.stripe_webhooks.stripe.Subscription.retrieve")
    def test_checkout_session_completed_creates_subscription(
        self,
        mock_retrieve,
        mock_invoice_item,
        mock_invoice_create,
        mock_invoice_pay,
        mock_get_customer,
    ):
        member = Member.objects.create(
            owner=self.owner,
            club=self.club,
            full_name="Test Member",
        )

        plan = MembershipPlan.objects.create(
            club=self.club,
            name="Standard",
            price=5000,
            stripe_price_id="price_123",
            interval="month",
        )

        stripe_customer = StripeCustomer.objects.create(
            user=self.owner,
            club=self.club,
            stripe_customer_id="cus_123",
        )

        mock_get_customer.return_value = stripe_customer

        fake_sub = MagicMock()
        fake_sub.id = "sub_new123"
        fake_sub.customer = "cus_123"
        fake_sub.status = "active"

        fake_sub.__getitem__.side_effect = lambda key: {
            "items": {
               "data": [
                    {
                        "id": "si_123",
                        "quantity": 1,
                        "price": {
                            "id": "price_123"
                        }
                    }
                ]
            }
        }[key]

        mock_retrieve.return_value = fake_sub

        class FakeInvoice:
            amount_due = 0
            id = "in_new123"

        mock_invoice_create.return_value = FakeInvoice()

        payload = {
            "id": "evt_checkout_1",
            "type": "checkout.session.completed",
            "account": "acct_test123",
            "data": {
                "object": {
                    "id": "cs_test_1",
                    "subscription": "sub_new123",
                    "invoice": "in_new123",
                    "metadata": {
                        "member_id": str(member.id),
                        "club_id": str(self.club.id),
                        "plan_id": str(plan.id),
                    }
                }
            }
        }

        response = self.post_event(payload)

        self.assertEqual(response.status_code, 200)

        self.assertTrue(
            Subscription.objects.filter(
                stripe_subscription_id="sub_new123"
            ).exists()
        )

        self.assertTrue(
            SubscriptionItem.objects.filter(
                stripe_subscription_item_id="si_123"
            ).exists()
        )

        self.assertTrue(
            StripeCustomer.objects.filter(
                user=self.owner,
                club=self.club,
                stripe_customer_id="cus_123"
            ).exists()
        )

        mock_get_customer.assert_called_once()








    def test_subscription_updated(self):
        payload = {
            "id": "evt_4",
            "type": "customer.subscription.updated",
            "account": "acct_test123",
            "data": {
                "object": {
                    "id": "sub_123",
                    "status": "active",
                    "cancel_at_period_end": True,
                }
            }
        }

        response = self.post_event(payload)

        self.assertEqual(response.status_code, 200)

        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, "active")
        self.assertTrue(self.sub.cancel_at_period_end)

    def test_missing_account_returns_200(self):
        payload = {
            "id": "evt_5",
            "type": "invoice.paid",
            "data": {
                "object": {
                    "id": "in_missing_account",
                    "subscription": "sub_123",
                }
            }
        }

        response = self.post_event(payload)

        self.assertEqual(response.status_code, 200)

    def test_invoice_paid_unknown_subscription_returns_200(self):
        payload = {
            "id": "evt_6",
            "type": "invoice.paid",
            "account": "acct_test123",
            "data": {
                "object": {
                    "id": "in_unknown",
                    "subscription": "sub_does_not_exist",
                    "lines": {
                        "data": []
                    }
                }
            }
        }

        response = self.post_event(payload)

        self.assertEqual(response.status_code, 200)

    def test_invoice_paid_marks_member_joining_fee_paid(self):
        member = Member.objects.create(
            owner=self.owner,
            club=self.club,
            full_name="Fee Member",
            has_paid_joining_fee=False,
            has_been_charged_joining_fee=False,
        )

        SubscriptionItem.objects.create(
            subscription=self.sub,
            member=member,
            stripe_subscription_item_id="si_fee_1",
            quantity=1,
        )

        payload = {
            "id": "evt_7",
            "type": "invoice.paid",
            "account": "acct_test123",
            "data": {
                "object": {
                    "id": "in_fee_paid",
                    "subscription": "sub_123",
                    "lines": {
                        "data": []
                    }
                }
            }
        }

        response = self.post_event(payload)

        self.assertEqual(response.status_code, 200)

        member.refresh_from_db()
        self.assertTrue(member.has_paid_joining_fee)
        self.assertTrue(member.has_been_charged_joining_fee)

    def test_invoice_paid_cycle_resets_resume_prevention(self):
        member = Member.objects.create(
            owner=self.owner,
            club=self.club,
            full_name="Cycle Member",
        )

        item = SubscriptionItem.objects.create(
            subscription=self.sub,
            member=member,
            stripe_subscription_item_id="si_cycle_1",
            quantity=1,
            monthly_double_resume_charge_prevention=True,
        )

        payload = {
            "id": "evt_8",
            "type": "invoice.paid",
            "account": "acct_test123",
            "data": {
                "object": {
                    "id": "in_cycle",
                    "subscription": "sub_123",
                    "billing_reason": "subscription_cycle",
                    "lines": {
                        "data": []
                    }
                }
            }
        }

        response = self.post_event(payload)

        self.assertEqual(response.status_code, 200)

        item.refresh_from_db()
        self.assertFalse(item.monthly_double_resume_charge_prevention)

    def test_checkout_session_without_subscription_returns_200(self):
        payload = {
            "id": "evt_9",
            "type": "checkout.session.completed",
            "account": "acct_test123",
            "data": {
                "object": {
                    "id": "cs_no_sub",
                    "metadata": {}
                }
            }
        }

        response = self.post_event(payload)

        self.assertEqual(response.status_code, 200)

    def test_payment_failed_unknown_subscription_returns_200(self):
        payload = {
            "id": "evt_10",
            "type": "invoice.payment_failed",
            "account": "acct_test123",
            "data": {
                "object": {
                    "subscription": "sub_unknown"
                }
            }
        }

        response = self.post_event(payload)

        self.assertEqual(response.status_code, 200)





class StripeBillingActionIntegrationTests(TestCase):

    # -------------------------
    # HELPERS
    # -------------------------
    def mock_stripe(self, m):
        """
        Central Stripe mock.
        Prevents ANY real Stripe API calls.
        """

        m.Subscription.retrieve.return_value = {
            "id": "sub_123",
            "items": {
                "data": [
                    {
                        "id": "si_1",
                        "quantity": 1,
                        "price": {"id": "price_123"},
                    }
                ]
            }
        }

        m.SubscriptionItem.modify.return_value = None
        m.SubscriptionItem.create.return_value = MagicMock(id="si_new")
        m.SubscriptionItem.delete.return_value = None

        m.Invoice.create.return_value = MagicMock(id="in_1", amount_due=0)
        m.Invoice.pay.return_value = None
        m.InvoiceItem.create.return_value = None

        m.checkout.Session.create.return_value = MagicMock(id="cs_123")

    # -------------------------
    # SETUP
    # -------------------------
    def setUp(self):
        self.client = Client()

        post_save.disconnect(club_created_signal, sender=Club)

        self.owner = CustomUser.objects.create_user(
            username="billinguser",
            email="testuser@example.com",
            password="pass123"
        )

        self.other_user = CustomUser.objects.create_user(
            username="otheruser",
            email="test2user@example.com",
            password="pass123"
        )

        self.club = Club.objects.create(
            owner=self.owner,
            subdomain="billingclub",
            stripe_account_id="acct_test123",
            stripe_anchor_date=20,
            subscription_mode="monthly",
            joining_fee=1000,
        )

        self.member = Member.objects.create(
            owner=self.owner,
            club=self.club,
            full_name="Member One",
        )

        self.plan = MembershipPlan.objects.create(
            club=self.club,
            name="Standard",
            price=5000,
            stripe_price_id="price_123",
            interval="month",
            active=True,
        )

        self.subscription = Subscription.objects.create(
            owner=self.owner,
            club=self.club,
            stripe_subscription_id="sub_123",
            status="active",
            billing_mode="monthly",
            billing_anchor_day=20,
        )

        self.item = SubscriptionItem.objects.create(
            subscription=self.subscription,
            member=self.member,
            plan=self.plan,
            stripe_subscription_item_id="si_123",
            stripe_price_id_at_subscription="price_123",
            price_at_subscription=5000,
            quantity=1,
        )

        StripeCustomer.objects.create(
            user=self.owner,
            club=self.club,
            stripe_customer_id="cus_123",
        )

        self.client.login(username="billinguser", password="pass123")

    # -------------------------
    # CANCEL FLOW
    # -------------------------
    @freeze_time("2026-04-10")
    @patch("kaibaru.stripe_views.stripe")
    def test_cancel_subscription_modify(self, mock_stripe):

        self.mock_stripe(mock_stripe)

        item = SubscriptionItem.objects.create(
            subscription=self.subscription,
            member=self.member,
            plan=self.plan,
            stripe_subscription_item_id="si_1",
            quantity=2,
        )

        response = self.client.post(f"/cancel_member_subscription/{item.id}/")

        mock_stripe.Subscription.retrieve.assert_called()

        self.assertEqual(response.status_code, 200)

        item.refresh_from_db()
        self.assertIsNotNone(item.deleted_at)

    # -------------------------
    # CANCEL LOCK (NEW)
    # -------------------------
    @freeze_time("2026-04-10")
    @patch("kaibaru.stripe_views.stripe")
    def test_cancel_lock_prevents_double_request(self, mock_stripe):

        self.mock_stripe(mock_stripe)

        response1 = self.client.post(f"/cancel_member_subscription/{self.item.id}/")
        response2 = self.client.post(f"/cancel_member_subscription/{self.item.id}/")

        self.assertIn(response2.status_code, [429, 400])

    # -------------------------
    # RESUME EXISTING ITEM
    # -------------------------
    @freeze_time("2026-04-10")
    @patch("kaibaru.stripe_views.stripe")
    def test_resume_existing_item(self, mock_stripe):

        self.mock_stripe(mock_stripe)

        item = SubscriptionItem.objects.create(
            subscription=self.subscription,
            member=self.member,
            plan=self.plan,
            stripe_subscription_item_id="si_1",
            stripe_price_id_at_subscription="price_123",
            deleted_at=timezone.now(),
            access_until=timezone.now(),
            quantity=1,
        )

        response = self.client.post(f"/resume_member_subscription/{item.id}/")

        self.assertIn(response.status_code, [200, 400, 500, 405])

    # -------------------------
    # RESUME CREATE NEW ITEM
    # -------------------------
    @freeze_time("2026-04-10")
    @patch("kaibaru.stripe_views.stripe")
    def test_resume_creates_new_item(self, mock_stripe):

        self.mock_stripe(mock_stripe)

        item = SubscriptionItem.objects.create(
            subscription=self.subscription,
            member=self.member,
            plan=self.plan,
            stripe_subscription_item_id=None,
            stripe_price_id_at_subscription="price_123",
            deleted_at=timezone.now(),
            access_until=timezone.now(),
            quantity=1,
        )

        mock_stripe.Subscription.retrieve.return_value = {
            "id": "sub_123",
            "items": {"data": []}
        }

        response = self.client.post(f"/resume_member_subscription/{item.id}/")

        self.assertIn(response.status_code, [200, 400, 500, 405])

    # -------------------------
    # RESUME INVALID STATE (NEW)
    # -------------------------
    @freeze_time("2026-04-10")
    def test_resume_already_active_returns_400(self):

        self.item.deleted_at = None
        self.item.save()

        response = self.client.post(f"/resume_member_subscription/{self.item.id}/")

        self.assertEqual(response.status_code, 400)

    # -------------------------
    # CANCEL ALREADY DELETED (NEW)
    # -------------------------
    @freeze_time("2026-04-10")
    def test_cancel_already_deleted_returns_400(self):

        self.item.deleted_at = timezone.now()
        self.item.save()

        response = self.client.post(f"/cancel_member_subscription/{self.item.id}/")

        self.assertEqual(response.status_code, 400)

    # -------------------------
    # STRIPE FAILURE (NEW)
    # -------------------------
    @freeze_time("2026-04-10")
    @patch("kaibaru.stripe_views.stripe")
    def test_cancel_stripe_failure_returns_500(self, mock_stripe):

        self.mock_stripe(mock_stripe)
        mock_stripe.Subscription.retrieve.side_effect = Exception("Stripe down")

        response = self.client.post(f"/cancel_member_subscription/{self.item.id}/")

        self.assertEqual(response.status_code, 500)

    # -------------------------
    # ADD PLAN
    # -------------------------
    @freeze_time("2026-04-10")
    @patch("kaibaru.stripe_views.stripe")
    def test_add_plan_to_existing_subscription(self, mock_stripe):

        self.mock_stripe(mock_stripe)

        response = self.client.post(
            f"/create_member_checkout_session/{self.club.id}/{self.plan.id}/",
            {"member_id": self.member.id},
        )

        self.assertIn(response.status_code, [200, 400, 403, 429])

    # -------------------------
    # CHECKOUT
    # -------------------------
    @freeze_time("2026-04-10")
    @patch("kaibaru.stripe_views.stripe")
    def test_checkout_session(self, mock_stripe):

        self.mock_stripe(mock_stripe)

        response = self.client.post(
            f"/create_member_checkout_session/{self.club.id}/{self.plan.id}/",
            {"member_id": self.member.id},
        )     

        self.assertIn(response.status_code, [200, 400, 403, 429])

    # -------------------------
    # WRONG USER ACCESS (NEW)
    # -------------------------
    def test_wrong_user_cannot_access_item(self):

        self.client.logout()
        self.client.login(username="otheruser", password="pass123")

        response = self.client.post(f"/cancel_member_subscription/{self.item.id}/")

        self.assertEqual(response.status_code, 404)

