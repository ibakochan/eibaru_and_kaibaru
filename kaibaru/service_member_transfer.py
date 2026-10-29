import logging
import secrets

from django.conf import settings
from django.core.mail import EmailMessage
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from accounts.models import CustomUser

from .models import Member, MemberTransfer

logger = logging.getLogger(__name__)


def _plan_name(item):
    return item.plan.name if item.plan_id else "プラン"


def _local_date(value):
    local = timezone.localtime(value)
    return f"{local.year}年{local.month}月{local.day}日"


def transfer_block_detail(member):
    """Why this member cannot move to another account, or None."""
    active_items = list(
        member.subscription_items
        .filter(deleted_at__isnull=True)
        .select_related("plan")
        .order_by("id")
    )
    if active_items:
        lines = [f"・{_plan_name(item)}" for item in active_items]
        return (
            "この会員には、まだ解約されていないプランがあります。\n"
            "別のアカウントへ移すには、先にサブスクを解約してください。\n"
            + "\n".join(lines)
        )

    grace_items = list(
        member.subscription_items
        .filter(deleted_at__isnull=False, access_until__gt=timezone.now())
        .select_related("plan")
        .order_by("access_until", "id")
    )
    if grace_items:
        lines = [
            f"・{_plan_name(item)}（{_local_date(item.access_until)}まで）"
            for item in grace_items
        ]
        latest = max(item.access_until for item in grace_items)
        return (
            "この会員には、まだ利用できるプランが残っています。\n"
            + "\n".join(lines)
            + f"\n{_local_date(latest)}を過ぎてから移してください。"
        )

    return None


def request_member_transfer(*, member, requester, email):
    if member.club_id is None or member.owner_id != requester.id:
        return {"ok": False, "status": 403, "detail": "許可なし"}

    if member.user_id is not None:
        return {
            "ok": False,
            "status": 400,
            "detail": "アカウントに紐づいている会員は移せません。",
        }

    blocked = transfer_block_detail(member)
    if blocked:
        return {"ok": False, "status": 400, "detail": blocked}

    cleaned = (email or "").strip()
    if not cleaned:
        return {"ok": False, "status": 400, "detail": "メールアドレスを入力してください。"}

    to_user = CustomUser.objects.filter(email__iexact=cleaned).first()
    if to_user is None:
        return {
            "ok": False,
            "status": 400,
            "detail": "このメールアドレスのアカウントが見つかりません。",
        }

    if to_user.id == requester.id:
        return {
            "ok": False,
            "status": 400,
            "detail": "自分のアカウントには移せません。",
        }

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
            "detail": "メールを送信できませんでした。しばらくしてから再度お試しください。",
        }
    return {"ok": True, "status": 200, "detail": "移管の依頼を送信しました。"}


def _send_transfer_email(transfer):
    member = transfer.member
    club = member.club
    club_name = (club.title or club.subdomain) if club else "クラブ"
    requester_name = transfer.from_owner.get_full_name() or transfer.from_owner.email
    link = (
        f"https://{club.subdomain}.kaibaru.jp/"
        f"accept_member_transfer/{transfer.token}/"
    )
    subject = f"【{club_name}】会員をあなたのアカウントへ移す依頼"
    message = (
        f"{requester_name}さんから、{club_name}の会員"
        f"「{member.full_name}」をあなたのアカウントへ移す依頼が届きました。\n\n"
        "受け入れると、この会員はあなたのアカウントに加わります。\n"
        "プランは引き継がれません。続ける場合は、受け入れたあと改めて加入してください。\n\n"
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

        if transfer.status != MemberTransfer.Status.PENDING:
            return {"ok": False, "mode": "invalid", "message": "この依頼はすでに完了しているか、取り消されています。"}

        if not user.is_authenticated or user.id != transfer.to_user_id:
            return {
                "ok": False,
                "mode": "wrong_user",
                "message": (
                    "この依頼は別のアカウント宛です。"
                    f"{transfer.to_user.email} でログインしてください。"
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
                "message": "この会員はもう移せません。",
            }

        blocked = transfer_block_detail(member)
        if blocked:
            return {"ok": False, "mode": "blocked", "message": blocked, "transfer": transfer}

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
                "message": "同じ名前と生年月日の会員が、すでにあなたのアカウントにいます。",
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

        return {
            "ok": True,
            "mode": "success",
            "message": (
                f"{member.full_name}さんをあなたのアカウントに移しました。"
                "プランは引き継がれていないので、続ける場合は改めて加入してください。"
            ),
            "transfer": transfer,
            "member": member,
        }
