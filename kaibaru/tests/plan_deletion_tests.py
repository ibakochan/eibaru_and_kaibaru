# kaibaru/tests/plan_deletion_tests.py
"""
Focused tests for the MembershipPlan scheduled-deletion lifecycle:

    scheduled_for_deletion=True, is_deleted=False -> deletion in progress
    is_deleted=True                               -> plan actually deleted
    neither                                        -> normal active plan

Covers:
    - every entry point that could create/reactivate a SubscriptionItem
      correctly refuses to do so for a scheduled/deleted plan
    - the checkout.session.completed webhook refuses to activate a
      scheduled/deleted plan
    - the deletion task cancels active subscribers, tolerates partial
      failures, and only finalizes the plan once no active items remain
    - the periodic reconciliation task retries failed cancellations and
      eventually finalizes the plan
    - re-running the deletion/reconciliation logic is safe
"""

from unittest.mock import patch, MagicMock

from django.core.exceptions import ValidationError
from django.db.models.signals import post_save
from django.test import TestCase, Client
from django.utils import timezone

from freezegun import freeze_time

from accounts.models import CustomUser
from kaibaru.models import (
    Club,
    Member,
    MembershipPlan,
    Subscription,
    SubscriptionItem,
    StripeCustomer,
)
from kaibaru.signals import club_created_signal

from kaibaru.service_subscription import SubscriptionItemService
from kaibaru.service_subscription_cash import CashSubscriptionItemService
from kaibaru.service_add_plan_cash import CashAddPlanService
from kaibaru.locks_and_reconciliation import MembershipPlanDeletionReconciler
from kaibaru.tasks import (
    delete_membership_plan_task,
    reconcile_scheduled_plan_deletions,
)


class PlanDeletionBaseTestCase(TestCase):
    """Shared fixtures for a club/owner/member with both a cash and a
    stripe-billed subscription available."""

    def setUp(self):
        post_save.disconnect(club_created_signal, sender=Club)
        self.addCleanup(
            lambda: post_save.connect(club_created_signal, sender=Club)
        )

        self.owner = CustomUser.objects.create_user(
            username="plandeluser",
            email="plandel@example.com",
            password="pass123",
        )

        self.club = Club.objects.create(
            owner=self.owner,
            subdomain="plandelclub",
            stripe_account_id="acct_plandel",
            stripe_anchor_date=20,
            subscription_mode="regular",
            joining_fee=0,
        )

        self.member = Member.objects.create(
            owner=self.owner,
            club=self.club,
            full_name="Plan Del Member",
        )

    def make_plan(self, **kwargs):
        defaults = dict(
            club=self.club,
            name="Standard",
            price=5000,
            stripe_price_id="price_plandel",
            interval="month",
            active=True,
        )
        defaults.update(kwargs)
        return MembershipPlan.objects.create(**defaults)

    def make_cash_subscription(self):
        return Subscription.objects.create(
            owner=self.owner,
            club=self.club,
            billing_method="cash",
            billing_mode="regular",
            billing_anchor_day=20,
            status="active",
        )

    def make_stripe_subscription(self, stripe_subscription_id):
        return Subscription.objects.create(
            owner=self.owner,
            club=self.club,
            billing_method="stripe",
            billing_mode="regular",
            billing_anchor_day=20,
            status="active",
            stripe_subscription_id=stripe_subscription_id,
        )


