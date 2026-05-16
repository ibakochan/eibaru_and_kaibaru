from types import SimpleNamespace
from django.test import SimpleTestCase
from datetime import date

from kaibaru.discounts import (
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
        self.conditions = SimpleNamespace(all=lambda: conditions)


class DiscountsTests(SimpleTestCase):

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
        member = SimpleNamespace(gender="male", birth_date=date(2000, 1, 1), owner=1)

        discount = SimpleNamespace(
            conditions=SimpleNamespace(all=lambda: [
                FakeCondition("gender", "male")
            ])
        )

        self.assertTrue(check_conditions(discount, member))

    def test_gender_condition_fail(self):
        member = SimpleNamespace(gender="female", birth_date=date(2000, 1, 1), owner=1)

        discount = SimpleNamespace(
            conditions=SimpleNamespace(all=lambda: [
                FakeCondition("gender", "male")
            ])
        )

        self.assertFalse(check_conditions(discount, member))

    def test_age_lt_condition(self):
        member = SimpleNamespace(gender="male", birth_date=date(2010, 1, 1), owner=1)

        discount = SimpleNamespace(
            conditions=SimpleNamespace(all=lambda: [
                FakeCondition("age_lt", "30")
            ])
        )

        self.assertTrue(check_conditions(discount, member))

    def test_age_gt_condition(self):
        member = SimpleNamespace(gender="male", birth_date=date(2000, 1, 1), owner=1)

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
            gender="male",
            birth_date=date(2000, 1, 1),
            owner=1
        )

        discount = SimpleNamespace(
            conditions=SimpleNamespace(all=lambda: [
                FakeCondition("is_family", "2")
            ])
        )

        # mock Member.objects.filter(...).count()
        from kaibaru import discounts as mod

        class FakeQuery:
            def count(self):
                return 3  # meets requirement (>=2)

        mod.Member.objects = SimpleNamespace(
            filter=lambda **kwargs: FakeQuery()
        )

        self.assertTrue(check_conditions(discount, member))

    def test_family_condition_fail(self):
        member = SimpleNamespace(
            gender="male",
            birth_date=date(2000, 1, 1),
            owner=1
        )

        discount = SimpleNamespace(
            conditions=SimpleNamespace(all=lambda: [
                FakeCondition("is_family", "5")
            ])
        )

        from kaibaru import discounts as mod

        class FakeQuery:
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
        member = SimpleNamespace(id=1, owner=1, gender="male", birth_date=date(2000, 1, 1))

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
        member = SimpleNamespace(id=1, owner=1, gender="male", birth_date=date(2000, 1, 1))

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
        member = SimpleNamespace(id=1, owner=1, gender="male", birth_date=date(2000, 1, 1))

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

    def test_no_discounts(self):
        member = SimpleNamespace(id=1, owner=1, gender="male", birth_date=date(2000, 1, 1))

        from kaibaru import discounts as mod
        mod.get_applicable_discounts = lambda *a, **k: []

        result = calculate_discounted_amount(
            club=SimpleNamespace(id=1),
            member=member,
            base_amount=1000,
            apply_to="subscription",
        )

        self.assertEqual(result, 1000)

    # -------------------------
    # BREAKDOWN TEST
    # -------------------------

    def test_breakdown_structure(self):
        member = SimpleNamespace(id=1, owner=1, gender="male", birth_date=date(2000, 1, 1))

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