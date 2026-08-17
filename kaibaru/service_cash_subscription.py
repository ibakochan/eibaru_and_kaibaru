from django.db import transaction
from django.utils import timezone
from datetime import datetime, timezone as dt_timezone

from .models import (
    Subscription,
    SubscriptionItem,
    Invoice,
    InvoiceItem,
)

from .pricing import (
    calculate_joining_fee,
    calculate_subscription_pricing,
)

from .discounts import (
    calculate_discounted_amount,
)

from .billing import (
    get_next_billing_cycle_anchor,
    resolve_and_apply_subscription_period,
)


class MemberCashSubscriptionService:

    @staticmethod
    def create_cash_subscription(
        *,
        club,
        member,
        plan,
    ):

        today = timezone.localtime().date()


        with transaction.atomic():

            # ==========================================
            # CREATE SUBSCRIPTION
            # ==========================================

            sub_obj, created = Subscription.objects.get_or_create(
                owner=member.owner,
                club=club,
                defaults={
                    "stripe_subscription_id": None,
                    "status": "active",
                    "billing_method": "cash",
                    "billing_mode": club.subscription_mode,
                    "billing_anchor_day": club.stripe_anchor_date,
                    "cancel_at_period_end": False,
                }
            )


            if not created:

                if sub_obj.status in [
                    "active",
                    "trialing",
                    "past_due",
                    "pending",
                ]:
                    raise Exception(
                        "Already has active subscription"
                    )


                sub_obj.status = "active"
                sub_obj.billing_method = "cash"

                sub_obj.save(
                    update_fields=[
                        "status",
                        "billing_method",
                    ]
                )


            # ==========================================
            # CREATE SUBSCRIPTION ITEM
            # ==========================================

            item, created = SubscriptionItem.objects.get_or_create(
                subscription=sub_obj,
                member=member,
                plan=plan,
                defaults={
                    "price_at_subscription": plan.price,
                    "stripe_price_id_at_subscription": plan.stripe_price_id,
                }
            )


            if not created:

                item.deleted_at = None
                item.price_at_subscription = plan.price
                item.stripe_price_id_at_subscription = plan.stripe_price_id

                item.save(
                    update_fields=[
                        "deleted_at",
                        "price_at_subscription",
                        "stripe_price_id_at_subscription",
                    ]
                )


            # ==========================================
            # CALCULATE PRICING
            # SAME AS STRIPE CHECKOUT
            # ==========================================

            pricing = calculate_subscription_pricing(
                club=club,
                member=member,
                plan=plan,
                plan_price=plan.price,
                today=today,
                mode=club.subscription_mode,
                anchor_day=club.stripe_anchor_date,
            )


            total_amount = 0


            # ==========================================
            # CREATE LOCAL INVOICE
            # ==========================================

            invoice = Invoice.objects.create(
                club=club,
                payer=member.owner,
                payer_name=member.owner.get_full_name(),
                payer_email=member.owner.email,
                subscription=sub_obj,
                billing_reason="initial_subscription",
                status="open",
                amount_due=0,
                amount_paid=0,
                currency="jpy",
            )


            # ==========================================
            # JOINING FEE
            # ==========================================

            if (
                club.joining_fee > 0
                and not member.has_been_charged_joining_fee
            ):

                joining_fee = calculate_joining_fee(
                    club,
                    member,
                )

                if joining_fee > 0:

                    InvoiceItem.objects.create(
                        invoice=invoice,
                        member=member,
                        description=f"{member.full_name} 入会金",
                        amount=joining_fee,
                        quantity=1,
                    )

                    total_amount += joining_fee



            # ==========================================
            # PRORATION
            # ==========================================

            prorated_amount = pricing["final_amount"]


            if prorated_amount > 0:

                InvoiceItem.objects.create(
                    invoice=invoice,
                    member=member,
                    description = (
                        f"{member.full_name}さんの"
                        f"{pricing['proration']['remaining_days']}日分の{plan.name}の日割り計算"
                    ),
                    amount=prorated_amount,
                    quantity=1,
                )

                total_amount += prorated_amount



            # ==========================================
            # MONTHLY NEXT MONTH PREPAY
            # ==========================================

            if (
                club.subscription_mode == "monthly"
                and today.day > club.stripe_anchor_date
            ):

                next_month_amount = calculate_discounted_amount(
                    club=club,
                    member=member,
                    plan=plan,
                    base_amount=plan.price,
                    apply_to="subscription",
                )


                InvoiceItem.objects.create(
                    invoice=invoice,
                    member=member,
                    description=f"{member.full_name}の{plan.name}翌月分前払い",
                    amount=next_month_amount,
                    quantity=1,
                )

                total_amount += next_month_amount



            invoice.amount_due = total_amount

            invoice.save(
                update_fields=[
                    "amount_due",
                ]
            )


            # ==========================================
            # APPLY ACCESS PERIOD
            # SAME RESULT AS invoice.paid
            # ==========================================

            period_end_ts = get_next_billing_cycle_anchor(
                today,
                sub_obj.billing_anchor_day,
            )

            resolve_and_apply_subscription_period(
                sub_obj,
                period_end_ts,
                today,
            )


            sub_obj.status = "active"

            sub_obj.save(
                update_fields=[
                    "status",
                    "current_period_end",
                    "access_until",
                ]
            )


            return {
                "success": True,
                "subscription_id": sub_obj.id,
                "invoice_id": invoice.id,
                "amount_due": total_amount,
                "message": "Cash subscription created",
            }