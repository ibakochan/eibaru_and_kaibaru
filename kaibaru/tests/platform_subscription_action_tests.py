"""Platform and cash subscription actions that had no direct tests.

Covers which clubs are billed, cancel versus remove, renewal deletion,
the subscription-deleted webhook, the overdue task, quantity proration
calls, cash cancel-at-period-end, and deleted-club cleanup.
"""

from datetime import timedelta
from unittest.mock import patch

from django.db.models.signals import post_save
from django.test import TestCase, SimpleTestCase, override_settings
from django.utils import timezone

from accounts.models import CustomUser
from kaibaru.models import (
    Club,
    ClubSubscriptionMutation,
    ClubVideo,
    Member,
    PlatformClub,
    PlatformSubscription,
    SlateImage,
    Subscription,
    SubscriptionItem,
)
from kaibaru.service_custom_domain import _live_domain_count, assign_domain, CustomDomainError
from kaibaru.service_deleted_club import finish_deleted_clubs
from kaibaru.service_platform_subscription import (
    REMOVAL_CLUB,
    REMOVAL_SUBSCRIPTION,
    _delete_clubs_on_renewal,
    _desired_quantities,
    apply_quantity_changes,
    delete_overdue_platform_subscriptions,
    handle_subscription_deleted,
    unsubscribe_club,
)
from kaibaru.service_subscription_cash import CashSubscriptionItemService
from kaibaru.signals import club_created_signal


LOC_MEM = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "platform-subscription-action-tests",
    }
}


class ApplyQuantityStripeCallTests(SimpleTestCase):
    @patch("kaibaru.service_platform_subscription.stripe.Subscription.modify")
    def test_increases_are_prorated_and_cuts_are_not(self, modify):
        apply_quantity_changes(
            "sub_test",
            [{"id": "si_up", "quantity": 3}],
            [{"id": "si_down", "deleted": True}],
            up_key="up-key",
            down_key="down-key",
        )
        self.assertEqual(
            modify.call_args_list[0].kwargs["proration_behavior"],
            "none",
        )
        self.assertEqual(modify.call_args_list[0].kwargs["idempotency_key"], "down-key")
        self.assertEqual(
            modify.call_args_list[1].kwargs["proration_behavior"],
            "create_prorations",
        )
        self.assertEqual(modify.call_args_list[1].kwargs["idempotency_key"], "up-key")


