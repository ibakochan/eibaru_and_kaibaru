import stripe
from django.conf import settings

stripe.api_key = settings.STRIPE_SECRET_KEY


def create_customer(name, metadata):

    return stripe.Customer.create(
        name=name,
        metadata=metadata
    )