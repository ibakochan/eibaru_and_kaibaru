from datetime import date, time
from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.db.models.signals import post_save
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from freezegun import freeze_time

from accounts.models import CustomUser
from kaibaru.models import (
    Club,
    Lesson,
    Member,
    MembershipPlan,
    Subscription,
    SubscriptionItem,
)
from kaibaru.rules_eligibility import (
    age_gender_block_reason,
    assert_member_eligible_for_plan,
    assert_visitor_eligible_for_lesson,
    format_restriction_message,
)
from kaibaru.rules_subscriptions import assert_plan_is_activatable
from kaibaru.service_add_plan_cash import CashAddPlanService
from kaibaru.service_reservation import MemberReservationService
from kaibaru.signals import club_created_signal


class RestrictionMessageTests(SimpleTestCase):
    def test_no_restriction(self):
        self.assertIsNone(
            format_restriction_message(
                noun="プラン",
                action="加入",
                age_min=None,
                age_max=None,
                allowed_gender=None,
            )
        )

    def test_male_only(self):
        self.assertEqual(
            format_restriction_message(
                noun="プラン",
                action="加入",
                age_min=None,
                age_max=None,
                allowed_gender="male",
            ),
            "このプランは男性のみ加入できます。",
        )

    def test_female_age_range(self):
        self.assertEqual(
            format_restriction_message(
                noun="レッスン",
                action="予約",
                age_min=6,
                age_max=12,
                allowed_gender="female",
            ),
            "このレッスンは女性（6〜12歳）のみ予約できます。",
        )

    def test_age_min_only(self):
        self.assertEqual(
            format_restriction_message(
                noun="プラン",
                action="加入",
                age_min=18,
                age_max=None,
                allowed_gender=None,
            ),
            "このプランは18歳以上の方のみ加入できます。",
        )


class AgeGenderBlockReasonTests(SimpleTestCase):
    def test_unrestricted_always_passes(self):
        self.assertIsNone(
            age_gender_block_reason(
                age=None,
                gender=None,
                age_min=None,
                age_max=None,
                allowed_gender=None,
                noun="プラン",
                action="加入",
            )
        )

    def test_wrong_gender(self):
        reason = age_gender_block_reason(
            age=20,
            gender="female",
            age_min=None,
            age_max=None,
            allowed_gender="male",
            noun="プラン",
            action="加入",
        )
        self.assertEqual(reason, "このプランは男性のみ加入できます。")

    def test_too_young(self):
        reason = age_gender_block_reason(
            age=10,
            gender="male",
            age_min=18,
            age_max=None,
            allowed_gender=None,
            noun="プラン",
            action="加入",
        )
        self.assertEqual(reason, "このプランは18歳以上の方のみ加入できます。")

    def test_visitor_missing_age(self):
        reason = age_gender_block_reason(
            age=None,
            gender="male",
            age_min=18,
            age_max=None,
            allowed_gender=None,
            noun="レッスン",
            action="予約",
            require_missing_fields=True,
        )
        self.assertIn("年齢を入力してください", reason)


@freeze_time("2026-09-22")
class PlanEligibilityServiceTests(TestCase):
    def setUp(self):
        post_save.disconnect(club_created_signal, sender=Club)
        self.addCleanup(
            lambda: post_save.connect(club_created_signal, sender=Club)
        )

        self.owner = CustomUser.objects.create_user(
            username="eliguser",
            email="elig@example.com",
            password="pass123",
        )
        self.club = Club.objects.create(
            owner=self.owner,
            subdomain="eligclub",
            stripe_account_id="acct_elig",
            stripe_anchor_date=20,
            subscription_mode="regular",
            joining_fee=0,
        )
        self.member = Member.objects.create(
            owner=self.owner,
            club=self.club,
            full_name="Eligible Member",
            gender="female",
            birth_date=date(2018, 1, 1),
        )
        self.subscription = Subscription.objects.create(
            owner=self.owner,
            club=self.club,
            billing_method="cash",
            billing_mode="regular",
            billing_anchor_day=20,
            status="active",
        )

    def make_plan(self, **kwargs):
        defaults = dict(
            club=self.club,
            name="Kids",
            price=3000,
            stripe_price_id="price_elig",
            interval="month",
            active=True,
        )
        defaults.update(kwargs)
        return MembershipPlan.objects.create(**defaults)

    def test_add_plan_blocks_wrong_gender(self):
        plan = self.make_plan(allowed_gender="male")

        with self.assertRaises(ValidationError) as ctx:
            CashAddPlanService.add_plan_to_existing_subscription(
                club=self.club,
                member=self.member,
                plan=plan,
                subscription=self.subscription,
            )

        self.assertIn("男性", str(ctx.exception))

    def test_add_plan_blocks_too_old(self):
        plan = self.make_plan(age_max=6)

        with self.assertRaises(ValidationError):
            assert_plan_is_activatable(plan, self.member)

    def test_add_plan_allows_matching_member(self):
        plan = self.make_plan(allowed_gender="female", age_max=12)
        assert_member_eligible_for_plan(self.member, plan)

        result = CashAddPlanService.add_plan_to_existing_subscription(
            club=self.club,
            member=self.member,
            plan=plan,
            subscription=self.subscription,
        )

        self.assertTrue(
            SubscriptionItem.objects.filter(
                member=self.member,
                plan=plan,
                deleted_at__isnull=True,
            ).exists()
        )
        self.assertIsNotNone(result)


@freeze_time("2026-09-22")
class LessonReservationEligibilityTests(TestCase):
    def setUp(self):
        post_save.disconnect(club_created_signal, sender=Club)
        self.addCleanup(
            lambda: post_save.connect(club_created_signal, sender=Club)
        )

        self.owner = CustomUser.objects.create_user(
            username="lessonelig",
            email="lessonelig@example.com",
            password="pass123",
        )
        self.club = Club.objects.create(
            owner=self.owner,
            subdomain="lessonelig",
            stripe_account_id="acct_lessonelig",
            stripe_anchor_date=20,
            member_reservation_price=1000,
            visitor_reservation_price=1500,
        )
        self.member = Member.objects.create(
            owner=self.owner,
            club=self.club,
            full_name="Adult Member",
            gender="male",
            birth_date=date(1990, 1, 1),
        )
        self.lesson = Lesson.objects.create(
            club=self.club,
            title="Kids class",
            weekday=1,
            start_time=time(10, 0),
            end_time=time(11, 0),
            reservation_only=True,
            age_max=12,
            allowed_gender="female",
            member_reservation_price=1000,
            visitor_reservation_price=1500,
        )

    def test_member_reservation_blocked(self):
        with self.assertRaises(ValueError) as ctx:
            MemberReservationService.create_reservation(
                club=self.club,
                lesson=self.lesson,
                member=self.member,
                reservation_date=date(2026, 9, 22),
                payment_method="stripe",
            )
        self.assertIn("女性", str(ctx.exception))

    def test_visitor_reservation_blocked_for_age(self):
        with self.assertRaises(ValueError) as ctx:
            assert_visitor_eligible_for_lesson(
                age=30,
                gender="female",
                lesson=self.lesson,
            )
        self.assertIn("12歳以下", str(ctx.exception))

    def test_visitor_reservation_allows_matching_person(self):
        assert_visitor_eligible_for_lesson(
            age=8,
            gender="female",
            lesson=self.lesson,
        )
