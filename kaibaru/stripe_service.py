import stripe
from django.conf import settings
from django.db import transaction

from .charge_amounts import platform_fee_yen
from .models import StripeCustomer
import logging
logger = logging.getLogger(__name__)

stripe.api_key = settings.STRIPE_SECRET_KEY


def apply_platform_fee_to_draft_invoice(invoice, *, stripe_account):
    """Take 1% of a connected-account invoice before it is finalized.

    Stripe only accepts application_fee_amount on a draft invoice, and
    rejects an explicit fee of 0. A fee already copied onto a finalized
    invoice is left alone.
    """
    status = getattr(invoice, "status", None)
    if status != "draft":
        return invoice

    fee = platform_fee_yen(getattr(invoice, "amount_due", 0))
    if fee <= 0:
        return invoice

    return stripe.Invoice.modify(
        invoice.id,
        application_fee_amount=fee,
        stripe_account=stripe_account,
    )


def create_customer(name, metadata, *, stripe_account):
    return stripe.Customer.create(
        name=name,
        metadata=metadata,
        stripe_account=stripe_account,
    )


def get_or_create_stripe_customer(user, club):
    with transaction.atomic():
        obj, _ = (
            StripeCustomer.objects
            .select_for_update()
            .get_or_create(
                user=user,
                club=club,
            )
        )

        # ---------------------------------------------------------
        # Existing local customer ID
        # ---------------------------------------------------------

        if obj.stripe_customer_id:

            try:
                customer = stripe.Customer.retrieve(
                    obj.stripe_customer_id,
                    stripe_account=club.stripe_account_id,
                )

                # Stripe can return a deleted customer object.
                if not customer.get("deleted", False):
                    return obj

                logger.warning(
                    "[STRIPE CUSTOMER] Customer %s is deleted. "
                    "Creating replacement.",
                    obj.stripe_customer_id,
                )

            except stripe.error.InvalidRequestError as exc:

                # Stripe returns resource_missing for:
                # "No such customer: 'cus_...'"
                if getattr(exc, "code", None) != "resource_missing":
                    raise

                logger.warning(
                    "[STRIPE CUSTOMER] Customer %s no longer exists "
                    "in Stripe account %s. Creating replacement.",
                    obj.stripe_customer_id,
                    club.stripe_account_id,
                )

        # ---------------------------------------------------------
        # No valid Stripe customer → create one
        # ---------------------------------------------------------

        customer = stripe.Customer.create(
            email=user.email,
            metadata={
                "user_id": user.id,
                "club_id": club.id,
            },
            stripe_account=club.stripe_account_id,
            idempotency_key=f"create_customer_{user.id}_{club.id}",
        )

        # ---------------------------------------------------------
        # Update existing local row OR populate newly-created row
        # ---------------------------------------------------------

        obj.stripe_customer_id = customer.id
        obj.save(update_fields=["stripe_customer_id"])

        return obj