from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMessage, send_mail
from accounts.models import CustomUser
import logging
logger = logging.getLogger(__name__)

from .models import Club, SubscriptionItem, Member
from django.db import transaction
 
from datetime import datetime, timedelta

def format_date(dt, language="ja"):
    if not dt:
        return "---"
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    if language == "en":
        return f"{dt.strftime('%B')} {dt.day}, {dt.year}"
    return dt.strftime("%Y年%m月%d日")


def club_is_english(club):
    return getattr(club, "language", "ja") == "en"


def account_is_english(user):
    """Platform mail is for the account, not one club.

    English only when every club on the account is English, so a
    Japanese club never starts receiving English platform mail.
    """
    if user is None:
        return False
    languages = set(
        Club.objects.filter(owner=user, is_deleted=False).values_list("language", flat=True)
    )
    return bool(languages) and languages == {"en"}


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=60,
    retry_kwargs={"max_retries": 5},
)
def send_invoice_created_email(self, invoice_id):
    from .models import Invoice

    invoice = (
        Invoice.objects
        .select_related(
            "subscription",
            "subscription__club",
            "subscription__owner",
            "payer",
        )
        .prefetch_related(
            "items__member",
        )
        .filter(id=invoice_id)
        .first()
    )

    if not invoice:
        return

    # ---------------------------------------------------------
    # Recipient
    # ---------------------------------------------------------

    owner_email = invoice.payer_email

    if not owner_email and invoice.subscription and invoice.subscription.owner:
        owner_email = invoice.subscription.owner.email

    if not owner_email:
        logger.warning(
            "[EMAIL] Invoice=%s has no recipient email. Skipping.",
            invoice.id,
        )
        return

    # ---------------------------------------------------------
    # Club
    # ---------------------------------------------------------

    club = invoice.subscription.club if invoice.subscription else invoice.club

    english = club_is_english(club)
    club_name = club.subdomain or ("the club" if english else "クラブ")

    # ---------------------------------------------------------
    # Recipient name
    #
    # Prefer the member whose user is the subscription owner.
    # This handles the normal case where the account owner is
    # also the member.
    # ---------------------------------------------------------

    subscription_owner_id = (
        invoice.subscription.owner_id
        if invoice.subscription
        else None
    )

    invoice_items = list(invoice.items.all())

    owner_member = next(
        (
            item.member
            for item in invoice_items
            if item.member
            and item.member.user_id == subscription_owner_id
        ),
        None,
    )

    if owner_member:
        recipient_name = owner_member.full_name
    else:
        recipient_name = (
            invoice.payer_name
            or (
                invoice.subscription.owner.get_full_name()
                if invoice.subscription
                and invoice.subscription.owner
                else None
            )
            or owner_email
        )

    # ---------------------------------------------------------
    # Invoice items
    # ---------------------------------------------------------

    item_texts = []

    for item in invoice_items:
        if item.member:
            member_name = item.member.full_name
        else:
            member_name = "Charge" if english else "ご利用料金"

        item_texts.append(
            f"{member_name} - {item.description} ¥{item.amount:,}"
        )

    item_text = (
        "\n".join(item_texts)
        if item_texts
        else ("Charge" if english else "ご利用料金")
    )

    # ---------------------------------------------------------
    # Email
    # ---------------------------------------------------------

    if english:
        subject = f"[{club_name}] Payment due"
        message = (
            f"Hi {recipient_name},\n\n"
            f"{club_name} has a payment due.\n\n"
            f"What it's for\n"
            f"{item_text}\n\n"
            f"Amount due\n"
            f"¥{invoice.amount_due:,}\n\n"
            f"Due\n"
            f"{format_date(invoice.due_date, 'en')}\n\n"
            f"This period is not extended until the payment is received.\n\n"
            f"Questions about how to pay go to {club_name}.\n\n"
            f"{club_name}"
        )
    else:
        subject = f"【{club_name}】お支払いについてのお知らせ"
        message = (
            f"{recipient_name} 様\n\n"
            f"{club_name}より、今月のお支払いについてご案内いたします。\n\n"

            f"■ ご利用内容\n"
            f"{item_text}\n\n"

            f"■ お支払い金額\n"
            f"¥{invoice.amount_due:,}\n\n"

            f"■ お支払い期限\n"
            f"{format_date(invoice.due_date)}\n\n"

            f"お支払いが確認されるまで、"
            f"今回のご利用期間は延長されません。\n\n"

            f"お支払い方法やご不明な点につきましては、"
            f"{club_name}までお問い合わせください。\n\n"

            f"{club_name}"
        )
    send_mail(
        subject=subject,
        message=message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[owner_email],
    )

    logger.info(
        "[EMAIL] Invoice payment reminder sent: "
        "invoice=%s club=%s recipient=%s",
        invoice.id,
        club_name,
        owner_email,
    )


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=30,
    retry_kwargs={"max_retries": 5},
)
def send_plan_deletion_emails(self, owner_map):
    for owner_id, data in owner_map.items():
        owner = CustomUser.objects.filter(id=owner_id).first()
        if not owner:
            continue

        members = data["members"]
        plans = data["plans"]
        access_until = data["access_until"]
        english = data.get("language") == "en"

        member_text = (", " if english else "、").join(members)
        plan_text = (", " if english else "、").join(plans)

        if english:
            message = (
                f"Hi {owner.get_full_name() or owner.email},\n\n"
                f"The plan \"{plan_text}\" is now scheduled to end.\n\n"
                f"Members\n"
                f"{member_text}\n\n"
                f"You can keep using it until\n"
                f"{format_date(access_until, 'en')}\n\n"
                f"After you log in, you can switch this scheduled removal to a plan change.\n\n"
            )
        else:
            message = (
                f"{owner.get_full_name() or owner.email} 様\n\n"
                f"ご利用中のプラン「{plan_text}」が削除スケジュールに入りました。\n\n"
                f"■ 対象メンバー\n"
                f"{member_text}\n\n"
                f"■ ご利用可能期限\n"
                f"{format_date(access_until)} まで\n\n"
                f"この日付までは引き続きご利用いただけます。\n\n"
                f"削除スケジュールはログイン後にプラン変更へ切り替えることが可能です。\n\n"
            )

        has_active = SubscriptionItem.objects.filter(
            member__owner_id=owner.id,
            deleted_at__isnull=True
        ).exists()

        if not has_active:
            if english:
                message += (
                    "\nEvery plan on this account is now scheduled to end.\n"
                    "If nobody is put on a new plan before the next billing cycle, the subscription stops.\n"
                    "Log in and change the plan if you want to keep going.\n"
                )
            else:
                message += (
                    "\n現在すべてのご契約プランが削除予約状態となっています。\n"
                    "次回の請求サイクルまでにいずれかのメンバーに対して新しいプラン設定が行われない場合、\n"
                    "サブスクリプションは停止されます。\n"
                    "継続をご希望の場合は、ログインの上、プラン変更を行ってください。\n"
                )

        send_mail(
            subject="[Kaibaru] A plan is scheduled to end" if english else "【Kaibaru】プラン削除スケジュールのお知らせ",
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[owner.email],
        )

        
@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=30, retry_kwargs={"max_retries": 5})
def send_invoice_paid_email(self, member_id, amount, items, period_end, plan_name):
    from .models import Member  # import inside to avoid circular imports

    member = Member.objects.select_related("owner", "club").filter(id=member_id).first()
    if not member:
        return

    owner_email = member.owner.email
    owner_name = member.owner.get_full_name() or member.owner.email
    english = club_is_english(member.club)

    item_text = (", " if english else "、").join(items) if items else ("Payment" if english else "お支払い")

    if english:
        subject = "[Kaibaru] Payment received"
        message = (
            f"Hi {owner_name},\n\n"
            f"This payment has gone through.\n\n"
            f"Plan: {plan_name}\n"
            f"Details: {item_text}\n"
            f"Amount: ¥{amount}\n"
            f"Access until: {format_date(period_end, 'en')}\n\n"
            f"Each payment extends this plan by one month.\n"
        )
    else:
        subject = "【Kaibaru】お支払いが完了しました"
        message = (
            f"{owner_name} 様\n\n"
            f"以下のお支払いが完了しました。\n\n"
            f"プラン: {plan_name}\n"
            f"内容: {item_text}\n"
            f"金額: ¥{amount}\n"
            f"ご利用可能期限: {format_date(period_end)}\n\n"
            f"本プランはお支払いごとに1ヶ月分ずつご利用期間が延長されます。\n"
            f"今後ともよろしくお願いいたします。\n"
        )
    send_mail(
        subject=subject,
        message=message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[owner_email],
    )

