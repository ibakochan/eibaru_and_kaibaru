from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from accounts.models import CustomUser
import logging
logger = logging.getLogger(__name__)

from .models import Club, SubscriptionItem
from django.db import transaction
 
from datetime import datetime

def format_date(dt):
    if not dt:
        return "---"
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    return dt.strftime("%Y年%m月%d日")


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

    club_name = club.subdomain or "クラブ"

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
            member_name = "ご利用料金"

        item_texts.append(
            f"{member_name} - {item.description} ¥{item.amount:,}"
        )

    item_text = (
        "\n".join(item_texts)
        if item_texts
        else "ご利用料金"
    )

    # ---------------------------------------------------------
    # Email
    # ---------------------------------------------------------

    send_mail(
        subject=f"【{club_name}】お支払いについてのお知らせ",
        message=(
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
        ),
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

        member_text = "、".join(members)
        plan_text = "、".join(plans)

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
            message += (
                "\n現在すべてのご契約プランが削除予約状態となっています。\n"
                "次回の請求サイクルまでにいずれかのメンバーに対して新しいプラン設定が行われない場合、\n"
                "サブスクリプションは停止されます。\n"
                "継続をご希望の場合は、ログインの上、プラン変更を行ってください。\n"
            )

        send_mail(
            subject="【Kaibaru】プラン削除スケジュールのお知らせ",
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[owner.email],
        )

        
@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=30, retry_kwargs={"max_retries": 5})
def send_invoice_paid_email(self, member_id, amount, items, period_end, plan_name):
    from .models import Member  # import inside to avoid circular imports

    member = Member.objects.select_related("owner").filter(id=member_id).first()
    if not member:
        return

    owner_email = member.owner.email
    owner_name = member.owner.get_full_name() or member.owner.email

    item_text = "、".join(items) if items else "お支払い"

    send_mail(
        subject="【Kaibaru】お支払いが完了しました",
        message=(
            f"{owner_name} 様\n\n"
            f"以下のお支払いが完了しました。\n\n"
            f"プラン: {plan_name}\n"
            f"内容: {item_text}\n"
            f"金額: ¥{amount}\n"
            f"ご利用可能期限: {format_date(period_end)}\n\n"
            f"本プランはお支払いごとに1ヶ月分ずつご利用期間が延長されます。\n"
            f"今後ともよろしくお願いいたします。\n"
        ),
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

    # Owner (Japanese)
    send_mail(
        subject="【Kaibaru】クラブが削除されました",
        message=(
            f"{owner_name} 様\n\n"
            f"クラブ「{subdomain}」は以下の理由により削除されました。\n\n"
            f"{reason}\n\n"
            f"ご不明な点がございましたら、Kaibaru サポートまでご連絡ください。\n\n"
            f"Kaibaru 運営チーム"
        ),
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
 
    send_mail(
        subject="【Kaibaru】ご利用プランの解約が完了しました",
        message=(
            f"{owner_name} 様\n\n"
            f"Kaibaru をご利用いただき、ありがとうございました。\n"
            f"クラブ「{subdomain}」のご利用プランは解約されました。\n\n"
            f"またのご利用をお待ちしております。\n\n"
            f"Kaibaru 運営チーム"
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[owner_email],
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
 
    send_mail(
        subject="【Kaibaru】ご利用プランの有効化が完了しました",
        message=(
            f"{owner.get_full_name()} 様\n\n"
            f"Kaibaru をご利用いただきありがとうございます。\n"
            f"クラブ「{club.subdomain}」のご利用プランが有効になりました。\n\n"
            f"今後も Kaibaru をよろしくお願いいたします。\n\n"
            f"Kaibaru 運営チーム"
        ),
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

    send_mail(
        subject="【Kaibaru】クラブ作成が完了しました",
        message=(
            f"{owner.get_full_name()} 様\n\n"
            f"Kaibaru にご登録いただきありがとうございます。\n"
            f"クラブ「{club.subdomain}」が作成されました。\n\n"
            f"Kaibaru 運営チーム"
        ),
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

    club_name = club.subdomain or "クラブ"

    member_name = (
        billing_user.get_full_name()
        if billing_user
        else None
    ) or (
        invoice.payer_name
        or invoice.payer_email
        or "お客様"
    )

    owner_name = (
        club_owner.get_full_name()
        if club_owner
        else "クラブ管理者"
    )

    amount_text = f"¥{invoice.amount_due:,}"
    due_date_text = format_date(invoice.due_date)

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
                    f"今後のお支払い方法を現金払いへ変更いたしました。\n\n"

                    f"■ 変更内容\n"
                    f"クレジットカード決済 → 現金払い\n\n"

                    f"■ 今回のお支払い\n"
                    f"{amount_text}\n\n"

                    f"■ お支払い期限\n"
                    f"{due_date_text}\n\n"

                    f"今回のクレジットカード決済は終了しており、"
                    f"今後のお支払いについては"
                    f"{club_name}へ直接お支払いください。\n\n"

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

                    f"■ お支払い期限\n"
                    f"{due_date_text}\n\n"

                    f"今回のお支払いについては、"
                    f"{club_name}へ直接お支払いください。\n\n"

                    f"なお、今後の通常のサブスクリプションのお支払いは、"
                    f"引き続きクレジットカードで処理されます。\n\n"

                    f"お支払い方法や金額についてご不明な点がございましたら、"
                    f"{club_name}までお問い合わせください。\n\n"

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
                f"「会員プラン」から会員様のサブスクリプションを"
                f"クレジットカード決済（Stripe）へ戻すことができます。\n\n"

                f"なお、未払いの請求書が残っている場合は、"
                f"クレジットカード決済へ戻すことはできません。\n\n"

                f"■ お支払い期限\n"
                f"{due_date_text}\n\n"

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

                f"■ お支払い期限\n"
                f"{due_date_text}\n\n"

                f"ご確認のうえ、必要に応じて会員様へ"
                f"現金でのお支払いをご案内ください。\n\n"

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