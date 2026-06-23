from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from accounts.models import CustomUser


from .models import Club, SubscriptionItem
 
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
