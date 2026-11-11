"""Finish a club after its platform period has ended.

The club row is already marked deleted. This pass unsubscribes member
plans, deletes the members once those plans are gone, removes the custom
domain, deletes this club's own pictures from storage, and syncs the
owner's remaining clubs. The delete_club mutation is marked succeeded
only when every part has finished.
"""

import logging

import stripe
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .locks_and_reconciliation import CacheLockError, subscription_lock
from .models import (
    Club,
    ClubNews,
    ClubSubscriptionMutation,
    ClubVideo,
    ExpenseReceipt,
    JoinRequest,
    Lesson,
    Member,
    PlatformClub,
    PlatformSubscription,
    SlateImage,
    Subscription,
    SubscriptionItem,
)
from .service_mutations import MutationLockedError
from .service_platform_subscription import (
    OPEN_STATUSES,
    PlatformBusy,
    _Wait,
    _apply_quantities,
    _covered_clubs,
    _mark_succeeded,
    platform_subscription_lock,
)
from .service_subscription import SubscriptionItemService
from .service_subscription_cash import CashSubscriptionItemService

logger = logging.getLogger(__name__)

LIVE_SUBSCRIPTION_STATUSES = (
    "active",
    "trialing",
    "past_due",
    "pending",
    "unpaid",
)


def queue_deleted_club_cleanup(club):
    """Remember that this deleted club still needs its members and domain closed."""
    if not club or not club.is_deleted:
        return

    def enqueue():
        _ensure_delete_mutation(club)
        from .tasks import cleanup_deleted_clubs
        cleanup_deleted_clubs.delay()

    transaction.on_commit(enqueue)


def finish_deleted_clubs():
    for club_id in _club_ids_to_finish():
        club = Club.all_objects.filter(id=club_id, is_deleted=True).first()
        if not club:
            continue
        try:
            _finish_one(club)
        except Exception:
            logger.exception(
                "[DELETED CLUB] cleanup failed club=%s",
                club_id,
            )


def _club_ids_to_finish():
    member_clubs = Member.objects.filter(
        club__is_deleted=True,
    ).values_list("club_id", flat=True)
    domain_clubs = Club.all_objects.filter(
        is_deleted=True,
    ).exclude(
        Q(custom_domain__isnull=True) | Q(custom_domain=""),
    ).values_list("id", flat=True)
    subscription_clubs = Subscription.objects.filter(
        club__is_deleted=True,
        status__in=LIVE_SUBSCRIPTION_STATUSES,
    ).values_list("club_id", flat=True)
    mutation_clubs = ClubSubscriptionMutation.objects.filter(
        type=ClubSubscriptionMutation.MutationType.DELETE_CLUB,
        status__in=OPEN_STATUSES,
    ).values_list("club_id", flat=True)
    return (
        set(member_clubs)
        | set(domain_clubs)
        | set(subscription_clubs)
        | set(mutation_clubs)
        | _club_ids_with_files()
    )


def _ensure_delete_mutation(club):
    link = (
        PlatformClub.objects
        .filter(club=club)
        .select_related("platform_subscription")
        .first()
    )
    if not link:
        return None
    with transaction.atomic():
        locked = (
            PlatformSubscription.objects
            .select_for_update()
            .get(id=link.platform_subscription_id)
        )
        mutation = (
            ClubSubscriptionMutation.objects
            .filter(
                platform_subscription=locked,
                club=club,
                type=ClubSubscriptionMutation.MutationType.DELETE_CLUB,
                status__in=OPEN_STATUSES,
            )
            .order_by("created_at")
            .first()
        )
        if mutation:
            return mutation
        return ClubSubscriptionMutation.objects.create(
            platform_subscription=locked,
            club=club,
            type=ClubSubscriptionMutation.MutationType.DELETE_CLUB,
            status=ClubSubscriptionMutation.Status.PENDING,
            payload={},
        )


def _finish_one(club):
    mutation = _ensure_delete_mutation(club)
    if _subscriptions_still_open(club):
        return
    _delete_members(club)
    _clear_domain(club)
    _delete_club_files(club)
    if not _sync_remaining_clubs(club, mutation):
        return
    if mutation:
        mutation.refresh_from_db()
        if mutation.status in OPEN_STATUSES:
            _mark_succeeded(mutation)


