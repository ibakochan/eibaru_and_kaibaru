from django.test import SimpleTestCase, override_settings

from kaibaru.service_platform_billing_history import (
    _parse_platform_invoice_lines,
    format_platform_cycle_receipt,
)


class PlatformCycleReceiptTests(SimpleTestCase):
    def test_receipt_matches_invoice_lines_and_omits_stripe_fee(self):
        entry = {
            "payment_year": 2026,
            "payment_month": 8,
            "paid_on": "2026-08-12",
            "invoice_number": "POIRSVA4-0001",
            "fee_source_year": 2026,
            "fee_source_month": 7,
            "club_base_yen": 2980,
            "extra_clubs": 0,
            "club_extra_yen": 0,
            "full_price_members": 110,
            "member_full_yen": 11000,
            "reduced_members": 0,
            "member_reduced_yen": 0,
            "cash_fee_yen": 91,
            "cash_invoice_base_yen": 9101,
            "cash_invoice_count": 7,
            "other_yen": 0,
            "total_yen": 13980,
            "kaibaru_application_fee_yen": 500,
        }

        text = format_platform_cycle_receipt(entry)

        self.assertEqual(
            text,
            "\n".join([
                "2026年8月の請求",
                "合計 ¥13,980 · 支払日 2026-08-12",
                "",
                "Kaibaru プラットフォーム利用料",
                "お支払い月：2026年8月（支払日 2026-08-12）",
                "請求書番号：POIRSVA4-0001",
                "",
                "クラブ基本料",
                "¥2,980",
                "",
                "会員 100名 × ¥100",
                "¥10,000",
                "",
                "会員 10名 × ¥50",
                "¥500",
                "",
                "現金・振込手数料1%（2026年7月分の請求に対する手数料）",
                "¥91",
                "",
                "Kaibaru請求 合計",
                "¥13,980",
                "",
                "※ 現金・振込の1%は、プラットフォーム請求月の前月（2026年7月）に発生した現金・振込請求が対象です。 対象合計 ¥9,101（7件）。",
            ]),
        )
        self.assertNotIn("Kaibaru手数料1%", text)
        self.assertNotIn("会員のStripe決済", text)
        self.assertNotIn("独自ドメイン", text)


@override_settings(
    STRIPE_BASE_PRICE_ID="price_base",
    STRIPE_BASE_PRICE_ID_REDUCED="price_base_reduced",
    STRIPE_MEMBER_PRICE_ID="price_member",
    STRIPE_MEMBER_PRICE_ID_REDUCED="price_member_reduced",
    STRIPE_CUSTOM_DOMAIN_PRICE_ID="price_domain",
    STRIPE_CUSTOM_DOMAIN_EXTRA_PRICE_ID="price_domain_extra",
)
class PlatformInvoiceDomainLineTests(SimpleTestCase):
    def test_domain_prices_are_their_own_lines(self):
        parsed = _parse_platform_invoice_lines([
            {
                "amount": 2980,
                "quantity": 1,
                "price": {"id": "price_base"},
            },
            {
                "amount": 500,
                "quantity": 1,
                "price": {"id": "price_domain"},
            },
            {
                "amount": 500,
                "quantity": 2,
                "price": {"id": "price_domain_extra"},
            },
        ])

        self.assertEqual(parsed["domain_yen"], 500)
        self.assertEqual(parsed["extra_domains"], 2)
        self.assertEqual(parsed["domain_extra_yen"], 500)
        self.assertEqual(parsed["domain_count"], 3)
        self.assertEqual(parsed["other_yen"], 0)
        self.assertEqual(parsed["subscription_yen"], 2980 + 1000)

        text = format_platform_cycle_receipt({
            "payment_year": 2026,
            "payment_month": 11,
            "club_base_yen": 2980,
            "extra_clubs": 0,
            **parsed,
            "cash_fee_yen": 0,
            "cash_invoice_base_yen": 0,
            "total_yen": parsed["subscription_yen"],
        })
        self.assertIn("独自ドメイン", text)
        self.assertIn("追加ドメイン × 2", text)
        self.assertIn("¥500", text)
