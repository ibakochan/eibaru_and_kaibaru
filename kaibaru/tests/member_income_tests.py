from django.test import SimpleTestCase

from kaibaru.service_member_income import allocate_fee_shares, settled_yen


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