def _subscriptions_still_open(club):
    """Unsubscribe live member plans. True while any plan is still open."""
    subscriptions = Subscription.objects.filter(
        club=club,
        status__in=LIVE_SUBSCRIPTION_STATUSES,
    ).order_by("id")
    still_open = False
    for subscription in subscriptions:
        try:
            if _unsubscribe_subscription(club, subscription):
                still_open = True
        except (CacheLockError, MutationLockedError):
            logger.info(
                "[DELETED CLUB] subscription busy club=%s subscription=%s",
                club.id,
                subscription.id,
            )
            still_open = True
        except Exception:
            logger.exception(
                "[DELETED CLUB] unsubscribe failed club=%s subscription=%s",
                club.id,
                subscription.id,
            )
            still_open = True
    return still_open


def _unsubscribe_subscription(club, subscription):
    with subscription_lock(subscription.id, timeout=300):
        subscription.refresh_from_db()
        if subscription.status not in LIVE_SUBSCRIPTION_STATUSES:
            return False
        if subscription.billing_method == "stripe" and not subscription.stripe_subscription_id:
            _close_local_subscription(subscription)
            return False
        items = list(
            SubscriptionItem.objects.filter(
                subscription=subscription,
                deleted_at__isnull=True,
            ).order_by("id")
        )
        service = (
            SubscriptionItemService
            if subscription.billing_method == "stripe"
            else CashSubscriptionItemService
        )
        for item in items:
            item.refresh_from_db()
            if item.deleted_at:
                continue
            service.cancel_item(
                item=item,
                subscription=subscription,
                club=club,
            )
        subscription.refresh_from_db()
        return _stripe_or_cash_still_open(club, subscription)


def _stripe_or_cash_still_open(club, subscription):
    if subscription.billing_method == "stripe" and subscription.stripe_subscription_id:
        account = {}
        if club.stripe_account_id:
            account["stripe_account"] = club.stripe_account_id
        try:
            stripe_sub = stripe.Subscription.retrieve(
                subscription.stripe_subscription_id,
                **account,
            )
        except stripe.error.InvalidRequestError:
            _close_local_subscription(subscription)
            return False
        if stripe_sub.status in ("canceled", "incomplete_expired"):
            _close_local_subscription(subscription)
            return False
        if not stripe_sub.cancel_at_period_end:
            stripe.Subscription.modify(
                subscription.stripe_subscription_id,
                cancel_at_period_end=True,
                idempotency_key=f"deleted-club-{club.id}-sub-{subscription.id}",
                **account,
            )
            subscription.cancel_at_period_end = True
            subscription.save(update_fields=["cancel_at_period_end"])
        return True
    if subscription.cancel_at_period_end:
        end = subscription.current_period_end
        if end and end > timezone.now():
            return True
    _close_local_subscription(subscription)
    return False


def _close_local_subscription(subscription):
    subscription.status = "canceled"
    subscription.cancel_at_period_end = False
    subscription.stripe_subscription_id = None
    subscription.save(
        update_fields=["status", "cancel_at_period_end", "stripe_subscription_id"]
    )


def _delete_members(club):
    members = Member.objects.filter(club=club)
    for member in members:
        full_name = member.full_name
        member.ticket_purchases.update(member_name=full_name)
        member.ticket_grants.update(member_name=full_name)
        member.teacher_invoices.update(instructor_name=full_name)
        _clear_file(member, "picture")
        member.delete()


def _filled_file(field):
    return Q(**{f"{field}__isnull": False}) & ~Q(**{field: ""})