@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=60,
    retry_kwargs={"max_retries": 5},
)
def send_club_deleted_emails(self, club_data):
    """
    club_data is a dict because the Club row is already deleted
    """
    owner_email = club_data["owner_email"]
    owner_name = club_data["owner_name"]
    subdomain = club_data["subdomain"]
    reason = club_data["reason"]

    # Admin
    send_mail(
        subject=f"[Kaibaru] Club deleted ({subdomain})",
        message=(
            f"Club: {subdomain}\n"
            f"Owner: {owner_name}\n"
            f"Email: {owner_email}\n"
            f"Reason: {reason}\n"
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[settings.SERVER_EMAIL],
    )

    if club_data.get("language") == "en":
        owner_subject = "Your Kaibaru club was deleted"
        owner_message = (
            f"Hi {owner_name},\n\n"
            f"The club {subdomain} was deleted for this reason:\n\n"
            f"{reason}\n\n"
            f"If something looks wrong, contact Kaibaru support.\n\n"
            f"Kaibaru"
        )
    else:
        owner_subject = "【Kaibaru】クラブが削除されました"
        owner_message = (
            f"{owner_name} 様\n\n"
            f"クラブ「{subdomain}」は以下の理由により削除されました。\n\n"
            f"{reason}\n\n"
            f"ご不明な点がございましたら、Kaibaru サポートまでご連絡ください。\n\n"
            f"Kaibaru 運営チーム"
        )
    send_mail(
        subject=owner_subject,
        message=owner_message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[owner_email],
    )


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=60,
    retry_kwargs={"max_retries": 5},
)
def send_subscription_canceled_emails(self, club_data):
    owner_email = club_data["owner_email"]
    owner_name = club_data["owner_name"]
    subdomain = club_data["subdomain"]
 
    send_mail(
        subject=f"[Kaibaru] Subscription canceled ({subdomain})",
        message=(
            f"Club: {subdomain}\n"
            f"Owner: {owner_name}\n"
            f"Email: {owner_email}\n"
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[settings.SERVER_EMAIL],
    )
 
    if club_data.get("language") == "en":
        owner_subject = "Your Kaibaru subscription is cancelled"
        owner_message = (
            f"Hi {owner_name},\n\n"
            f"Thanks for using Kaibaru.\n"
            f"The subscription for {subdomain} is cancelled.\n\n"
            f"You're welcome back any time.\n\n"
            f"Kaibaru"
        )
    else:
        owner_subject = "【Kaibaru】ご利用プランの解約が完了しました"
        owner_message = (
            f"{owner_name} 様\n\n"
            f"Kaibaru をご利用いただき、ありがとうございました。\n"
            f"クラブ「{subdomain}」のご利用プランは解約されました。\n\n"
            f"またのご利用をお待ちしております。\n\n"
            f"Kaibaru 運営チーム"
        )
    send_mail(
        subject=owner_subject,
        message=owner_message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[owner_email],
    )



@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=60,
    retry_kwargs={"max_retries": 5},
)
def send_platform_cycle_paid_email(self, platform_subscription_id, invoice_id):
    """Receipt for a paid platform subscription cycle.

    Uses the same invoice lines as 請求歴, without the member-Stripe 1%
    which is collected separately and is not on this invoice.
    """
    import stripe

    from .models import PlatformSubscription
    from .service_platform_billing_history import (
        _build_history_entry,
        _owner_club_ids,
        _stripe,
        format_platform_cycle_receipt,
    )

    platform_sub = (
        PlatformSubscription.objects
        .select_related("user")
        .filter(id=platform_subscription_id)
        .first()
    )
    if not platform_sub or not platform_sub.user_id:
        logger.warning(
            "[EMAIL] Platform cycle receipt missing subscription id=%s",
            platform_subscription_id,
        )
        return

    owner = platform_sub.user
    if not owner.email:
        logger.warning(
            "[EMAIL] Platform cycle receipt owner has no email sub=%s",
            platform_subscription_id,
        )
        return

    _stripe()
    stripe_invoice = stripe.Invoice.retrieve(invoice_id, expand=["lines"])
    entry = _build_history_entry(
        stripe_invoice,
        _owner_club_ids(platform_sub.user_id),
        include_stripe_processing=False,
    )
    english = account_is_english(owner)
    receipt = format_platform_cycle_receipt(entry, "en" if english else "ja")
    pay_month = ""
    if entry.get("payment_year") and entry.get("payment_month"):
        if english:
            months = [
                "", "January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December",
            ]
            pay_month = f"{months[int(entry['payment_month'])]} {entry['payment_year']}"
        else:
            pay_month = f"{entry['payment_year']}年{entry['payment_month']}月"
    owner_name = owner.get_full_name() or owner.email

    if english:
        subject = f"[Kaibaru] Platform fee for {pay_month}" if pay_month else "[Kaibaru] Platform fee"
        message = (
            f"Hi {owner_name},\n\n"
            f"We received your Kaibaru platform fee.\n\n"
            f"{receipt}\n\n"
            f"Kaibaru"
        )
    else:
        subject = f"【Kaibaru】{pay_month}のプラットフォーム利用料" if pay_month else "【Kaibaru】プラットフォーム利用料"
        message = (
            f"{owner_name} 様\n\n"
            f"Kaibaru プラットフォーム利用料のお支払いを確認しました。\n\n"
            f"{receipt}\n\n"
            f"Kaibaru 運営チーム"
        )
    send_mail(
        subject=subject,
        message=message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[owner.email],
    )


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=60,
    retry_kwargs={"max_retries": 5},
)
def send_subscription_activated_emails(self, club_id, invoice_id):
    club = Club.objects.select_related("owner").get(id=club_id)
    owner = club.owner
    if not owner or not owner.email:
        logger.warning(
            "[EMAIL] Subscription activated skipped, no owner email club=%s",
            club_id,
        )
        return

    send_mail(
        subject=f"[Kaibaru] Subscription activated ({club.subdomain})",
        message=(
            f"Club: {club.subdomain}\n"
            f"Owner: {owner.get_full_name()}\n"
            f"Email: {owner.email}\n"
            f"Invoice ID: {invoice_id}\n"
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[settings.SERVER_EMAIL],
    )

    if club_is_english(club):
        owner_subject = "Your Kaibaru plan is active"
        owner_message = (
            f"Hi {owner.get_full_name() or owner.email},\n\n"
            f"Thanks for using Kaibaru.\n"
            f"The plan for {club.subdomain} is now active.\n\n"
            f"Upcoming charges are on the club page under Next bill.\n\n"
            f"Kaibaru"
        )
    else:
        owner_subject = "【Kaibaru】ご利用プランの有効化が完了しました"
        owner_message = (
            f"{owner.get_full_name() or owner.email} 様\n\n"
            f"Kaibaru をご利用いただきありがとうございます。\n"
            f"クラブ「{club.subdomain}」のご利用プランが有効になりました。\n\n"
            f"次回以降の請求内容は、クラブページの「次回請求」から確認できます。\n\n"
            f"今後も Kaibaru をよろしくお願いいたします。\n\n"
            f"Kaibaru 運営チーム"
        )
    send_mail(
        subject=owner_subject,
        message=owner_message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[owner.email],
    )


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=60,
    retry_kwargs={"max_retries": 5},
)
def send_platform_club_added_email(self, club_id, prorated_yen=0):
    club = Club.objects.select_related("owner").filter(id=club_id).first()
    if not club or not club.owner_id:
        return
    owner = club.owner
    if not owner.email:
        logger.warning(
            "[EMAIL] Platform club added skipped, no owner email club=%s",
            club_id,
        )
        return

    try:
        prorated_yen = max(0, int(prorated_yen or 0))
    except (TypeError, ValueError):
        prorated_yen = 0

    english = club_is_english(club)
    club_name = club.title or club.subdomain
    owner_name = owner.get_full_name() or owner.email
    if prorated_yen > 0:
        fee_line = (
            (
                f"The prorated charge for adding it is ¥{prorated_yen:,}. "
                f"It is added to the next bill.\n"
            )
            if english else
            (
                f"今回の追加に伴う日割り料金は ¥{prorated_yen:,} です。"
                f"次回の請求に加算されます。\n"
            )
        )
    else:
        fee_line = (
            "Nothing extra is added to the current bill for this club.\n"
            if english else
            "今回の追加について、いま請求に載る日割り料金はありません。\n"
        )

    if english:
        subject = f"[Kaibaru] {club.subdomain} was added to your plan"
        message = (
            f"Hi {owner_name},\n\n"
            f"{club_name} ({club.subdomain}) was added to your Kaibaru plan.\n\n"
            f"{fee_line}"
            f"The updated next bill is on the club page under Next bill.\n\n"
            f"Kaibaru"
        )
    else:
        subject = f"【Kaibaru】クラブ「{club.subdomain}」を契約に追加しました"
        message = (
            f"{owner_name} 様\n\n"
            f"クラブ「{club_name}」（{club.subdomain}）を"
            f"Kaibaru の契約に追加しました。\n\n"
            f"{fee_line}"
            f"更新後の次回請求額は、クラブページの「次回請求」から確認できます。\n\n"
            f"Kaibaru 運営チーム"
        )
    send_mail(
        subject=subject,
        message=message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[owner.email],
    )


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=60,
    retry_kwargs={"max_retries": 5},
)
def send_platform_domain_added_email(self, club_id, domain, prorated_yen=0):
    club = Club.objects.select_related("owner").filter(id=club_id).first()
    if not club or not club.owner_id:
        return
    owner = club.owner
    if not owner.email:
        logger.warning(
            "[EMAIL] Platform domain added skipped, no owner email club=%s",
            club_id,
        )
        return

    try:
        prorated_yen = max(0, int(prorated_yen or 0))
    except (TypeError, ValueError):
        prorated_yen = 0

    english = club_is_english(club)
    domain = (domain or club.custom_domain or "").strip()
    club_name = club.title or club.subdomain
    owner_name = owner.get_full_name() or owner.email
    if prorated_yen > 0:
        fee_line = (
            (
                f"The prorated charge for the domain is ¥{prorated_yen:,}. "
                f"It is added to the next bill.\n"
            )
            if english else
            (
                f"独自ドメイン追加に伴う日割り料金は ¥{prorated_yen:,} です。"
                f"次回の請求に加算されます。\n"
            )
        )
    else:
        fee_line = (
            "Nothing extra is added to the current bill for this domain.\n"
            if english else
            "今回のドメイン追加について、いま請求に載る日割り料金はありません。\n"
        )

    if english:
        subject = f"[{club_name}] Your domain is ready"
        message = (
            f"Hi {owner_name},\n\n"
            f"{domain} is now the public address for {club_name}.\n\n"
            f"https://{domain}\n\n"
            f"{fee_line}"
            f"The updated next bill is on the club page under Next bill.\n\n"
            f"Kaibaru"
        )
    else:
        subject = f"【{club_name}】独自ドメインの準備ができました"
        message = (
            f"{owner_name} 様\n\n"
            f"{domain} を{club_name}のアドレスとして公開しました。\n\n"
            f"https://{domain}\n\n"
            f"{fee_line}"
            f"更新後の次回請求額は、クラブページの「次回請求」から確認できます。\n\n"
            f"Kaibaru 運営チーム"
        )
    send_mail(
        subject=subject,
        message=message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[owner.email],
    )