# =============================================================
# 1-3: ADD PLAN
# =============================================================
class AddPlanGuardTests(PlanDeletionBaseTestCase):

    @freeze_time("2026-04-10")
    def test_normal_plan_can_be_added(self):
        plan = self.make_plan()
        subscription = self.make_cash_subscription()

        result = CashAddPlanService.add_plan_to_existing_subscription(
            club=self.club,
            member=self.member,
            plan=plan,
            subscription=subscription,
        )

        self.assertTrue(result["success"])
        self.assertTrue(
            SubscriptionItem.objects.filter(
                subscription=subscription,
                member=self.member,
                plan=plan,
                deleted_at__isnull=True,
            ).exists()
        )

    @freeze_time("2026-04-10")
    def test_scheduled_for_deletion_plan_cannot_be_added(self):
        plan = self.make_plan(scheduled_for_deletion=True)
        subscription = self.make_cash_subscription()

        with self.assertRaises(ValidationError):
            CashAddPlanService.add_plan_to_existing_subscription(
                club=self.club,
                member=self.member,
                plan=plan,
                subscription=subscription,
            )

        self.assertFalse(
            SubscriptionItem.objects.filter(
                subscription=subscription,
                plan=plan,
            ).exists()
        )

    @freeze_time("2026-04-10")
    def test_deleted_plan_cannot_be_added(self):
        plan = self.make_plan(
            is_deleted=True,
            deleted_at=timezone.now(),
        )
        subscription = self.make_cash_subscription()

        with self.assertRaises(ValidationError):
            CashAddPlanService.add_plan_to_existing_subscription(
                club=self.club,
                member=self.member,
                plan=plan,
                subscription=subscription,
            )

        self.assertFalse(
            SubscriptionItem.objects.filter(
                subscription=subscription,
                plan=plan,
            ).exists()
        )


# =============================================================
# 4-5: CHANGE PLAN
# =============================================================
class ChangePlanGuardTests(PlanDeletionBaseTestCase):

    def _make_old_item(self, subscription, old_plan):
        return SubscriptionItem.objects.create(
            subscription=subscription,
            member=self.member,
            plan=old_plan,
            price_at_subscription=old_plan.price,
            stripe_price_id_at_subscription=old_plan.stripe_price_id,
        )

    def test_scheduled_for_deletion_plan_cannot_be_changed_to(self):
        old_plan = self.make_plan(name="Old")
        new_plan = self.make_plan(
            name="New",
            stripe_price_id="price_new",
            scheduled_for_deletion=True,
        )
        subscription = self.make_cash_subscription()
        old_item = self._make_old_item(subscription, old_plan)

        with self.assertRaises(ValidationError):
            CashSubscriptionItemService.change_plan(
                item=old_item,
                new_plan=new_plan,
                subscription=subscription,
                club=self.club,
                old_item_is_grace=False,
            )

        old_item.refresh_from_db()
        self.assertIsNone(old_item.deleted_at)
        self.assertFalse(
            SubscriptionItem.objects.filter(
                subscription=subscription, plan=new_plan
            ).exists()
        )

    def test_deleted_plan_cannot_be_changed_to(self):
        old_plan = self.make_plan(name="Old")
        new_plan = self.make_plan(
            name="New",
            stripe_price_id="price_new",
            is_deleted=True,
            deleted_at=timezone.now(),
        )
        subscription = self.make_cash_subscription()
        old_item = self._make_old_item(subscription, old_plan)

        with self.assertRaises(ValidationError):
            CashSubscriptionItemService.change_plan(
                item=old_item,
                new_plan=new_plan,
                subscription=subscription,
                club=self.club,
                old_item_is_grace=False,
            )

        old_item.refresh_from_db()
        self.assertIsNone(old_item.deleted_at)
        self.assertFalse(
            SubscriptionItem.objects.filter(
                subscription=subscription, plan=new_plan
            ).exists()
        )


