import logging

import stripe
from django.utils import timezone


logger = logging.getLogger(__name__)

TERMINAL_PAYMENT_INTENT_STATUSES = {
    "requires_payment_method",
    "canceled",
}


class StripePaymentView:
    def __init__(
        self,
        *,
        paid=False,
        open_url=None,
        payment_intent_id=None,
        session_id=None,
        confirmed_unpaid=False,
    ):
        self.paid = paid
        self.open_url = open_url
        self.payment_intent_id = payment_intent_id
        self.session_id = session_id
        self.confirmed_unpaid = confirmed_unpaid


def read_stripe_payment(reservation, stripe_account, *, metadata_key):
    """
    Read whether this reservation was paid in Stripe.

    confirmed_unpaid is true only when Stripe explicitly has nothing
    left that can still capture money: no session, an expired session,
    or a terminal PaymentIntent. An open Checkout Session is not unpaid.
    A Stripe error is neither paid nor confirmed unpaid.
    """

    if not stripe_account:
        return StripePaymentView(confirmed_unpaid=True)

    session = _retrieve_session(
        reservation.stripe_checkout_session_id,
        stripe_account,
    )
    if session is False:
        return StripePaymentView()

    if session is not None:
        payment_intent_id = (
            session.get("payment_intent")
            or reservation.stripe_payment_intent_id
        )
        if session.get("payment_status") == "paid":
            return StripePaymentView(
                paid=True,
                payment_intent_id=payment_intent_id,
                session_id=session.id,
            )

        if session.get("status") == "open" and session.url:
            return StripePaymentView(
                open_url=session.url,
                payment_intent_id=payment_intent_id,
                session_id=session.id,
            )

        payment_intent = _retrieve_payment_intent(
            payment_intent_id,
            stripe_account,
        )
        paid_view = _paid_payment_intent(payment_intent)
        if paid_view is not None:
            paid_view.session_id = session.id
            return paid_view

        if payment_intent is False:
            return StripePaymentView(session_id=session.id)

        if session.get("status") == "expired":
            searched = _search_succeeded_payment_intent(
                reservation,
                stripe_account,
                metadata_key=metadata_key,
            )
            if searched is not None:
                searched.session_id = session.id
                return searched
            if searched is False:
                return StripePaymentView(session_id=session.id)
            return StripePaymentView(
                confirmed_unpaid=True,
                session_id=session.id,
                payment_intent_id=payment_intent_id,
            )

        return StripePaymentView(session_id=session.id)

    payment_intent = _retrieve_payment_intent(
        reservation.stripe_payment_intent_id,
        stripe_account,
    )
    if payment_intent is False:
        return StripePaymentView()

    paid_view = _paid_payment_intent(payment_intent)
    if paid_view is not None:
        return paid_view

    if (
        payment_intent is not None
        and payment_intent.status not in TERMINAL_PAYMENT_INTENT_STATUSES
    ):
        return StripePaymentView(payment_intent_id=payment_intent.id)

    searched = _search_succeeded_payment_intent(
        reservation,
        stripe_account,
        metadata_key=metadata_key,
    )
    if searched is not None:
        return searched
    if searched is False:
        return StripePaymentView()

    return StripePaymentView(
        confirmed_unpaid=True,
        payment_intent_id=(
            payment_intent.id if payment_intent is not None else None
        ),
    )


def record_stripe_payment(reservation, view):
    reservation.status = reservation.Status.PAID
    reservation.paid_at = timezone.now()
    reservation.is_deleted = False
    update_fields = ["status", "paid_at", "is_deleted"]

    if view.payment_intent_id:
        reservation.stripe_payment_intent_id = view.payment_intent_id
        update_fields.append("stripe_payment_intent_id")

    if view.session_id:
        reservation.stripe_checkout_session_id = view.session_id
        update_fields.append("stripe_checkout_session_id")

    reservation.save(update_fields=update_fields)
    return reservation


def soft_delete_reservation(reservation):
    reservation.is_deleted = True
    reservation.save(update_fields=["is_deleted"])


def _retrieve_session(session_id, stripe_account):
    if not session_id:
        return None

    try:
        return stripe.checkout.Session.retrieve(
            session_id,
            stripe_account=stripe_account,
        )
    except stripe.error.InvalidRequestError:
        return None
    except stripe.error.StripeError:
        logger.exception(
            "Could not retrieve Checkout Session %s",
            session_id,
        )
        return False


def _retrieve_payment_intent(payment_intent_id, stripe_account):
    if not payment_intent_id:
        return None

    try:
        return stripe.PaymentIntent.retrieve(
            payment_intent_id,
            stripe_account=stripe_account,
        )
    except stripe.error.InvalidRequestError:
        return None
    except stripe.error.StripeError:
        logger.exception(
            "Could not retrieve PaymentIntent %s",
            payment_intent_id,
        )
        return False


def _paid_payment_intent(payment_intent):
    if not payment_intent or payment_intent is False:
        return None

    if payment_intent.status == "succeeded":
        return StripePaymentView(
            paid=True,
            payment_intent_id=payment_intent.id,
        )

    return None


def _search_succeeded_payment_intent(
    reservation,
    stripe_account,
    *,
    metadata_key,
):
    try:
        search_result = stripe.PaymentIntent.search(
            query=(
                f"metadata['{metadata_key}']:"
                f"'{reservation.id}'"
            ),
            limit=10,
            stripe_account=stripe_account,
        )
    except stripe.error.StripeError:
        logger.exception(
            "Could not search PaymentIntents for reservation %s",
            reservation.id,
        )
        return False

    succeeded = [
        item
        for item in search_result.data
        if item.status == "succeeded"
    ]
    if not succeeded:
        return None

    payment_intent = max(
        succeeded,
        key=lambda item: item.created or 0,
    )
    return StripePaymentView(
        paid=True,
        payment_intent_id=payment_intent.id,
    )
