import stripe

from collections import Counter

from django.db import transaction
from django.utils import timezone

from .billing import get_next_billing_cycle_anchor
from .stripe_service import get_or_create_stripe_customer


class CashToStripeSubscriptionService:

    @staticmethod
    def create_checkout_session(
        *,
        club,
        subscription,
        billing_user,
    ):
        """
        Create a Stripe Checkout session for an EXISTING cash subscription.

        This is a migration only.

        Important:
        - Existing local Subscription is reused.
        - Existing SubscriptionItems are reused.
        - No joining fee.
        - No local proration calculation.
        - No next-month prepayment calculation.
        - No local invoice creation.
        - Stripe subscription uses the existing subscription billing anchor.
        - Stripe subscription is created with proration_behavior='none'.
        """

        if subscription.billing_method != "cash":
            raise ValueError(
                "Only cash subscriptions can be migrated to Stripe."
            )

        if subscription.club_id != club.id:
            raise ValueError(
                "Subscription does not belong to this club."
            )

        if subscription.owner_id != billing_user.id:
            raise ValueError(
                "Subscription owner does not match billing user."
            )

        if subscription.stripe_subscription_id:
            raise ValueError(
                "Subscription already has a Stripe subscription."
            )

        # ---------------------------------------------------------
        # EXISTING ACTIVE SUBSCRIPTION ITEMS
        # ---------------------------------------------------------

        active_items = (
            subscription.items
            .filter(deleted_at__isnull=True)
            .select_related("plan")
        )

        if not active_items.exists():
            raise ValueError(
                "Cannot migrate a subscription with no active plans."
            )

        # ---------------------------------------------------------
        # BUILD STRIPE LINE ITEMS FROM EXISTING DB ITEMS
        #
        # Example:
        #
        # Member A -> Gold
        # Member B -> Gold
        # Member C -> Basic
        #
        # becomes:
        #
        # Gold  x 2
        # Basic x 1
        # ---------------------------------------------------------

        price_quantities = Counter()

        for item in active_items:

            if not item.plan:
                raise ValueError(
                    f"Subscription item {item.id} has no plan."
                )

            if not item.plan.stripe_price_id:
                raise ValueError(
                    f"Plan {item.plan.id} has no Stripe price configured."
                )

            # Prefer the Stripe price stored at subscription time.
            #
            # This is important because the DB subscription may have
            # an old price snapshot.
            stripe_price_id = (
                item.stripe_price_id_at_subscription
                or item.plan.stripe_price_id
            )

            if not stripe_price_id:
                raise ValueError(
                    f"Subscription item {item.id} has no Stripe price."
                )

            price_quantities[stripe_price_id] += 1

        line_items = [
            {
                "price": stripe_price_id,
                "quantity": quantity,
            }
            for stripe_price_id, quantity
            in price_quantities.items()
        ]

        # ---------------------------------------------------------
        # STRIPE CUSTOMER
        # ---------------------------------------------------------

        stripe_customer_obj = get_or_create_stripe_customer(
            billing_user,
            club,
        )

        # ---------------------------------------------------------
        # METADATA
        # ---------------------------------------------------------

        metadata = {
            "member_id": (
                str(active_items.first().member_id)
                if active_items.exists()
                else ""
            ),
            "club_id": str(club.id),
            "type": "cash_to_stripe",
            "local_subscription_id": str(subscription.id),
        }

        # ---------------------------------------------------------
        # BILLING CYCLE ANCHOR
        #
        # Use the EXISTING subscription's anchor day.
        #
        # This is intentionally not using club.stripe_anchor_date.
        # The local cash subscription already has its own billing
        # anchor stored on Subscription.
        # ---------------------------------------------------------

        today = timezone.localtime().date()

        anchor_day = subscription.billing_anchor_day

        if not anchor_day:
            raise ValueError(
                "Subscription has no billing anchor day."
            )

        billing_cycle_anchor = get_next_billing_cycle_anchor(
            today=today,
            anchor_day=anchor_day,
        )

        # ---------------------------------------------------------
        # STRIPE SUBSCRIPTION DATA
        # ---------------------------------------------------------

        subscription_data = {
            "metadata": metadata,
            "proration_behavior": "none",
        }

        if billing_cycle_anchor:
            now = int(timezone.now().timestamp())

            # Same safety behavior as the normal checkout.
            if billing_cycle_anchor <= now:
                billing_cycle_anchor = now + 60

            subscription_data["billing_cycle_anchor"] = (
                billing_cycle_anchor
            )

        # ---------------------------------------------------------
        # CREATE CHECKOUT
        # ---------------------------------------------------------

        session = stripe.checkout.Session.create(
            customer=stripe_customer_obj.stripe_customer_id,
            mode="subscription",
            payment_method_types=["card"],

            line_items=line_items,

            metadata=metadata,

            subscription_data=subscription_data,

            success_url=(
                f"https://{club.subdomain}.kaibaru.jp/"
                "?subscription=migration_success"
            ),

            cancel_url=(
                f"https://{club.subdomain}.kaibaru.jp/"
                "?subscription=migration_cancel"
            ),

            stripe_account=club.stripe_account_id,
        )

        return {
            "id": session.id,
        }