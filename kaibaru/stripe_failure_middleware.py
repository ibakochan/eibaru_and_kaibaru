from .stripe_failure import fail_this_request, failure_testing_enabled

FAIL_TEST_EMAILS = {
    "slammsa@gmail.com",
    "slammsa5555@gmail.com",
    "slammsa@hotmail.com",
    "info@kaibaru.jp",
}


def failure_test_allowed(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return False
    email = (getattr(user, "email", "") or "").strip().lower()
    return email in FAIL_TEST_EMAILS


class StripeFailureMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = None
        if (
            failure_testing_enabled()
            and failure_test_allowed(request)
            and request.headers.get("X-Stripe-Fail") == "1"
        ):
            token = fail_this_request.set(True)
        try:
            return self.get_response(request)
        finally:
            if token is not None:
                fail_this_request.reset(token)