# =============================================================
# 6-7: RESUME (via HTTP, both stripe + cash entry points)
# =============================================================
class ResumeGuardTests(PlanDeletionBaseTestCase):

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.login(username="plandeluser", password="pass123")

    def test_scheduled_for_deletion_plan_cannot_be_resumed_cash(self):
        plan = self.make_plan(scheduled_for_deletion=True)
        subscription = self.make_cash_subscription()

        item = SubscriptionItem.objects.create(
            subscription=subscription,
            member=self.member,
            plan=plan,
            deleted_at=timezone.now(),
            access_until=timezone.now() + timezone.timedelta(days=5),
        )

        response = self.client.post(
            f"/resume_cash_member_subscription/{item.id}/"
        )

        self.assertEqual(response.status_code, 400)

        item.refresh_from_db()
        self.assertIsNotNone(item.deleted_at)

    def test_deleted_plan_cannot_be_resumed_stripe(self):
        plan = self.make_plan(
            is_deleted=True,
            deleted_at=timezone.now(),
        )
        subscription = self.make_stripe_subscription("sub_resume_guard")

        item = SubscriptionItem.objects.create(
            subscription=subscription,
            member=self.member,
            plan=plan,
            deleted_at=timezone.now(),
            access_until=timezone.now() + timezone.timedelta(days=5),
        )

        response = self.client.post(
            f"/resume_member_subscription/{item.id}/"
        )

        self.assertEqual(response.status_code, 400)

        item.refresh_from_db()
        self.assertIsNotNone(item.deleted_at)

    def test_service_layer_also_blocks_resume_for_scheduled_plan(self):
        """
        Defense in depth: the service itself refuses to resume a
        scheduled/deleted plan, independent of any view-level check.
        """
        plan = self.make_plan(scheduled_for_deletion=True)
        subscription = self.make_cash_subscription()

        item = SubscriptionItem.objects.create(
            subscription=subscription,
            member=self.member,
            plan=plan,
            deleted_at=timezone.now(),
            access_until=timezone.now() + timezone.timedelta(days=5),
        )

        with self.assertRaises(ValidationError):
            CashSubscriptionItemService.resume_item(
                item=item,
                subscription=subscription,
                club=self.club,
            )

        item.refresh_from_db()
        self.assertIsNotNone(item.deleted_at)


# =============================================================
# 8: CANCEL PENDING PLAN CHANGE MUST NOT REVIVE A SCHEDULED OLD PLAN
# =============================================================
class CancelPlanChangeGuardTests(PlanDeletionBaseTestCase):

    def test_cancel_pending_change_cannot_revive_scheduled_old_plan(self):
        old_plan = self.make_plan(
            name="Old",
            scheduled_for_deletion=True,
        )
        new_plan = self.make_plan(
            name="New",
            stripe_price_id="price_new",
        )
        subscription = self.make_cash_subscription()

        now = timezone.now()

        old_item = SubscriptionItem.objects.create(
            subscription=subscription,
            member=self.member,
            plan=old_plan,
            deleted_at=now,
            access_until=now,
        )

        new_item = SubscriptionItem.objects.create(
            subscription=subscription,
            member=self.member,
            plan=new_plan,
            source_item=old_item,
        )

        with self.assertRaises(ValidationError):
            CashSubscriptionItemService.cancel_change(
                new_item=new_item,
                old_item=old_item,
                subscription=subscription,
                club=self.club,
            )

        old_item.refresh_from_db()
        new_item.refresh_from_db()

        # old item was NOT revived
        self.assertIsNotNone(old_item.deleted_at)
        # new item is untouched
        self.assertIsNone(new_item.deleted_at)


