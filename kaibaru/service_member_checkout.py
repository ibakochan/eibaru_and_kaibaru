import stripe
from django.utils import timezone

from .billing import get_next_billing_cycle_anchor
from .pricing import calculate_joining_fee, calculate_subscription_pricing
from .discounts import (
    calculate_discounted_amount,
)
from .stripe_service import get_or_create_stripe_customer


class MemberSubscriptionCheckoutService:

    @staticmethod
    def create_checkout_session(
        *,
        club,
        member,
        plan,
        billing_user,
    ):
        """
        PURE EXTRACTION of checkout branch.
        NO LOGIC CHANGES.
        """

        today = timezone.localtime().date()

        stripe_customer_obj = get_or_create_stripe_customer(billing_user, club)

        # -------------------------
        # billing cycle anchor logic (UNCHANGED)
        # -------------------------
        billing_cycle_anchor = get_next_billing_cycle_anchor(
            today=today,
            anchor_day=club.stripe_anchor_date
        )

        subscription_data = {
            "metadata": {
                "member_id": member.id,
                "club_id": club.id,
                "plan_id": plan.id,
                "type": "checkout",
            },
        }

        if billing_cycle_anchor:
            now = int(timezone.now().timestamp())

            if billing_cycle_anchor <= now:
                billing_cycle_anchor = now + 60

            subscription_data["billing_cycle_anchor"] = billing_cycle_anchor
            subscription_data["proration_behavior"] = "none"

        # -------------------------
        # pricing calculations (UNCHANGED)
        # -------------------------
        pricing = calculate_subscription_pricing(
            club=club,
            member=member,
            plan=plan,
            plan_price=plan.price,
            today=today,
            mode=club.subscription_mode,
            anchor_day=club.stripe_anchor_date,
        )

        joining_fee = calculate_joining_fee(club, member)
        prorated_amount = pricing["final_amount"]
        remaining_days = pricing["proration"]["remaining_days"]

        next_month_amount = 0
        if (today.day > club.stripe_anchor_date) and club.subscription_mode == "monthly":
            next_month_amount = calculate_discounted_amount(
                club=club,
                member=member,
                plan=plan,
                base_amount=plan.price,
                apply_to="subscription",
            )

        # -------------------------
        # Stripe checkout session (UNCHANGED)
        # -------------------------
        session = stripe.checkout.Session.create(
            customer=stripe_customer_obj.stripe_customer_id,
            mode="subscription",
            payment_method_types=["card"],
            line_items=[
                {
                    "price": plan.stripe_price_id,
                    "quantity": 1,
                }
            ],
            metadata={
                "member_id": member.id,
                "club_id": club.id,
                "plan_id": plan.id,
                "type": "checkout",
            },
            subscription_data=subscription_data,
            success_url=f"https://{club.subdomain}.kaibaru.jp/?subscription=success",
            cancel_url=f"https://{club.subdomain}.kaibaru.jp/?subscription=cancel",
            stripe_account=club.stripe_account_id,

            custom_text={
                "submit": {
                    "message": (
                        f"今回のお支払い予定:\n"
                        f"・入会金: ¥{joining_fee}\n"
                        f"・日割り料金 ({remaining_days}日): ¥{prorated_amount}\n"
                        f"{'・翌月前払い: ¥' + str(next_month_amount) if next_month_amount else ''}\n\n"
                        f"※最終金額はシステム計算に基づき確定されます"
                    )
                }
            },
        )

        return {"id": session.id}