@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=30, retry_kwargs={"max_retries": 5})
def send_club_created_emails(self, club_id):
    club = Club.objects.select_related("owner").get(id=club_id)
    owner = club.owner

    send_mail(
        subject=f"[Kaibaru] New club created: {club.subdomain}",
        message=(
            f"Subdomain: {club.subdomain}\n"
            f"Owner: {owner.get_full_name()}\n"
            f"Email: {owner.email}\n"
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[settings.SERVER_EMAIL],
    )

    if club.language == "en":
        owner_subject = "Your Kaibaru site is ready"
        owner_message = (
            f"Hi {owner.get_full_name()},\n\n"
            f"Your site {club.subdomain} is ready.\n"
            f"It is set to English, so anyone who visits it sees it in English too. "
            f"You can switch it back to Japanese in Settings.\n\n"
            f"Kaibaru"
        )
    else:
        owner_subject = "【Kaibaru】クラブ作成が完了しました"
        owner_message = (
            f"{owner.get_full_name()} 様\n\n"
            f"Kaibaru にご登録いただきありがとうございます。\n"
            f"クラブ「{club.subdomain}」が作成されました。\n\n"
            f"Kaibaru 運営チーム"
        )
    send_mail(
        subject=owner_subject,
        message=owner_message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[owner.email],
    )


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=60,
    retry_kwargs={"max_retries": 5},
)
def send_stripe_cash_transition_email(self, invoice_id):
    """
    Notify the billing user and club owner that an invoice or
    subscription has been moved from Stripe collection to cash.

    Member and owner emails are claimed independently.

    This provides at-most-once delivery per recipient from the
    application side:

        stripe_cash_member_email_sent
        stripe_cash_owner_email_sent

    If one email succeeds and the other fails, Celery can retry
    the task without sending the successful email again.
    """

    from .models import Invoice

    # ---------------------------------------------------------
    # LOAD INVOICE
    # ---------------------------------------------------------

    invoice = (
        Invoice.objects
        .select_related(
            "subscription",
            "subscription__owner",
            "club",
            "club__owner",
        )
        .filter(id=invoice_id)
        .first()
    )

    if not invoice:
        logger.warning(
            "[EMAIL] Stripe→cash transition invoice=%s "
            "does not exist.",
            invoice_id,
        )
        return

    if invoice.stripe_cash_transition_status != "succeeded":
        logger.warning(
            "[EMAIL] Stripe→cash transition invoice=%s "
            "is not succeeded. status=%s. Skipping email.",
            invoice.id,
            invoice.stripe_cash_transition_status,
        )
        return

    subscription = invoice.subscription
    club = invoice.club

    if not subscription:
        logger.warning(
            "[EMAIL] Stripe→cash transition invoice=%s "
            "has no subscription.",
            invoice.id,
        )
        return

    billing_user = subscription.owner
    club_owner = club.owner

    # ---------------------------------------------------------
    # TRANSITION TYPE
    # ---------------------------------------------------------

    is_subscription_level = invoice.billing_reason in [
        "initial_subscription",
        "subscription_cycle",
    ]

    club_name = club.subdomain or ("the club" if club_is_english(club) else "クラブ")
    

    member = (
        Member.objects
        .filter(
            club=club,
            user=billing_user,
            owner=billing_user,
        )
            .first()
    )

    if member:
        member_name = member.full_name
    else:
        member_name = (
            billing_user.get_full_name()
            if billing_user
            else None
        ) or (
            invoice.payer_name
            or invoice.payer_email
            or ("there" if club_is_english(club) else "お客様")
        )

    owner_name = (
        club_owner.get_full_name()
        if club_owner
        else ("the club manager" if club_is_english(club) else "クラブ管理者")
    )

    amount_text = f"¥{invoice.amount_due:,}"

    # =========================================================
    # 1. MEMBER EMAIL
    # =========================================================

    member_email = None

    if billing_user and billing_user.email:
        member_email = billing_user.email

    elif invoice.payer_email:
        member_email = invoice.payer_email

    if member_email:

        # -----------------------------------------------------
        # CLAIM MEMBER EMAIL
        #
        # Only this recipient is locked/claimed.
        # -----------------------------------------------------

        with transaction.atomic():

            invoice_for_claim = (
                Invoice.objects
                .select_for_update()
                .get(id=invoice.id)
            )

            if (
                invoice_for_claim
                .stripe_cash_member_email_sent
            ):
                logger.info(
                    "[EMAIL] Stripe→cash member email already sent "
                    "invoice=%s. Skipping.",
                    invoice.id,
                )

                member_email = None

            else:

                invoice_for_claim.stripe_cash_member_email_sent = True

                invoice_for_claim.save(
                    update_fields=[
                        "stripe_cash_member_email_sent",
                    ]
                )

        # -----------------------------------------------------
        # SEND MEMBER EMAIL
        # -----------------------------------------------------

        if member_email:

            member_subject = (
                f"【{club_name}】お支払い方法変更のお知らせ"
            )

            if is_subscription_level:

                member_message = (
                    f"{member_name} 様\n\n"

                    f"{club_name}のお支払いについて、"
                    f"クレジットカードでのお支払いを確認できなかったため、"
                    f"今回の請求を現金払いへ変更し、"
                    f"今後のお支払い方法も現金払いへ変更いたしました。\n\n"
                
                    f"■ 変更内容\n"
                    f"今回の請求：クレジットカード決済 → 現金払い\n"
                    f"今後のお支払い：現金払い\n\n"
                
                    f"■ 今回のお支払い\n"
                    f"{amount_text}\n\n"
                
                    f"今回の請求については、"
                    f"{club_name}へ直接お支払いください。\n\n"
                
                    f"未払いの請求書をすべてお支払いいただいた後は、"
                    f"ログイン後、{club_name}のホームページの「会員プラン」から"
                    f"お支払い方法をクレジットカード決済（Stripe）へ"
                    f"戻すことができます。\n\n"
                
                    f"なお、未払いの請求書が残っている場合は、"
                    f"クレジットカード決済（Stripe）へ戻すことはできません。\n\n"
                
                    f"お支払い方法や金額についてご不明な点がございましたら、"
                    f"{club_name}までお問い合わせください。\n\n"
                
                    f"{club_name}"
                )                
                
            else:

                member_message = (
                    f"{member_name} 様\n\n"

                    f"{club_name}のお支払いについて、"
                    f"今回のクレジットカード決済を確認できなかったため、"
                    f"この請求のお支払い方法を現金払いへ変更いたしました。\n\n"

                    f"■ 変更内容\n"
                    f"クレジットカード決済 → 現金払い\n\n"

                    f"■ 今回のお支払い\n"
                    f"{amount_text}\n\n"

                    f"今回のお支払いについては、"
                    f"{club_name}へ直接お支払いください。\n\n"

                    f"なお、今後の通常のサブスクリプションのお支払いは、"
                    f"引き続きクレジットカードで処理されます。\n\n"

                    f"お支払い方法や金額についてご不明な点がございましたら、"
                    f"{club_name}までお問い合わせください。\n\n"

                    f"{club_name}"
                )

            if club_is_english(club):
                member_subject = f"[{club_name}] How you pay has changed"
                if is_subscription_level:
                    member_message = (
                        f"Hi {member_name},\n\n"
                        f"The card payment for {club_name} did not go through, "
                        f"so this bill and future bills are now pay-at-the-club.\n\n"
                        f"What changed\n"
                        f"This bill: card → pay at the club\n"
                        f"From now on: pay at the club\n\n"
                        f"This bill\n"
                        f"{amount_text}\n\n"
                        f"Pay {club_name} directly for this bill.\n\n"
                        f"Once every unpaid bill is settled, you can log in and switch "
                        f"the member plan back to card (Stripe).\n\n"
                        f"You can't switch back while an unpaid bill is still open.\n\n"
                        f"Questions about the amount go to {club_name}.\n\n"
                        f"{club_name}"
                    )
                else:
                    member_message = (
                        f"Hi {member_name},\n\n"
                        f"The card payment for this {club_name} bill did not go through, "
                        f"so this bill is now pay-at-the-club.\n\n"
                        f"What changed\n"
                        f"Card → pay at the club\n\n"
                        f"This bill\n"
                        f"{amount_text}\n\n"
                        f"Pay {club_name} directly for this one.\n\n"
                        f"Your regular subscription still bills the card.\n\n"
                        f"Questions about the amount go to {club_name}.\n\n"
                        f"{club_name}"
                    )
            send_mail(
                subject=member_subject,
                message=member_message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[member_email],
            )

            logger.info(
                "[EMAIL] Stripe→cash member email sent "
                "invoice=%s recipient=%s subscription_level=%s",
                invoice.id,
                member_email,
                is_subscription_level,
            )

    else:

        logger.warning(
            "[EMAIL] Stripe→cash invoice=%s "
            "has no member/billing recipient email.",
            invoice.id,
        )

    # =========================================================
    # 2. CLUB OWNER EMAIL
    # =========================================================

    if not club_owner or not club_owner.email:

        logger.warning(
            "[EMAIL] Stripe→cash invoice=%s "
            "has no club owner email.",
            invoice.id,
        )
        return

    owner_email = club_owner.email

    # ---------------------------------------------------------
    # CLAIM OWNER EMAIL
    # ---------------------------------------------------------

    with transaction.atomic():

        invoice_for_claim = (
            Invoice.objects
            .select_for_update()
            .get(id=invoice.id)
        )

        if invoice_for_claim.stripe_cash_owner_email_sent:

            logger.info(
                "[EMAIL] Stripe→cash owner email already sent "
                "invoice=%s. Skipping.",
                invoice.id,
            )

            owner_email = None

        else:

            invoice_for_claim.stripe_cash_owner_email_sent = True

            invoice_for_claim.save(
                update_fields=[
                    "stripe_cash_owner_email_sent",
                ]
            )

    # ---------------------------------------------------------
    # SEND OWNER EMAIL
    # ---------------------------------------------------------

    if owner_email:

        if is_subscription_level:

            owner_subject = (
                f"【{club_name}】会員の決済失敗に伴う"
                f"現金払いへの変更について"
            )

            owner_message = (
                f"{owner_name} 様\n\n"

                f"会員「{member_name}」様のクレジットカード決済が"
                f"一定期間にわたり完了しなかったため、"
                f"対象の請求書を現金払いへ変更し、"
                f"サブスクリプション全体の支払い方法も"
                f"現金払いへ変更しました。\n\n"
            
                f"■ 対象会員\n"
                f"{member_name}\n\n"
            
                f"■ 対象請求書\n"
                f"{invoice.number or invoice.id}\n\n"
            
                f"■ 請求金額\n"
                f"{amount_text}\n\n"
            
                f"■ 請求理由\n"
                f"{invoice.billing_reason or '---'}\n\n"
            
                f"■ 変更内容\n"
                f"クレジットカード決済 → 現金払い\n"
                f"サブスクリプション全体も現金払いへ変更\n\n"
            
                f"今後、この会員様の請求は現金での回収となります。\n\n"
            
                f"会員様から未払いの請求について現金でのお支払いを受けた場合は、"
                f"管理画面から該当する請求書を「支払済み」として"
                f"処理してください。\n\n"
            
                f"未払いの請求書がすべて支払済みになった後は、"
                f"会員様ご本人がログインし、"
                f"{club_name}のホームページの「会員プラン」から"
                f"サブスクリプションのお支払い方法を"
                f"クレジットカード決済（Stripe）へ戻すことができます。\n\n"
            
                f"なお、未払いの請求書が残っている場合は、"
                f"クレジットカード決済（Stripe）へ戻すことはできません。\n\n"
            
                f"ご確認のうえ、必要に応じて現金でのお支払いを"
                f"ご案内ください。\n\n"
            
                f"{club_name}"
            )
            
        else:

            owner_subject = (
                f"【{club_name}】会員の請求が現金払いへ変更されました"
            )

            owner_message = (
                f"{owner_name} 様\n\n"

                f"会員「{member_name}」様のクレジットカード決済が"
                f"一定期間にわたり完了しなかったため、"
                f"対象の請求書を現金払いへ変更しました。\n\n"

                f"■ 対象会員\n"
                f"{member_name}\n\n"

                f"■ 対象請求書\n"
                f"{invoice.number or invoice.id}\n\n"

                f"■ 請求金額\n"
                f"{amount_text}\n\n"

                f"■ 請求理由\n"
                f"{invoice.billing_reason or '---'}\n\n"

                f"■ 変更内容\n"
                f"クレジットカード決済 → 現金払い\n\n"

                f"今回の請求のみ現金払いへ変更されており、"
                f"会員様の通常のサブスクリプションは"
                f"引き続きクレジットカード決済（Stripe）のままです。\n\n"

                f"会員様から現金でのお支払いを受けた場合は、"
                f"管理画面から該当する請求書を「支払済み」として"
                f"処理してください。\n\n"

                f"ご確認のうえ、必要に応じて会員様へ"
                f"現金でのお支払いをご案内ください。\n\n"

                f"{club_name}"
            )

        if club_is_english(club):
            if is_subscription_level:
                owner_subject = f"[{club_name}] A member's billing moved to pay-at-the-club"
                owner_message = (
                    f"Hi {owner_name},\n\n"
                    f"The card for {member_name} did not go through for a while, "
                    f"so that bill and the whole subscription now collect at the club.\n\n"
                    f"Member\n{member_name}\n\n"
                    f"Invoice\n{invoice.number or invoice.id}\n\n"
                    f"Amount\n{amount_text}\n\n"
                    f"Reason\n{invoice.billing_reason or '---'}\n\n"
                    f"What changed\n"
                    f"Card → pay at the club\n"
                    f"The whole subscription now collects at the club\n\n"
                    f"When they pay you in person, mark that invoice paid in the admin.\n\n"
                    f"After every unpaid invoice is paid, they can log in and switch the subscription back to card (Stripe).\n"
                    f"They can't switch back while an unpaid invoice is still open.\n\n"
                    f"{club_name}"
                )
            else:
                owner_subject = f"[{club_name}] One member bill is now pay-at-the-club"
                owner_message = (
                    f"Hi {owner_name},\n\n"
                    f"The card for {member_name} did not go through for a while, "
                    f"so that invoice now collects at the club.\n\n"
                    f"Member\n{member_name}\n\n"
                    f"Invoice\n{invoice.number or invoice.id}\n\n"
                    f"Amount\n{amount_text}\n\n"
                    f"Reason\n{invoice.billing_reason or '---'}\n\n"
                    f"What changed\nCard → pay at the club\n\n"
                    f"Only this invoice changed. Their regular subscription still bills the card.\n\n"
                    f"When they pay you in person, mark that invoice paid in the admin.\n\n"
                    f"{club_name}"
                )
        send_mail(
            subject=owner_subject,
            message=owner_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[owner_email],
        )

        logger.info(
            "[EMAIL] Stripe→cash owner email sent "
            "invoice=%s billing_reason=%s recipient=%s "
            "subscription_level=%s",
            invoice.id,
            invoice.billing_reason,
            owner_email,
            is_subscription_level,
        )