def _club_ids_with_files():
    """Deleted clubs that still have their own pictures in storage."""
    ids = set(
        Club.all_objects.filter(is_deleted=True).filter(
            _filled_file("picture")
            | _filled_file("favicon")
            | _filled_file("og_image")
            | _filled_file("line_qr_code")
        ).values_list("id", flat=True)
    )
    ids.update(SlateImage.objects.filter(club__is_deleted=True).values_list("club_id", flat=True))
    ids.update(ClubVideo.objects.filter(club__is_deleted=True).values_list("club_id", flat=True))
    ids.update(ExpenseReceipt.objects.filter(club__is_deleted=True).values_list("club_id", flat=True))
    ids.update(
        ClubNews.objects.filter(club__is_deleted=True).filter(_filled_file("image")).values_list("club_id", flat=True)
    )
    ids.update(
        Lesson.objects.filter(club__is_deleted=True).filter(_filled_file("picture")).values_list("club_id", flat=True)
    )
    ids.update(
        JoinRequest.objects.filter(club__is_deleted=True).filter(_filled_file("picture")).values_list("club_id", flat=True)
    )
    ids.update(
        Member.objects.filter(club__is_deleted=True).filter(_filled_file("picture")).values_list("club_id", flat=True)
    )
    return ids


def _delete_stored_file(field_file):
    name = getattr(field_file, "name", "") or ""
    if not name:
        return
    try:
        field_file.storage.delete(name)
    except Exception:
        logger.exception("[DELETED CLUB] file delete failed name=%s", name)


def _clear_file(instance, field_name):
    field_file = getattr(instance, field_name)
    if not getattr(field_file, "name", ""):
        return
    _delete_stored_file(field_file)
    manager = getattr(type(instance), "all_objects", type(instance).objects)
    manager.filter(pk=instance.pk).update(**{field_name: ""})


def _delete_file_rows(rows, field_name):
    for row in rows:
        _clear_file(row, field_name)
        row.delete()


def _delete_club_files(club):
    """Remove this club's own pictures. Shared sample photos are not rows here."""
    for field_name in ("picture", "favicon", "og_image", "line_qr_code"):
        _clear_file(club, field_name)
    for news in ClubNews.objects.filter(club=club):
        _clear_file(news, "image")
    for lesson in Lesson.objects.filter(club=club):
        _clear_file(lesson, "picture")
    for request in JoinRequest.objects.filter(club=club):
        _clear_file(request, "picture")
    _delete_file_rows(SlateImage.objects.filter(club=club), "image")
    _delete_file_rows(ClubVideo.objects.filter(club=club), "file")
    _delete_file_rows(ExpenseReceipt.objects.filter(club=club), "image")


def _clear_domain(club):
    if (
        not club.custom_domain
        and not club.custom_domain_status
        and not club.scheduled_for_domain_cert
    ):
        return
    from .service_custom_domain import _bump_domain_fee_generation

    owner_id = club.owner_id
    club.custom_domain = None
    club.custom_domain_status = ""
    club.scheduled_for_domain_cert = False
    club.save(update_fields=[
        "custom_domain",
        "custom_domain_status",
        "scheduled_for_domain_cert",
    ])
    _bump_domain_fee_generation(owner_id)


def _sync_remaining_clubs(club, mutation):
    """Match member and domain quantities when the owner still has a club.

    The last club ends the platform subscription, so there is nothing to sync.
    """
    platform_sub = (
        PlatformSubscription.objects
        .filter(user_id=club.owner_id)
        .exclude(stripe_subscription_id__isnull=True)
        .exclude(stripe_subscription_id="")
        .exclude(status="canceled")
        .first()
    )
    if not platform_sub or not _covered_clubs(platform_sub):
        return True
    if not mutation:
        mutation = _ensure_delete_mutation(club)
    if not mutation:
        return True
    try:
        with platform_subscription_lock(platform_sub.id):
            from .service_custom_domain import CustomDomainError, sync_custom_domain_fee

            try:
                sync_custom_domain_fee(club.owner_id)
            except CustomDomainError:
                logger.info(
                    "[DELETED CLUB] no platform subscription for domain club=%s",
                    club.id,
                )
            platform_sub.refresh_from_db()
            try:
                _apply_quantities(platform_sub, mutation)
            except _Wait:
                pass
        return True
    except PlatformBusy:
        logger.info(
            "[DELETED CLUB] quantity sync busy club=%s",
            club.id,
        )
        return False
