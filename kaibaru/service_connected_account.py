"""The owner's connected Stripe account, shared by every club they own.

The account lives on PlatformSubscription. Each club keeps a copy so member
charges can keep using club.stripe_account_id. Connecting also creates
membership-plan and ticket-package prices on that account.
"""
import logging

import stripe

from .models import Club, MembershipPlan, PlatformSubscription, TicketPackage

logger = logging.getLogger(__name__)

CONNECT_FIELDS = (
    "stripe_account_id",
    "stripe_charges_enabled",
    "stripe_payouts_enabled",
    "stripe_onboarding_completed",
    "stripe_details_submitted",
)


def _price_id(value):
    if isinstance(value, dict):
        return value.get("id")
    return value


def _retrieve_price(price_id, stripe_account):
    if not price_id or not stripe_account:
        return None
    try:
        return stripe.Price.retrieve(price_id, stripe_account=stripe_account)
    except Exception:
        return None


def ensure_plan_price(plan, stripe_account):
    """Make sure this plan has a recurring price on the connected account."""
    if not stripe_account:
        return plan
    existing = _retrieve_price(plan.stripe_price_id, stripe_account)
    if existing and int(existing.get("unit_amount") or 0) == int(plan.price):
        product_id = _price_id(existing.get("product"))
        if product_id:
            stripe.Product.modify(
                product_id,
                name=plan.name,
                description=plan.description or "",
                stripe_account=stripe_account,
            )
            if plan.stripe_product_id != product_id:
                plan.stripe_product_id = product_id
                plan.save(update_fields=["stripe_product_id"])
        return plan

    product_id = _price_id(existing.get("product")) if existing else None
    if not product_id:
        product = stripe.Product.create(
            name=plan.name,
            description=plan.description or "",
            metadata={
                "club_id": str(plan.club_id),
                "plan_id": str(plan.id),
                "plan_type": plan.plan_type,
            },
            stripe_account=stripe_account,
        )
        product_id = product.id
    price = stripe.Price.create(
        product=product_id,
        unit_amount=int(plan.price),
        currency=plan.currency,
        recurring={"interval": plan.interval},
        stripe_account=stripe_account,
    )
    plan.stripe_product_id = product_id
    plan.stripe_price_id = price.id
    plan.save(update_fields=["stripe_product_id", "stripe_price_id"])
    return plan


def ensure_package_price(package, stripe_account):
    """Make sure this ticket package has a one-time price on the connected account."""
    if not stripe_account or int(package.price or 0) <= 0:
        return package
    existing = _retrieve_price(package.stripe_price_id, stripe_account)
    if existing and int(existing.get("unit_amount") or 0) == int(package.price):
        product_id = _price_id(existing.get("product"))
        if product_id and package.stripe_product_id != product_id:
            package.stripe_product_id = product_id
            package.save(update_fields=["stripe_product_id"])
        return package

    product_id = _price_id(existing.get("product")) if existing else None
    if not product_id:
        product = stripe.Product.create(
            name=package.name,
            description=package.description or "",
            metadata={
                "club_id": str(package.club_id),
                "ticket_package_id": str(package.id),
                "ticket_type_id": str(package.ticket_type_id),
            },
            stripe_account=stripe_account,
        )
        product_id = product.id
    price = stripe.Price.create(
        product=product_id,
        unit_amount=int(package.price),
        currency=package.currency,
        stripe_account=stripe_account,
    )
    package.stripe_product_id = product_id
    package.stripe_price_id = price.id
    package.save(update_fields=["stripe_product_id", "stripe_price_id"])
    return package


def ensure_club_connected_prices(club, stripe_account):
    plans = MembershipPlan.objects.filter(club=club, is_deleted=False)
    for plan in plans:
        try:
            ensure_plan_price(plan, stripe_account)
        except Exception:
            logger.exception(
                "[CONNECT] could not create price for plan %s club %s",
                plan.id,
                club.id,
            )
    packages = TicketPackage.objects.filter(club=club, active=True)
    for package in packages:
        try:
            ensure_package_price(package, stripe_account)
        except Exception:
            logger.exception(
                "[CONNECT] could not create price for package %s club %s",
                package.id,
                club.id,
            )


def owned_clubs(platform_sub):
    return Club.objects.filter(owner_id=platform_sub.user_id, is_deleted=False)


def mirror_connected_account(platform_sub, clubs=None):
    """Copy the connected account onto the owner's clubs and price their plans."""
    if not platform_sub.stripe_account_id:
        return
    if clubs is None:
        clubs = owned_clubs(platform_sub)
    for club in clubs:
        changed = []
        for field in CONNECT_FIELDS:
            value = getattr(platform_sub, field)
            if getattr(club, field) != value:
                setattr(club, field, value)
                changed.append(field)
        if changed:
            club.save(update_fields=changed)
        ensure_club_connected_prices(club, platform_sub.stripe_account_id)


def save_connected_account(platform_sub, account):
    """Store a Stripe Account on the platform subscription and copy it out."""
    account_id = account["id"] if isinstance(account, dict) else account.id
    other = (
        PlatformSubscription.objects
        .filter(stripe_account_id=account_id)
        .exclude(pk=platform_sub.pk)
        .exists()
    )
    other_owner = (
        Club.all_objects
        .filter(stripe_account_id=account_id)
        .exclude(owner_id=platform_sub.user_id)
        .exists()
    )
    if other or other_owner:
        raise ValueError("このStripeアカウントは別のオーナーに接続されています。")
    if (
        platform_sub.stripe_account_id
        and platform_sub.stripe_account_id != account_id
    ):
        raise ValueError("すでに別のStripeアカウントが接続されています。")

    platform_sub.stripe_account_id = account_id
    platform_sub.stripe_charges_enabled = bool(account.get("charges_enabled", False))
    platform_sub.stripe_payouts_enabled = bool(account.get("payouts_enabled", False))
    platform_sub.stripe_details_submitted = bool(account.get("details_submitted", False))
    platform_sub.stripe_onboarding_completed = platform_sub.stripe_details_submitted
    platform_sub.save(update_fields=[
        "stripe_account_id",
        "stripe_charges_enabled",
        "stripe_payouts_enabled",
        "stripe_details_submitted",
        "stripe_onboarding_completed",
        "updated_at",
    ])
    mirror_connected_account(platform_sub)
    return platform_sub


def record_connected_account_update(account):
    """Apply an account.updated webhook to the platform subscription and its clubs."""
    account_id = account.get("id")
    if not account_id:
        return None
    platform_sub = (
        PlatformSubscription.objects
        .filter(stripe_account_id=account_id)
        .first()
    )
    if platform_sub is None:
        owner_ids = list(
            Club.all_objects
            .filter(stripe_account_id=account_id)
            .values_list("owner_id", flat=True)
            .distinct()
        )
        if len(owner_ids) != 1 or not owner_ids[0]:
            return None
        platform_sub, _created = PlatformSubscription.objects.get_or_create(
            user_id=owner_ids[0],
        )
        if (
            platform_sub.stripe_account_id
            and platform_sub.stripe_account_id != account_id
        ):
            logger.error(
                "[CONNECT] account %s belongs to clubs of user %s, "
                "but their platform subscription is %s",
                account_id,
                owner_ids[0],
                platform_sub.stripe_account_id,
            )
            return None
    try:
        save_connected_account(platform_sub, account)
    except ValueError:
        logger.exception("[CONNECT] refused account update %s", account_id)
        return None
    return platform_sub


def platform_subscription_for_owner(user):
    return PlatformSubscription.objects.filter(user_id=user.id).first()