@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=60,
    retry_kwargs={"max_retries": 5},
)
def send_stripe_payment_failure_warning_email(self, invoice_id):
    """
    Notify the member that a Stripe payment failed.

    This is the initial warning only.

    For subscription-level invoices:
        - initial_subscription
        - subscription_cycle

    the member is warned that if the payment is not resolved
    within 10 days, the invoice and the subscription billing
    method will be moved to cash.

    For other invoices:
        - only the affected invoice will be moved to cash.

    The email is claimed before sending so that the same warning
    cannot be sent twice by concurrent Celery workers.
    """

    from .models import Invoice

    # ---------------------------------------------------------
    # LOAD INVOICE
    # ---------------------------------------------------------

    invoice = (
        Invoice.objects
        .select_related(
            "subscription",
            "subscription__owner",
            "club",
        )
        .filter(id=invoice_id)
        .first()
    )

    if not invoice:
        logger.warning(
            "[EMAIL] Stripe payment failure warning invoice=%s "
            "does not exist.",
            invoice_id,
        )
        return

    # ---------------------------------------------------------
    # SAFETY CHECKS
    # ---------------------------------------------------------

    if not invoice.stripe_payment_failed_at:
        logger.warning(
            "[EMAIL] Stripe payment failure warning invoice=%s "
            "has no stripe_payment_failed_at. Skipping.",
            invoice.id,
        )
        return

    if invoice.stripe_payment_failure_email_sent:
        logger.info(
            "[EMAIL] Stripe payment failure warning already sent "
            "invoice=%s. Skipping.",
            invoice.id,
        )
        return

    subscription = invoice.subscription
    club = invoice.club

    if not subscription:
        logger.warning(
            "[EMAIL] Stripe payment failure warning invoice=%s "
            "has no subscription. Skipping.",
            invoice.id,
        )
        return

    billing_user = subscription.owner

    # ---------------------------------------------------------
    # FIND THE ACTUAL MEMBER
    # ---------------------------------------------------------

    member = (
        Member.objects
        .filter(
            club=club,
            user=billing_user,
            owner=billing_user,
        )
        .first()
    )

    if not member:
        logger.warning(
            "[EMAIL] Stripe payment failure warning invoice=%s "
            "could not find member for club=%s billing_user=%s.",
            invoice.id,
            club.id,
            billing_user.id if billing_user else None,
        )
        return

    # ---------------------------------------------------------
    # MEMBER EMAIL
    # ---------------------------------------------------------

    member_email = (
        member.owner.email
        if member.owner and member.owner.email
        else None
    )

    if not member_email:
        logger.warning(
            "[EMAIL] Stripe payment failure warning invoice=%s "
            "member=%s has no email. Skipping.",
            invoice.id,
            member.id,
        )
        return

    member_name = (
        member.full_name
        or (
            member.owner.get_full_name()
            if member.owner
            else None
        )
        or member_email
    )

    club_name = club.subdomain or ("the club" if club_is_english(club) else "クラブ")

    # ---------------------------------------------------------
    # TRANSITION TYPE
    # ---------------------------------------------------------

    is_subscription_level = invoice.billing_reason in [
        "initial_subscription",
        "subscription_cycle",
    ]

    # ---------------------------------------------------------
    # FAILURE DEADLINE
    #
    # The 10 days starts from the FIRST payment failure.
    # ---------------------------------------------------------

    fallback_at = (
        invoice.stripe_payment_failed_at
        + timedelta(days=10)
    )

    fallback_date_text = format_date(fallback_at)

    amount_text = f"¥{invoice.amount_due:,}"

    # =========================================================
    # SUBSCRIPTION-LEVEL WARNING
    # =========================================================

    if is_subscription_level:

        subject = (
            f"【{club_name}】クレジットカード決済についての重要なお知らせ"
        )

        message = (
            f"{member_name} 様\n\n"
            
            f"{club_name}のお支払いについて、"
            f"クレジットカードでの決済が正常に完了しませんでした。\n\n"
            
            f"今後10日間、お支払いの再試行が行われます。"
            f"クレジットカード情報をご確認いただき、"
            f"必要に応じてカード情報を更新してください。\n\n"
            
            f"■ 今回のお支払い\n"
            f"{amount_text}\n\n"
            
            f"■ お支払い方法\n"
            f"クレジットカード決済\n\n"
        
            f"最初の決済失敗から10日以内にお支払いが完了しない場合、"
            f"今回の請求は現金払いへ変更され、"
            f"今後の会員プランのお支払い方法も"
            f"現金払いへ変更されます。\n\n"
            
            f"今回の請求は現金払いへ変更された後、"
            f"{club_name}へ直接お支払いいただく必要があります。\n\n"
            
            f"■ 現金払いへの変更予定日\n"
            f"{fallback_date_text}\n\n"
            
            f"期限までにクレジットカードでのお支払いが完了した場合は、"
            f"現金払いへの変更は行われません。\n\n"
            
            f"お支払い方法やカード情報についてご不明な点がございましたら、"
            f"{club_name}までお問い合わせください。\n\n"
            
            f"{club_name}"
        )

    # =========================================================
    # INVOICE-ONLY WARNING
    # =========================================================

    else:

        subject = (
            f"【{club_name}】クレジットカード決済についてのお知らせ"
        )

        message = (
            f"{member_name} 様\n\n"

            f"{club_name}のお支払いについて、"
            f"今回のクレジットカード決済が正常に完了しませんでした。\n\n"

            f"今後10日間、お支払いの再試行が行われます。"
            f"クレジットカード情報をご確認いただき、"
            f"必要に応じてカード情報を更新してください。\n\n"
        
            f"■ 今回のお支払い\n"
            f"{amount_text}\n\n"
        
            f"■ お支払い方法\n"
            f"クレジットカード決済\n\n"
        
            f"最初の決済失敗から10日以内にお支払いが完了しない場合、"
            f"今回の請求はクレジットカード決済から"
            f"現金払いへ変更されます。\n\n"
        
            f"現金払いへ変更された場合は、"
            f"{club_name}へ直接お支払いいただく必要があります。\n\n"
        
            f"■ 現金払いへの変更予定日\n"
            f"{fallback_date_text}\n\n"
        
            f"期限までにクレジットカードでのお支払いが完了した場合は、"
            f"現金払いへの変更は行われません。\n\n"
        
            f"お支払い方法やカード情報についてご不明な点がございましたら、"
            f"{club_name}までお問い合わせください。\n\n"
        
            f"{club_name}"
        )

    # =========================================================
    # CLAIM EMAIL
    #
    # Claim immediately before sending.
    # =========================================================

    with transaction.atomic():

        invoice_for_claim = (
            Invoice.objects
            .select_for_update()
            .get(id=invoice.id)
        )

        if invoice_for_claim.stripe_payment_failure_email_sent:
            logger.info(
                "[EMAIL] Stripe payment failure warning already "
                "claimed invoice=%s. Skipping.",
                invoice.id,
            )
            return

        invoice_for_claim.stripe_payment_failure_email_sent = True

        invoice_for_claim.save(
            update_fields=[
                "stripe_payment_failure_email_sent",
            ]
        )

    # =========================================================
    # SEND
    # =========================================================

    if club_is_english(club):
        fallback_date_text = format_date(fallback_at, "en")
        if is_subscription_level:
            subject = f"[{club_name}] Your card payment needs attention"
            message = (
                f"Hi {member_name},\n\n"
                f"The card payment for {club_name} did not go through.\n\n"
                f"We'll retry for the next 10 days. Check the card, and update it if you need to.\n\n"
                f"This bill\n{amount_text}\n\n"
                f"How it's billed\nCard\n\n"
                f"If it still hasn't been paid 10 days after the first failure, "
                f"this bill and future membership bills become pay-at-the-club.\n\n"
                f"You would then pay {club_name} directly for this bill.\n\n"
                f"Switch to pay-at-the-club on\n{fallback_date_text}\n\n"
                f"If the card payment succeeds before then, nothing changes.\n\n"
                f"Questions about the card go to {club_name}.\n\n"
                f"{club_name}"
            )
        else:
            subject = f"[{club_name}] A card payment did not go through"
            message = (
                f"Hi {member_name},\n\n"
                f"The card payment for this {club_name} bill did not go through.\n\n"
                f"We'll retry for the next 10 days. Check the card, and update it if you need to.\n\n"
                f"This bill\n{amount_text}\n\n"
                f"How it's billed\nCard\n\n"
                f"If it still hasn't been paid 10 days after the first failure, "
                f"this bill becomes pay-at-the-club.\n\n"
                f"You would then pay {club_name} directly.\n\n"
                f"Switch to pay-at-the-club on\n{fallback_date_text}\n\n"
                f"If the card payment succeeds before then, nothing changes.\n\n"
                f"Questions about the card go to {club_name}.\n\n"
                f"{club_name}"
            )

    send_mail(
        subject=subject,
        message=message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[member_email],
    )

    logger.info(
        "[EMAIL] Stripe payment failure warning sent "
        "invoice=%s member=%s recipient=%s "
        "subscription_level=%s fallback_date=%s",
        invoice.id,
        member.id,
        member_email,
        is_subscription_level,
        fallback_date_text,
    )

