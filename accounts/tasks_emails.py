from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.urls import reverse
from django.utils.http import urlsafe_base64_encode
from django.utils.encoding import force_bytes

from .models import CustomUser
from .tokens import email_verification_token

@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=60, retry_kwargs={"max_retries": 5})
def send_verification_email(self, user_id):
    user = CustomUser.objects.get(id=user_id)

    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = email_verification_token.make_token(user)

    verification_url = f"https://kaibaru.jp/accounts/verify/{uid}/{token}/"

    send_mail(
        subject="【Kaibaru】メールアドレス確認のお願い",
        message=(
            f"{user.username} 様\n\n"
            f"Kaibaru へご登録ありがとうございます。\n\n"
            f"以下のリンクをクリックしてアカウントを有効化してください。\n\n"
            f"{verification_url}\n\n"
            f"このリンクは一定時間で無効になります。\n\n"
            f"Kaibaru 運営チーム"
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
    )