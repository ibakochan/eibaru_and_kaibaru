import stripe
from django.conf import settings
from django.utils import timezone
import logging

from .models import SubscriptionItem
from .billing import get_cancel_quantity_action

logger = logging.getLogger(__name__)

stripe.api_key = settings.STRIPE_SECRET_KEY


class SubscriptionItemService:

    # =========================================================
    # CANCEL ITEM (PURE EXECUTION)
    # =========================================================
    @staticmethod
    def cancel_item(*, item, subscription, club):
        stripe_sub = stripe.Subscription.retrieve(
            subscription.stripe_subscription_id,
            stripe_account=club.stripe_account_id,
            expand=["items.data"]
        )

        stripe_item = next(
            (i for i in stripe_sub["items"]["data"]
             if i["id"] == item.stripe_subscription_item_id),
            None
        )

        if not stripe_item:
            raise Exception("Stripe item not found")

        current_qty = stripe_item["quantity"]
        action, new_qty = get_cancel_quantity_action(current_qty)

        if action == "delete":
            stripe.SubscriptionItem.delete(
                stripe_item["id"],
                proration_behavior="none",
                stripe_account=club.stripe_account_id
            )
        else:
            stripe.SubscriptionItem.modify(
                stripe_item["id"],
                quantity=new_qty,
                proration_behavior="none",
                stripe_account=club.stripe_account_id
            )

        item.deleted_at = timezone.now()
        item.access_until = subscription.access_until
        item.save(update_fields=["deleted_at", "access_until"])

        return item

    # =========================================================
    # CHANGE PLAN (PURE EXECUTION)
    # =========================================================
    @staticmethod
    def change_plan(
        *,
        item,
        new_plan,
        subscription,
        club,
        old_item_is_grace: bool
    ):
        stripe_sub = stripe.Subscription.retrieve(
            subscription.stripe_subscription_id,
            stripe_account=club.stripe_account_id,
            expand=["items.data"]
        )

        # =========================================================
        # 1. STRIPE: decrement OLD plan item
        #    (SKIPPED during grace period — IMPORTANT FIX)
        # =========================================================
        if not old_item_is_grace:
            old_stripe_item = next(
                (i for i in stripe_sub["items"]["data"]
                 if i["id"] == item.stripe_subscription_item_id),
                None
            )

            if not old_stripe_item:
                raise Exception("Stripe item not found")

            old_qty = old_stripe_item["quantity"]

            if old_qty > 1:
                stripe.SubscriptionItem.modify(
                    old_stripe_item["id"],
                    quantity=old_qty - 1,
                    proration_behavior="none",
                    stripe_account=club.stripe_account_id
                )
            else:
                stripe.SubscriptionItem.delete(
                    old_stripe_item["id"],
                    proration_behavior="none",
                    stripe_account=club.stripe_account_id
                )

        # =========================================================
        # 2. STRIPE: increment or create NEW plan item
        # =========================================================
        new_stripe_item = next(
            (i for i in stripe_sub["items"]["data"]
             if i["price"]["id"] == new_plan.stripe_price_id),
            None
        )

        if new_stripe_item:
            stripe.SubscriptionItem.modify(
                new_stripe_item["id"],
                quantity=new_stripe_item["quantity"] + 1,
                proration_behavior="none",
                stripe_account=club.stripe_account_id
            )
            new_item_id = new_stripe_item["id"]
        else:
            created = stripe.SubscriptionItem.create(
                subscription=subscription.stripe_subscription_id,
                price=new_plan.stripe_price_id,
                quantity=1,
                proration_behavior="none",
                stripe_account=club.stripe_account_id
            )
            new_item_id = created["id"]

        # =========================================================
        # 3. DB: close old item
        # =========================================================
        now = timezone.now()

        if item.deleted_at is None:
            item.deleted_at = now

        if item.access_until is None:
            item.access_until = subscription.access_until

        item.save(update_fields=["deleted_at", "access_until"])

        # =========================================================
        # 4. DB: create new scheduled item
        # =========================================================
        new_item = SubscriptionItem.objects.create(
            subscription=subscription,
            member=item.member,
            plan=new_plan,
            price_at_subscription=new_plan.price,
            stripe_price_id_at_subscription=new_plan.stripe_price_id,
            quantity=1,
            source_item=item,
            access_start=item.access_until,
            stripe_subscription_item_id=new_item_id,
            plan_change_locked=False
        )

        return new_item

    @staticmethod
    def cancel_items_by_plan(*, subscription, plan, club):
        items = SubscriptionItem.objects.filter(
            subscription=subscription,
            plan=plan,
            deleted_at__isnull=True
        )

        results = []

        for item in items:
            results.append(
                SubscriptionItemService.cancel_item(
                    item=item,
                    subscription=subscription,
                    club=club
                )
            )

        return results

    @staticmethod
    def change_items_plan_bulk(*, subscription, old_plan, new_plan, club):
        items = SubscriptionItem.objects.filter(
            subscription=subscription,
            plan=old_plan,
            deleted_at__isnull=True
        )

        results = []

        for item in items:
            results.append(
                SubscriptionItemService.change_plan(
                    item=item,
                    new_plan=new_plan,
                    subscription=subscription,
                    club=club,
                    old_item_is_grace=(item_state(item) == "grace")
                )
            )

        return results