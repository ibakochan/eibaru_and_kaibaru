import stripe

from django.db import transaction
from django.utils import timezone

from .pricing import calculate_ticket_expiration

from .models import (
    Member,
    TicketPackage,
    TicketPurchase,
    TicketGrant,
    StripeCustomer,
    Subscription,
)


STRIPE_CHECKOUT_MINUTES = 30


class TicketPurchaseService:

    @staticmethod
    def create_purchase(
        *,
        club,
        package,
        member,
    ):
        # --------------------------------------------------
        # Basic validation
        # --------------------------------------------------

        if package.club_id != club.id:
            raise ValueError(
                "このチケットパッケージは指定されたクラブに属していません。"
            )

        if member.club_id != club.id:
            raise ValueError(
                "この会員は指定されたクラブに所属していません。"
            )

        if not package.active:
            raise ValueError(
                "このチケットパッケージは現在販売されていません。"
            )

        ticket_type = package.ticket_type

        if not ticket_type.active:
            raise ValueError(
                "このチケットタイプは現在利用できません。"
            )

        # --------------------------------------------------
        # Make sure this package really belongs to this
        # ticket type / club.
        # --------------------------------------------------

        if ticket_type.club_id != club.id:
            raise ValueError(
                "チケットタイプとクラブが一致していません。"
            )

        # --------------------------------------------------
        # Member eligibility
        #
        # Ticket types are only available to members whose
        # membership plan matches one of the eligible plans.
        # --------------------------------------------------

        eligible_plan_ids = set(
            ticket_type.eligible_plans.values_list(
                "id",
                flat=True,
            )
        )

        if not eligible_plan_ids:
            raise ValueError(
                "このチケットには利用可能なプランが設定されていません。"
            )

        member_plan_ids = set(
            member.subscription_items
            .filter(
                deleted_at__isnull=True,
                subscription__status__in=[
                    "active",
                    "trialing",
                    "pending",
                ],
            )
            .values_list(
                "plan_id",
                flat=True,
            )
        )

        # The member already has every plan that this
        # ticket type covers, so the ticket is unnecessary.
        has_all_eligible_plans = eligible_plan_ids.issubset(
            member_plan_ids
        )

        if has_all_eligible_plans:
            raise ValueError(
                "現在加入している会員プランですべての対象プランを利用できるため、"
                "このチケットを購入することはできません。"
            )

        # --------------------------------------------------
        # Stripe configuration
        # --------------------------------------------------

        if not club.stripe_account_id:
            raise ValueError(
                "このクラブではオンライン決済が設定されていません。"
            )

        if not member.owner:
            raise ValueError(
                "この会員には決済用アカウントが登録されていません。"
            )

        email = (
            member.owner.email or ""
        ).strip().lower()

        if not email:
            raise ValueError(
                "会員アカウントにメールアドレスが登録されていません。"
            )

        # --------------------------------------------------
        # Create local purchase first.
        #
        # The purchase itself is the idempotency anchor.
        # --------------------------------------------------

        with transaction.atomic():

            purchase = TicketPurchase.objects.create(
                member=member,
                package=package,
                quantity=package.quantity,
                amount=package.price,
                currency=package.currency,
                status=TicketPurchase.Status.PENDING,
            )

        # --------------------------------------------------
        # Stripe customer
        # --------------------------------------------------

        stripe_customer = (
            StripeCustomer.objects
            .filter(
                user=member.owner,
                club=club,
            )
            .first()
        )

        # --------------------------------------------------
        # Only use the saved Stripe payment method when
        # the member's account owner has an active Stripe
        # subscription for this club.
        #
        # A stale StripeCustomer from a previous Stripe
        # subscription must not be enough by itself.
        # The member themselves does not need a plan item.
        # --------------------------------------------------

        stripe_subscription = (
            Subscription.objects
            .filter(
                owner=member.owner,
                club=club,
                billing_method="stripe",
                status="active",
            )
            .first()
        )

        if (
            stripe_customer
            and stripe_subscription
        ):

            try:

                stripe_customer_obj = (
                    stripe.Customer.retrieve(
                        stripe_customer.stripe_customer_id,
                        expand=[
                            "invoice_settings.default_payment_method"
                        ],
                        stripe_account=(
                            club.stripe_account_id
                        ),
                    )
                )

                default_payment_method = (
                    stripe_customer_obj
                    .get("invoice_settings", {})
                    .get("default_payment_method")
                )

                if default_payment_method:

                    payment_method_id = (
                        default_payment_method["id"]
                        if isinstance(
                            default_payment_method,
                            dict,
                        )
                        else default_payment_method
                    )

                    payment_intent = (
                        stripe.PaymentIntent.create(
                            amount=purchase.amount,
                            currency=purchase.currency,
                            customer=(
                                stripe_customer
                                .stripe_customer_id
                            ),
                            payment_method=(
                                payment_method_id
                            ),
                            off_session=True,
                            confirm=True,
                            metadata={
                                "ticket_purchase_id":
                                    str(purchase.id),
                                "club_id":
                                    str(club.id),
                                "member_id":
                                    str(member.id),
                                "ticket_package_id":
                                    str(package.id),
                                "ticket_type_id":
                                    str(ticket_type.id),
                            },
                            stripe_account=(
                                club.stripe_account_id
                            ),
                            idempotency_key=(
                                f"ticket_purchase_"
                                f"{purchase.id}"
                            ),
                        )
                    )

                    if (
                        payment_intent.status
                        == "succeeded"
                    ):

                        with transaction.atomic():

                            locked_purchase = (
                                TicketPurchase.objects
                                .select_for_update()
                                .get(
                                    id=purchase.id
                                )
                            )

                            locked_purchase.status = (
                                TicketPurchase.Status.PAID
                            )

                            paid_at = timezone.now()

                            locked_purchase.paid_at = paid_at

                            locked_purchase.stripe_payment_intent_id = (
                                payment_intent.id
                            )

                            locked_purchase.save(
                                update_fields=[
                                    "status",
                                    "paid_at",
                                    "stripe_payment_intent_id",
                                ]
                            )

                            TicketGrant.objects.create(
                                member=member,
                                ticket_type=ticket_type,
                                source=(
                                    TicketGrant.Source.PURCHASE
                                ),
                                package=package,
                                quantity=package.quantity,
                                expires_at=calculate_ticket_expiration(
                                    package=package,
                                    granted_at=paid_at,
                                ),
                            )

                        return {
                            "purchase_id":
                                purchase.id,
                            "success": True,
                            "paid": True,
                            "requires_checkout": False,
                            "payment_intent_id":
                                payment_intent.id,
                        }

            except (
                stripe.error.CardError,
                stripe.error.InvalidRequestError,
            ):
                # Saved card failed → fall back to Checkout.
                pass

        # --------------------------------------------------
        # Stripe Checkout fallback
        # --------------------------------------------------

        try:

            session_kwargs = {
                "mode": "payment",

                "payment_method_types": [
                    "card",
                ],

                "expires_at": int(
                    (
                        timezone.now()
                        + timezone.timedelta(
                            minutes=
                            STRIPE_CHECKOUT_MINUTES
                        )
                    ).timestamp()
                ),

                "line_items": [
                    {
                        "price": (
                            package.stripe_price_id
                        ),
                        "quantity": 1,
                    }
                ],

                "metadata": {
                    "ticket_purchase_id":
                        str(purchase.id),
                    "club_id":
                        str(club.id),
                    "member_id":
                        str(member.id),
                    "ticket_package_id":
                        str(package.id),
                    "ticket_type_id":
                        str(ticket_type.id),
                },

                # Also put the metadata on the PaymentIntent.
                # This makes payment_intent.succeeded useful
                # for reconciliation if needed.
                "payment_intent_data": {
                    "metadata": {
                        "ticket_purchase_id":
                            str(purchase.id),
                        "club_id":
                            str(club.id),
                        "member_id":
                            str(member.id),
                        "ticket_package_id":
                            str(package.id),
                        "ticket_type_id":
                            str(ticket_type.id),
                    }
                },

                "success_url": (
                    f"https://"
                    f"{club.subdomain}"
                    f".kaibaru.jp/"
                    f"?ticket_purchase=success"
                    f"&purchase_id={purchase.id}"
                ),

                "cancel_url": (
                    f"https://"
                    f"{club.subdomain}"
                    f".kaibaru.jp/"
                    f"?ticket_purchase=cancel"
                    f"&purchase_id={purchase.id}"
                ),

                "stripe_account":
                    club.stripe_account_id,

                "idempotency_key": (
                    f"ticket_purchase_"
                    f"{purchase.id}"
                ),
            }

            if stripe_customer:

                session_kwargs["customer"] = (
                    stripe_customer
                    .stripe_customer_id
                )

            else:

                session_kwargs["customer_email"] = (
                    email
                )

            session = (
                stripe.checkout.Session.create(
                    **session_kwargs
                )
            )

        except Exception:
            purchase.delete()
            raise

        purchase.stripe_checkout_session_id = (
            session.id
        )

        purchase.save(
            update_fields=[
                "stripe_checkout_session_id"
            ]
        )

        return {
            "purchase_id":
                purchase.id,
            "checkout_session_id":
                session.id,
            "checkout_url":
                session.url,
            "requires_checkout":
                True,
            "paid":
                False,
        }