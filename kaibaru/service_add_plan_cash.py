from django.db import transaction
from django.utils import timezone

from .models import (
    SubscriptionItem,
    Invoice,
    InvoiceItem,
    Member,
)

from .pricing import (
    calculate_joining_fee,
    calculate_subscription_pricing,
)

from .discounts import calculate_discounted_amount


class CashAddPlanService:

    @staticmethod
    def add_plan_to_existing_subscription(
        *,
        club,
        member,
        plan,
        subscription,
    ):

        today = timezone.localtime().date()


        with transaction.atomic():

            # =====================================================
            # CALCULATE SAME AMOUNTS AS STRIPE ADD PLAN
            # =====================================================

            pricing = calculate_subscription_pricing(
                club=club,
                member=member,
                plan=plan,
                plan_price=plan.price,
                today=today,
                mode=subscription.billing_mode,
                anchor_day=subscription.billing_anchor_day,
            )


            joining_fee_amount = 0
            proration_amount = pricing["final_amount"]
            next_month_amount = 0


            if (
                club.joining_fee > 0
                and not member.has_been_charged_joining_fee
            ):

                joining_fee_amount = calculate_joining_fee(
                    club,
                    member,
                )


            if (
                subscription.billing_mode == "monthly"
                and today.day > subscription.billing_anchor_day
            ):

                next_month_amount = calculate_discounted_amount(
                    club=club,
                    member=member,
                    plan=plan,
                    base_amount=plan.price,
                    apply_to="subscription",
                )


            # =====================================================
            # CREATE LOCAL INVOICE
            # =====================================================

            invoice = Invoice.objects.create(
                club=club,
                payer=subscription.owner,
                payer_name=subscription.owner.get_full_name(),
                payer_email=subscription.owner.email,
                subscription=subscription,
                billing_reason="add_plan",
                status="open",
                amount_due=0,
                amount_paid=0,
                currency="jpy",
            )


            total = 0


            # =====================================================
            # JOINING FEE
            # =====================================================

            if joining_fee_amount > 0:

                InvoiceItem.objects.create(
                    invoice=invoice,
                    member=member,
                    description=f"{member.full_name} 入会金",
                    amount=joining_fee_amount,
                    quantity=1,
                )

                total += joining_fee_amount



            # =====================================================
            # PRORATION
            # =====================================================

            if proration_amount > 0:

                InvoiceItem.objects.create(
                    invoice=invoice,
                    member=member,
                    description = (
                        f"{member.full_name}さんの"
                        f"{pricing['proration']['remaining_days']}日分の{plan.name}の日割り計算"
                    ),
                    amount=proration_amount,
                    quantity=1,
                )

                total += proration_amount



            # =====================================================
            # NEXT MONTH PREPAY
            # =====================================================

            if next_month_amount > 0:

                InvoiceItem.objects.create(
                    invoice=invoice,
                    member=member,
                    description=f"{member.full_name}の{plan.name}翌月分前払い",
                    amount=next_month_amount,
                    quantity=1,
                )

                total += next_month_amount



            invoice.amount_due = total

            invoice.save(
                update_fields=[
                    "amount_due",
                ]
            )



            # =====================================================
            # UPDATE SUBSCRIPTION ITEM IMMEDIATELY
            # (same as Stripe after successful payment)
            # =====================================================

            item = SubscriptionItem.objects.filter(
                subscription=subscription,
                member=member,
                plan=plan,
            ).first()


            if item:

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

            else:

                SubscriptionItem.objects.create(
                    subscription=subscription,
                    member=member,
                    plan=plan,
                    price_at_subscription=plan.price,
                    stripe_price_id_at_subscription=plan.stripe_price_id,
                )


            return {
                "success": True,
                "invoice_id": invoice.id,
                "amount_due": total,
                "message": "Cash invoice created",
            }