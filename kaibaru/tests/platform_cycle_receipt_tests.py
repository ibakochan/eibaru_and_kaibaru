from django.test import SimpleTestCase

from kaibaru.service_platform_billing_history import format_platform_cycle_receipt


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
