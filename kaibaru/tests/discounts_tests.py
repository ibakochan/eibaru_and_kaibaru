from types import SimpleNamespace
from django.test import SimpleTestCase
from datetime import date

from kaibaru.charge_amounts import (
    SUBSCRIPTION_SMALL_AMOUNT_MESSAGE,
    platform_fee_yen,
)
from kaibaru.service_cash_platform_fee import (
    previous_month,
    total_platform_fee,
)
from kaibaru.discounts import (
    apply_discounts,
    calculate_discounted_amount,
    calculate_discount_breakdown,
    check_conditions,
    calculate_age,
    apply_joining_fee_discount,
    apply_subscription_discount,
)


# =========================
# Helpers
# =========================

class FakeCondition:
    def __init__(self, type, value):
        self.type = type
        self.value = value


class FakeDiscount:
    def __init__(self, id, discount_type, value, conditions):
        self.id = id
        self.discount_type = discount_type
        self.value = value
        self.apply_to = "subscription"
        self.conditions = SimpleNamespace(
            all=lambda: conditions,
            exists=lambda: True,
        )
        self.plans = SimpleNamespace(exists=lambda: False)


class DiscountsTests(SimpleTestCase):

    def setUp(self):
        from kaibaru import discounts as mod

        self._adjustment_lookup = mod.get_member_pricing_adjustment
        mod.get_member_pricing_adjustment = lambda member_id: []

    def tearDown(self):
        from kaibaru import discounts as mod

        mod.get_member_pricing_adjustment = self._adjustment_lookup

    # -------------------------
    # AGE CALC
    # -------------------------

    def test_calculate_age(self):
        birth = date(2000, 1, 1)
        age = calculate_age(birth)
        self.assertTrue(age >= 0)

    def test_calculate_age_none(self):
        self.assertIsNone(calculate_age(None))

    # -------------------------
    # CONDITION ENGINE
    # -------------------------

    def test_gender_condition_pass(self):
        member = SimpleNamespace(id=1, gender="male", birth_date=date(2000, 1, 1), owner=1, club=1)

        discount = SimpleNamespace(
            conditions=SimpleNamespace(all=lambda: [
                FakeCondition("gender", "male")
            ])
        )

        self.assertTrue(check_conditions(discount, member))

    def test_gender_condition_fail(self):
        member = SimpleNamespace(id=1, gender="female", birth_date=date(2000, 1, 1), owner=1, club=1)

        discount = SimpleNamespace(
            conditions=SimpleNamespace(all=lambda: [
                FakeCondition("gender", "male")
            ])
        )

        self.assertFalse(check_conditions(discount, member))

    def test_age_lt_condition(self):
        member = SimpleNamespace(id=1, gender="male", birth_date=date(2010, 1, 1), owner=1, club=1)

        discount = SimpleNamespace(
            conditions=SimpleNamespace(all=lambda: [
                FakeCondition("age_lt", "30")
            ])
        )

        self.assertTrue(check_conditions(discount, member))

    def test_age_gt_condition(self):
        member = SimpleNamespace(id=1, gender="male", birth_date=date(2000, 1, 1), owner=1, club=1)

        discount = SimpleNamespace(
            conditions=SimpleNamespace(all=lambda: [
                FakeCondition("age_gt", "10")
            ])
        )

        self.assertTrue(check_conditions(discount, member))

    # -------------------------
    # FAMILY CONDITION (NEW)
    # -------------------------

    def test_family_condition_pass(self):
        member = SimpleNamespace(
            id=1,
            gender="male",
            birth_date=date(2000, 1, 1),
            owner=1,
            club=1,
        )

        discount = SimpleNamespace(
            conditions=SimpleNamespace(all=lambda: [
                FakeCondition("is_family", "2")
            ])
        )

        # mock Member.objects.filter(...).count()
        from kaibaru import discounts as mod

        class FakeQuery:
            def exclude(self, **kwargs):
                return self

            def count(self):
                return 3  # meets requirement (>=2)

        mod.Member.objects = SimpleNamespace(
            filter=lambda **kwargs: FakeQuery()
        )

        self.assertTrue(check_conditions(discount, member))

    def test_family_condition_fail(self):
        member = SimpleNamespace(
            id=1,
            gender="male",
            birth_date=date(2000, 1, 1),
            owner=1,
            club=1,
        )

        discount = SimpleNamespace(
            conditions=SimpleNamespace(all=lambda: [
                FakeCondition("is_family", "5")
            ])
        )

        from kaibaru import discounts as mod

        class FakeQuery:
            def exclude(self, **kwargs):
                return self

            def count(self):
                return 2  # not enough (<5)

        mod.Member.objects = SimpleNamespace(
            filter=lambda **kwargs: FakeQuery()
        )

        self.assertFalse(check_conditions(discount, member))

    # -------------------------
    # CORE DISCOUNT ENGINE
    # -------------------------

    def test_percentage_then_fixed_stack(self):
        member = SimpleNamespace(id=1, club=1, owner=1, gender="male", birth_date=date(2000, 1, 1))

        discount1 = FakeDiscount(1, "percentage", 10, [])
        discount2 = FakeDiscount(2, "fixed", 100, [])

        from kaibaru import discounts as mod
        mod.get_applicable_discounts = lambda *a, **k: [discount1, discount2]

        result = calculate_discounted_amount(
            club=SimpleNamespace(id=1),
            member=member,
            base_amount=1000,
            apply_to="subscription",
        )

        self.assertEqual(result, 800)

    def test_multiple_percentage_stack(self):
        member = SimpleNamespace(id=1, club=1, owner=1, gender="male", birth_date=date(2000, 1, 1))

        d1 = FakeDiscount(1, "percentage", 10, [])
        d2 = FakeDiscount(2, "percentage", 10, [])

        from kaibaru import discounts as mod
        mod.get_applicable_discounts = lambda *a, **k: [d1, d2]

        result = calculate_discounted_amount(
            club=SimpleNamespace(id=1),
            member=member,
            base_amount=1000,
            apply_to="subscription",
        )

        self.assertEqual(result, 810)

    def test_clamp_to_zero(self):
        member = SimpleNamespace(id=1, club=1, owner=1, gender="male", birth_date=date(2000, 1, 1))

        discount = FakeDiscount(1, "fixed", 5000, [])

        from kaibaru import discounts as mod
        mod.get_applicable_discounts = lambda *a, **k: [discount]

        result = calculate_discounted_amount(
            club=SimpleNamespace(id=1),
            member=member,
            base_amount=1000,
            apply_to="subscription",
        )

        self.assertEqual(result, 0)

    def test_small_positive_remainder_floors_to_minimum(self):
        member = SimpleNamespace(id=1, club=1, owner=1, gender="male", birth_date=date(2000, 1, 1))

        discount = FakeDiscount(1, "fixed", 992, [])

        from kaibaru import discounts as mod
        mod.get_applicable_discounts = lambda *a, **k: [discount]

        result = calculate_discounted_amount(
            club=SimpleNamespace(id=1),
            member=member,
            base_amount=1000,
            apply_to="subscription",
        )

        self.assertEqual(result, 50)

    def test_no_discounts(self):
        member = SimpleNamespace(id=1, club=1, owner=1, gender="male", birth_date=date(2000, 1, 1))

        from kaibaru import discounts as mod
        mod.get_applicable_discounts = lambda *a, **k: []

        result = calculate_discounted_amount(
            club=SimpleNamespace(id=1),
            member=member,
            base_amount=1000,
            apply_to="subscription",
        )

        self.assertEqual(result, 1000)

    def test_subscription_floor_without_discounts(self):
        member = SimpleNamespace(id=1, club=1, owner=1, gender="male", birth_date=date(2000, 1, 1))

        from kaibaru import discounts as mod
        mod.get_applicable_discounts = lambda *a, **k: []

        self.assertEqual(
            calculate_discounted_amount(
                club=SimpleNamespace(id=1),
                member=member,
                base_amount=30,
                apply_to="subscription",
            ),
            50,
        )
        self.assertEqual(
            calculate_discounted_amount(
                club=SimpleNamespace(id=1),
                member=member,
                base_amount=0,
                apply_to="subscription",
            ),
            0,
        )

    def test_pricing_map_explains_small_subscription_amounts(self):
        member = SimpleNamespace(id=1)

        def price(base_amount, discount_type="subscription"):
            return apply_discounts(
                member=member,
                member_adjustments={},
                member_id=member.id,
                base_amount=base_amount,
                discount_type=discount_type,
                member_subscription_discounts={},
                member_joining_discounts={},
                subscription_discount_plan_ids={},
                joining_discount_plan_ids={},
            )

        rounded = price(30)
        self.assertEqual(rounded["final"], 50)
        self.assertEqual(
            rounded["1-49_message"],
            SUBSCRIPTION_SMALL_AMOUNT_MESSAGE,
        )

        free = price(0)
        self.assertEqual(free["final"], 0)
        self.assertNotIn("1-49_message", free)

        full = price(80)
        self.assertEqual(full["final"], 80)
        self.assertNotIn("1-49_message", full)

        joining = price(30, "joining_fee")
        self.assertEqual(joining["final"], 50)
        self.assertNotIn("1-49_message", joining)

    def test_non_subscription_keeps_free_and_rounds_small_amounts(self):
        member = SimpleNamespace(id=1, club=1, owner=1, gender="male", birth_date=date(2000, 1, 1))

        from kaibaru import discounts as mod
        mod.get_applicable_discounts = lambda *a, **k: []

        self.assertEqual(
            calculate_discounted_amount(
                club=SimpleNamespace(id=1),
                member=member,
                base_amount=0,
                apply_to="joining_fee",
            ),
            0,
        )
        self.assertEqual(
            calculate_discounted_amount(
                club=SimpleNamespace(id=1),
                member=member,
                base_amount=30,
                apply_to="joining_fee",
            ),
            50,
        )

    # -------------------------
    # BREAKDOWN TEST
    # -------------------------

    def test_breakdown_structure(self):
        member = SimpleNamespace(id=1, club=1, owner=1, gender="male", birth_date=date(2000, 1, 1))

        d1 = FakeDiscount(1, "percentage", 10, [])
        d2 = FakeDiscount(2, "fixed", 100, [])

        from kaibaru import discounts as mod
        mod.get_applicable_discounts = lambda *a, **k: [d1, d2]

        result = calculate_discount_breakdown(
            club=SimpleNamespace(id=1),
            member=member,
            base_amount=1000,
            apply_to="subscription",
        )

        self.assertEqual(result["base_amount"], 1000)
        self.assertEqual(result["final_amount"], 800)
        self.assertEqual(len(result["steps"]), 2)

    # -------------------------
    # ROUTER TESTS
    # -------------------------

    def test_joining_fee_wrapper(self):
        member = SimpleNamespace(id=1, owner=1)

        club = SimpleNamespace(joining_fee=2000, id=1)

        from kaibaru import discounts as mod
        mod.get_applicable_discounts = lambda *a, **k: []

        result = apply_joining_fee_discount(club, member)

        self.assertEqual(result, 2000)

    def test_subscription_wrapper(self):
        member = SimpleNamespace(id=1, owner=1)

        club = SimpleNamespace(id=1)

        from kaibaru import discounts as mod
        mod.get_applicable_discounts = lambda *a, **k: []

        result = apply_subscription_discount(club, member, 1500)

        self.assertEqual(result, 1500)


class PlatformFeeTests(SimpleTestCase):

    def test_rounds_half_up_to_the_nearest_yen(self):
        self.assertEqual(platform_fee_yen(0), 0)
        self.assertEqual(platform_fee_yen(49), 0)
        self.assertEqual(platform_fee_yen(50), 1)
        self.assertEqual(platform_fee_yen(99), 1)
        self.assertEqual(platform_fee_yen(100), 1)
        self.assertEqual(platform_fee_yen(149), 1)
        self.assertEqual(platform_fee_yen(150), 2)
        self.assertEqual(platform_fee_yen(1000), 10)
        self.assertEqual(platform_fee_yen(1050), 11)

    def test_cash_fees_are_rounded_per_invoice_then_added(self):
        self.assertEqual(total_platform_fee([1000, 1050, 0]), 21)
        self.assertEqual(total_platform_fee([50, 50]), 2)

    def test_previous_month_wraps_the_year(self):
        self.assertEqual(previous_month(date(2026, 10, 13)), (2026, 9))
        self.assertEqual(previous_month(date(2026, 1, 1)), (2025, 12))