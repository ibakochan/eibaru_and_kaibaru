import stripe
from django.conf import settings

stripe.api_key = settings.STRIPE_SECRET_KEY
from django.db import transaction
from .models import StripeCustomer

def create_customer(name, metadata):

    return stripe.Customer.create(
        name=name,
        metadata=metadata
    )

def get_or_create_stripe_customer(user, club):
    with transaction.atomic():
        obj, _ = StripeCustomer.objects.select_for_update().get_or_create(
            user=user,
            club=club
        )

        if not obj.stripe_customer_id:
            customer = stripe.Customer.create(
                email=user.email,
                metadata={
                    "user_id": user.id,
                    "club_id": club.id,
                },
                stripe_account=club.stripe_account_id,
                idempotency_key=f"create_customer_{user.id}_{club.id}"
            )
            obj.stripe_customer_id = customer.id
            obj.save(update_fields=["stripe_customer_id"])

    return obj