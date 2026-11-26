import logging
import secrets

from django.conf import settings
from django.core.mail import EmailMessage
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from accounts.models import CustomUser

from .club_language import is_english, pick
from .models import Member, MemberTransfer, Subscription

logger = logging.getLogger(__name__)


def _plan_name(item, language="ja"):
    if item.plan_id:
        return item.plan.visible_name()
    return "Plan" if language == "en" else "プラン"


def _local_date(value, language="ja"):
    local = timezone.localtime(value)
    if language == "en":
        return f"{local.strftime('%B')} {local.day}, {local.year}"
    return f"{local.year}年{local.month}月{local.day}日"


class TransferMoveError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


def _live_plan_items(member):
    now = timezone.now()
    return list(
        member.subscription_items
        .filter(Q(deleted_at__isnull=True) | Q(access_until__gt=now))
        .select_related("plan", "subscription")
        .order_by("id")
    )


def _plan_lines(items, language="ja"):
    lines = []
    for item in items:
        if item.deleted_at and item.access_until:
            if language == "en":
                lines.append(
                    f"· {_plan_name(item, language)} (until {_local_date(item.access_until, language)})"
                )
            else:
                lines.append(
                    f"・{_plan_name(item)}（{_local_date(item.access_until)}まで）"
                )
        elif language == "en":
            lines.append(f"· {_plan_name(item, language)}")
        else:
            lines.append(f"・{_plan_name(item)}")
    return lines


def _is_stripe_subscription(subscription):
    return subscription is not None and subscription.billing_method == "stripe"


def transfer_block_detail(member, to_user):
    """Why this member cannot move to another account, or None.

    A member with no active or grace plans can move even if either
    account pays by card. Plans can move only when neither account does.
    """
    items = _live_plan_items(member)
    if not items:
        return None

    english = is_english(member.club)
    lines = "\n".join(_plan_lines(items, "en" if english else "ja"))
    if any(_is_stripe_subscription(item.subscription) for item in items):
        if english:
            return (
                "This member still has a plan.\n"
                "A member with a plan can't be moved off a card-billing account.\n"
                "Cancel the plan first.\n"
                + lines
            )
        return (
            "この会員にはプランが残っています。\n"
            "クレジットカード決済のアカウントからは、プラン付きで移せません。\n"
            "先にプランを解約してください。\n"
            + lines
        )

    destination = Subscription.objects.filter(
        owner=to_user,
        club_id=member.club_id,
    ).first()
    if _is_stripe_subscription(destination):
        if english:
            return (
                "The account that would receive this member pays by card.\n"
                "A member who still has a plan can move only when neither account pays by card.\n"
                + lines
            )
        return (
            "受け取るアカウントはクレジットカード決済です。\n"
            "プランが残っている会員を移せるのは、移す側と受け取る側の両方が"
            "クレジットカード決済ではないときだけです。\n"
            + lines
        )

    return None


def member_plans_will_move(member):
    return bool(_live_plan_items(member))


def request_member_transfer(*, member, requester, email):
    if member.club_id is None or member.owner_id != requester.id:
        return {"ok": False, "status": 403, "detail": pick(member.club, "許可なし", "You don't have permission to do that.")}

    if member.user_id is not None:
        return {
            "ok": False,
            "status": 400,
            "detail": pick(member.club, "アカウントに紐づいている会員は移せません。", "A member who already has their own login can't be moved."),
        }

    cleaned = (email or "").strip()
    if not cleaned:
        return {"ok": False, "status": 400, "detail": pick(member.club, "メールアドレスを入力してください。", "Enter an email address.")}

    to_user = CustomUser.objects.filter(email__iexact=cleaned).first()
    if to_user is None:
        return {
            "ok": False,
            "status": 400,
            "detail": pick(member.club, "このメールアドレスのアカウントが見つかりません。", "No account uses that email address."),
        }

    if to_user.id == requester.id:
        return {
            "ok": False,
            "status": 400,
            "detail": pick(member.club, "自分のアカウントには移せません。", "You can't move a member to your own account."),
        }

    blocked = transfer_block_detail(member, to_user)
    if blocked:
        return {"ok": False, "status": 400, "detail": blocked}

    MemberTransfer.objects.filter(
        member=member,
        status=MemberTransfer.Status.PENDING,
    ).update(status=MemberTransfer.Status.CANCELED)

    transfer = MemberTransfer.objects.create(
        member=member,
        from_owner=requester,
        to_user=to_user,
        token=secrets.token_urlsafe(32),
    )
    try:
        _send_transfer_email(transfer)
    except Exception:
        logger.exception(
            "Member transfer email failed member=%s to=%s",
            member.id,
            to_user.id,
        )
        return {
            "ok": False,
            "status": 400,
            "detail": pick(member.club, "メールを送信できませんでした。しばらくしてから再度お試しください。", "The email could not be sent. Try again in a little while."),
        }
    return {"ok": True, "status": 200, "detail": pick(member.club, "移管の依頼を送信しました。", "The transfer request was sent.")}


