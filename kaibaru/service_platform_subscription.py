"""Platform billing: what a club owner pays Kaibaru.

Separate from member subscriptions. One Stripe subscription per owner.
The 2,980 price stays at quantity 1 while any club remains. Each extra
club is one quantity of the 1,490 price. Member headcount is pooled:
the first 100 at 100 yen, the rest at 50 yen.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone

import stripe
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from .club_access import FIRST_OF_MONTH_BLOCKED
from .models import (
    Club,
    ClubSubscriptionMutation,
    Member,
    PlatformClub,
    PlatformSubscription,
)
from .tasks_emails import (
    send_club_deleted_emails,
    send_subscription_activated_emails,
)

logger = logging.getLogger(__name__)

OPEN_STATUSES = [
    ClubSubscriptionMutation.Status.PENDING,
    ClubSubscriptionMutation.Status.PROCESSING,
]
PAYMENT_WINDOW = timedelta(days=1)
PAYMENT_WINDOW_BLOCKED = (
    "請求日の前後1日は変更できません。別の日にお試しください。"
)
MEMBER_DELETE_BLOCKED = (
    "請求日の前後1日は会員を削除できません。別の日にお試しください。"
)
IN_PROGRESS = "別の変更処理が進行中です。完了してから再度お試しください。"
BUSY = "処理中です。しばらくしてから再度お試しください。"
REMOVAL_CLUB = "club"
REMOVAL_SUBSCRIPTION = "subscription"

CLUB_BASE_YEN = 2980
CLUB_EXTRA_YEN = 1490
MEMBER_FULL_YEN = 100
MEMBER_REDUCED_YEN = 50
MEMBER_FULL_CAP = 100


def _subtract_one_calendar_month(day):
    import calendar

    year = day.year
    month = day.month - 1
    if month < 1:
        month = 12
        year -= 1
    last = calendar.monthrange(year, month)[1]
    return day.replace(year=year, month=month, day=min(day.day, last))


def platform_cycle_received_this_month(platform_sub, today=None):
    """True when this calendar month's platform renewal has already paid.

    After a cycle payment, current_period_end moves forward, so the current
    period starts in this calendar month. Before that payment, the period
    still started last month (or earlier).
    """
    today = today or timezone.localdate()
    if not platform_sub or not platform_sub.current_period_end:
        return False
    period_end = timezone.localtime(platform_sub.current_period_end).date()
    period_start = _subtract_one_calendar_month(period_end)
    return (
        period_start.year == today.year
        and period_start.month == today.month
    )


def estimate_platform_bill_for_owner(user):
    """Next-cycle Kaibaru fee across every covered club under this owner."""
    from .service_cash_platform_fee import estimate_cash_platform_fees_for_clubs

    empty_cash = {
        "cash_fee_next_yen": 0,
        "cash_fee_next_base_yen": 0,
        "cash_fee_previous_year": None,
        "cash_fee_previous_month": None,
        "cash_fee_current_month_estimate_yen": 0,
        "cash_fee_current_month_base_yen": 0,
        "cash_fee_current_month_from_subscription_invoices_yen": 0,
        "cash_fee_current_month_from_expected_subscriptions_yen": 0,
        "cash_fee_current_month_from_other_invoices_yen": 0,
    }
    empty = {
        "club_count": 0,
        "member_count": 0,
        "full_price_members": 0,
        "reduced_members": 0,
        "extra_clubs": 0,
        "club_base_yen": 0,
        "club_extra_yen": 0,
        "member_full_yen": 0,
        "member_reduced_yen": 0,
        "subscription_yen": 0,
        "total_yen": 0,
        **empty_cash,
    }
    if not user or not getattr(user, "id", None):
        return empty

    platform_sub = (
        PlatformSubscription.objects
        .filter(user_id=user.id)
        .prefetch_related("clubs")
        .first()
    )
    if platform_sub and platform_sub.stripe_subscription_id:
        clubs = _covered_clubs(platform_sub)
    else:
        clubs = list(
            Club.objects.filter(owner_id=user.id, is_deleted=False)
            .exclude(stripe_subscription_id__isnull=True)
            .exclude(stripe_subscription_id="")
        )
        if not clubs:
            clubs = list(
                Club.objects.filter(owner_id=user.id, is_deleted=False)
            )

    # Cash fee spans every club the owner runs, including ones pending removal.
    cash_club_ids = list(
        Club.objects.filter(owner_id=user.id, is_deleted=False)
        .values_list("id", flat=True)
    )
    # After this month's platform cycle, next invoice is next month and uses
    # this month's cash volume; before that, next invoice still bills last month.
    cash_fees = estimate_cash_platform_fees_for_clubs(
        cash_club_ids,
        next_invoice_uses_current_month=platform_cycle_received_this_month(
            platform_sub
        ),
    )

    club_count = len(clubs)
    if club_count == 0:
        return {
            **empty,
            **cash_fees,
            "total_yen": cash_fees.get("cash_fee_next_yen", 0),
        }

    member_count = Member.objects.filter(club__in=clubs).count()
    full_price_members = min(member_count, MEMBER_FULL_CAP)
    reduced_members = max(member_count - MEMBER_FULL_CAP, 0)
    extra_clubs = max(club_count - 1, 0)
    club_base_yen = CLUB_BASE_YEN
    club_extra_yen = extra_clubs * CLUB_EXTRA_YEN
    member_full_yen = full_price_members * MEMBER_FULL_YEN
    member_reduced_yen = reduced_members * MEMBER_REDUCED_YEN
    subscription_yen = (
        club_base_yen
        + club_extra_yen
        + member_full_yen
        + member_reduced_yen
    )
    cash_fee_next_yen = cash_fees.get("cash_fee_next_yen", 0)
    return {
        "club_count": club_count,
        "member_count": member_count,
        "full_price_members": full_price_members,
        "reduced_members": reduced_members,
        "extra_clubs": extra_clubs,
        "club_base_yen": club_base_yen,
        "club_extra_yen": club_extra_yen,
        "member_full_yen": member_full_yen,
        "member_reduced_yen": member_reduced_yen,
        "subscription_yen": subscription_yen,
        "total_yen": subscription_yen + cash_fee_next_yen,
        **cash_fees,
    }


class PlatformActionError(Exception):
    def __init__(self, message):
        self.message = message
        super().__init__(message)


class PlatformBusy(Exception):
    pass


class _Wait(Exception):
    """Stripe is not ready for this mutation yet. Leave it open."""


def platform_subscription_lock(subscription_id, timeout=300):
    key = f"platform_sub_lock:{subscription_id}"
    token = str(uuid.uuid4())
    if not cache.add(key, token, timeout=timeout):
        raise PlatformBusy()

    class _Lock:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            try:
                if cache.get(key) == token:
                    cache.delete(key)
            except Exception:
                logger.exception(
                    "[PLATFORM SUB] lock release failed id=%s",
                    subscription_id,
                )
            return False

    return _Lock()


def _stripe():
    stripe.api_key = settings.STRIPE_SECRET_KEY


def _price_ids():
    return [
        settings.STRIPE_BASE_PRICE_ID,
        settings.STRIPE_BASE_PRICE_ID_REDUCED,
        settings.STRIPE_MEMBER_PRICE_ID,
        settings.STRIPE_MEMBER_PRICE_ID_REDUCED,
    ]


def near_payment(platform_sub):
    end = platform_sub.current_period_end
    if not end:
        return False
    return abs(timezone.now() - end) <= PAYMENT_WINDOW


def member_deletion_block_reason(club):
    """Refuse a member delete while the renewal invoice is being prepared.

    The quantity sync waits during this day, so a delete now would stay on
    the invoice that is about to be charged.
    """
    link = (
        PlatformClub.objects
        .filter(club=club, awaiting_checkout=False)
        .select_related("platform_subscription")
        .first()
    )
    if not link or not link.platform_subscription.stripe_subscription_id:
        return None
    if near_payment(link.platform_subscription):
        return MEMBER_DELETE_BLOCKED
    return None


def _period_end_from_stripe(sub):
    stamp = sub.get("current_period_end")
    if not stamp:
        ends = []
        for item in sub.get("items", {}).get("data", []):
            item_end = item.get("current_period_end")
            if item_end:
                ends.append(item_end)
        stamp = max(ends) if ends else None
    if not stamp:
        return None
    return datetime.fromtimestamp(stamp, tz=dt_timezone.utc)


def _invoice_subscription_id(invoice):
    subscription_id = invoice.get("subscription")
    if subscription_id:
        return subscription_id
    parent = invoice.get("parent") or {}
    details = parent.get("subscription_details") or {}
    return details.get("subscription")


def _price_id_of(item):
    price = item["price"]
    if isinstance(price, str):
        return price
    return price["id"]


def _links(platform_sub):
    links = list(platform_sub.clubs.all())
    clubs = {
        club.id: club
        for club in Club.all_objects.filter(id__in=[link.club_id for link in links])
    }
    return [
        (link, clubs[link.club_id])
        for link in links
        if link.club_id in clubs
    ]


def _is_covered(link, club):
    return (
        not link.awaiting_checkout
        and not link.pending_removal
        and not club.is_deleted
    )


def _covered_clubs(platform_sub):
    return [club for link, club in _links(platform_sub) if _is_covered(link, club)]


def _desired_quantities(platform_sub):
    clubs = _covered_clubs(platform_sub)
    if not clubs:
        return {}
    member_count = Member.objects.filter(club__in=clubs).count()
    quantities = {settings.STRIPE_BASE_PRICE_ID: 1}
    extra_clubs = len(clubs) - 1
    if extra_clubs > 0:
        quantities[settings.STRIPE_BASE_PRICE_ID_REDUCED] = extra_clubs
    full_price_members = min(member_count, 100)
    reduced_members = max(member_count - 100, 0)
    if full_price_members:
        quantities[settings.STRIPE_MEMBER_PRICE_ID] = full_price_members
    if reduced_members:
        quantities[settings.STRIPE_MEMBER_PRICE_ID_REDUCED] = reduced_members
    return quantities


def _mirror_club(club, platform_sub, *, canceling):
    club.stripe_subscription_id = platform_sub.stripe_subscription_id
    if platform_sub.stripe_customer_id:
        club.stripe_customer_id = platform_sub.stripe_customer_id
    club.subscription_active = platform_sub.status in ("active", "trialing")
    club.subscription_cancel_at_period_end = canceling
    club.subscription_current_period_end = platform_sub.current_period_end
    update_fields = [
        "stripe_subscription_id",
        "stripe_customer_id",
        "subscription_active",
        "subscription_cancel_at_period_end",
        "subscription_current_period_end",
    ]
    if platform_sub.current_period_end:
        club.expiration_date = platform_sub.current_period_end
        update_fields.append("expiration_date")
    club.save(update_fields=update_fields)


def _remember_period(platform_sub, stripe_sub):
    period_end = _period_end_from_stripe(stripe_sub)
    platform_sub.status = stripe_sub.get("status") or platform_sub.status
    platform_sub.cancel_at_period_end = bool(
        stripe_sub.get("cancel_at_period_end")
    )
    if period_end:
        platform_sub.current_period_end = period_end
    platform_sub.save(
        update_fields=[
            "status",
            "cancel_at_period_end",
            "current_period_end",
            "updated_at",
        ]
    )


def _ensure_customer(platform_sub, user, club):
    if platform_sub.stripe_customer_id:
        return platform_sub.stripe_customer_id
    customer = stripe.Customer.create(
        email=getattr(user, "email", None) or None,
        name=(
            user.get_full_name()
            or club.title
            or club.subdomain
            or ""
        )[:200],
        metadata={"user_id": str(user.id)},
    )
    platform_sub.stripe_customer_id = customer.id
    platform_sub.save(update_fields=["stripe_customer_id", "updated_at"])
    return customer.id


def _get_or_create_platform_sub(user):
    platform_sub, _created = PlatformSubscription.objects.get_or_create(
        user=user,
    )
    return platform_sub


def _open_mutations(platform_sub):
    return ClubSubscriptionMutation.objects.filter(
        platform_subscription=platform_sub,
        status__in=OPEN_STATUSES,
    ).order_by("created_at")


def _assert_can_start(platform_sub, mutation_type, club):
    if near_payment(platform_sub):
        raise PlatformActionError(PAYMENT_WINDOW_BLOCKED)
    same = None
    for mutation in _open_mutations(platform_sub):
        if mutation.type == mutation_type and mutation.club_id == getattr(club, "id", None):
            same = mutation
            continue
        raise PlatformActionError(IN_PROGRESS)
    return same


def _create_mutation(platform_sub, mutation_type, club):
    return ClubSubscriptionMutation.objects.create(
        platform_subscription=platform_sub,
        club=club,
        type=mutation_type,
        status=ClubSubscriptionMutation.Status.PROCESSING,
        payload={},
    )


def _checkout_line_items(platform_sub, extra_club):
    quantities = _desired_quantities(platform_sub)
    if not quantities:
        member_count = Member.objects.filter(club=extra_club).count()
        quantities = {settings.STRIPE_BASE_PRICE_ID: 1}
        full_price_members = min(member_count, 100)
        reduced_members = max(member_count - 100, 0)
        if full_price_members:
            quantities[settings.STRIPE_MEMBER_PRICE_ID] = full_price_members
        if reduced_members:
            quantities[settings.STRIPE_MEMBER_PRICE_ID_REDUCED] = reduced_members
    return [
        {"price": price_id, "quantity": quantity}
        for price_id, quantity in quantities.items()
    ]


def _apply_local(mutation):
    if mutation.local_state_applied:
        return
    platform_sub = mutation.platform_subscription
    club = Club.all_objects.filter(id=mutation.club_id).first() if mutation.club_id else None

    if mutation.type == ClubSubscriptionMutation.MutationType.ADD_CLUB:
        link, _created = PlatformClub.objects.get_or_create(
            club=club,
            defaults={"platform_subscription": platform_sub},
        )
        if link.platform_subscription_id != platform_sub.id:
            raise PlatformActionError("このクラブは別の契約に入っています。")
        if platform_sub.stripe_subscription_id:
            link.awaiting_checkout = False
            link.pending_removal = False
            link.removal_kind = ""
            link.access_until = None
            link.save(
                update_fields=[
                    "awaiting_checkout",
                    "pending_removal",
                    "removal_kind",
                    "access_until",
                ]
            )
            for other, _other_club in _links(platform_sub):
                if other.removal_kind == REMOVAL_SUBSCRIPTION:
                    other.removal_kind = REMOVAL_CLUB
                    other.pending_removal = True
                    other.access_until = platform_sub.current_period_end
                    other.save(
                        update_fields=[
                            "removal_kind",
                            "pending_removal",
                            "access_until",
                        ]
                    )
            platform_sub.cancel_at_period_end = False
            platform_sub.save(update_fields=["cancel_at_period_end", "updated_at"])
            _mirror_club(club, platform_sub, canceling=False)
        else:
            link.awaiting_checkout = True
            link.pending_removal = False
            link.removal_kind = ""
            link.save(
                update_fields=[
                    "awaiting_checkout",
                    "pending_removal",
                    "removal_kind",
                ]
            )

    elif mutation.type == ClubSubscriptionMutation.MutationType.REMOVE_CLUB:
        link = PlatformClub.objects.get(club=club, platform_subscription=platform_sub)
        link.pending_removal = True
        link.removal_kind = REMOVAL_CLUB
        link.access_until = platform_sub.current_period_end
        link.save(update_fields=["pending_removal", "removal_kind", "access_until"])
        _mirror_club(club, platform_sub, canceling=True)

    elif mutation.type == ClubSubscriptionMutation.MutationType.RESUME_CLUB:
        link = PlatformClub.objects.get(club=club, platform_subscription=platform_sub)
        link.pending_removal = False
        link.removal_kind = ""
        link.access_until = None
        link.save(update_fields=["pending_removal", "removal_kind", "access_until"])
        _mirror_club(club, platform_sub, canceling=False)

    elif mutation.type == ClubSubscriptionMutation.MutationType.CANCEL_SUBSCRIPTION:
        link = PlatformClub.objects.get(club=club, platform_subscription=platform_sub)
        platform_sub.cancel_at_period_end = True
        platform_sub.save(update_fields=["cancel_at_period_end", "updated_at"])
        link.pending_removal = True
        link.removal_kind = REMOVAL_SUBSCRIPTION
        link.access_until = platform_sub.current_period_end
        link.save(update_fields=["pending_removal", "removal_kind", "access_until"])
        _mirror_club(club, platform_sub, canceling=True)

    elif mutation.type == ClubSubscriptionMutation.MutationType.RESUME_SUBSCRIPTION:
        platform_sub.cancel_at_period_end = False
        platform_sub.save(update_fields=["cancel_at_period_end", "updated_at"])
        for link, linked_club in _links(platform_sub):
            if link.removal_kind != REMOVAL_SUBSCRIPTION:
                continue
            link.pending_removal = False
            link.removal_kind = ""
            link.access_until = None
            link.save(
                update_fields=["pending_removal", "removal_kind", "access_until"]
            )
            _mirror_club(linked_club, platform_sub, canceling=False)

    mutation.local_state_applied = True
    mutation.save(update_fields=["local_state_applied"])


def _apply_quantities(platform_sub, mutation):
    if not platform_sub.stripe_subscription_id:
        raise _Wait()
    if not _covered_clubs(platform_sub):
        return
    desired = _desired_quantities(platform_sub)
    stripe_sub = stripe.Subscription.retrieve(platform_sub.stripe_subscription_id)
    existing = {
        _price_id_of(item): item
        for item in stripe_sub["items"]["data"]
    }
    changes = []
    for price_id in _price_ids():
        quantity = desired.get(price_id, 0)
        item = existing.get(price_id)
        if quantity <= 0:
            if item:
                changes.append({"id": item["id"], "deleted": True})
            continue
        if item:
            if item["quantity"] != quantity:
                changes.append({"id": item["id"], "quantity": quantity})
        else:
            changes.append({"price": price_id, "quantity": quantity})
    if not changes:
        return
    stripe.Subscription.modify(
        platform_sub.stripe_subscription_id,
        items=changes,
        proration_behavior="none",
        idempotency_key=_quantity_key(mutation, desired),
    )


def _quantity_key(mutation, desired):
    parts = [f"{price_id}-{desired[price_id]}" for price_id in sorted(desired)]
    return f"platform-mut-{mutation.id}-{'-'.join(parts)}"[:255]


def _set_cancel_flag(platform_sub, mutation, cancel):
    if not platform_sub.stripe_subscription_id:
        raise _Wait()
    stripe.Subscription.modify(
        platform_sub.stripe_subscription_id,
        cancel_at_period_end=cancel,
        idempotency_key=f"platform_mut_{mutation.id}_cancel_{int(cancel)}",
    )
    platform_sub.cancel_at_period_end = cancel
    platform_sub.save(update_fields=["cancel_at_period_end", "updated_at"])


def _apply_stripe(mutation):
    platform_sub = mutation.platform_subscription
    if (
        mutation.type == ClubSubscriptionMutation.MutationType.SYNC_MEMBERS
        and near_payment(platform_sub)
    ):
        raise _Wait()

    if mutation.type == ClubSubscriptionMutation.MutationType.ADD_CLUB:
        if not platform_sub.stripe_subscription_id:
            _finish_checkout_mutation(mutation)
            platform_sub.refresh_from_db()
            if not platform_sub.stripe_subscription_id:
                raise _Wait()
        else:
            stripe_sub = stripe.Subscription.retrieve(
                platform_sub.stripe_subscription_id
            )
            if stripe_sub.get("cancel_at_period_end"):
                _set_cancel_flag(platform_sub, mutation, False)
            _apply_quantities(platform_sub, mutation)
        return

    if mutation.type == ClubSubscriptionMutation.MutationType.CANCEL_SUBSCRIPTION:
        _set_cancel_flag(platform_sub, mutation, True)
        return

    if mutation.type == ClubSubscriptionMutation.MutationType.RESUME_SUBSCRIPTION:
        _set_cancel_flag(platform_sub, mutation, False)
        _apply_quantities(platform_sub, mutation)
        return

    _apply_quantities(platform_sub, mutation)


def _finish_checkout_mutation(mutation):
    session_id = (mutation.payload or {}).get("checkout_session_id")
    if not session_id:
        raise _Wait()
    session = stripe.checkout.Session.retrieve(session_id)
    if session.get("status") == "open":
        raise _Wait()
    if session.get("status") == "expired":
        _fail_add_checkout(mutation)
        return
    _checkout_completed_locked(session)


def _fail_add_checkout(mutation):
    club_id = mutation.club_id
    if club_id:
        PlatformClub.objects.filter(
            club_id=club_id,
            awaiting_checkout=True,
            platform_subscription=mutation.platform_subscription,
        ).delete()
    mutation.status = ClubSubscriptionMutation.Status.FAILED
    mutation.processed_at = timezone.now()
    mutation.save(update_fields=["status", "processed_at"])


def _mark_succeeded(mutation):
    mutation.status = ClubSubscriptionMutation.Status.SUCCEEDED
    mutation.stripe_finished = True
    mutation.processed_at = timezone.now()
    mutation.save(
        update_fields=["status", "stripe_finished", "processed_at"]
    )


def _reconcile_locked(platform_sub_id):
    _stripe()
    platform_sub = (
        PlatformSubscription.objects
        .select_for_update()
        .get(id=platform_sub_id)
    )
    for mutation in _open_mutations(platform_sub):
        platform_sub.refresh_from_db()
        mutation.platform_subscription = platform_sub
        try:
            with transaction.atomic():
                _apply_local(mutation)
            if mutation.status == ClubSubscriptionMutation.Status.FAILED:
                continue
            _apply_stripe(mutation)
        except _Wait:
            if mutation.type == ClubSubscriptionMutation.MutationType.SYNC_MEMBERS:
                continue
            return
        except Exception:
            logger.exception(
                "[PLATFORM SUB] mutation failed id=%s type=%s",
                mutation.id,
                mutation.type,
            )
            raise
        mutation.refresh_from_db()
        if mutation.status == ClubSubscriptionMutation.Status.FAILED:
            continue
        _mark_succeeded(mutation)


def reconcile_platform_subscription(platform_sub_id):
    _stripe()
    try:
        with platform_subscription_lock(platform_sub_id):
            with transaction.atomic():
                _reconcile_locked(platform_sub_id)
    except PlatformBusy:
        logger.info(
            "[PLATFORM SUB] reconcile skipped, locked id=%s",
            platform_sub_id,
        )


def reconcile_open_platform_mutations():
    _stripe()
    subscription_ids = (
        ClubSubscriptionMutation.objects
        .filter(status__in=OPEN_STATUSES)
        .values_list("platform_subscription_id", flat=True)
        .distinct()
    )
    for platform_sub_id in subscription_ids:
        try:
            reconcile_platform_subscription(platform_sub_id)
        except Exception:
            logger.exception(
                "[PLATFORM SUB] reconcile failed id=%s",
                platform_sub_id,
            )


def _subscription_payload(platform_sub, club):
    club.refresh_from_db()
    return {
        "subscribed": True,
        "stripe_subscription_id": platform_sub.stripe_subscription_id,
        "subscription_cancel_at_period_end": club.subscription_cancel_at_period_end,
        "subscription_active": club.subscription_active,
        "success": True,
    }


def start_club_subscription(club, user):
    if club.owner_id != user.id:
        raise PlatformActionError("許可なし")
    if timezone.localdate().day == 1:
        raise PlatformActionError(FIRST_OF_MONTH_BLOCKED)

    _stripe()
    platform_sub = _get_or_create_platform_sub(user)
    existing_link = PlatformClub.objects.filter(club=club).first()
    if existing_link and existing_link.platform_subscription_id != platform_sub.id:
        raise PlatformActionError("このクラブは別の契約に入っています。")

    if platform_sub.stripe_subscription_id and near_payment(platform_sub):
        raise PlatformActionError(PAYMENT_WINDOW_BLOCKED)

    if (
        existing_link
        and not existing_link.awaiting_checkout
        and not existing_link.pending_removal
        and platform_sub.stripe_subscription_id
    ):
        raise PlatformActionError("すでに契約済みです。")

    if existing_link and existing_link.pending_removal:
        raise PlatformActionError("解約予約中です。再開してください。")

    with platform_subscription_lock(platform_sub.id):
        same = _assert_can_start(
            platform_sub,
            ClubSubscriptionMutation.MutationType.ADD_CLUB,
            club,
        )
        if same and (same.payload or {}).get("checkout_session_id"):
            session = stripe.checkout.Session.retrieve(
                same.payload["checkout_session_id"]
            )
            if session.get("status") == "open":
                return {"id": session["id"]}
            if session.get("status") == "complete":
                _checkout_completed_locked(session)
                platform_sub.refresh_from_db()
                return _subscription_payload(platform_sub, club)
            _fail_add_checkout(same)
            same = None
        if same and platform_sub.stripe_subscription_id:
            _run_inside_lock(platform_sub)
            platform_sub.refresh_from_db()
            return _subscription_payload(platform_sub, club)

        mutation = same or _create_mutation(
            platform_sub,
            ClubSubscriptionMutation.MutationType.ADD_CLUB,
            club,
        )
        with transaction.atomic():
            _apply_local(mutation)

        if platform_sub.stripe_subscription_id:
            _apply_stripe(mutation)
            _mark_succeeded(mutation)
            platform_sub.refresh_from_db()
            return _subscription_payload(platform_sub, club)

        customer_id = _ensure_customer(platform_sub, user, club)
        session = stripe.checkout.Session.create(
            customer=customer_id,
            mode="subscription",
            payment_method_types=["card"],
            line_items=_checkout_line_items(platform_sub, club),
            metadata={
                "club_id": str(club.id),
                "platform_subscription_id": str(platform_sub.id),
                "mutation_id": str(mutation.id),
            },
            subscription_data={
                "metadata": {
                    "user_id": str(user.id),
                    "platform_subscription_id": str(platform_sub.id),
                }
            },
            success_url=f"https://{club.subdomain}.kaibaru.jp/?payment=success",
            cancel_url=f"https://{club.subdomain}.kaibaru.jp/?payment=cancel",
            idempotency_key=f"platform_mut_{mutation.id}_checkout",
        )
        mutation.payload = {"checkout_session_id": session.id}
        mutation.save(update_fields=["payload"])
        return {"id": session.id}


def _run_inside_lock(platform_sub):
    with transaction.atomic():
        _reconcile_locked(platform_sub.id)


def unsubscribe_club(club, user):
    if club.owner_id != user.id:
        raise PlatformActionError("許可なし")
    link = PlatformClub.objects.filter(club=club).select_related(
        "platform_subscription"
    ).first()
    if not link or link.awaiting_checkout or not link.platform_subscription.stripe_subscription_id:
        raise PlatformActionError("契約がありません。")
    if link.pending_removal:
        raise PlatformActionError("すでに解約予約済みです。")

    platform_sub = link.platform_subscription
    if near_payment(platform_sub):
        raise PlatformActionError(PAYMENT_WINDOW_BLOCKED)

    covered = _covered_clubs(platform_sub)
    mutation_type = (
        ClubSubscriptionMutation.MutationType.CANCEL_SUBSCRIPTION
        if len(covered) <= 1
        else ClubSubscriptionMutation.MutationType.REMOVE_CLUB
    )

    with platform_subscription_lock(platform_sub.id):
        same = _assert_can_start(platform_sub, mutation_type, club)
        mutation = same or _create_mutation(platform_sub, mutation_type, club)
        with transaction.atomic():
            _apply_local(mutation)
        _apply_stripe(mutation)
        _mark_succeeded(mutation)

    return {"success": True, "subscription_cancel_at_period_end": True}


def resume_club_subscription(club, user):
    if club.owner_id != user.id:
        raise PlatformActionError("許可なし")
    link = PlatformClub.objects.filter(club=club).select_related(
        "platform_subscription"
    ).first()
    if not link or not link.pending_removal:
        raise PlatformActionError("解約予約がありません。")

    platform_sub = link.platform_subscription
    if near_payment(platform_sub):
        raise PlatformActionError(PAYMENT_WINDOW_BLOCKED)

    if link.removal_kind == REMOVAL_SUBSCRIPTION:
        mutation_type = ClubSubscriptionMutation.MutationType.RESUME_SUBSCRIPTION
    else:
        mutation_type = ClubSubscriptionMutation.MutationType.RESUME_CLUB

    with platform_subscription_lock(platform_sub.id):
        same = _assert_can_start(platform_sub, mutation_type, club)
        mutation = same or _create_mutation(platform_sub, mutation_type, club)
        with transaction.atomic():
            _apply_local(mutation)
        _apply_stripe(mutation)
        _mark_succeeded(mutation)

    return {"success": True, "subscription_cancel_at_period_end": False}


def queue_member_sync(club):
    link = (
        PlatformClub.objects
        .filter(club=club, awaiting_checkout=False)
        .select_related("platform_subscription")
        .first()
    )
    if not link or not link.platform_subscription.stripe_subscription_id:
        return
    platform_sub = link.platform_subscription
    with transaction.atomic():
        locked = (
            PlatformSubscription.objects
            .select_for_update()
            .get(id=platform_sub.id)
        )
        already = ClubSubscriptionMutation.objects.filter(
            platform_subscription=locked,
            type=ClubSubscriptionMutation.MutationType.SYNC_MEMBERS,
            status__in=OPEN_STATUSES,
        ).exists()
        if not already:
            ClubSubscriptionMutation.objects.create(
                platform_subscription=locked,
                club=club,
                type=ClubSubscriptionMutation.MutationType.SYNC_MEMBERS,
                status=ClubSubscriptionMutation.Status.PENDING,
                payload={},
            )
    try:
        reconcile_platform_subscription(platform_sub.id)
    except PlatformBusy:
        return
    except Exception:
        logger.exception(
            "[PLATFORM SUB] member sync failed club=%s",
            club.id,
        )
        return


def handle_checkout_completed(session):
    metadata = session.get("metadata") or {}
    platform_sub_id = metadata.get("platform_subscription_id")
    if not platform_sub_id:
        return False
    _stripe()
    with platform_subscription_lock(int(platform_sub_id)):
        _checkout_completed_locked(session)
        with transaction.atomic():
            _reconcile_locked(int(platform_sub_id))
    return True


def _checkout_completed_locked(session):
    metadata = session.get("metadata") or {}
    platform_sub_id = metadata.get("platform_subscription_id")
    if not platform_sub_id:
        return
    platform_sub = PlatformSubscription.objects.filter(id=platform_sub_id).first()
    if not platform_sub:
        logger.error(
            "[PLATFORM SUB] checkout for missing subscription id=%s",
            platform_sub_id,
        )
        return

    subscription_id = session.get("subscription")
    customer_id = session.get("customer")
    if subscription_id and platform_sub.stripe_subscription_id not in (None, "", subscription_id):
        logger.error(
            "[PLATFORM SUB] checkout subscription mismatch local=%s stripe=%s",
            platform_sub.stripe_subscription_id,
            subscription_id,
        )
        return
    if subscription_id:
        platform_sub.stripe_subscription_id = subscription_id
    if customer_id:
        platform_sub.stripe_customer_id = customer_id
    platform_sub.save(
        update_fields=[
            "stripe_subscription_id",
            "stripe_customer_id",
            "updated_at",
        ]
    )
    if platform_sub.stripe_subscription_id:
        stripe_sub = stripe.Subscription.retrieve(platform_sub.stripe_subscription_id)
        _remember_period(platform_sub, stripe_sub)

    club = None
    club_id = metadata.get("club_id")
    if club_id:
        club = Club.all_objects.filter(id=club_id, is_deleted=False).first()
    if club:
        link, _created = PlatformClub.objects.get_or_create(
            club=club,
            defaults={"platform_subscription": platform_sub},
        )
        link.awaiting_checkout = False
        link.pending_removal = False
        link.removal_kind = ""
        link.access_until = None
        link.save(
            update_fields=[
                "awaiting_checkout",
                "pending_removal",
                "removal_kind",
                "access_until",
            ]
        )
        _mirror_club(club, platform_sub, canceling=False)

    mutation_id = metadata.get("mutation_id")
    if mutation_id:
        ClubSubscriptionMutation.objects.filter(
            id=mutation_id,
            status__in=OPEN_STATUSES,
        ).update(
            status=ClubSubscriptionMutation.Status.SUCCEEDED,
            stripe_finished=True,
            local_state_applied=True,
            processed_at=timezone.now(),
        )


def handle_invoice_paid(invoice):
    subscription_id = _invoice_subscription_id(invoice)
    if not subscription_id:
        return False
    platform_sub = PlatformSubscription.objects.filter(
        stripe_subscription_id=subscription_id
    ).first()
    if not platform_sub:
        return False
    _stripe()
    with platform_subscription_lock(platform_sub.id):
        _invoice_paid_locked(platform_sub, invoice)
        with transaction.atomic():
            _reconcile_locked(platform_sub.id)
    return True


def _invoice_paid_locked(platform_sub, invoice):
    if platform_sub.last_paid_invoice_id == invoice["id"]:
        return
    stripe_sub = stripe.Subscription.retrieve(platform_sub.stripe_subscription_id)
    _remember_period(platform_sub, stripe_sub)
    platform_sub.last_paid_invoice_id = invoice["id"]
    platform_sub.save(update_fields=["last_paid_invoice_id", "updated_at"])

    if invoice.get("billing_reason") == "subscription_cycle":
        _delete_clubs_on_renewal(platform_sub)

    remaining = None
    for link, club in _links(platform_sub):
        if link.awaiting_checkout or club.is_deleted:
            continue
        canceling = link.pending_removal or platform_sub.cancel_at_period_end
        _mirror_club(club, platform_sub, canceling=canceling)
        club.last_paid_invoice_id = invoice["id"]
        club.save(update_fields=["last_paid_invoice_id"])
        if remaining is None and not link.pending_removal:
            remaining = club
    if remaining:
        send_subscription_activated_emails.delay(remaining.id, invoice["id"])


def _delete_clubs_on_renewal(platform_sub):
    today = timezone.localdate()
    for link, club in _links(platform_sub):
        if link.removal_kind != REMOVAL_CLUB or club.is_deleted:
            continue
        club.subscription_active = False
        club.is_deleted = True
        club.deleted_at = today
        club.save(
            update_fields=["subscription_active", "is_deleted", "deleted_at"]
        )


def handle_subscription_deleted(stripe_sub):
    platform_sub = PlatformSubscription.objects.filter(
        stripe_subscription_id=stripe_sub["id"]
    ).first()
    if not platform_sub:
        return False
    _stripe()
    with platform_subscription_lock(platform_sub.id):
        _end_platform_subscription(platform_sub)
    return True


def _end_platform_subscription(platform_sub):
    today = timezone.localdate()
    for _link, club in _links(platform_sub):
        if club.is_deleted:
            continue
        club.subscription_active = False
        club.subscription_cancel_at_period_end = False
        club.is_deleted = True
        club.deleted_at = today
        club.save(
            update_fields=[
                "subscription_active",
                "subscription_cancel_at_period_end",
                "is_deleted",
                "deleted_at",
            ]
        )
    platform_sub.status = "canceled"
    platform_sub.cancel_at_period_end = False
    platform_sub.save(
        update_fields=["status", "cancel_at_period_end", "updated_at"]
    )


def _force_end_platform_subscription(platform_sub):
    try:
        stripe.Subscription.delete(platform_sub.stripe_subscription_id)
    except stripe.error.InvalidRequestError:
        logger.info(
            "[PLATFORM SUB] scheduled subscription already gone id=%s",
            platform_sub.id,
        )
    except Exception:
        logger.exception(
            "[PLATFORM SUB] failed to finish scheduled subscription id=%s",
            platform_sub.id,
        )
        return
    try:
        with platform_subscription_lock(platform_sub.id):
            _end_platform_subscription(platform_sub)
    except PlatformBusy:
        pass


def delete_overdue_platform_subscriptions(today):
    """Cancel platform subscriptions that are still unpaid 28 days after period end.

    One the owner already scheduled to end is closed if Stripe has not
    ended it by the day after the period.
    """
    _stripe()
    for platform_sub in PlatformSubscription.objects.exclude(
        stripe_subscription_id__isnull=True
    ).exclude(stripe_subscription_id=""):
        if platform_sub.status == "canceled" or not platform_sub.current_period_end:
            continue
        period_end = timezone.localtime(platform_sub.current_period_end).date()
        days_past = (today - period_end).days
        if platform_sub.cancel_at_period_end:
            if days_past < 1:
                continue
            _force_end_platform_subscription(platform_sub)
            continue
        if days_past < 28:
            continue
        clubs = [
            club
            for _link, club in _links(platform_sub)
            if not club.is_deleted
        ]
        try:
            stripe.Subscription.delete(platform_sub.stripe_subscription_id)
        except stripe.error.InvalidRequestError:
            logger.info(
                "[PLATFORM SUB] overdue subscription already gone id=%s",
                platform_sub.id,
            )
        except Exception:
            logger.exception(
                "[PLATFORM SUB] failed to delete overdue id=%s",
                platform_sub.id,
            )
            continue
        try:
            with platform_subscription_lock(platform_sub.id):
                _end_platform_subscription(platform_sub)
        except PlatformBusy:
            pass
        for club in clubs:
            owner = club.owner
            send_club_deleted_emails.delay({
                "subdomain": club.subdomain,
                "owner_name": owner.get_full_name() if owner else "",
                "owner_email": owner.email if owner else settings.SERVER_EMAIL,
                "reason": "お支払いが確認できず、一定期間が経過したため",
            })
