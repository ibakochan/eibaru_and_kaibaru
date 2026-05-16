
from datetime import date
import stripe
from django.conf import settings
from .models import Member




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
    stripe.api_key = settings.STRIPE_SECRET_KEY

    active_members = Member.objects.filter(
        club=club
    ).exclude(is_kyukai=True, is_kyukai_paid=True).count()

    billable_members = max(active_members, 1)

    if not club.stripe_subscription_id:
        return

    sub = stripe.Subscription.retrieve(club.stripe_subscription_id)

    member_item = next(
        (
            item for item in sub["items"]["data"]
            if item["price"]["id"] == settings.STRIPE_MEMBER_PRICE_ID
        ),
        None
    )

    if member_item:
        stripe.Subscription.modify(
            sub.id,
            items=[{
                "id": member_item.id,
                "quantity": billable_members,
            }],
            proration_behavior="none",
        )
    else:
        stripe.Subscription.modify(
            sub.id,
            items=[{
                "price": settings.STRIPE_MEMBER_PRICE_ID,
                "quantity": billable_members,
            }],
            proration_behavior="none",
        )
