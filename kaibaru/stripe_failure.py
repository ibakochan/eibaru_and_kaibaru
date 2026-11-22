import contextvars

import stripe
from django.conf import settings

fail_this_request = contextvars.ContextVar("stripe_fail", default=False)
FAKE_KEY = "sk_test_invalid"


def failure_testing_enabled():
    return bool(getattr(settings, "STRIPE_FAILURE_TEST", False))


class FailingStripeClient:
    """Swap the secret on Stripe HTTP calls for the current request only."""

    def __init__(self, inner):
        self.inner = inner

    def request(self, method, url, headers, post_data=None, *args, **kwargs):
        if fail_this_request.get() and headers is not None:
            headers = dict(headers)
            headers["Authorization"] = f"Bearer {FAKE_KEY}"
        return self.inner.request(
            method, url, headers, post_data, *args, **kwargs
        )

    def __getattr__(self, name):
        return getattr(self.inner, name)


def install_stripe_failure_client():
    if isinstance(stripe.default_http_client, FailingStripeClient):
        return
    try:
        from stripe._http_client import new_default_http_client
    except ImportError:
        from stripe.http_client import new_default_http_client

    inner = stripe.default_http_client or new_default_http_client()
    stripe.default_http_client = FailingStripeClient(inner)
