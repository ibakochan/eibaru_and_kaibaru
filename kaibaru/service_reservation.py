import hashlib
import stripe

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone


from .tasks_emails import send_visitor_reservation_confirmation_email

from .models import (
    Lesson,
    Reservation,
    StripeCustomer,
    Subscription,
    TicketGrant,
    TicketUsage,
)
from .discounts import calculate_age
from .rules_eligibility import assert_member_eligible_for_lesson
from .rules_reservations import (
    assert_member_reservation_caps,
    assert_reservation_horizon,
    resolve_max_days_ahead,
    unpaid_hold_q,
)


STRIPE_CHECKOUT_MINUTES = 30
RESERVATION_HOLD_MINUTES = 31


def member_has_lesson_access(
    *,
    member,
    allowed_plan_ids,
    reservation_date,
):
    """
    Determine whether the member has access to a lesson
    through one of their subscription plans.

    Access is granted when:

    1. The lesson has no allowed_plans restrictions, OR
    2. The member directly subscribes to one of the allowed
       plans, OR
    3. The member subscribes to a bundle containing one of
       the allowed plans.

    A subscription item with access_until can still provide
    access when the requested reservation date is on or
    before the access_until date.
    """

    for item in member.subscription_items.select_related(
        "plan"
    ).all():

        if not item.plan_id:
            continue

        subscription_plan = item.plan

        # --------------------------------------------------
        # Determine which lesson plans this subscription
        # provides access to.
        #
        # Normal plan:
        #     [that plan]
        #
        # Bundle:
        #     [bundle itself + every plan in the bundle]
        #
        # Bundle plans should not normally be allowed directly
        # on a Lesson, but including the bundle itself here
        # keeps the access logic defensive.
        # --------------------------------------------------

        accessible_plan_ids = {
            subscription_plan.id,
        }

        accessible_plan_ids.update(
            subscription_plan.bundled_plans.values_list(
                "id",
                flat=True,
            )
        )

        # --------------------------------------------------
        # Empty allowed_plans means the lesson is available
        # to every membership plan.
        #
        # Otherwise the member's direct plan or one of the
        # plans contained in their bundle must match.
        # --------------------------------------------------

        if (
            allowed_plan_ids
            and not (
                accessible_plan_ids
                & allowed_plan_ids
            )
        ):
            continue

        # --------------------------------------------------
        # No access_until means the subscription currently
        # provides ongoing access.
        # --------------------------------------------------

        if item.access_until is None:
            return True

        # --------------------------------------------------
        # A canceled subscription can still provide access
        # through its access_until date.
        #
        # Compare against the requested reservation date,
        # not today's date.
        # --------------------------------------------------

        access_until_date = (
            timezone.localtime(
                item.access_until
            ).date()
        )

        if reservation_date <= access_until_date:
            return True

    return False