def _send_transfer_email(transfer):
    member = transfer.member
    club = member.club
    club_name = (club.title or club.subdomain) if club else "クラブ"
    requester_name = transfer.from_owner.get_full_name() or transfer.from_owner.email
    link = (
        f"https://{club.subdomain}.kaibaru.jp/"
        f"accept_member_transfer/{transfer.token}/"
    )
    english = is_english(club)
    if member_plans_will_move(member):
        plan_note = (
            "Accepting moves this member and the plans they still have onto your account. Nothing is billed for the move."
            if english else
            "受け入れると、この会員と残っているプランがあなたのアカウントに移ります。"
            "今の請求は発生しません。"
        )
    else:
        plan_note = (
            "Their plan does not come with them. If they should stay on a plan, add one after you accept."
            if english else
            "プランは引き継がれません。続ける場合は、受け入れたあと改めて加入してください。"
        )
    if english:
        subject = f"[{club_name}] Move a member to your account"
        message = (
            f"{requester_name} asked to move {member.full_name} at {club_name} onto your account.\n\n"
            "If you accept, this member is added to your account.\n"
            f"{plan_note}\n\n"
            f"Accept:\n{link}\n"
        )
    else:
        subject = f"【{club_name}】会員をあなたのアカウントへ移す依頼"
        message = (
            f"{requester_name}さんから、{club_name}の会員"
            f"「{member.full_name}」をあなたのアカウントへ移す依頼が届きました。\n\n"
            "受け入れると、この会員はあなたのアカウントに加わります。\n"
            f"{plan_note}\n\n"
            f"受け入れる：\n{link}\n"
        )
    EmailMessage(
        subject=subject,
        body=message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[transfer.to_user.email],
    ).send()


def accept_member_transfer(*, token, user):
    with transaction.atomic():
        transfer = (
            MemberTransfer.objects
            .select_for_update(of=("self",))
            .select_related("to_user", "member", "member__club")
            .filter(token=token)
            .first()
        )
        if transfer is None:
            return {"ok": False, "mode": "invalid", "message": "この依頼は見つかりません。"}

        club = transfer.member.club
        if transfer.status != MemberTransfer.Status.PENDING:
            return {
                "ok": False,
                "mode": "invalid",
                "message": pick(
                    club,
                    "この依頼はすでに完了しているか、取り消されています。",
                    "This request is already finished or has been cancelled.",
                ),
                "transfer": transfer,
            }

        if not user.is_authenticated or user.id != transfer.to_user_id:
            return {
                "ok": False,
                "mode": "wrong_user",
                "message": pick(
                    club,
                    "この依頼は別のアカウント宛です。"
                    f"{transfer.to_user.email} でログインしてください。",
                    f"This request was sent to a different account. Log in as {transfer.to_user.email}.",
                ),
                "transfer": transfer,
            }

        member = Member.objects.select_for_update().get(pk=transfer.member_id)
        if member.owner_id != transfer.from_owner_id or member.user_id is not None:
            transfer.status = MemberTransfer.Status.CANCELED
            transfer.save(update_fields=["status"])
            return {
                "ok": False,
                "mode": "invalid",
                "message": pick(member.club, "この会員はもう移せません。", "This member can't be moved anymore."),
            }

        blocked = transfer_block_detail(member, user)
        if blocked:
            return {"ok": False, "mode": "blocked", "message": blocked, "transfer": transfer}

        plans_move = member_plans_will_move(member)

        has_household = (
            Member.objects
            .filter(club_id=member.club_id)
            .filter(Q(owner=user) | Q(user=user))
            .exclude(pk=member.pk)
            .exists()
        )
        member.owner = user
        update_fields = ["owner"]
        if not has_household:
            member.user = user
            update_fields.append("user")
        try:
            with transaction.atomic():
                member.save(update_fields=update_fields)
        except IntegrityError:
            return {
                "ok": False,
                "mode": "blocked",
                "message": pick(
                    member.club,
                    "同じ名前と生年月日の会員が、すでにあなたのアカウントにいます。",
                    "Your account already has a member with the same name and date of birth.",
                ),
                "transfer": transfer,
            }

        if plans_move:
            try:
                with transaction.atomic():
                    _move_live_plans(member, user)
            except (IntegrityError, TransferMoveError) as exc:
                transaction.set_rollback(True)
                if isinstance(exc, IntegrityError):
                    message = pick(
                        member.club,
                        "受け取るアカウントに、同じ会員の同じプランがすでにあります。",
                        "The receiving account already has this member on that plan.",
                    )
                else:
                    message = exc.message
                return {
                    "ok": False,
                    "mode": "blocked",
                    "message": message,
                    "transfer": transfer,
                }

        now = timezone.now()
        transfer.status = MemberTransfer.Status.ACCEPTED
        transfer.accepted_at = now
        transfer.save(update_fields=["status", "accepted_at"])
        MemberTransfer.objects.filter(
            member=member,
            status=MemberTransfer.Status.PENDING,
        ).exclude(pk=transfer.pk).update(status=MemberTransfer.Status.CANCELED)

        if plans_move:
            success = pick(
                member.club,
                f"{member.full_name}さんをあなたのアカウントに移しました。"
                "残っているプランも一緒に移しています。",
                f"{member.full_name} is now on your account, including the plans they still had.",
            )
        else:
            success = pick(
                member.club,
                f"{member.full_name}さんをあなたのアカウントに移しました。"
                "プランは引き継がれていないので、続ける場合は改めて加入してください。",
                f"{member.full_name} is now on your account. Their plan did not come with them, so add one if they should stay enrolled.",
            )
        return {
            "ok": True,
            "mode": "success",
            "message": success,
            "transfer": transfer,
            "member": member,
        }


