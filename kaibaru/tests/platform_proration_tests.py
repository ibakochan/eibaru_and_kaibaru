from django.test import SimpleTestCase

from kaibaru.service_platform_subscription import (
    quantity_change_key,
    split_quantity_changes,
)


class QuantityProrationSplitTests(SimpleTestCase):
    def test_raise_is_prorated_and_cut_is_separate(self):
        existing = {
            "price_members": {"id": "si_members", "quantity": 10},
            "price_extra": {"id": "si_extra", "quantity": 2},
        }
        increases, decreases = split_quantity_changes(
            existing,
            ["price_members", "price_extra", "price_domain"],
            {"price_members": 40, "price_extra": 1, "price_domain": 1},
        )
        self.assertEqual(
            increases,
            [
                {"id": "si_members", "quantity": 40},
                {"price": "price_domain", "quantity": 1},
            ],
        )
        self.assertEqual(decreases, [{"id": "si_extra", "quantity": 1}])

    def test_matching_quantity_is_omitted(self):
        existing = {"price_members": {"id": "si_members", "quantity": 10}}
        increases, decreases = split_quantity_changes(
            existing,
            ["price_members"],
            {"price_members": 10},
        )
        self.assertEqual(increases, [])
        self.assertEqual(decreases, [])

    def test_removing_a_price_is_a_cut(self):
        existing = {"price_domain": {"id": "si_domain", "quantity": 1}}
        increases, decreases = split_quantity_changes(
            existing,
            ["price_domain"],
            {},
        )
        self.assertEqual(increases, [])
        self.assertEqual(decreases, [{"id": "si_domain", "deleted": True}])

    def test_same_change_keeps_the_same_idempotency_key(self):
        changes = [{"id": "si_members", "quantity": 40}]
        first = quantity_change_key("platform-mut-9-up", changes)
        second = quantity_change_key("platform-mut-9-up", changes)
        other = quantity_change_key(
            "platform-mut-9-up",
            [{"id": "si_members", "quantity": 41}],
        )
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertLessEqual(len(first), 255)