# =============================================================
# 9-10: CHECKOUT COMPLETION WEBHOOK
# =============================================================
class CheckoutCompletionPlanGuardTests(PlanDeletionBaseTestCase):

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.url = "/stripe_webhook/connected/"

        self.sub = Subscription.objects.create(
            owner=self.owner,
            club=self.club,
            stripe_subscription_id="sub_precreated_123",
            status="pending",
        )

    def post_event(self, payload):
        with patch(
            "kaibaru.stripe_webhooks.stripe.Webhook.construct_event",
            return_value=payload,
        ):
            return self.client.post(
                self.url,
                data="{}",
                content_type="application/json",
                HTTP_STRIPE_SIGNATURE="testsig",
            )

    def _checkout_payload(self, event_id, plan, stripe_sub_id):
        return {
            "id": event_id,
            "type": "checkout.session.completed",
            "account": "acct_plandel",
            "data": {
                "object": {
                    "id": f"cs_{event_id}",
                    "subscription": stripe_sub_id,
                    "invoice": f"in_{event_id}",
                    "metadata": {
                        "member_id": str(self.member.id),
                        "club_id": str(self.club.id),
                        "plan_id": str(plan.id),
                    },
                }
            },
        }

    @patch("kaibaru.locks_and_reconciliation.stripe.Subscription.cancel")
    @patch("kaibaru.stripe_webhooks.stripe.Subscription.retrieve")
    def test_checkout_completion_cannot_activate_scheduled_plan(
        self,
        mock_retrieve,
        mock_cancel,
    ):
        plan = self.make_plan(scheduled_for_deletion=True)

        fake_sub = MagicMock()
        fake_sub.id = "sub_scheduled_guard"
        fake_sub.status = "active"

        mock_retrieve.return_value = fake_sub
        mock_cancel.return_value = MagicMock(
            id="sub_scheduled_guard", status="canceled"
        )

        payload = self._checkout_payload(
            "evt_scheduled_guard", plan, "sub_scheduled_guard"
        )

        response = self.post_event(payload)

        self.assertEqual(response.status_code, 200)

        self.assertFalse(
            Subscription.objects.filter(
                stripe_subscription_id="sub_scheduled_guard"
            ).exists()
        )
        self.assertFalse(
            SubscriptionItem.objects.filter(plan=plan).exists()
        )

        mock_cancel.assert_called_once()

    @freeze_time("2026-04-10")
    @patch("kaibaru.stripe_webhooks.get_or_create_stripe_customer")
    @patch("kaibaru.stripe_webhooks.stripe.Invoice.pay")
    @patch("kaibaru.stripe_webhooks.stripe.Invoice.retrieve")
    @patch("kaibaru.stripe_webhooks.stripe.Invoice.create")
    @patch("kaibaru.stripe_webhooks.stripe.InvoiceItem.create")
    @patch("kaibaru.stripe_webhooks.stripe.Subscription.retrieve")
    def test_normal_checkout_completion_still_activates(
        self,
        mock_retrieve,
        mock_invoice_item,
        mock_invoice_create,
        mock_invoice_retrieve,
        mock_invoice_pay,
        mock_get_customer,
    ):
        plan = self.make_plan(scheduled_for_deletion=False, is_deleted=False)

        stripe_customer = StripeCustomer.objects.create(
            user=self.owner,
            club=self.club,
            stripe_customer_id="cus_plandel",
        )
        mock_get_customer.return_value = stripe_customer

        fake_sub = MagicMock()
        fake_sub.id = "sub_normal_guard"
        fake_sub.customer = "cus_plandel"
        fake_sub.status = "active"

        fake_sub.__getitem__.side_effect = lambda key: {
            "items": {
                "data": [
                    {
                        "id": "si_normal_guard",
                        "quantity": 1,
                        "price": {"id": plan.stripe_price_id},
                    }
                ]
            }
        }[key]

        mock_retrieve.return_value = fake_sub

        class FakeInvoice:
            amount_due = 0
            id = "in_normal_guard"

        mock_invoice_create.return_value = FakeInvoice()

        fake_invoice = MagicMock()
        fake_invoice.id = "in_normal_guard"
        fake_invoice.status = "open"
        fake_invoice.amount_due = 0
        fake_invoice.amount_paid = 0
        fake_invoice.get.side_effect = lambda key, default=None: {
            "id": "in_normal_guard",
            "amount_due": 0,
            "amount_paid": 0,
            "currency": "jpy",
            "lines": {"data": []},
        }.get(key, default)
        fake_invoice.__getitem__.side_effect = lambda key: {
            "id": "in_normal_guard",
            "lines": {"data": []},
        }[key]

        mock_invoice_retrieve.return_value = fake_invoice

        payload = self._checkout_payload(
            "evt_normal_guard", plan, "sub_normal_guard"
        )

        response = self.post_event(payload)

        self.assertEqual(response.status_code, 200)

        self.assertTrue(
            Subscription.objects.filter(
                stripe_subscription_id="sub_normal_guard"
            ).exists()
        )
        self.assertTrue(
            SubscriptionItem.objects.filter(
                plan=plan,
                deleted_at__isnull=True,
            ).exists()
        )


