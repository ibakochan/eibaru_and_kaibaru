
from datetime import date
import stripe
from django.conf import settings
from .models import Member, MembershipPlan, SubscriptionItem

from django.utils import timezone
from django.db.models import Q
from django.core.exceptions import ValidationError


def add_one_month(d):
    year = d.year
    month = d.month + 1
    if month > 12:
        month -= 12
        year += 1

    day = min(
        d.day,
        [31, 29 if year % 4 == 0 else 28, 31, 30, 31, 30,
         31, 31, 30, 31, 30, 31][month - 1]
    )
    return date(year, month, day)


def sync_member_quantity(club):
    from .models import PlatformClub

    if PlatformClub.objects.filter(club=club, awaiting_checkout=False).exists():
        from .service_platform_subscription import queue_member_sync
        queue_member_sync(club)
        return

    stripe.api_key = settings.STRIPE_SECRET_KEY

    active_members = Member.objects.filter(
        club=club
    ).count()

    billable_members = max(active_members, 1)
    full_members = min(billable_members, 100)
    reduced_members = max(billable_members - 100, 0)

    if not club.stripe_subscription_id:
        return

    sub = stripe.Subscription.retrieve(club.stripe_subscription_id)
    items = list(sub["items"]["data"])

    def price_id_of(item):
        price = item.get("price")
        if isinstance(price, str):
            return price
        if isinstance(price, dict) and price.get("id"):
            return price["id"]
        details = (item.get("pricing") or {}).get("price_details") or {}
        return details.get("price")

    def find_item(price_id):
        return next(
            (item for item in items if price_id_of(item) == price_id),
            None,
        )

    changes = []
    member_item = find_item(settings.STRIPE_MEMBER_PRICE_ID)
    reduced_item = find_item(settings.STRIPE_MEMBER_PRICE_ID_REDUCED)
    if member_item:
        if member_item["quantity"] != full_members:
            changes.append({"id": member_item["id"], "quantity": full_members})
    else:
        changes.append({
            "price": settings.STRIPE_MEMBER_PRICE_ID,
            "quantity": full_members,
        })
    if reduced_members:
        if reduced_item:
            if reduced_item["quantity"] != reduced_members:
                changes.append({
                    "id": reduced_item["id"],
                    "quantity": reduced_members,
                })
        else:
            changes.append({
                "price": settings.STRIPE_MEMBER_PRICE_ID_REDUCED,
                "quantity": reduced_members,
            })
    elif reduced_item:
        changes.append({"id": reduced_item["id"], "deleted": True})

    if not changes:
        return

    stripe.Subscription.modify(
        sub.id,
        items=changes,
        proration_behavior="none",
    )