@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=30,
    retry_kwargs={"max_retries": 5},
)
def send_visitor_reservation_confirmation_email(
    self,
    reservation_id,
):
    from .models import Reservation

    reservation = (
        Reservation.objects
        .select_related(
            "club",
            "club__owner",
            "lesson",
        )
        .filter(id=reservation_id)
        .first()
    )

    if not reservation:
        logger.warning(
            "[EMAIL] Visitor reservation=%s "
            "does not exist.",
            reservation_id,
        )
        return

    if reservation.status != Reservation.Status.PAID:
        logger.warning(
            "[EMAIL] Visitor reservation=%s "
            "is not paid. Skipping confirmation.",
            reservation.id,
        )
        return

    _queue_trial_staff_email(reservation)

    if not reservation.email:
        logger.warning(
            "[EMAIL] Visitor reservation=%s "
            "has no email.",
            reservation.id,
        )
        return

    # ---------------------------------------------------------
    # Claim the email before sending.
    #
    # This prevents duplicate emails if the webhook/task
    # is retried.
    # ---------------------------------------------------------

    with transaction.atomic():

        reservation_for_claim = (
            Reservation.objects
            .select_for_update()
            .get(id=reservation.id)
        )

        if reservation_for_claim.confirmation_email_sent:
            logger.info(
                "[EMAIL] Visitor reservation=%s "
                "confirmation already sent. Skipping.",
                reservation.id,
            )
            return

        reservation_for_claim.confirmation_email_sent = True

        reservation_for_claim.save(
            update_fields=[
                "confirmation_email_sent",
            ]
        )

    club = reservation.club
    lesson = reservation.lesson
    english = club.language == "en"

    club_name = (
        club.title
        or club.subdomain
        or ("the club" if english else "クラブ")
    )

    recipient_name = (
        reservation.full_name
        or reservation.email
    )

    weekday_names = (
        ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        if english
        else [
            "月曜日",
            "火曜日",
            "水曜日",
            "木曜日",
            "金曜日",
            "土曜日",
            "日曜日",
        ]
    )

    weekday = weekday_names[
        reservation.reservation_date.weekday()
    ]

    reservation_date = (
        reservation.reservation_date.strftime("%B %d, %Y" if english else "%Y年%m月%d日")
    )

    start_time = reservation.lesson.start_time.strftime(
        "%H:%M"
    )

    end_time = reservation.lesson.end_time.strftime(
        "%H:%M"
    )

    amount_text = (
        f"¥{reservation.amount:,}"
    )

    is_free = reservation.amount == 0
    is_trial = (
        reservation.reservation_type
        == Reservation.ReservationType.TRIAL
    )

    if english:
        if is_free and is_trial:
            subject = f"{club_name}: your intro class is booked"
            intro = (
                f"Thanks for booking an intro class at {club_name}.\n"
                f"You're booked in.\n\n"
            )
            payment_section = "Fee\nFree\n\n"
        elif is_free:
            subject = f"{club_name}: your booking is confirmed"
            intro = (
                f"Thanks for booking at {club_name}.\n"
                f"Your booking is confirmed.\n\n"
            )
            payment_section = "Fee\nFree\n\n"
        elif reservation.payment_method == "ticket":
            subject = f"{club_name}: your booking is confirmed"
            intro = (
                f"Thanks for booking at {club_name}.\n"
                f"Your booking is confirmed, and one class credit was used.\n\n"
            )
            payment_section = "Payment\n1 class credit\n\n"
        else:
            subject = f"{club_name}: booking and payment confirmed"
            intro = (
                f"Thanks for booking at {club_name}.\n"
                f"Your payment went through and the booking is confirmed.\n\n"
            )
            payment_section = (
                f"Payment\n"
                f"Amount: {amount_text}\n"
                f"Card\n\n"
            )
        message = (
            f"Hi {recipient_name},\n\n"
            f"{intro}"
            f"Booking\n"
            f"Class: {lesson.title}\n"
            f"Date: {reservation_date} ({weekday})\n"
            f"Time: {start_time}–{end_time}\n\n"
            f"{payment_section}"
            f"Booking number\n"
            f"{reservation.id}\n\n"
            f"Cancel:\n"
            f"{_cancel_link(club, 'lesson', reservation.id)}\n"
            f"{_cancel_notice(reservation, language='en')}\n\n"
            f"See you there.\n"
            f"If you have a question about this booking, contact {club_name}.\n\n"
            f"{club_name}"
        )
    else:
        if is_free and is_trial:
            subject = f"【{club_name}】体験予約確定のお知らせ"
            intro = (
                f"{club_name}の体験予約ありがとうございます。\n"
                f"体験予約が確定しました。\n\n"
            )
            payment_section = (
                "■ 料金\n"
                "無料\n\n"
            )
        elif is_free:
            subject = f"【{club_name}】ご予約確定のお知らせ"
            intro = (
                f"{club_name}へのご予約ありがとうございます。\n"
                f"ご予約が確定しました。\n\n"
            )
            payment_section = (
                "■ 料金\n"
                "無料\n\n"
            )
        elif reservation.payment_method == "ticket":
            subject = f"【{club_name}】ご予約確定のお知らせ"
            intro = (
                f"{club_name}へのご予約ありがとうございます。\n"
                f"チケットを使用して、ご予約が確定しました。\n\n"
            )
            payment_section = (
                "■ お支払い\n"
                "チケットを1枚使用\n\n"
            )
        else:
            subject = (
                f"【{club_name}】ご予約・お支払い完了のお知らせ"
            )
            intro = (
                f"{club_name}へのご予約ありがとうございます。\n"
                f"お支払いが完了し、ご予約が確定しました。\n\n"
            )
            payment_section = (
                f"■ お支払い\n"
                f"金額：{amount_text}\n"
                f"お支払い方法：クレジットカード\n\n"
            )

        message = (
            f"{recipient_name} 様\n\n"

            f"{intro}"

            f"■ ご予約内容\n"
            f"レッスン：{lesson.title}\n"
            f"日時：{reservation_date}（{weekday}）\n"
            f"時間：{start_time}〜{end_time}\n\n"

            f"{payment_section}"

            f"■ ご予約番号\n"
            f"{reservation.id}\n\n"

            f"予約のキャンセル：\n"
            f"{_cancel_link(club, 'lesson', reservation.id)}\n"
            f"{_cancel_notice(reservation)}\n\n"

            f"当日はお気をつけてお越しください。\n"
            f"ご予約内容についてご不明な点がございましたら、"
            f"{club_name}までお問い合わせください。\n\n"

            f"{club_name}"
        )

    headers = {}

    if (
        club.owner
        and club.owner.email
        and club.owner.email != reservation.email
    ):
        headers["Reply-To"] = club.owner.email

    send_mail(
        subject=subject,
        message=message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[reservation.email],
    )

    logger.info(
        "[EMAIL] Visitor reservation confirmation sent "
        "reservation=%s recipient=%s club=%s",
        reservation.id,
        reservation.email,
        club_name,
    )


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=30,
    retry_kwargs={"max_retries": 5},
)
def send_reservation_start_email(self, start_token):
    from .models import Reservation
    from .rules_reservations import START_LINK_HOURS

    reservations = list(
        Reservation.objects
        .select_related(
            "club",
            "club__owner",
            "lesson",
        )
        .filter(start_token=start_token)
        .exclude(status=Reservation.Status.PAID)
        .order_by("id")
    )

    if not reservations:
        logger.warning(
            "[EMAIL] No pending reservations for start token."
        )
        return

    reservation = reservations[0]
    club = reservation.club
    lesson = reservation.lesson

    club_name = club.title or club.subdomain or ("the club" if club_is_english(club) else "クラブ")
    kind = (
        "体験予約"
        if reservation.reservation_type == Reservation.ReservationType.TRIAL
        else "ビジター予約"
    )

    weekday_names = [
        "月曜日",
        "火曜日",
        "水曜日",
        "木曜日",
        "金曜日",
        "土曜日",
        "日曜日",
    ]
    weekday = weekday_names[reservation.reservation_date.weekday()]
    reservation_date = reservation.reservation_date.strftime("%Y年%m月%d日")
    start_time = lesson.start_time.strftime("%H:%M")
    end_time = lesson.end_time.strftime("%H:%M")

    people = []
    for person in reservations:
        if person.age is None:
            people.append(f"・{person.full_name}")
        else:
            people.append(f"・{person.full_name}（{person.age}歳）")

    people_text = "\n".join(people)

    total = sum(person.amount for person in reservations)
    if total == 0:
        price_text = "無料"
    elif len(reservations) == 1:
        price_text = f"¥{total:,}"
    else:
        price_text = f"¥{total:,}（{len(reservations)}名）"

    greeting = (
        reservation.full_name
        if len(reservations) == 1
        else "お客様"
    )
    link = (
        f"https://{club.subdomain}.kaibaru.jp/"
        f"start_reservation/{start_token}/"
    )

    if club.language == "en":
        weekday_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        weekday = weekday_names[reservation.reservation_date.weekday()]
        reservation_date = reservation.reservation_date.strftime("%B %d, %Y")
        people = []
        for person in reservations:
            if person.age is None:
                people.append(f"- {person.full_name}")
            else:
                people.append(f"- {person.full_name} (age {person.age})")
        people_text = "\n".join(people)
        if total == 0:
            price_text = "Free"
        elif len(reservations) == 1:
            price_text = f"¥{total:,}"
        else:
            price_text = f"¥{total:,} ({len(reservations)} people)"
        greeting = reservation.full_name if len(reservations) == 1 else "there"
        kind_en = (
            "intro class"
            if reservation.reservation_type == Reservation.ReservationType.TRIAL
            else "visit"
        )
        subject = f"{club_name}: finish your {kind_en} booking"
        message = (
            f"Hi {greeting},\n\n"
            f"Thanks for requesting a {kind_en} at {club_name}.\n"
            f"It is not booked yet.\n"
            f"Open the link below to finish.\n\n"
            f"Request\n"
            f"Class: {lesson.title}\n"
            f"Date: {reservation_date} ({weekday})\n"
            f"Time: {start_time}–{end_time}\n"
            f"Who is coming:\n"
            f"{people_text}\n\n"
            f"Fee: {price_text}\n\n"
            f"Continue:\n"
            f"{link}\n\n"
            f"This link lasts {START_LINK_HOURS} hours.\n"
            f"The spot is not held until you open the link and start.\n\n"
            f"{club_name}"
        )
    else:
        subject = f"【{club_name}】{kind}手続きのご案内"
        message = (
            f"{greeting} 様\n\n"
            f"{club_name}の{kind}をお申し込みいただきありがとうございます。\n"
            f"まだ予約は確定していません。\n"
            f"以下のリンクを開き、手続きを完了してください。\n\n"
            f"■ お申し込み内容\n"
            f"レッスン：{lesson.title}\n"
            f"日時：{reservation_date}（{weekday}）\n"
            f"時間：{start_time}〜{end_time}\n"
            f"予約者：\n"
            f"{people_text}\n\n"
            f"料金：{price_text}\n\n"
            f"手続きを続ける：\n"
            f"{link}\n\n"
            f"このリンクの有効期限は{START_LINK_HOURS}時間です。\n"
            f"リンクを開いて手続きを始めるまで、レッスンの枠は確保されません。\n\n"
            f"{club_name}"
        )

    reply_to = None
    if (
        club.owner
        and club.owner.email
        and club.owner.email != reservation.email
    ):
        reply_to = [club.owner.email]

    email = EmailMessage(
        subject=subject,
        body=message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[reservation.email],
        reply_to=reply_to,
    )
    email.send()

    logger.info(
        "[EMAIL] Reservation start email sent "
        "reservations=%s recipient=%s club=%s",
        [person.id for person in reservations],
        reservation.email,
        club_name,
    )


