import stripe
from django.conf import settings
from django.utils import timezone
import logging
import hashlib

from .models import SubscriptionItem
from .billing import get_cancel_quantity_action

logger = logging.getLogger(__name__)

stripe.api_key = settings.STRIPE_SECRET_KEY


def stripe_idempotency_key(*parts: str) -> str:
    raw = ":".join(map(str, parts))
    return hashlib.sha256(raw.encode()).hexdigest()


class SubscriptionItemService:

    # =========================================================
    # CANCEL ITEM (PURE EXECUTION)
    # =========================================================
    @staticmethod
    def cancel_item(*, item, subscription, club):
        active_items_count = SubscriptionItem.objects.filter(
            subscription=subscription,
            deleted_at__isnull=True
        ).count()
        is_last_item = active_items_count <= 1  # this item is the last one

        if is_last_item:
            # Don't delete — schedule cancellation instead
            key = stripe_idempotency_key(
                "sub_cancel", subscription.id, club.id, item.id
            )
            stripe.Subscription.modify(
                subscription.stripe_subscription_id,
                cancel_at_period_end=True,
                stripe_account=club.stripe_account_id,
                idempotency_key=key,
            )
            subscription.cancel_at_period_end = True
            subscription.save(update_fields=["cancel_at_period_end"])

        else:
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
                with transaction.atomic():
                    item.deleted_at = timezone.now()  # Fixed typo
                    item.access_until = subscription.access_until
                    item.save(update_fields=["deleted_at", "access_until"])
                return item

            db_active_qty = SubscriptionItem.objects.filter(
                subscription=subscription,
                stripe_price_id_at_subscription=item.stripe_price_id_at_subscription,
                deleted_at__isnull=True
            ).count()

            desired_qty = db_active_qty - 1

            key_modify = stripe_idempotency_key(
                "plan_remove",
                "modify",
                subscription.id,
                item.stripe_price_id_at_subscription,
                item.id,
            )

            key_delete = stripe_idempotency_key(
                "plan_remove",
                "delete",
                subscription.id,
                item.stripe_price_id_at_subscription,
                item.id,
            )

            

            if desired_qty <= 0:
                stripe.SubscriptionItem.delete(
                    stripe_item["id"],
                    proration_behavior="none",
                    stripe_account=club.stripe_account_id,
                    idempotency_key=key_delete
                )
            else:
                stripe.SubscriptionItem.modify(
                    stripe_item["id"],
                    quantity=desired_qty,
                    proration_behavior="none",
                    stripe_account=club.stripe_account_id,
                    idempotency_key=key_modify
                )
            with transaction.atomic():

                item.deleted_at = timezone.nnow()
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
        # 1. STRIPE: increment or create NEW plan item
        # =========================================================
        new_stripe_item = next(
            (i for i in stripe_sub["items"]["data"]
             if i["price"]["id"] == new_plan.stripe_price_id),
            None
        )
        key_modify = stripe_idempotency_key(
            "plan_add",
            "modify",
            subscription.id,
            new_plan.id,
            item.id
        )
        key_create = stripe_idempotency_key(
            "plan_add",
            "create",
            subscription.id,
            new_plan.id,
            item.id
        )
        key_uncancel = stripe_idempotency_key(
            "plan_add",
            "uncancel",
            subscription.id,
            item.id,
        )
        
        if new_stripe_item:
            stripe.SubscriptionItem.modify(
                new_stripe_item["id"],
                quantity=new_stripe_item["quantity"] + 1,
                proration_behavior="none",
                stripe_account=club.stripe_account_id,
                idempotency_key=key_modify
            )
            new_item_id = new_stripe_item["id"]
        else:
            created = stripe.SubscriptionItem.create(
                subscription=subscription.stripe_subscription_id,
                price=new_plan.stripe_price_id,
                quantity=1,
                proration_behavior="none",
                stripe_account=club.stripe_account_id,
                idempotency_key=key_create
            )
            new_item_id = created["id"]

        if subscription.cancel_at_period_end:
            stripe.Subscription.modify(
                subscription.stripe_subscription_id,
                cancel_at_period_end=False,
                stripe_account=club.stripe_account_id,
                idempotency_key=key_uncancel
            )
            subscription.cancel_at_period_end = False
            subscription.save(update_fields=["cancel_at_period_end"])

        # =========================================================
        # 2. STRIPE: decrement OLD plan item
        #    (SKIPPED during grace period — IMPORTANT FIX)
        # =========================================================
        needs_stripe_cleanup = not old_item_is_grace or subscription.cancel_at_period_end

        if needs_stripe_cleanup:
            old_stripe_item = next(
                (i for i in stripe_sub["items"]["data"]
                 if i["id"] == item.stripe_subscription_item_id),
                None
            )
            if not old_stripe_item:
                raise Exception("Stripe item not found")

            key = stripe_idempotency_key(
                "plan_remove",
                "modify",
                subscription.id,
                item.id,
                item.stripe_price_id_at_subscription,
            )

            key_delete = stripe_idempotency_key(
                "plan_remove",
                "delete",
                subscription.id,
                item.id,
                item.stripe_price_id_at_subscription,
            )

            db_old_qty = SubscriptionItem.objects.filter(
                subscription=subscription,
                stripe_price_id_at_subscription=item.stripe_price_id_at_subscription,
                deleted_at__isnull=True
            ).count()

            desired_old_qty = db_old_qty - 1

            if desired_old_qty > 0:
                stripe.SubscriptionItem.modify(
                    old_stripe_item["id"],
                    quantity=desired_old_qty,
                    proration_behavior="none",
                    stripe_account=club.stripe_account_id,
                    idempotency_key=key
                )
            else:
                stripe.SubscriptionItem.delete(
                    old_stripe_item["id"],
                    proration_behavior="none",
                    stripe_account=club.stripe_account_id,
                    idempotency_key=key_delete
                )

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