# =============================================================
# 11-14, 17: DELETION TASK
# =============================================================
class DeleteMembershipPlanTaskTests(PlanDeletionBaseTestCase):

    def _make_active_item(self, stripe_subscription_id, plan, member=None):
        subscription = self.make_stripe_subscription(stripe_subscription_id)
        item = SubscriptionItem.objects.create(
            subscription=subscription,
            member=member or self.member,
            plan=plan,
            price_at_subscription=plan.price,
            stripe_price_id_at_subscription=plan.stripe_price_id,
            stripe_subscription_item_id=f"si_{stripe_subscription_id}",
        )
        return subscription, item

    @patch("kaibaru.service_subscription.stripe")
    def test_deletion_task_cancels_active_subscribers(self, mock_stripe):
        plan = self.make_plan(scheduled_for_deletion=True)
        subscription, item = self._make_active_item("sub_del_ok", plan)

        mock_stripe.Subscription.modify.return_value = None

        result = delete_membership_plan_task(plan.id)

        item.refresh_from_db()
        plan.refresh_from_db()

        self.assertIsNotNone(item.deleted_at)
        self.assertTrue(plan.is_deleted)
        self.assertIsNotNone(plan.deleted_at)
        self.assertEqual(result["cancelled"], 1)
        self.assertEqual(result["failed"], 0)

    @patch("kaibaru.service_subscription.stripe")
    def test_one_failed_cancellation_does_not_block_others(self, mock_stripe):
        plan = self.make_plan(scheduled_for_deletion=True)

        # A second, independent owner/member/subscription so the two
        # SubscriptionItems don't collide with the
        # one-active-subscription-per-owner-club constraint.
        owner_b = CustomUser.objects.create_user(
            username="plandeluser_b",
            email="plandel_b@example.com",
            password="pass123",
        )
        member_b = Member.objects.create(
            owner=owner_b,
            club=self.club,
            full_name="Member B",
        )

        sub_ok, item_ok = self._make_active_item("sub_del_good", plan)

        sub_fail = Subscription.objects.create(
            owner=owner_b,
            club=self.club,
            billing_method="stripe",
            billing_mode="regular",
            billing_anchor_day=20,
            status="active",
            stripe_subscription_id="sub_del_bad",
        )
        item_fail = SubscriptionItem.objects.create(
            subscription=sub_fail,
            member=member_b,
            plan=plan,
            price_at_subscription=plan.price,
            stripe_price_id_at_subscription=plan.stripe_price_id,
            stripe_subscription_item_id="si_sub_del_bad",
        )

        def modify_side_effect(subscription_id, **kwargs):
            if subscription_id == "sub_del_bad":
                raise Exception("stripe boom")
            return None

        mock_stripe.Subscription.modify.side_effect = modify_side_effect

        result = MembershipPlanDeletionReconciler.process_plan(plan)

        item_ok.refresh_from_db()
        item_fail.refresh_from_db()
        plan.refresh_from_db()

        self.assertIsNotNone(item_ok.deleted_at)
        self.assertIsNone(item_fail.deleted_at)

        self.assertEqual(result["cancelled"], 1)
        self.assertEqual(result["failed"], 1)

        # plan must stay in-progress: one active item remains
        self.assertFalse(plan.is_deleted)
        self.assertTrue(plan.scheduled_for_deletion)

    @patch("kaibaru.service_subscription.stripe")
    def test_plan_not_marked_deleted_while_active_items_remain(
        self, mock_stripe
    ):
        plan = self.make_plan(scheduled_for_deletion=True)
        subscription, item = self._make_active_item("sub_del_remains", plan)

        mock_stripe.Subscription.modify.side_effect = Exception("stripe boom")

        MembershipPlanDeletionReconciler.process_plan(plan)

        item.refresh_from_db()
        plan.refresh_from_db()

        self.assertIsNone(item.deleted_at)
        self.assertFalse(plan.is_deleted)
        self.assertIsNone(plan.deleted_at)
        self.assertTrue(plan.scheduled_for_deletion)

    @patch("kaibaru.service_subscription.stripe")
    def test_plan_marked_deleted_once_all_active_items_gone(
        self, mock_stripe
    ):
        plan = self.make_plan(scheduled_for_deletion=True)
        subscription, item = self._make_active_item("sub_del_finalize", plan)

        mock_stripe.Subscription.modify.return_value = None

        MembershipPlanDeletionReconciler.process_plan(plan)

        item.refresh_from_db()
        plan.refresh_from_db()

        self.assertIsNotNone(item.deleted_at)
        self.assertTrue(plan.is_deleted)
        self.assertIsNotNone(plan.deleted_at)

    @patch("kaibaru.service_subscription.stripe")
    def test_rerunning_deletion_task_is_safe(self, mock_stripe):
        plan = self.make_plan(scheduled_for_deletion=True)
        subscription, item = self._make_active_item("sub_del_rerun", plan)

        mock_stripe.Subscription.modify.return_value = None

        first = MembershipPlanDeletionReconciler.process_plan(plan)
        plan.refresh_from_db()
        self.assertTrue(plan.is_deleted)

        # Run again on the now-deleted plan: must not raise, must not
        # re-cancel the already-cancelled item.
        second = MembershipPlanDeletionReconciler.process_plan(plan)

        self.assertEqual(mock_stripe.Subscription.modify.call_count, 1)
        self.assertEqual(second["cancelled"], 0)
        self.assertEqual(second["failed"], 0)

        # The one-shot task itself also safely no-ops on an already
        # deleted plan.
        third = delete_membership_plan_task(plan.id)
        self.assertEqual(third["status"], "already_deleted")
        self.assertEqual(mock_stripe.Subscription.modify.call_count, 1)