def _queue_trial_staff_email(reservation):
    from .models import Reservation

    if reservation.reservation_type != Reservation.ReservationType.TRIAL:
        return

    if reservation.trial_staff_email_sent:
        return

    send_trial_staff_email.delay(reservation.id)


def _member_account_email(member):
    if member is None:
        return None

    user = getattr(member, "user", None)
    if user is not None and user.email:
        return user.email.strip()

    owner = getattr(member, "owner", None)
    if owner is not None and owner.email:
        return owner.email.strip()

    return None


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=30,
    retry_kwargs={"max_retries": 5},
)
def send_trial_staff_email(self, reservation_id):
    from .models import Reservation

    seed = (
        Reservation.objects
        .select_related(
            "club",
            "club__owner",
            "lesson",
            "lesson__instructor",
            "lesson__instructor__user",
            "lesson__instructor__owner",
        )
        .filter(id=reservation_id)
        .first()
    )

    if not seed:
        return

    if (
        seed.reservation_type != Reservation.ReservationType.TRIAL
        or seed.status != Reservation.Status.PAID
        or seed.trial_staff_email_sent
    ):
        return

    if seed.start_token:
        group = list(
            Reservation.objects
            .select_related(
                "club",
                "club__owner",
                "lesson",
                "lesson__instructor",
                "lesson__instructor__user",
                "lesson__instructor__owner",
            )
            .filter(
                start_token=seed.start_token,
                lesson_id=seed.lesson_id,
                reservation_date=seed.reservation_date,
                reservation_type=Reservation.ReservationType.TRIAL,
                status=Reservation.Status.PAID,
                trial_staff_email_sent=False,
            )
            .order_by("id")
        )
    else:
        group = [seed]

    if not group:
        return

    with transaction.atomic():
        claimed_ids = list(
            Reservation.objects
            .select_for_update()
            .filter(
                id__in=[reservation.id for reservation in group],
                status=Reservation.Status.PAID,
                trial_staff_email_sent=False,
            )
            .values_list("id", flat=True)
        )

        if not claimed_ids:
            return

        Reservation.objects.filter(id__in=claimed_ids).update(
            trial_staff_email_sent=True
        )

    group = [
        reservation
        for reservation in group
        if reservation.id in claimed_ids
    ]
    reservation = group[0]
    club = reservation.club
    lesson = reservation.lesson
    club_name = club.title or club.subdomain or ("the club" if club_is_english(club) else "クラブ")

    recipients = []
    owner_email = (
        club.owner.email.strip()
        if club.owner and club.owner.email
        else ""
    )
    if owner_email:
        recipients.append(owner_email)

    instructor_email = _member_account_email(lesson.instructor)
    if instructor_email:
        recipients.append(instructor_email)

    unique_recipients = []
    seen = set()
    for email in recipients:
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        unique_recipients.append(email)

    if not unique_recipients:
        logger.warning(
            "[EMAIL] Trial staff notice has no recipients "
            "reservations=%s",
            claimed_ids,
        )
        return

    weekday_names = [
        "月曜日",
        "火曜日",
        "水曜日",
        "木曜日",
        "金曜日",
        "土曜日",
        "日曜日",
    ]
    weekday = weekday_names[reservation.reservation_date.weekday()]
    date_text = reservation.reservation_date.strftime("%Y年%m月%d日")
    start_time = lesson.start_time.strftime("%H:%M")
    end_time = lesson.end_time.strftime("%H:%M")
    gender_labels = {
        "male": "男性",
        "female": "女性",
    }

    people = []
    for person in group:
        age_text = (
            f"{person.age}歳"
            if person.age is not None
            else "未入力"
        )
        gender_text = gender_labels.get(person.gender, "未入力")
        phone_text = person.phone_number or "未入力"
        amount_text = (
            "無料" if person.amount == 0 else f"¥{person.amount:,}"
        )
        people.append(
            f"・{person.full_name}\n"
            f"  年齢：{age_text}\n"
            f"  性別：{gender_text}\n"
            f"  電話番号：{phone_text}\n"
            f"  メール：{person.email}\n"
            f"  料金：{amount_text}\n"
            f"  予約番号：{person.id}"
        )

    people_text = "\n".join(people)
    if club_is_english(club):
        weekday = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][
            reservation.reservation_date.weekday()
        ]
        day = reservation.reservation_date
        date_text = f"{day.strftime('%B')} {day.day}, {day.year}"
        people = []
        for person in group:
            age_text = f"age {person.age}" if person.age is not None else "not given"
            gender_text = {"male": "male", "female": "female"}.get(person.gender, "not given")
            phone_text = person.phone_number or "not given"
            amount_text = "Free" if person.amount == 0 else f"¥{person.amount:,}"
            people.append(
                f"· {person.full_name}\n"
                f"  Age: {age_text}\n"
                f"  Gender: {gender_text}\n"
                f"  Phone: {phone_text}\n"
                f"  Email: {person.email}\n"
                f"  Price: {amount_text}\n"
                f"  Booking number: {person.id}"
            )
        people_text = "\n".join(people)
        subject = f"[{club_name}] An intro class is booked"
        message = (
            f"An intro class at {club_name} is confirmed.\n\n"
            f"Class\n"
            f"{lesson.title}\n"
            f"When: {date_text} ({weekday})\n"
            f"Time: {start_time}–{end_time}\n\n"
            f"Who's coming\n"
            f"{people_text}\n\n"
            f"{club_name}"
        )
    else:
        subject = f"【{club_name}】体験予約が確定しました"
        message = (
        f"{club_name}の体験予約が確定しました。\n\n"
        f"■ レッスン\n"
        f"{lesson.title}\n"
        f"日時：{date_text}（{weekday}）\n"
        f"時間：{start_time}〜{end_time}\n\n"
        f"■ 予約者\n"
        f"{people_text}\n\n"
        f"{club_name}"
    )

    EmailMessage(
        subject=subject,
        body=message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=unique_recipients,
    ).send()

    logger.info(
        "[EMAIL] Trial staff notice sent reservations=%s recipients=%s",
        [person.id for person in group],
        unique_recipients,
    )


def _cancel_link(club, kind, reservation_id):
    from .reservation_cancel import cancel_url

    return cancel_url(club, kind, reservation_id)


def _restore_link(club, kind, reservation_id):
    from .reservation_cancel import restore_url

    return restore_url(club, kind, reservation_id)


def _cancel_notice(reservation, language="ja"):
    if language == "en":
        if getattr(reservation, "payment_method", "") == "ticket":
            return "Cancelling here does not put the class credit back."
        if reservation.amount > 0:
            return "Cancelling a paid booking here does not refund it."
        return "You can cancel a free booking from here too."
    if getattr(reservation, "payment_method", "") == "ticket":
        return (
            "こちらからキャンセルしても、使用したチケットは戻りません。"
        )
    if reservation.amount > 0:
        return (
            "料金が発生している予約をこちらからキャンセルしても、"
            "返金は行われません。"
        )
    return "無料の予約も、こちらからキャンセルできます。"


def _event_reply_to(club, recipient):
    if (
        club.owner
        and club.owner.email
        and club.owner.email != recipient
    ):
        return [club.owner.email]
    return None


