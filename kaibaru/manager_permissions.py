"""Manager permissions. The club owner always passes."""
from django.core.exceptions import ObjectDoesNotExist

PERMISSION_FIELDS = (
    "members",
    "plans",
    "lessons",
    "site",
    "events",
    "teacher_pay",
    "member_payments",
    "expenses",
    "income",
)

SELF_PAYMENT_MESSAGE = "自分の支払いや給与は変更できません。"


def permission_map(role):
    return {
        field: bool(role and getattr(role, field, False))
        for field in PERMISSION_FIELDS
    }


def manager_role(user, club):
    if not user or not getattr(user, "is_authenticated", False) or club is None:
        return None
    from .models import ClubManager

    try:
        return ClubManager.objects.select_related("member").get(
            member__club=club,
            member__user=user,
            member__is_manager=True,
        )
    except ObjectDoesNotExist:
        return None


def has_permission(user, club, permission):
    if permission not in PERMISSION_FIELDS:
        return False
    if not user or not getattr(user, "is_authenticated", False) or club is None:
        return False
    if club.owner_id == user.id:
        return True
    role = manager_role(user, club)
    return bool(role and getattr(role, permission, False))


def manages_own_money(user, member):
    """A manager acting on the member record tied to their own login."""
    if not user or not getattr(user, "is_authenticated", False) or member is None:
        return False
    club = getattr(member, "club", None)
    if club is not None and club.owner_id == user.id:
        return False
    return member.user_id == user.id


def manages_own_invoice(user, invoice):
    """A manager confirming a bill that they themselves owe the club."""
    if not user or not getattr(user, "is_authenticated", False) or invoice is None:
        return False
    club = getattr(invoice, "club", None)
    if club is not None and club.owner_id == user.id:
        return False
    if invoice.payer_id == user.id:
        return True
    subscription = getattr(invoice, "subscription", None)
    return subscription is not None and subscription.owner_id == user.id
