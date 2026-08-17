from django.db import transaction
from django.utils import timezone

from .models import (
    MembershipPlan,
    Subscription,
    SubscriptionItem,
)

from .billing import (
    get_next_billing_cycle_anchor,
    resolve_and_apply_subscription_period,
)

class LegacySubscriptionService:

    @staticmethod
    def create_legacy_subscription(
        *,
        club,
        member,
        plans,
    ):
        today = timezone.localtime().date()

        plans = list(
            MembershipPlan.objects.filter(
                id__in=[plan.id for plan in plans],
                club=club,
                is_deleted=False,
            )
        )

        if not plans:
            raise ValueError(
                "Legacy subscription requires at least one valid plan."
            )

        with transaction.atomic():

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
                },
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
                sub_obj.stripe_subscription_id = None

                sub_obj.save(
                    update_fields=[
                        "status",
                        "billing_method",
                        "stripe_subscription_id",
                    ]
                )

            # -------------------------------------------------
            # Existing plans
            # -------------------------------------------------

            for plan in plans:

                item, item_created = (
                    SubscriptionItem.objects.get_or_create(
                        subscription=sub_obj,
                        member=member,
                        plan=plan,
                        defaults={
                            "price_at_subscription": plan.price,
                            "stripe_price_id_at_subscription": (
                                plan.stripe_price_id
                            ),
                        },
                    )
                )

                if not item_created:
                    item.deleted_at = None
                    item.price_at_subscription = plan.price
                    item.stripe_price_id_at_subscription = (
                        plan.stripe_price_id
                    )

                    item.save(
                        update_fields=[
                            "deleted_at",
                            "price_at_subscription",
                            "stripe_price_id_at_subscription",
                        ]
                    )

            # -------------------------------------------------
            # This member already paid their joining fee
            # historically.
            # -------------------------------------------------

            member.has_paid_joining_fee = True
            member.has_been_charged_joining_fee = True

            member.save(
                update_fields=[
                    "has_paid_joining_fee",
                    "has_been_charged_joining_fee",
                ]
            )

            # -------------------------------------------------
            # Establish the normal billing period.
            #
            # No invoice is created.
            # No payment is created.
            # -------------------------------------------------

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

            return sub_obj