def _send_event_email(*, club, recipient, subject, message):
    email = EmailMessage(
        subject=subject,
        body=message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[recipient],
        reply_to=_event_reply_to(club, recipient),
    )
    email.send()


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=30,
    retry_kwargs={"max_retries": 5},
)
def send_event_reservation_start_email(self, start_token):
    from .models import EventReservation
    from .service_event import START_LINK_HOURS, format_event_when

    reservation = (
        EventReservation.objects
        .select_related("club", "club__owner", "event")
        .filter(start_token=start_token)
        .exclude(status=EventReservation.Status.PAID)
        .first()
    )

    if not reservation:
        logger.warning("[EMAIL] No pending event reservation for start token.")
        return

    club = reservation.club
    event = reservation.event
    club_name = club.title or club.subdomain or ("the club" if club_is_english(club) else "クラブ")
    price_text = "無料" if reservation.amount == 0 else f"¥{reservation.amount:,}"
    if reservation.price_name:
        price_text = f"{reservation.price_name} {price_text}"
    link = (
        f"https://{club.subdomain}.kaibaru.jp/"
        f"start_event_reservation/{start_token}/"
    )
    subject = f"【{club_name}】イベント予約手続きのご案内"
    message = (
        f"{reservation.full_name} 様\n\n"
        f"{club_name}のイベントにお申し込みいただきありがとうございます。\n"
        f"まだ予約は確定していません。\n"
        f"以下のリンクを開き、手続きを完了してください。\n\n"
        f"■ お申し込み内容\n"
        f"イベント：{event.title}\n"
        f"日時：{format_event_when(event)}\n"
        f"料金：{price_text}\n\n"
        f"手続きを続ける：\n"
        f"{link}\n\n"
        f"このリンクの有効期限は{START_LINK_HOURS}時間です。\n"
        f"リンクを開いて手続きを始めるまで、イベントの枠は確保されません。\n\n"
        f"{club_name}"
    )
    if club_is_english(club):
        from .service_event import format_event_when as _format_event_when

        price_text = "Free" if reservation.amount == 0 else f"¥{reservation.amount:,}"
        if reservation.price_name:
            price_text = f"{reservation.price_name} {price_text}"
        subject = f"[{club_name}] Finish your event booking"
        message = (
            f"Hi {reservation.full_name},\n\n"
            f"Thanks for signing up for an event at {club_name}.\n"
            f"It isn't confirmed yet.\n"
            f"Open this link and finish the booking.\n\n"
            f"What you signed up for\n"
            f"Event: {event.title}\n"
            f"When: {_format_event_when(event, 'en')}\n"
            f"Price: {price_text}\n\n"
            f"Continue:\n"
            f"{link}\n\n"
            f"This link lasts {START_LINK_HOURS} hours.\n"
            f"Your spot is not held until you open the link and start.\n\n"
            f"{club_name}"
        )
    _send_event_email(
        club=club,
        recipient=reservation.email,
        subject=subject,
        message=message,
    )
    logger.info(
        "[EMAIL] Event start email sent reservation=%s recipient=%s",
        reservation.id,
        reservation.email,
    )


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=30,
    retry_kwargs={"max_retries": 5},
)
def send_event_reservation_confirmation_email(self, reservation_id):
    from .models import EventReservation
    from .service_event import format_event_when

    reservation = (
        EventReservation.objects
        .select_related("club", "club__owner", "event")
        .filter(id=reservation_id)
        .first()
    )

    if not reservation or reservation.status != EventReservation.Status.PAID:
        return

    if not reservation.email:
        return

    with transaction.atomic():
        locked = (
            EventReservation.objects
            .select_for_update()
            .get(id=reservation.id)
        )
        if locked.confirmation_email_sent:
            return
        locked.confirmation_email_sent = True
        locked.save(update_fields=["confirmation_email_sent"])

    club = reservation.club
    event = reservation.event
    club_name = club.title or club.subdomain or ("the club" if club_is_english(club) else "クラブ")
    is_free = reservation.amount == 0
    if is_free:
        subject = f"【{club_name}】イベント予約確定のお知らせ"
        intro = (
            f"{club_name}のイベント予約ありがとうございます。\n"
            f"ご予約が確定しました。\n\n"
        )
        payment_section = (
            f"■ 料金\n"
            f"{reservation.price_name + ' ' if reservation.price_name else ''}無料\n\n"
        )
    else:
        subject = f"【{club_name}】イベント予約・お支払い完了のお知らせ"
        intro = (
            f"{club_name}のイベント予約ありがとうございます。\n"
            f"お支払いが完了し、ご予約が確定しました。\n\n"
        )
        payment_section = (
            f"■ お支払い\n"
            f"料金：{reservation.price_name + ' ' if reservation.price_name else ''}¥{reservation.amount:,}\n"
            f"お支払い方法：クレジットカード\n\n"
        )

    terms = (event.terms or "").strip()
    terms_section = ""
    if terms:
        terms_section = (
            f"\n\n"
            f"■ 同意いただいた参加条件\n"
            f"{terms}"
        )

    message = (
        f"{reservation.full_name} 様\n\n"
        f"{intro}"
        f"■ ご予約内容\n"
        f"イベント：{event.title}\n"
        f"日時：{format_event_when(event)}\n\n"
        f"{payment_section}"
        f"■ ご予約番号\n"
        f"{reservation.id}\n\n"
        f"予約のキャンセル：\n"
        f"{_cancel_link(club, 'event', reservation.id)}\n"
        f"{_cancel_notice(reservation)}\n\n"
        f"当日はお気をつけてお越しください。\n"
        f"{club_name}"
        f"{terms_section}"
    )
    if club_is_english(club):
        from .service_event import format_event_when as _format_event_when

        if is_free:
            subject = f"[{club_name}] Your event booking is confirmed"
            intro = (
                f"Thanks for booking an event at {club_name}.\n"
                f"You're confirmed.\n\n"
            )
            payment_section = (
                f"Price\n"
                f"{(reservation.price_name + ' ') if reservation.price_name else ''}Free\n\n"
            )
        else:
            subject = f"[{club_name}] Event booking paid"
            intro = (
                f"Thanks for booking an event at {club_name}.\n"
                f"Payment is complete and you're confirmed.\n\n"
            )
            payment_section = (
                f"Payment\n"
                f"Price: {(reservation.price_name + ' ') if reservation.price_name else ''}¥{reservation.amount:,}\n"
                f"Paid by card\n\n"
            )
        terms_section = ""
        if terms:
            terms_section = f"\n\nTerms you agreed to\n{terms}"
        message = (
            f"Hi {reservation.full_name},\n\n"
            f"{intro}"
            f"Your booking\n"
            f"Event: {event.title}\n"
            f"When: {_format_event_when(event, 'en')}\n\n"
            f"{payment_section}"
            f"Booking number\n"
            f"{reservation.id}\n\n"
            f"Cancel:\n"
            f"{_cancel_link(club, 'event', reservation.id)}\n"
            f"{_cancel_notice(reservation, 'en')}\n\n"
            f"See you there.\n"
            f"{club_name}"
            f"{terms_section}"
        )
    _send_event_email(
        club=club,
        recipient=reservation.email,
        subject=subject,
        message=message,
    )
    logger.info(
        "[EMAIL] Event confirmation sent reservation=%s recipient=%s",
        reservation.id,
        reservation.email,
    )


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=30,
    retry_kwargs={"max_retries": 5},
)
def send_reservation_canceled_email(self, kind, reservation_id):
    from .models import EventReservation, Reservation, ServiceReservation
    from .service_event import format_event_when

    if kind == "lesson":
        reservation = (
            Reservation.objects
            .select_related("club", "club__owner", "lesson")
            .filter(id=reservation_id, canceled=True)
            .first()
        )
    elif kind == "service":
        reservation = (
            ServiceReservation.objects
            .select_related("club", "club__owner", "course")
            .filter(id=reservation_id, canceled=True)
            .first()
        )
    else:
        reservation = (
            EventReservation.objects
            .select_related("club", "club__owner", "event")
            .filter(id=reservation_id, canceled=True)
            .first()
        )

    if not reservation or not reservation.email:
        return

    club = reservation.club
    club_name = club.title or club.subdomain or ("the club" if club_is_english(club) else "クラブ")
    if kind == "lesson":
        title = reservation.lesson.title
        when_text = reservation.reservation_date.strftime("%Y年%m月%d日")
        label = "レッスン"
    elif kind == "service":
        from .service_service_reservation import WEEKDAY_NAMES

        course = reservation.course
        title = (
            course.name if course is not None else (reservation.price_name or "コース")
        )
        weekday = WEEKDAY_NAMES[reservation.reservation_date.weekday()]
        date_text = reservation.reservation_date.strftime("%Y年%m月%d日")
        start_text = reservation.start_time.strftime("%H:%M")
        end_text = reservation.end_time.strftime("%H:%M")
        when_text = f"{date_text}（{weekday}） {start_text}〜{end_text}"
        label = "コース"
    else:
        title = reservation.event.title
        when_text = format_event_when(reservation.event)
        label = "イベント"

    if reservation.payment_method == "ticket":
        kept = "使用したチケットは戻りません。"
    elif reservation.amount > 0:
        kept = "お支払いに対する返金は行われません。"
    else:
        kept = "この予約に料金は発生していません。"

    link = _restore_link(club, kind, reservation.id)
    subject = f"【{club_name}】予約をキャンセルしました"
    message = (
        f"{reservation.full_name} 様\n\n"
        f"{club_name}の予約をキャンセルしました。\n"
        f"{kept}\n\n"
        f"■ キャンセルした予約\n"
        f"{label}：{title}\n"
        f"日時：{when_text}\n\n"
        f"まだ日時が過ぎておらず、定員に空きがある場合は、"
        f"次のリンクからキャンセルを取り消せます。\n"
        f"{link}\n\n"
        f"{club_name}"
    )
    if club_is_english(club):
        if reservation.payment_method == "ticket":
            kept = "The class credit is not returned."
        elif reservation.amount > 0:
            kept = "This is not refunded."
        else:
            kept = "This booking had no charge."
        if kind == "lesson":
            day = reservation.reservation_date
            when_text = f"{day.strftime('%B')} {day.day}, {day.year}"
            label = "Class"
        elif kind == "service":
            weekday = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][
                reservation.reservation_date.weekday()
            ]
            day = reservation.reservation_date
            when_text = (
                f"{day.strftime('%B')} {day.day}, {day.year} ({weekday}) "
                f"{reservation.start_time.strftime('%H:%M')}–{reservation.end_time.strftime('%H:%M')}"
            )
            label = "Service"
        else:
            when_text = format_event_when(reservation.event, "en")
            label = "Event"
        subject = f"[{club_name}] Booking cancelled"
        message = (
            f"Hi {reservation.full_name},\n\n"
            f"Your booking at {club_name} is cancelled.\n"
            f"{kept}\n\n"
            f"What was cancelled\n"
            f"{label}: {title}\n"
            f"When: {when_text}\n\n"
            f"If the time hasn't passed and there's still room, you can undo it here:\n"
            f"{link}\n\n"
            f"{club_name}"
        )
    _send_event_email(
        club=club,
        recipient=reservation.email,
        subject=subject,
        message=message,
    )
    logger.info(
        "[EMAIL] Cancellation email sent kind=%s reservation=%s",
        kind,
        reservation.id,
    )


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=30,
    retry_kwargs={"max_retries": 5},
)
def send_inquiry_email(self, club_id, name, furigana, phone_number, email, header, message):
    club = (
        Club.objects
        .select_related("owner")
        .filter(id=club_id, is_deleted=False)
        .first()
    )
    if not club or not club.owner_id or not club.owner.email:
        logger.warning("[EMAIL] Inquiry for club %s has no owner email.", club_id)
        return

    club_name = club.title or club.subdomain or ("the club" if club_is_english(club) else "クラブ")
    if club_is_english(club):
        subject = f"[{club_name}] Inquiry: {header}"
        body = (
            f"Someone wrote in from the {club_name} site.\n\n"
            f"Name: {name}\n"
            f"Pronunciation: {furigana}\n"
            f"Phone: {phone_number}\n"
            f"Email: {email}\n"
            f"Subject: {header}\n\n"
            f"Message:\n{message}\n"
        )
    else:
        subject = f"【{club_name}】お問い合わせ：{header}"
        body = (
            f"{club_name}のサイトからお問い合わせがありました。\n\n"
            f"お名前：{name}\n"
            f"ふりがな：{furigana}\n"
            f"電話番号：{phone_number}\n"
            f"メールアドレス：{email}\n"
            f"件名：{header}\n\n"
            f"問い合わせ内容：\n{message}\n"
        )
    mail = EmailMessage(
        subject=subject,
        body=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[club.owner.email],
        reply_to=[email],
    )
    mail.send()
    logger.info("[EMAIL] Inquiry sent club=%s", club_id)


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=30,
    retry_kwargs={"max_retries": 5},
)
def send_service_reservation_start_email(self, start_token):
    from .models import ServiceReservation
    from .service_service_reservation import START_LINK_HOURS, WEEKDAY_NAMES

    reservation = (
        ServiceReservation.objects
        .select_related("club", "club__owner", "course")
        .filter(start_token=start_token)
        .exclude(status=ServiceReservation.Status.PAID)
        .first()
    )
    if not reservation:
        logger.warning("[EMAIL] No pending service reservation for start token.")
        return

    club = reservation.club
    course = reservation.course
    club_name = club.title or club.subdomain or ("the studio" if club_is_english(club) else "サロン")
    course_name = course.name if course else (reservation.price_name or "コース")
    weekday = WEEKDAY_NAMES[reservation.reservation_date.weekday()]
    date_text = reservation.reservation_date.strftime("%Y年%m月%d日")
    start_text = reservation.start_time.strftime("%H:%M")
    end_text = reservation.end_time.strftime("%H:%M")
    price_text = "無料" if reservation.amount == 0 else f"¥{reservation.amount:,}"
    if reservation.payment_method == "cash" and reservation.amount > 0:
        price_text = f"{price_text}（現地払い）"
    link = (
        f"https://{club.subdomain}.kaibaru.jp/"
        f"start_service_reservation/{start_token}/"
    )
    subject = f"【{club_name}】ご予約手続きのご案内"
    message = (
        f"{reservation.full_name} 様\n\n"
        f"{club_name}へのご予約をお申し込みいただきありがとうございます。\n"
        f"まだ予約は確定していません。\n"
        f"以下のリンクを開き、手続きを完了してください。\n\n"
        f"■ お申し込み内容\n"
        f"コース：{course_name}\n"
        f"日時：{date_text}（{weekday}）\n"
        f"時間：{start_text}〜{end_text}\n"
        f"料金：{price_text}\n\n"
        f"手続きを続ける：\n"
        f"{link}\n\n"
        f"このリンクの有効期限は{START_LINK_HOURS}時間です。\n"
        f"リンクを開いて手続きを始めるまで、枠は確保されません。\n\n"
        f"{club_name}"
    )
    if club_is_english(club):
        weekday = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][
            reservation.reservation_date.weekday()
        ]
        day = reservation.reservation_date
        date_text = f"{day.strftime('%B')} {day.day}, {day.year}"
        price_text = "Free" if reservation.amount == 0 else f"¥{reservation.amount:,}"
        if reservation.payment_method == "cash" and reservation.amount > 0:
            price_text = f"{price_text} (pay at the studio)"
        subject = f"[{club_name}] Finish your booking"
        message = (
            f"Hi {reservation.full_name},\n\n"
            f"Thanks for requesting a booking at {club_name}.\n"
            f"It isn't confirmed yet.\n"
            f"Open this link and finish it.\n\n"
            f"What you requested\n"
            f"Service: {course_name}\n"
            f"When: {date_text} ({weekday})\n"
            f"Time: {start_text}–{end_text}\n"
            f"Price: {price_text}\n\n"
            f"Continue:\n"
            f"{link}\n\n"
            f"This link lasts {START_LINK_HOURS} hours.\n"
            f"Your spot is not held until you open the link and start.\n\n"
            f"{club_name}"
        )
    _send_event_email(
        club=club,
        recipient=reservation.email,
        subject=subject,
        message=message,
    )
    logger.info(
        "[EMAIL] Service start email sent reservation=%s recipient=%s",
        reservation.id,
        reservation.email,
    )


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=30,
    retry_kwargs={"max_retries": 5},
)
def send_service_reservation_confirmation_email(self, reservation_id):
    from .models import ServiceReservation
    from .service_service_reservation import WEEKDAY_NAMES

    reservation = (
        ServiceReservation.objects
        .select_related("club", "club__owner", "course", "worker")
        .filter(id=reservation_id)
        .first()
    )
    if not reservation or reservation.status != ServiceReservation.Status.PAID:
        return
    if not reservation.email:
        return

    with transaction.atomic():
        locked = (
            ServiceReservation.objects
            .select_for_update()
            .get(id=reservation.id)
        )
        if locked.confirmation_email_sent:
            return
        locked.confirmation_email_sent = True
        locked.save(update_fields=["confirmation_email_sent"])

    club = reservation.club
    course = reservation.course
    club_name = club.title or club.subdomain or ("the studio" if club_is_english(club) else "サロン")
    course_name = course.name if course else (reservation.price_name or "コース")
    weekday = WEEKDAY_NAMES[reservation.reservation_date.weekday()]
    date_text = reservation.reservation_date.strftime("%Y年%m月%d日")
    start_text = reservation.start_time.strftime("%H:%M")
    end_text = reservation.end_time.strftime("%H:%M")
    worker_name = (
        reservation.worker.full_name if reservation.worker_id else "スタッフ"
    )
    is_free = reservation.amount == 0
    is_cash = reservation.payment_method == "cash" and not is_free

    if is_free:
        subject = f"【{club_name}】ご予約確定のお知らせ"
        intro = (
            f"{club_name}へのご予約ありがとうございます。\n"
            f"ご予約が確定しました。\n\n"
        )
        payment_section = "■ 料金\n無料\n\n"
    elif is_cash:
        subject = f"【{club_name}】ご予約確定のお知らせ"
        intro = (
            f"{club_name}へのご予約ありがとうございます。\n"
            f"ご予約が確定しました。料金は当日、店舗でお支払いください。\n\n"
        )
        payment_section = (
            f"■ お支払い\n"
            f"料金：¥{reservation.amount:,}\n"
            f"お支払い方法：現地払い\n\n"
        )
    else:
        subject = f"【{club_name}】ご予約・お支払い完了のお知らせ"
        intro = (
            f"{club_name}へのご予約ありがとうございます。\n"
            f"お支払いが完了し、ご予約が確定しました。\n\n"
        )
        payment_section = (
            f"■ お支払い\n"
            f"料金：¥{reservation.amount:,}\n"
            f"お支払い方法：クレジットカード\n\n"
        )

    message = (
        f"{reservation.full_name} 様\n\n"
        f"{intro}"
        f"■ ご予約内容\n"
        f"コース：{course_name}\n"
        f"日時：{date_text}（{weekday}）\n"
        f"時間：{start_text}〜{end_text}\n"
        f"担当：{worker_name}\n\n"
        f"{payment_section}"
        f"■ ご予約番号\n"
        f"{reservation.id}\n\n"
        f"予約をキャンセルする場合は、次のリンクを開いてください。\n"
        f"{_cancel_link(club, 'service', reservation.id)}\n\n"
        f"当日はお気をつけてお越しください。\n"
        f"{club_name}"
    )
    if club_is_english(club):
        weekday = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][
            reservation.reservation_date.weekday()
        ]
        day = reservation.reservation_date
        date_text = f"{day.strftime('%B')} {day.day}, {day.year}"
        worker_name = reservation.worker.full_name if reservation.worker_id else "a staff member"
        if is_free:
            subject = f"[{club_name}] Your booking is confirmed"
            intro = (
                f"Thanks for booking at {club_name}.\n"
                f"You're confirmed.\n\n"
            )
            payment_section = "Price\nFree\n\n"
        elif is_cash:
            subject = f"[{club_name}] Your booking is confirmed"
            intro = (
                f"Thanks for booking at {club_name}.\n"
                f"You're confirmed. Pay at the studio on the day.\n\n"
            )
            payment_section = (
                f"Payment\n"
                f"Price: ¥{reservation.amount:,}\n"
                f"Pay at the studio\n\n"
            )
        else:
            subject = f"[{club_name}] Booking paid"
            intro = (
                f"Thanks for booking at {club_name}.\n"
                f"Payment is complete and you're confirmed.\n\n"
            )
            payment_section = (
                f"Payment\n"
                f"Price: ¥{reservation.amount:,}\n"
                f"Paid by card\n\n"
            )
        message = (
            f"Hi {reservation.full_name},\n\n"
            f"{intro}"
            f"Your booking\n"
            f"Service: {course_name}\n"
            f"When: {date_text} ({weekday})\n"
            f"Time: {start_text}–{end_text}\n"
            f"With: {worker_name}\n\n"
            f"{payment_section}"
            f"Booking number\n"
            f"{reservation.id}\n\n"
            f"To cancel, open this link:\n"
            f"{_cancel_link(club, 'service', reservation.id)}\n\n"
            f"See you there.\n"
            f"{club_name}"
        )
    _send_event_email(
        club=club,
        recipient=reservation.email,
        subject=subject,
        message=message,
    )
    logger.info(
        "[EMAIL] Service confirmation sent reservation=%s recipient=%s",
        reservation.id,
        reservation.email,
    )

