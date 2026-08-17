from django.db import transaction
from django.utils import timezone

from .models import SubscriptionItem


class CashSubscriptionItemService:

    # =========================================================
    # RESUME ITEM
    # =========================================================
    @staticmethod
    def resume_item(
        *,
        item,
        subscription,
        club,
    ):

        with transaction.atomic():

            item.deleted_at = None
            item.access_until = None

            item.save(
                update_fields=[
                    "deleted_at",
                    "access_until",
                ]
            )


            subscription.cancel_at_period_end = False

            subscription.save(
                update_fields=[
                    "cancel_at_period_end",
                ]
            )


        return item



    # =========================================================
    # CANCEL ITEM
    # =========================================================
    @staticmethod
    def cancel_item(
        *,
        item,
        subscription,
        club,
    ):

        active_items_count = SubscriptionItem.objects.filter(
            subscription=subscription,
            deleted_at__isnull=True,
        ).count()


        is_last_item = active_items_count <= 1


        with transaction.atomic():

            if is_last_item:

                # same business meaning as Stripe:
                # no active plans remain after period end

                subscription.cancel_at_period_end = True

                subscription.save(
                    update_fields=[
                        "cancel_at_period_end",
                    ]
                )


            item.deleted_at = timezone.now()
            item.access_until = subscription.access_until

            item.save(
                update_fields=[
                    "deleted_at",
                    "access_until",
                ]
            )


        return item



    # =========================================================
    # CHANGE PLAN
    # =========================================================
    @staticmethod
    def change_plan(
        *,
        item,
        new_plan,
        subscription,
        club,
        old_item_is_grace: bool,
    ):


        now = timezone.now()


        with transaction.atomic():

            # -------------------------------------------------
            # Close old item
            # -------------------------------------------------

            if item.deleted_at is None:
                item.deleted_at = now


            if item.access_until is None:
                item.access_until = subscription.access_until


            item.save(
                update_fields=[
                    "deleted_at",
                    "access_until",
                ]
            )


            # -------------------------------------------------
            # Create / revive new scheduled item
            # -------------------------------------------------

            new_item = (
                SubscriptionItem.objects
                .select_for_update()
                .filter(
                    subscription=subscription,
                    member=item.member,
                    plan=new_plan,
                )
                .first()
            )


            if new_item:

                new_item.deleted_at = None
                new_item.access_start = item.access_until
                new_item.source_item = item

                new_item.price_at_subscription = new_plan.price
                new_item.stripe_price_id_at_subscription = (
                    new_plan.stripe_price_id
                )


                new_item.save(
                    update_fields=[
                        "deleted_at",
                        "access_start",
                        "source_item",
                        "price_at_subscription",
                        "stripe_price_id_at_subscription",
                    ]
                )


            else:

                new_item = SubscriptionItem.objects.create(
                    subscription=subscription,
                    member=item.member,
                    plan=new_plan,
                    price_at_subscription=new_plan.price,
                    stripe_price_id_at_subscription=new_plan.stripe_price_id,
                    source_item=item,
                    access_start=item.access_until,
                )


            # For cash there is no Stripe subscription item ID.

        return new_item



    # =========================================================
    # CANCEL PLAN CHANGE
    # =========================================================
    @staticmethod
    def cancel_change(
        *,
        new_item,
        old_item,
        subscription,
        club,
        old_plan_deleted,
    ):

        now = timezone.now()


        with transaction.atomic():

            # -------------------------------------------------
            # Restore old item
            # -------------------------------------------------

            old_item.deleted_at = None
            old_item.access_until = None
            old_item.source_item = None


            old_item.save(
                update_fields=[
                    "deleted_at",
                    "access_until",
                    "source_item",
                ]
            )


            # -------------------------------------------------
            # Remove scheduled new item
            # -------------------------------------------------

            new_item.deleted_at = now
            new_item.access_until = now
            new_item.source_item = None


            new_item.save(
                update_fields=[
                    "deleted_at",
                    "access_until",
                    "source_item",
                ]
            )


        return old_item