def _move_live_plans(member, to_user):
    """Move active and grace items onto the receiver's non-card subscription."""
    now = timezone.now()
    items = list(
        member.subscription_items
        .select_for_update()
        .filter(Q(deleted_at__isnull=True) | Q(access_until__gt=now))
        .order_by("id")
    )
    if not items:
        return

    source = (
        Subscription.objects
        .select_for_update()
        .get(pk=items[0].subscription_id)
    )
    if _is_stripe_subscription(source):
        raise TransferMoveError(
            pick(
                member.club,
                "この会員にはプランが残っています。\n"
                "クレジットカード決済のアカウントからは、プラン付きで移せません。\n"
                "先にプランを解約してください。",
                "This member still has a plan.\n"
                "A member with a plan can't be moved off a card-billing account.\n"
                "Cancel the plan first.",
            )
        )

    billing_method = (
        source.billing_method
        if source.billing_method in ("cash", "bank_transfer", "manual")
        else "cash"
    )
    destination, created = Subscription.objects.get_or_create(
        owner=to_user,
        club_id=member.club_id,
        defaults={
            "stripe_subscription_id": None,
            "status": "active",
            "billing_method": billing_method,
            "billing_mode": source.billing_mode,
            "billing_anchor_day": source.billing_anchor_day,
            "cancel_at_period_end": False,
            "current_period_end": source.current_period_end,
            "access_until": source.access_until,
        },
    )
    if not created:
        destination = Subscription.objects.select_for_update().get(pk=destination.pk)
        if _is_stripe_subscription(destination):
            raise TransferMoveError(
                pick(
                    member.club,
                    "受け取るアカウントはクレジットカード決済です。\n"
                    "プランが残っている会員を移せるのは、移す側と受け取る側の両方が"
                    "クレジットカード決済ではないときだけです。",
                    "The account that would receive this member pays by card.\n"
                    "A member who still has a plan can move only when neither account pays by card.",
                )
            )
        if destination.status not in ("active", "trialing", "past_due", "pending"):
            destination.status = "active"
            update_fields = ["status"]
            if source.current_period_end and (
                destination.current_period_end is None
                or destination.current_period_end < now
            ):
                destination.current_period_end = source.current_period_end
                destination.access_until = source.access_until
                update_fields.extend(["current_period_end", "access_until"])
            destination.save(update_fields=update_fields)

    for item in items:
        item.subscription = destination
        item.save(update_fields=["subscription"])

    still_live = source.items.filter(
        Q(deleted_at__isnull=True) | Q(access_until__gt=now)
    ).exists()
    if not still_live and source.status in ("active", "trialing", "past_due", "pending"):
        source.status = "canceled"
        source.save(update_fields=["status"])