@override_settings(
    CACHES=LOC_MEM,
    STRIPE_SECRET_KEY="sk_test",
    STRIPE_BASE_PRICE_ID="price_base",
    STRIPE_BASE_PRICE_ID_REDUCED="price_extra",
    STRIPE_MEMBER_PRICE_ID="price_member",
    STRIPE_MEMBER_PRICE_ID_REDUCED="price_member_reduced",
)
class PlatformSubscriptionActionTests(TestCase):
    def setUp(self):
        post_save.disconnect(club_created_signal, sender=Club)
        self.addCleanup(lambda: post_save.connect(club_created_signal, sender=Club))
        self.owner = CustomUser.objects.create_user(
            username="platformowner",
            email="platform@example.com",
            password="pass123",
        )
        self.period_end = timezone.now() + timedelta(days=20)
        self.platform = PlatformSubscription.objects.create(
            user=self.owner,
            stripe_customer_id="cus_platform",
            stripe_subscription_id="sub_platform",
            status="active",
            current_period_end=self.period_end,
            stripe_account_id="acct_platform",
        )
        self.club = self.make_club("platformclub")
        self.link = self.link_club(self.club)

    def make_club(self, subdomain, **kwargs):
        return Club.objects.create(
            owner=self.owner,
            subdomain=subdomain,
            stripe_anchor_date=12,
            subscription_mode="regular",
            expiration_date=self.period_end,
            **kwargs,
        )

    def link_club(self, club, **kwargs):
        defaults = dict(
            platform_subscription=self.platform,
            awaiting_checkout=False,
            pending_removal=False,
            removal_kind="",
            access_until=self.period_end,
        )
        defaults.update(kwargs)
        return PlatformClub.objects.create(club=club, **defaults)

    def test_pending_removal_clubs_and_their_members_are_not_billed(self):
        extra = self.make_club("extraclub")
        self.link_club(extra, pending_removal=True, removal_kind=REMOVAL_CLUB)
        Member.objects.create(owner=self.owner, club=self.club, full_name="Kept")
        Member.objects.create(owner=self.owner, club=extra, full_name="Scheduled")
        self.assertEqual(
            _desired_quantities(self.platform),
            {"price_base": 1, "price_member": 1},
        )

    def test_pending_removal_domain_is_not_counted(self):
        self.club.custom_domain = "live.example.com"
        self.club.custom_domain_status = "active"
        self.club.save(update_fields=["custom_domain", "custom_domain_status"])
        leaving = self.make_club(
            "leavingclub",
            custom_domain="leaving.example.com",
            custom_domain_status="active",
        )
        self.link_club(leaving, pending_removal=True, removal_kind=REMOVAL_CLUB)
        self.assertEqual(_live_domain_count(self.owner.id), 1)

    def test_domain_save_requires_a_continuing_subscription(self):
        self.platform.status = "canceled"
        self.platform.save(update_fields=["status"])
        with self.assertRaises(CustomDomainError):
            assign_domain(self.club, "new.example.com")
        self.platform.status = "active"
        self.platform.save(update_fields=["status"])
        assign_domain(self.club, "new.example.com")
        self.club.refresh_from_db()
        self.assertEqual(self.club.custom_domain, "new.example.com")
        self.link.pending_removal = True
        self.link.removal_kind = REMOVAL_CLUB
        self.link.save(update_fields=["pending_removal", "removal_kind"])
        with self.assertRaises(CustomDomainError):
            assign_domain(self.club, "later.example.com")

    def test_renewal_deletes_only_a_club_scheduled_on_its_own(self):
        scheduled = self.make_club("scheduledclub")
        self.link_club(scheduled, pending_removal=True, removal_kind=REMOVAL_CLUB)
        kept = self.make_club("keptclub")
        self.link_club(kept, pending_removal=True, removal_kind=REMOVAL_SUBSCRIPTION)
        _delete_clubs_on_renewal(self.platform)
        scheduled = Club.all_objects.get(pk=scheduled.pk)
        kept.refresh_from_db()
        self.assertTrue(scheduled.is_deleted)
        self.assertEqual(scheduled.expiration_date, self.period_end)
        self.assertFalse(kept.is_deleted)

    def test_subscription_deleted_clears_the_cancel_flag_and_keeps_the_period(self):
        self.platform.cancel_at_period_end = True
        self.platform.save(update_fields=["cancel_at_period_end"])
        self.club.subscription_active = True
        self.club.save(update_fields=["subscription_active"])
        handled = handle_subscription_deleted({"id": "sub_platform"})
        self.assertTrue(handled)
        self.platform.refresh_from_db()
        self.club = Club.all_objects.get(pk=self.club.pk)
        self.assertEqual(self.platform.status, "canceled")
        self.assertFalse(self.platform.cancel_at_period_end)
        self.assertEqual(self.platform.current_period_end, self.period_end)
        self.assertTrue(self.club.is_deleted)
        self.assertFalse(self.club.subscription_cancel_at_period_end)

    def test_overdue_task_waits_until_the_stored_period_has_passed(self):
        self.platform.cancel_at_period_end = True
        self.platform.save(update_fields=["cancel_at_period_end"])
        with patch("kaibaru.service_platform_subscription.stripe.Subscription.delete") as delete:
            delete_overdue_platform_subscriptions(timezone.localdate())
            delete.assert_not_called()
        self.platform.current_period_end = timezone.now() - timedelta(days=2)
        self.platform.save(update_fields=["current_period_end"])
        with patch("kaibaru.service_platform_subscription.stripe.Subscription.delete") as delete:
            delete_overdue_platform_subscriptions(timezone.localdate())
        delete.assert_called_once_with("sub_platform")
        self.platform.refresh_from_db()
        self.club = Club.all_objects.get(pk=self.club.pk)
        self.assertEqual(self.platform.status, "canceled")
        self.assertTrue(self.club.is_deleted)

    @patch("kaibaru.service_platform_subscription.stripe.Subscription.modify")
    def test_last_club_sets_cancel_at_period_end(self, modify):
        unsubscribe_club(self.club, self.owner)
        self.platform.refresh_from_db()
        self.link.refresh_from_db()
        self.assertTrue(self.platform.cancel_at_period_end)
        self.assertEqual(self.link.removal_kind, REMOVAL_SUBSCRIPTION)
        self.assertTrue(modify.call_args.kwargs["cancel_at_period_end"])
        self.assertEqual(
            ClubSubscriptionMutation.objects.get().type,
            ClubSubscriptionMutation.MutationType.CANCEL_SUBSCRIPTION,
        )

    @patch("kaibaru.service_platform_subscription._apply_domain_quantity")
    @patch("kaibaru.service_platform_subscription._apply_quantities")
    def test_extra_club_is_scheduled_without_canceling_the_subscription(self, quantities, domains):
        extra = self.make_club("secondclub")
        extra_link = self.link_club(extra)
        unsubscribe_club(extra, self.owner)
        self.platform.refresh_from_db()
        extra_link.refresh_from_db()
        self.assertFalse(self.platform.cancel_at_period_end)
        self.assertTrue(extra_link.pending_removal)
        self.assertEqual(extra_link.removal_kind, REMOVAL_CLUB)
        quantities.assert_called_once()
        domains.assert_called_once()

    def test_cash_cancel_keeps_the_subscription_until_period_end(self):
        member = Member.objects.create(owner=self.owner, club=self.club, full_name="Cash")
        subscription = Subscription.objects.create(
            owner=self.owner,
            club=self.club,
            billing_method="cash",
            billing_mode="regular",
            billing_anchor_day=12,
            status="active",
            current_period_end=self.period_end,
        )
        item = SubscriptionItem.objects.create(
            subscription=subscription,
            member=member,
            price_at_subscription=3000,
        )
        CashSubscriptionItemService.cancel_item(
            item=item,
            subscription=subscription,
            club=self.club,
        )
        subscription.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(subscription.status, "active")
        self.assertTrue(subscription.cancel_at_period_end)
        self.assertIsNotNone(item.deleted_at)

    def test_cleanup_removes_members_domain_and_club_files_after_plans_end(self):
        self.platform.status = "canceled"
        self.platform.save(update_fields=["status"])
        self.club.is_deleted = True
        self.club.deleted_at = timezone.localdate()
        self.club.custom_domain = "gone.example.com"
        self.club.custom_domain_status = "active"
        self.club.picture.name = "platformclub/pic.jpg"
        self.club.save(update_fields=[
            "is_deleted",
            "deleted_at",
            "custom_domain",
            "custom_domain_status",
            "picture",
        ])
        member = Member.objects.create(owner=self.owner, club=self.club, full_name="Gone")
        slate = SlateImage(club=self.club, hash="abc")
        slate.image.name = "platformclub/slate/a.jpg"
        slate.save()
        video = ClubVideo(club=self.club)
        video.file.name = "platformclub/video/a.mp4"
        video.save()
        with patch("storages.backends.gcloud.GoogleCloudStorage.delete") as delete:
            finish_deleted_clubs()
        deleted_names = {call.args[0] for call in delete.call_args_list}
        self.assertIn("platformclub/pic.jpg", deleted_names)
        self.assertIn("platformclub/slate/a.jpg", deleted_names)
        self.assertIn("platformclub/video/a.mp4", deleted_names)
        self.assertFalse(Member.objects.filter(id=member.id).exists())
        self.assertFalse(SlateImage.objects.filter(id=slate.id).exists())
        self.assertFalse(ClubVideo.objects.filter(id=video.id).exists())
        self.club = Club.all_objects.get(pk=self.club.pk)
        self.assertFalse(self.club.custom_domain)
        self.assertEqual(self.club.picture.name, "")
        mutation = ClubSubscriptionMutation.objects.get()
        self.assertEqual(mutation.type, ClubSubscriptionMutation.MutationType.DELETE_CLUB)
        self.assertEqual(mutation.status, ClubSubscriptionMutation.Status.SUCCEEDED)

    def test_cleanup_waits_while_a_cash_plan_is_still_in_its_period(self):
        self.platform.status = "canceled"
        self.platform.save(update_fields=["status"])
        self.club.is_deleted = True
        self.club.deleted_at = timezone.localdate()
        self.club.save(update_fields=["is_deleted", "deleted_at"])
        member = Member.objects.create(owner=self.owner, club=self.club, full_name="Still")
        Subscription.objects.create(
            owner=self.owner,
            club=self.club,
            billing_method="cash",
            billing_mode="regular",
            billing_anchor_day=12,
            status="active",
            cancel_at_period_end=True,
            current_period_end=self.period_end,
        )
        finish_deleted_clubs()
        self.assertTrue(Member.objects.filter(id=member.id).exists())
        mutation = ClubSubscriptionMutation.objects.get()
        self.assertEqual(mutation.status, ClubSubscriptionMutation.Status.PENDING)
