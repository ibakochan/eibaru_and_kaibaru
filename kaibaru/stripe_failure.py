import contextvars

import stripe
from django.conf import settings

fail_this_request = contextvars.ContextVar("stripe_fail", default=False)
FAKE_KEY = "sk_test_invalid"


def failure_testing_enabled():
    return bool(getattr(settings, "STRIPE_FAILURE_TEST", False))


def _failed_headers(headers):
    if not fail_this_request.get() or headers is None:
        return headers
    headers = dict(headers)
    headers["Authorization"] = f"Bearer {FAKE_KEY}"
    return headers


class FailingStripeClient:
    """Swap the secret on Stripe HTTP calls for the current request only.

    Stripe calls request_with_retries on the client. That method is bound to
    the inner client, so overriding request alone never sees the call.
    """

    def __init__(self, inner):
        self.inner = inner

    def request(self, method, url, headers, post_data=None, *args, **kwargs):
        return self.inner.request(
            method, url, _failed_headers(headers), post_data, *args, **kwargs
        )

    def request_with_retries(self, method, url, headers, post_data=None, *args, **kwargs):
        return self.inner.request_with_retries(
            method, url, _failed_headers(headers), post_data, *args, **kwargs
        )

    def request_stream_with_retries(
        self, method, url, headers, post_data=None, *args, **kwargs
    ):
        return self.inner.request_stream_with_retries(
            method, url, _failed_headers(headers), post_data, *args, **kwargs
        )

    async def request_with_retries_async(
        self, method, url, headers, post_data=None, *args, **kwargs
    ):
        return await self.inner.request_with_retries_async(
            method, url, _failed_headers(headers), post_data, *args, **kwargs
        )

    async def request_stream_with_retries_async(
        self, method, url, headers, post_data=None, *args, **kwargs
    ):
        return await self.inner.request_stream_with_retries_async(
            method, url, _failed_headers(headers), post_data, *args, **kwargs
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
