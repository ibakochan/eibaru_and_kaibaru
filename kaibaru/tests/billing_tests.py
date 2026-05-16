from types import SimpleNamespace
from django.test import SimpleTestCase
from datetime import date, datetime, timezone as dt_timezone


from kaibaru.billing import (
    resolve_and_apply_subscription_period,
    extract_subscription_id_from_invoice,
    get_next_billing_cycle_anchor,
    calculate_monthly_proration,
    calculate_regular_proration,
    is_near_anchor, is_valid_billing_day,
    validate_plan_change_window,
    get_cancel_quantity_action,
    should_set_monthly_resume_prevention,
    should_cancel_subscription,
    get_cancel_success_message,
    can_resume_subscription,
    get_resume_item_action,
    should_charge_resume_next_month,
    get_resume_charge_amount,
    get_resume_success_message,
)


class BillingRulesTests(SimpleTestCase):

        # -------------------------
    # PRORATION / ANCHOR TESTS
    # -------------------------

    def test_extract_subscription_id_from_invoice_direct(self):
        invoice = {
            "subscription": "sub_123"
        }

        result = extract_subscription_id_from_invoice(invoice)

        self.assertEqual(result, "sub_123")

    def test_extract_subscription_id_from_invoice_parent(self):
        invoice = {
            "parent": {
                "subscription_details": {
                    "subscription": "sub_456"
                }
            }
        }

        result = extract_subscription_id_from_invoice(invoice)

        self.assertEqual(result, "sub_456")

    def test_extract_subscription_id_from_invoice_lines(self):
        invoice = {
            "lines": {
                "data": [
                    {
                        "parent": {
                            "subscription_item_details": {
                                "subscription": "sub_789"
                            }
                        }
                    }
                ]
            }
        }

        result = extract_subscription_id_from_invoice(invoice)

        self.assertEqual(result, "sub_789")

    def test_extract_subscription_id_from_invoice_none(self):
        invoice = {}

        result = extract_subscription_id_from_invoice(invoice)

        self.assertIsNone(result)

    def test_resolve_and_apply_subscription_period_regular(self):
        sub = SimpleNamespace(
            billing_mode="regular",
            billing_anchor_day=20,
            current_period_end=None,
            access_until=None,
        )

        period_end_ts = int(
            datetime(
                2026, 5, 20,
                tzinfo=dt_timezone.utc
            ).timestamp()
        )

        resolve_and_apply_subscription_period(
            sub=sub,
            period_end_ts=period_end_ts,
            today=date(2026, 4, 10),
        )

        expected = datetime(
            2026, 5, 20,
            tzinfo=dt_timezone.utc
        )

        self.assertEqual(sub.current_period_end, expected)
        self.assertEqual(sub.access_until, expected)

    def test_resolve_and_apply_subscription_period_monthly(self):
        sub = SimpleNamespace(
            billing_mode="monthly",
            billing_anchor_day=20,
            current_period_end=None,
            access_until=None,
        )

        period_end_ts = int(
            datetime(
                2026, 5, 20,
                tzinfo=dt_timezone.utc
            ).timestamp()
        )

        resolve_and_apply_subscription_period(
            sub=sub,
            period_end_ts=period_end_ts,
            today=date(2026, 4, 10),
        )

        self.assertEqual(
            sub.current_period_end,
            datetime(
                2026, 5, 20,
                tzinfo=dt_timezone.utc
            )
        )

        self.assertEqual(
            sub.access_until,
            datetime(
                2026, 5, 31, 23, 59, 59,
                tzinfo=dt_timezone.utc
            )
        )

    def test_resolve_and_apply_subscription_period_overrides_near_today(self):
        sub = SimpleNamespace(
            billing_mode="regular",
            billing_anchor_day=20,
            current_period_end=None,
            access_until=None,
        )

        # Stripe gave today-ish date -> should override to next anchor
        period_end_ts = int(
            datetime(
                2026, 4, 10,
                tzinfo=dt_timezone.utc
            ).timestamp()
        )

        resolve_and_apply_subscription_period(
            sub=sub,
            period_end_ts=period_end_ts,
            today=date(2026, 4, 10),
        )

        expected = datetime(
            2026, 4, 20,
            tzinfo=dt_timezone.utc
        )

        self.assertEqual(sub.current_period_end, expected)
        self.assertEqual(sub.access_until, expected)

    def test_get_next_billing_cycle_anchor_same_month(self):
        result = get_next_billing_cycle_anchor(
            today=date(2026, 4, 10),
            anchor_day=20,
        )

        expected = int(
            datetime(2026, 4, 20, tzinfo=dt_timezone.utc).timestamp()
        )

        self.assertEqual(result, expected)

    def test_get_next_billing_cycle_anchor_next_month(self):
        result = get_next_billing_cycle_anchor(
            today=date(2026, 4, 25),
            anchor_day=20,
        )

        expected = int(
            datetime(2026, 5, 20, tzinfo=dt_timezone.utc).timestamp()
        )

        self.assertEqual(result, expected)

    def test_get_next_billing_cycle_anchor_caps_day(self):
        result = get_next_billing_cycle_anchor(
            today=date(2026, 2, 1),
            anchor_day=31,
        )

        expected = int(
            datetime(2026, 2, 28, tzinfo=dt_timezone.utc).timestamp()
        )

        self.assertEqual(result, expected)

    def test_calculate_monthly_proration(self):
        result = calculate_monthly_proration(
            today=date(2026, 4, 10),
            monthly_price=3000,
        )

        self.assertEqual(result["days_in_month"], 30)
        self.assertEqual(result["remaining_days"], 21)
        self.assertEqual(result["prorated_amount"], 2100)

    def test_calculate_regular_proration(self):
        result = calculate_regular_proration(
            today=date(2026, 4, 10),
            anchor_day=20,
            monthly_price=3000,
        )

        self.assertEqual(
            result["prev_anchor_date"],
            date(2026, 3, 20)
        )

        self.assertEqual(
            result["next_anchor_date"],
            date(2026, 4, 20)
        )

        self.assertEqual(result["remaining_days"], 10)
        self.assertEqual(result["billing_period_days"], 31)
        self.assertEqual(result["prorated_amount"], 967)

    def test_is_valid_billing_day_true(self):
        self.assertTrue(
            is_valid_billing_day(date(2026, 4, 10))
        )

    def test_is_valid_billing_day_false_day_1(self):
        self.assertFalse(
            is_valid_billing_day(date(2026, 4, 1))
        )

    def test_is_valid_billing_day_false_day_28(self):
        self.assertFalse(
            is_valid_billing_day(date(2026, 4, 28))
        )

    def test_is_near_anchor_true_same_day(self):
        self.assertTrue(
            is_near_anchor(
                date(2026, 4, 20),
                20
            )
        )

    def test_is_near_anchor_true_one_day_before(self):
        self.assertTrue(
            is_near_anchor(
                date(2026, 4, 19),
                20
            )
        )

    def test_is_near_anchor_false_far_day(self):
        self.assertFalse(
            is_near_anchor(
                date(2026, 4, 10),
                20
            )
        )


    def test_cancel_window_blocks_day_1(self):
        subscription = SimpleNamespace(
            billing_anchor_day=20,
            current_period_end=None,
            billing_mode="monthly",
        )

        result = validate_plan_change_window(
            today=date(2026, 4, 1),
            subscription=subscription,
        )

        self.assertEqual(
            result,
            "この期間はプランの変更ができません。毎月2日〜27日のみ変更可能です。"
        )

    def test_cancel_window_blocks_day_28(self):
        subscription = SimpleNamespace(
            billing_anchor_day=20,
            current_period_end=None,
            billing_mode="monthly",
        )

        result = validate_plan_change_window(
            today=date(2026, 4, 28),
            subscription=subscription,
        )

        self.assertIsNotNone(result)

    def test_cancel_window_blocks_monthly_processing(self):
        subscription = SimpleNamespace(
            billing_anchor_day=20,
            current_period_end=datetime(2026, 4, 27, tzinfo=dt_timezone.utc),
            billing_mode="monthly",
        )

        result = validate_plan_change_window(
            today=date(2026, 4, 25),
            subscription=subscription,
        )

        self.assertEqual(
            result,
            "請求処理中のため、この期間はプランの変更ができません。しばらくしてからお試しください。"
        )

    def test_cancel_window_allows_valid_day(self):
        subscription = SimpleNamespace(
            billing_anchor_day=20,
            current_period_end=None,
            billing_mode="regular",
        )

        result = validate_plan_change_window(
            today=date(2026, 4, 10),
            subscription=subscription,
        )

        self.assertIsNone(result)

    def test_get_cancel_quantity_action_delete(self):
        self.assertEqual(
            get_cancel_quantity_action(1),
            ("delete", None)
        )

    def test_get_cancel_quantity_action_modify(self):
        self.assertEqual(
            get_cancel_quantity_action(3),
            ("modify", 2)
        )

    def test_should_set_monthly_resume_prevention_true(self):
        subscription = SimpleNamespace(
            billing_mode="monthly",
            billing_anchor_day=20,
        )

        result = should_set_monthly_resume_prevention(
            date(2026, 4, 25),
            subscription
        )

        self.assertTrue(result)

    def test_should_set_monthly_resume_prevention_false(self):
        subscription = SimpleNamespace(
            billing_mode="monthly",
            billing_anchor_day=20,
        )

        result = should_set_monthly_resume_prevention(
            date(2026, 4, 10),
            subscription
        )

        self.assertFalse(result)

    def test_should_cancel_subscription(self):
        self.assertTrue(should_cancel_subscription(False))
        self.assertFalse(should_cancel_subscription(True))

    def test_success_message_with_date(self):
        subscription = SimpleNamespace(
            access_until=datetime(2026, 4, 30)
        )

        msg = get_cancel_success_message(subscription)

        self.assertEqual(
            msg,
            "プランは削除されました。 2026/04/30 まで利用可能です"
        )

    def test_success_message_without_date(self):
        subscription = SimpleNamespace(
            access_until=None
        )

        msg = get_cancel_success_message(subscription)

        self.assertEqual(
            msg,
            "プランは削除されました。 次回更新日 まで利用可能です"
        )



    # -------------------------
    # RESUME TESTS
    # -------------------------

    def test_can_resume_subscription_true(self):
        now = datetime(2026, 4, 10, tzinfo=dt_timezone.utc)

        item = SimpleNamespace(
            deleted_at=datetime(2026, 4, 1, tzinfo=dt_timezone.utc),
            access_until=datetime(2026, 4, 30, tzinfo=dt_timezone.utc),
        )

        self.assertTrue(can_resume_subscription(item, now))

    def test_can_resume_subscription_false_when_not_deleted(self):
        now = datetime(2026, 4, 10, tzinfo=dt_timezone.utc)

        item = SimpleNamespace(
            deleted_at=None,
            access_until=datetime(2026, 4, 30, tzinfo=dt_timezone.utc),
        )

        self.assertFalse(can_resume_subscription(item, now))

    def test_can_resume_subscription_false_when_expired(self):
        now = datetime(2026, 4, 10, tzinfo=dt_timezone.utc)

        item = SimpleNamespace(
            deleted_at=datetime(2026, 4, 1, tzinfo=dt_timezone.utc),
            access_until=datetime(2026, 4, 5, tzinfo=dt_timezone.utc),
        )

        self.assertFalse(can_resume_subscription(item, now))

    def test_get_resume_item_action_modify(self):
        existing_item = {
            "id": "si_123",
            "quantity": 2,
        }

        result = get_resume_item_action(existing_item)

        self.assertEqual(
            result,
            ("modify", "si_123", 3)
        )

    def test_get_resume_item_action_create(self):
        result = get_resume_item_action(None)

        self.assertEqual(
            result,
            ("create", None, 1)
        )

    def test_should_charge_resume_next_month_true(self):
        subscription = SimpleNamespace(
            billing_mode="monthly",
            billing_anchor_day=20,
        )

        item = SimpleNamespace(
            monthly_double_resume_charge_prevention=False
        )

        result = should_charge_resume_next_month(
            date(2026, 4, 25),
            subscription,
            item
        )

        self.assertTrue(result)

    def test_should_charge_resume_next_month_false_before_anchor(self):
        subscription = SimpleNamespace(
            billing_mode="monthly",
            billing_anchor_day=20,
        )

        item = SimpleNamespace(
            monthly_double_resume_charge_prevention=False
        )

        result = should_charge_resume_next_month(
            date(2026, 4, 10),
            subscription,
            item
        )

        self.assertFalse(result)

    def test_should_charge_resume_next_month_false_if_already_prevented(self):
        subscription = SimpleNamespace(
            billing_mode="monthly",
            billing_anchor_day=20,
        )

        item = SimpleNamespace(
            monthly_double_resume_charge_prevention=True
        )

        result = should_charge_resume_next_month(
            date(2026, 4, 25),
            subscription,
            item
        )

        self.assertFalse(result)

    def test_get_resume_charge_amount_uses_saved_price(self):
        item = SimpleNamespace(
            price_at_subscription=5000,
            plan=SimpleNamespace(price=7000),
        )

        self.assertEqual(
            get_resume_charge_amount(item),
            5000
        )

    def test_get_resume_charge_amount_falls_back_to_plan_price(self):
        item = SimpleNamespace(
            price_at_subscription=None,
            plan=SimpleNamespace(price=7000),
        )

        self.assertEqual(
            get_resume_charge_amount(item),
            7000
        )

    def test_get_resume_success_message(self):
        self.assertEqual(
            get_resume_success_message(),
            "解約を取り消しました。プランが再開されました"
        )