class MemberReservationService:

    @staticmethod
    def create_reservation(
        *,
        club,
        lesson,
        member,
        reservation_date,
        payment_method,
        ticket_grant_id=None,
    ):
        # --------------------------------------------------
        # Basic ownership validation
        # --------------------------------------------------

        if lesson.club_id != club.id:
            raise ValueError(
                "このレッスンは指定されたクラブに属していません。"
            )

        if member.club_id != club.id:
            raise ValueError(
                "この会員は指定されたクラブに所属していません。"
            )

        if payment_method not in ("stripe", "ticket"):
            raise ValueError(
                "無効な支払い方法です。"
            )

        # --------------------------------------------------
        # Reservation availability
        # --------------------------------------------------

        if club.member_reservations_disabled:
            raise ValueError(
                "現在、会員予約を受け付けていません。"
            )

        if lesson.member_reservation_disabled:
            raise ValueError(
                "このレッスンでは会員予約を受け付けていません。"
            )

        # --------------------------------------------------
        # Validate weekday
        # --------------------------------------------------

        if lesson.weekday != reservation_date.weekday():
            raise ValueError(
                "選択した日付がレッスンの曜日と一致していません。"
            )

        assert_member_eligible_for_lesson(member, lesson)

        assert_reservation_horizon(
            reservation_date=reservation_date,
            max_days_ahead=resolve_max_days_ahead(
                lesson.member_reservation_max_days_ahead,
                club.member_reservation_max_days_ahead,
            ),
        )

        # --------------------------------------------------
        # Determine member reservation price.
        #
        # Lesson price overrides club price.
        # NULL means use club price.
        # --------------------------------------------------

        if lesson.member_reservation_price is not None:
            reservation_price = (
                lesson.member_reservation_price
            )
        else:
            reservation_price = (
                club.member_reservation_price
            )

        if reservation_price is None:
            raise ValueError(
                "このレッスンでは会員予約を"
                "受け付けていません。"
            )

        # --------------------------------------------------
        # Determine whether the member already has access
        # to this lesson through a membership plan on the
        # requested reservation date.
        #
        # Empty allowed_plans means all membership plans
        # are allowed.
        #
        # A member can also have access through a bundle:
        #
        #     Bundle A
        #       ├── Plan A
        #       └── Plan B
        #
        # If the lesson allows Plan A, someone subscribed
        # to Bundle A also has access.
        #
        # A canceled subscription item can still provide
        # access until its access_until date.
        #
        # reservation_only=True allows member reservation
        # even when the member has an eligible plan.
        # --------------------------------------------------

        allowed_plan_ids = set(
            lesson.allowed_plans.values_list(
                "id",
                flat=True,
            )
        )

        has_lesson_access = False

        if not lesson.reservation_only:

            has_lesson_access = member_has_lesson_access(
                member=member,
                allowed_plan_ids=allowed_plan_ids,
                reservation_date=reservation_date,
            )

            if has_lesson_access:
                raise ValueError(
                    "このレッスンは現在ご利用中の"
                    "会員プランで参加できます。"
                    "会員予約は必要ありません。"
                )

        # --------------------------------------------------
        # Stripe configuration
        # --------------------------------------------------

        if not club.stripe_account_id and payment_method != "ticket":
            raise ValueError(
                "このクラブではオンライン決済が設定されていません。"
            )

        # --------------------------------------------------
        # Customer information
        #
        # Member reservations use the member's stored
        # information rather than accepting customer data
        # from the frontend.
        # --------------------------------------------------

        if not member.full_name:
            raise ValueError(
                "会員のお名前が登録されていません。"
            )

        if not member.owner:
            raise ValueError(
                "この会員にはログインアカウントが"
                "登録されていません。"
            )

        email = (
            member.owner.email.strip().lower()
        )

        if not email:
            raise ValueError(
                "会員アカウントにメールアドレスが"
                "登録されていません。"
            )

        full_name = member.full_name.strip()
        phone_number = (
            member.phone_number or ""
        ).strip()
        reservation_age = calculate_age(member.birth_date)
        reservation_gender = member.gender or ""

        # --------------------------------------------------
        # Reservation idempotency key
        #
        # One member can only have one reservation for
        # the same lesson/date through this flow.
        # --------------------------------------------------

        reservation_key = hashlib.sha256(
            (
                f"member:"
                f"{member.id}:"
                f"{lesson.id}:"
                f"{reservation_date.isoformat()}"
            ).encode()
        ).hexdigest()

        now = timezone.now()

        hold_cutoff = (
            now
            - timezone.timedelta(
                minutes=RESERVATION_HOLD_MINUTES
            )
        )

        # --------------------------------------------------
        # Create reservation
        # --------------------------------------------------

        with transaction.atomic():

            locked_lesson = (
                Lesson.objects
                .select_for_update()
                .get(
                    id=lesson.id
                )
            )

            # Re-check lesson-level settings after
            # acquiring the lock.
            if (
                locked_lesson
                .member_reservation_disabled
            ):
                raise ValueError(
                    "このレッスンでは会員予約を"
                    "受け付けていません。"
                )

            # --------------------------------------------------
            # Re-check the member's plan access using the
            # locked lesson.
            #
            # This uses the same helper as the initial check,
            # including bundle access and access_until.
            # --------------------------------------------------

            locked_allowed_plan_ids = set(
                locked_lesson.allowed_plans.values_list(
                    "id",
                    flat=True,
                )
            )

            locked_has_lesson_access = False

            if not locked_lesson.reservation_only:

                locked_has_lesson_access = (
                    member_has_lesson_access(
                        member=member,
                        allowed_plan_ids=(
                            locked_allowed_plan_ids
                        ),
                        reservation_date=(
                            reservation_date
                        ),
                    )
                )

                if locked_has_lesson_access:
                    raise ValueError(
                        "このレッスンは現在ご利用中の"
                        "会員プランで参加できます。"
                        "会員予約は必要ありません。"
                    )



            # --------------------------------------------------
            # Prevent duplicate reservation attempts.
            # --------------------------------------------------

            existing = (
                Reservation.objects
                .filter(
                    reservation_key=reservation_key
                )
                .first()
            )

            if existing:

                if (
                    existing.status
                    == Reservation.Status.PAID
                ):
                    if existing.canceled:
                        from .reservation_cancel import canceled_rebook_message

                        raise ValueError(canceled_rebook_message())

                    raise ValueError(
                        "このレッスンはすでに予約済みです。"
                    )

                raise ValueError(
                    "このレッスンの予約処理が"
                    "すでに開始されています。"
                )

            # --------------------------------------------------
            # Reservation limit
            # --------------------------------------------------

            if (
                locked_lesson.reservation_limit
                is not None
            ):

                active_reservations = (
                    Reservation.objects
                    .filter(
                        lesson=locked_lesson,
                        reservation_date=reservation_date,
                    )
                    .filter(
                        Q(status=Reservation.Status.PAID, canceled=False)
                        |
                        unpaid_hold_q(hold_cutoff)
                    )
                    .count()
                )

                if (
                    active_reservations
                    >= locked_lesson.reservation_limit
                ):
                    raise ValueError(
                        "このレッスンは満員です。"
                    )

            assert_member_reservation_caps(
                club=club,
                lesson=locked_lesson,
                member=member,
                hold_cutoff=hold_cutoff,
            )

            # --------------------------------------------------
            # Create local reservation
            # --------------------------------------------------
            if payment_method == "ticket":

                if locked_lesson.reservation_only:
                    raise ValueError(
                        "このレッスンではチケットを利用できません。"
                    )

                ticket_query = Q(
                    member=member,
                    ticket_type__club=club,
                    ticket_type__active=True,
                )
            
                # If the lesson has allowed plans, the ticket type must
                # be eligible for at least one of those plans.
                if locked_allowed_plan_ids:
                    ticket_query &= Q(
                        ticket_type__eligible_plans__in=locked_allowed_plan_ids
                )
            
                grants = (
                    TicketGrant.objects
                    .select_for_update()
                    .filter(ticket_query)
                    .filter(
                        Q(expires_at__isnull=True)
                        | Q(expires_at__gte=timezone.now())
                    )
                    .distinct()
                    .order_by(
                        "expires_at",
                        "granted_at",
                        "id",
                    )
                )
            
                usable_grants = []

                for candidate in grants:

                    used_quantity = (
                        TicketUsage.objects
                        .filter(
                            grant=candidate,
                            refunded_at__isnull=True,
                        )
                        .aggregate(
                            total=Sum("quantity")
                        )["total"] or 0
                    )

                    if candidate.quantity - used_quantity >= 1:
                        usable_grants.append(candidate)

                if not usable_grants:
                    raise ValueError(
                        "このレッスンに利用できるチケットがありません。"
                    )

                if ticket_grant_id is None:
                    if len(usable_grants) != 1:
                        raise ValueError(
                            "使用するチケットを選択してください。"
                        )
                    grant = usable_grants[0]
                else:
                    try:
                        selected_grant_id = int(ticket_grant_id)
                    except (TypeError, ValueError):
                        raise ValueError(
                            "使用するチケットを選択してください。"
                        )

                    grant = next(
                        (
                            candidate
                            for candidate in usable_grants
                            if candidate.id == selected_grant_id
                        ),
                        None,
                    )

                    if grant is None:
                        raise ValueError(
                            "選択されたチケットは利用できません。"
                        )
            
                        
                reservation = Reservation.objects.create(
                    lesson=locked_lesson,
                    club=club,
                    user=member.owner,
                    member=member,
                    payment_method=payment_method,
                    reservation_type=(
                        Reservation.ReservationType.MEMBER
                    ),
                    status=Reservation.Status.PAID,
                    full_name=full_name,
                    email=email,
                    phone_number=phone_number,
                    age=reservation_age,
                    gender=reservation_gender,
                    amount=reservation_price,
                    currency="jpy",
                    reservation_date=reservation_date,
                    reservation_key=reservation_key,
                    paid_at=timezone.now(),
                )
            
                TicketUsage.objects.create(
                    grant=grant,
                    reservation=reservation,
                    quantity=1,
                )
                    
                        
            else:
                reservation = Reservation.objects.create(
                    lesson=locked_lesson,
                    club=club,
                    user=member.owner,
                    member=member,
                    payment_method=payment_method,
                    reservation_type=(
                        Reservation.ReservationType.MEMBER
                    ),
                    status=Reservation.Status.UNPAID,
                    full_name=full_name,
                    email=email,
                    phone_number=phone_number,
                    age=reservation_age,
                    gender=reservation_gender,
                    amount=reservation_price,
                    currency="jpy",
                    reservation_date=reservation_date,
                    reservation_key=reservation_key,
                )

        if payment_method == "ticket":
            send_visitor_reservation_confirmation_email.delay(
                reservation.id
            )

            return {
            "reservation_id": reservation.id,
                "success": True,
                "paid": True,
                "payment_method": "ticket",
                "requires_checkout": False,
            }
    
        # --------------------------------------------------
        # Free member reservation.
        #
        # No Stripe Checkout is necessary when the effective
        # member reservation price is zero.
        # --------------------------------------------------

        if reservation_price == 0:

            reservation.status = (
                Reservation.Status.PAID
            )

            reservation.paid_at = (
                timezone.now()
            )

            reservation.save(
                update_fields=[
                    "status",
                    "paid_at",
                ]
            )

            send_visitor_reservation_confirmation_email.delay(
                reservation.id
            )

            return {
                "reservation_id": reservation.id,
                "success": True,
                "paid": True,
                "requires_checkout": False,
            }

        # --------------------------------------------------
        # Stripe payment
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
                        stripe_customer
                        .stripe_customer_id,
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
                            amount=reservation.amount,
                            currency=reservation.currency,
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
                                "reservation_id":
                                    str(reservation.id),
                                "club_id":
                                    str(club.id),
                                "lesson_id":
                                    str(locked_lesson.id),
                                "member_id":
                                    str(member.id),
                                "reservation_type":
                                    "member",
                            },
                            stripe_account=(
                                club.stripe_account_id
                            ),
                            idempotency_key=(
                                f"member_reservation_"
                                f"{reservation.id}"
                            ),
                        )
                    )

                    if (
                        payment_intent.status
                        == "succeeded"
                    ):

                        reservation.status = (
                            Reservation.Status.PAID
                        )

                        reservation.paid_at = (
                            timezone.now()
                        )

                        reservation.stripe_payment_intent_id = (
                            payment_intent.id
                        )

                        reservation.save(
                            update_fields=[
                                "status",
                                "paid_at",
                                "stripe_payment_intent_id",
                            ]
                        )

                        send_visitor_reservation_confirmation_email.delay(
                            reservation.id
                        )

                        return {
                            "reservation_id":
                                reservation.id,
                            "success": True,
                            "paid": True,
                            "requires_checkout":
                                False,
                            "payment_intent_id":
                                payment_intent.id,
                        }

            except (
                stripe.error.CardError,
                stripe.error.InvalidRequestError,
            ):
                pass

        # --------------------------------------------------
        # Stripe Checkout fallback
        # --------------------------------------------------

        try:

            session_kwargs = {
                "mode": "payment",
                "payment_method_types": [
                    "card"
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
                        "price_data": {
                            "currency":
                                reservation.currency,
                            "product_data": {
                                "name":
                                    locked_lesson.title,
                            },
                            "unit_amount":
                                reservation.amount,
                        },
                        "quantity": 1,
                    }
                ],
                "metadata": {
                    "reservation_id":
                        str(reservation.id),
                    "club_id":
                        str(club.id),
                    "lesson_id":
                        str(locked_lesson.id),
                    "member_id":
                        str(member.id),
                    "reservation_type":
                        "member",
                },
                "success_url": (
                    f"https://"
                    f"{club.subdomain}"
                    f".kaibaru.jp/"
                    f"?reservation=success"
                    f"&reservation_id="
                    f"{reservation.id}"
                ),
                "cancel_url": (
                    f"https://"
                    f"{club.subdomain}"
                    f".kaibaru.jp/"
                    f"?reservation=cancel"
                    f"&reservation_id="
                    f"{reservation.id}"
                ),
                "stripe_account":
                    club.stripe_account_id,
                "idempotency_key": (
                    f"member_reservation_"
                    f"{reservation.id}"
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
            reservation.delete()
            raise

        # --------------------------------------------------
        # Store Checkout Session ID.
        # --------------------------------------------------

        reservation.stripe_checkout_session_id = (
            session.id
        )

        reservation.save(
            update_fields=[
                "stripe_checkout_session_id"
            ]
        )

        return {
            "reservation_id":
                reservation.id,
            "checkout_session_id":
                session.id,
            "checkout_url":
                session.url,
            "requires_checkout":
                True,
        }
