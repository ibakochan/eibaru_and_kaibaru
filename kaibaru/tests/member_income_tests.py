from django.test import SimpleTestCase

from kaibaru.service_member_income import (
    allocate_fee_shares,
    fee_from_invoice_payments,
    income_remainder,
    settled_yen,
)


class MemberIncomeSettlementTests(SimpleTestCase):
    def test_stripe_net_subtracts_the_whole_fee(self):
        # ¥9,999 charge, Stripe dashboard fees ¥496, net ¥9,503.
        self.assertEqual(settled_yen(9999, "stripe", 496), 9503)

    def test_cash_keeps_the_billed_amount(self):
        self.assertEqual(settled_yen(40750, "cash", None), 40750)
        self.assertEqual(settled_yen(6500, "bank_transfer", 100), 6500)

    def test_unknown_stripe_fee_keeps_the_billed_amount(self):
        self.assertEqual(settled_yen(9999, "stripe", None), 9999)

    def test_shared_charge_fee_is_split_across_reservations(self):
        shares = allocate_fee_shares([6000, 3999], 496)
        self.assertEqual(sum(shares), 496)
        self.assertEqual(
            settled_yen(6000, "stripe", shares[0])
            + settled_yen(3999, "stripe", shares[1]),
            9999 - 496,
        )

    def test_cycle_invoice_payment_intent_fee(self):
        rows = [{
            "status": "paid",
            "payment": {
                "type": "payment_intent",
                "payment_intent": {
                    "id": "pi_cycle",
                    "latest_charge": {
                        "balance_transaction": {"fee": 496},
                    },
                },
            },
        }]
        self.assertEqual(fee_from_invoice_payments(rows), 496)

    def test_cycle_invoice_charge_fee(self):
        rows = [{
            "status": "paid",
            "payment": {
                "type": "charge",
                "charge": {"balance_transaction": {"fee": 360}},
            },
        }]
        self.assertEqual(fee_from_invoice_payments(rows), 360)

    def test_unreadable_cycle_invoice_fee_stays_unknown(self):
        rows = [{"status": "paid", "payment": {"type": "payment_intent"}}]
        self.assertIsNone(fee_from_invoice_payments(rows))

    def test_remainder_subtracts_platform_fee_and_expenses(self):
        self.assertEqual(income_remainder(100000, 14080, 3200), 82720)