# =============================================================
# 15-16: PERIODIC RECONCILIATION
# =============================================================
class ReconcileScheduledPlanDeletionsTests(PlanDeletionBaseTestCase):

    def _make_active_item(self, stripe_subscription_id, plan):
        subscription = self.make_stripe_subscription(stripe_subscription_id)
        item = SubscriptionItem.objects.create(
            subscription=subscription,
            member=self.member,
            plan=plan,
            price_at_subscription=plan.price,
            stripe_price_id_at_subscription=plan.stripe_price_id,
            stripe_subscription_item_id=f"si_{stripe_subscription_id}",
        )
        return subscription, item

    @patch("kaibaru.service_subscription.stripe")
    def test_reconciliation_retries_then_finalizes(self, mock_stripe):
        plan = self.make_plan(scheduled_for_deletion=True)
        subscription, item = self._make_active_item("sub_recon", plan)

        # First periodic run: Stripe is failing -> nothing cancelled,
        # plan stays scheduled for the next run to retry.
        mock_stripe.Subscription.modify.side_effect = Exception("down")

        first_results = reconcile_scheduled_plan_deletions()

        item.refresh_from_db()
        plan.refresh_from_db()

        self.assertIsNone(item.deleted_at)
        self.assertFalse(plan.is_deleted)
        self.assertTrue(plan.scheduled_for_deletion)
        self.assertEqual(len(first_results), 1)
        self.assertEqual(first_results[0]["failed"], 1)

        # Second periodic run: Stripe recovers -> the remaining item
        # is retried successfully and the plan is finalized.
        mock_stripe.Subscription.modify.side_effect = None
        mock_stripe.Subscription.modify.return_value = None

        second_results = reconcile_scheduled_plan_deletions()

        item.refresh_from_db()
        plan.refresh_from_db()

        self.assertIsNotNone(item.deleted_at)
        self.assertTrue(plan.is_deleted)
        self.assertIsNotNone(plan.deleted_at)
        self.assertEqual(second_results[0]["cancelled"], 1)

    @patch("kaibaru.service_subscription.stripe")
    def test_reconciliation_ignores_already_finalized_plans(
        self, mock_stripe
    ):
        # A plan that is already fully deleted must not be touched by
        # the periodic scan at all.
        plan = self.make_plan(
            scheduled_for_deletion=True,
            is_deleted=True,
            deleted_at=timezone.now(),
        )

        results = reconcile_scheduled_plan_deletions()

        self.assertEqual(results, [])
        mock_stripe.Subscription.modify.assert_not_called()
