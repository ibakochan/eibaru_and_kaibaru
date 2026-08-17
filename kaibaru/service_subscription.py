import stripe
from django.conf import settings
from django.utils import timezone
import logging

from .models import SubscriptionItem, SubscriptionMutation
from django.db import transaction

from .locks_and_reconciliation import StripeSubscriptionReconciler

from .service_mutations import (
    assert_mutation_not_locked,
    stripe_idempotency_key,
    get_or_create_mutation_strict,
)

logger = logging.getLogger(__name__)

stripe.api_key = settings.STRIPE_SECRET_KEY

class SubscriptionItemService:

    @staticmethod
    def resume_item(*, item, subscription, club):
        assert_mutation_not_locked(
            item=item,
            mutation_type=SubscriptionMutation.MutationType.RESUME
        )

        mutation, created = get_or_create_mutation_strict(
            subscription=subscription,
            item=item,
            mutation_type=SubscriptionMutation.MutationType.RESUME,
            payload={
                "item_id": item.id,
                "price_id": item.stripe_price_id_at_subscription,
            },
        )


        stripe.Subscription.modify(
            subscription.stripe_subscription_id,
            cancel_at_period_end=False,
            stripe_account=club.stripe_account_id,
        )
        
        stripe_sub = stripe.Subscription.retrieve(
            subscription.stripe_subscription_id,
            stripe_account=club.stripe_account_id,
            expand=["items.data"],
        )

        stripe_items = stripe_sub["items"]["data"]

        stripe_item = next(
            (
                i for i in stripe_items
                if i["price"]["id"] == item.stripe_price_id_at_subscription
            ),
            None
        )


        db_active_qty = SubscriptionItem.objects.filter(
            subscription=subscription,
            stripe_price_id_at_subscription=item.stripe_price_id_at_subscription,
            deleted_at__isnull=True
        ).count()

        desired_qty = db_active_qty + 1

        

        if stripe_item:
            stripe_item_id = stripe_item["id"]
            key = stripe_idempotency_key(
                mutation,
                "resume_modify_item"
            )
            stripe.SubscriptionItem.modify(
                stripe_item_id,
                quantity=desired_qty,
                proration_behavior="none",
                stripe_account=club.stripe_account_id,
                idempotency_key=key,
            )
        else:
            key = stripe_idempotency_key(
                mutation,
                "resume_create_item"
            )
            new_item = stripe.SubscriptionItem.create(
                subscription=stripe_sub["id"],
                price=item.stripe_price_id_at_subscription,
                quantity=1,
                proration_behavior="none",
                stripe_account=club.stripe_account_id,
                idempotency_key=key,
            )
            stripe_item_id = new_item["id"]
        
        with transaction.atomic():

            item.deleted_at = None
            item.access_until = None
            item.stripe_subscription_item_id = stripe_item_id
    
    
    
            update_fields = [
                "stripe_subscription_item_id",
                "deleted_at",
                "access_until",
            ]
    

        
            item.save(update_fields=update_fields)
    
            subscription.cancel_at_period_end = False
            subscription.save(update_fields=[
                "cancel_at_period_end"
            ])

            mutation.status = "succeeded"
            mutation.processed_at = timezone.now()

            if subscription.current_period_end:
                mutation.secondary_mutation_blocked_until = subscription.current_period_end
            else:
                mutation.secondary_mutation_blocked_until = timezone.now()

            mutation.save()
    
        return item
        
    # =========================================================
    # CANCEL ITEM (PURE EXECUTION)
    # =========================================================
    @staticmethod
    def cancel_item(*, item, subscription, club):
        assert_mutation_not_locked(
            item=item,
            mutation_type=SubscriptionMutation.MutationType.CANCEL
        )


        mutation, created = get_or_create_mutation_strict(
            subscription=subscription,
            item=item,
            mutation_type=SubscriptionMutation.MutationType.CANCEL,
            payload={
                "item_id": item.id,
                "price_id": item.stripe_price_id_at_subscription,
                "stripe_subscription_item_id": item.stripe_subscription_item_id,
            },
        )

        active_items_count = SubscriptionItem.objects.filter(
            subscription=subscription,
            deleted_at__isnull=True
        ).count()
        is_last_item = active_items_count <= 1  # this item is the last one

        if is_last_item:
            # Don't delete — schedule cancellation instead
            key = stripe_idempotency_key(
                mutation,
                "cancel_subscription"
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
                mutation,
                "remove_item_modify_quantity"
            )

            key_delete = stripe_idempotency_key(
                mutation,
                "remove_item_delete"
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

            item.deleted_at = timezone.now()
            item.access_until = subscription.access_until
            item.save(update_fields=["deleted_at", "access_until"])
        
            mutation.status = SubscriptionMutation.Status.SUCCEEDED
            mutation.processed_at = timezone.now()

            if subscription.current_period_end:
                mutation.secondary_mutation_blocked_until = subscription.current_period_end
                mutation.can_resume_until = subscription.current_period_end
            else:
                mutation.secondary_mutation_blocked_until = timezone.now()
            mutation.save()

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
        assert_mutation_not_locked(
            item=item,
            mutation_type=SubscriptionMutation.MutationType.CHANGE_PLAN
        )
        mutation, created = get_or_create_mutation_strict(
            subscription=subscription,
            item=item,
            mutation_type=SubscriptionMutation.MutationType.CHANGE_PLAN,
            payload={
                "old_plan_id": item.plan_id,
                "new_plan_id": new_plan.id,
                "old_price_id": item.stripe_price_id_at_subscription,
                "old_stripe_item_id": item.stripe_subscription_item_id,
                "new_price_id": new_plan.stripe_price_id,
                "is_grace": old_item_is_grace,
            },
        )

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
            mutation,
            "add_new_plan_modify_quantity"
        )
        key_create = stripe_idempotency_key(
            mutation,
            "add_new_plan_create_item"
        )
        key_uncancel = stripe_idempotency_key(
            mutation,
            "uncancel_subscription"
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
            created_stripe_item = stripe.SubscriptionItem.create(
                subscription=subscription.stripe_subscription_id,
                price=new_plan.stripe_price_id,
                quantity=1,
                proration_behavior="none",
                stripe_account=club.stripe_account_id,
                idempotency_key=key_create
            )
            new_item_id = created_stripe_item["id"]
        
        mutation.payload["new_stripe_item_id"] = new_item_id

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
        needs_stripe_cleanup = not old_item_is_grace

        if needs_stripe_cleanup:
            old_stripe_item = next(
                (i for i in stripe_sub["items"]["data"]
                 if i["id"] == item.stripe_subscription_item_id),
                None
            )
            if not old_stripe_item:
                raise Exception("Stripe item not found")

            key = stripe_idempotency_key(
                mutation,
                "remove_old_plan_modify_quantity"
            )

            key_delete = stripe_idempotency_key(
                mutation,
                "remove_old_plan_delete_item"
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
        with transaction.atomic():
            if item.deleted_at is None:
                item.deleted_at = now
            if item.access_until is None:
                item.access_until = subscription.access_until
            item.save(update_fields=["deleted_at", "access_until"])
    
            # =========================================================
            # 4. DB: create new scheduled item
            # =========================================================
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
                # -------------------------------------------------
                # REVIVE EXISTING SOFT-DELETED ROW
                # -------------------------------------------------
                new_item.deleted_at = None
                new_item.access_start = item.access_until
                new_item.source_item = item
                new_item.stripe_subscription_item_id = new_item_id
        
                new_item.save(update_fields=[
                    "deleted_at",
                    "access_start",
                    "quantity",
                    "source_item",
                    "stripe_subscription_item_id",
                ])
        
            else:
                # -------------------------------------------------
                # CREATE NEW ROW (FIRST TIME EVER FOR THIS PLAN)
                # -------------------------------------------------
                new_item = SubscriptionItem.objects.create(
                    subscription=subscription,
                    member=item.member,
                    plan=new_plan,
                    price_at_subscription=new_plan.price,
                    stripe_price_id_at_subscription=new_plan.stripe_price_id,
                    source_item=item,
                    access_start=item.access_until,
                    stripe_subscription_item_id=new_item_id,
                )

            mutation.status = "succeeded"
            mutation.processed_at = timezone.now()

            if subscription.current_period_end:
                mutation.secondary_mutation_blocked_until = subscription.current_period_end
            else:
                mutation.secondary_mutation_blocked_until = timezone.now()

            mutation.save()
        
        StripeSubscriptionReconciler.reconcile(
            subscription=subscription,
            club=club,
        )

        return new_item

    

    @staticmethod
    def cancel_change(
        *,
        new_item,
        old_item,
        subscription,
        club,
        old_plan_deleted,
    ):
        assert_mutation_not_locked(
            item=old_item,
            mutation_type=SubscriptionMutation.MutationType.CANCEL_CHANGE_PLAN
        )
        mutation, created = get_or_create_mutation_strict(
            subscription=subscription,
            item=old_item,
            mutation_type=SubscriptionMutation.MutationType.CANCEL_CHANGE_PLAN,
            payload={
                "new_item_id": new_item.id,
                "old_item_id": old_item.id,
                "new_price_id": new_item.stripe_price_id_at_subscription,
                "old_price_id": old_item.stripe_price_id_at_subscription,
                "old_stripe_item_id": old_item.stripe_subscription_item_id,
            },
        )
        now = timezone.now()

        stripe_sub = stripe.Subscription.retrieve(
            subscription.stripe_subscription_id,
            stripe_account=club.stripe_account_id,
            expand=["items.data"]
        )
    
        # =========================================================
        # 1. REMOVE NEW STRIPE ITEM (decrement or delete)
        # =========================================================
        new_stripe_item = next(
            (
                i for i in stripe_sub["items"]["data"]
                if i["id"] == new_item.stripe_subscription_item_id
            ),
            None
        )
    
        if new_stripe_item:

            new_plan_active_qty = SubscriptionItem.objects.filter(
                subscription=subscription,
                stripe_price_id_at_subscription=new_item.stripe_price_id_at_subscription,
                deleted_at__isnull=True
            ).count()

            desired_new_qty = new_plan_active_qty - 1

            if desired_new_qty <= 0:

                key = stripe_idempotency_key(
                    mutation,
                    "cancel_change_remove_new_item_delete"
                )
    
                stripe.SubscriptionItem.delete(
                    new_stripe_item["id"],
                    proration_behavior="none",
                    stripe_account=club.stripe_account_id,
                    idempotency_key=key,
                )
    
            else:
    
                key = stripe_idempotency_key(
                    mutation,
                    "cancel_change_remove_new_item_modify_quantity"
                )
    
                stripe.SubscriptionItem.modify(
                    new_stripe_item["id"],
                    quantity=desired_new_qty,
                    proration_behavior="none",
                    stripe_account=club.stripe_account_id,
                    idempotency_key=key,
                )
        
        # =========================================================
        # 2. RESTORE OLD STRIPE ITEM safely
        # =========================================================
        if not old_plan_deleted:
            old_stripe_item = next(
                (
                    i for i in stripe_sub["items"]["data"]
                    if i["price"]["id"] == old_item.plan.stripe_price_id
                ),
                None
            )

            old_plan_active_qty = SubscriptionItem.objects.filter(
                subscription=subscription,
                stripe_price_id_at_subscription=old_item.stripe_price_id_at_subscription,
                deleted_at__isnull=True
            ).count()

            desired_old_qty = old_plan_active_qty + 1
    
            if old_stripe_item:
                key = stripe_idempotency_key(
                    mutation,
                    "cancel_change_restore_old_item_modify"
                )
                stripe.SubscriptionItem.modify(
                    old_stripe_item["id"],
                    quantity=desired_old_qty,
                    proration_behavior="none",
                    stripe_account=club.stripe_account_id,
                    idempotency_key=key,
                )
                restored_id = old_stripe_item["id"]
            else:
                key = stripe_idempotency_key(
                    mutation,
                    "cancel_change_restore_old_item_create"
                )
                created_stripe_item = stripe.SubscriptionItem.create(
                    subscription=subscription.stripe_subscription_id,
                    price=old_item.plan.stripe_price_id,
                    quantity=desired_old_qty,
                    proration_behavior="none",
                    stripe_account=club.stripe_account_id,
                    idempotency_key=key,
                )
                restored_id = created_stripe_item["id"]
    
            with transaction.atomic():

                # restore old item
                old_item.stripe_subscription_item_id = restored_id
                old_item.deleted_at = None
                old_item.access_until = None
                old_item.save(update_fields=[
                    "deleted_at",
                    "access_until",
                    "stripe_subscription_item_id"
                ])
    
                # SOFT CANCEL new item (IMPORTANT CHANGE)
                new_item.deleted_at = now
                new_item.access_until = now
                new_item.source_item = None
                new_item.save(update_fields=[
                    "deleted_at",
                    "access_until",
                    "source_item",
                ])
    
                # mark mutation success
                mutation.status = SubscriptionMutation.Status.SUCCEEDED
                mutation.processed_at = now
    
                if subscription.current_period_end:
                    mutation.secondary_mutation_blocked_until = subscription.current_period_end
                else:
                    mutation.secondary_mutation_blocked_until = now
    
                mutation.save()
    
