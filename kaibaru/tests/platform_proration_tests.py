from django.test import SimpleTestCase, override_settings

from kaibaru.service_platform_subscription import (
    bucket_proration_lines,
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


@override_settings(
    STRIPE_BASE_PRICE_ID="price_base",
    STRIPE_BASE_PRICE_ID_REDUCED="price_extra",
    STRIPE_MEMBER_PRICE_ID="price_member",
    STRIPE_MEMBER_PRICE_ID_REDUCED="price_member_reduced",
    STRIPE_CUSTOM_DOMAIN_PRICE_ID="price_domain",
    STRIPE_CUSTOM_DOMAIN_EXTRA_PRICE_ID="price_domain_extra",
)
class PendingProrationLineTests(SimpleTestCase):
    def test_positive_prorations_are_split_by_price(self):
        lines = [
            {"proration": True, "amount": 745, "price": {"id": "price_extra"}},
            {"proration": True, "amount": 40, "price": "price_member"},
            {"proration": True, "amount": 250, "price": {"id": "price_domain"}},
            {"proration": False, "amount": 2980, "price": {"id": "price_base"}},
            {"proration": True, "amount": -100, "price": {"id": "price_member"}},
            {
                "amount": 20,
                "pricing": {"price_details": {"price": "price_member_reduced"}},
                "parent": {"subscription_item_details": {"proration": True}},
            },
        ]
        self.assertEqual(
            bucket_proration_lines(lines),
            {
                "prorated_clubs_yen": 745,
                "prorated_domains_yen": 250,
                "prorated_members_yen": 60,
                "prorated_yen": 1055,
            },
        )

    def test_no_proration_lines_are_zero(self):
        self.assertEqual(
            bucket_proration_lines([
                {"proration": False, "amount": 2980, "price": {"id": "price_base"}},
            ]),
            {
                "prorated_clubs_yen": 0,
                "prorated_domains_yen": 0,
                "prorated_members_yen": 0,
                "prorated_yen": 0,
            },
        )
