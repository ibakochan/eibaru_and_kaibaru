from django.shortcuts import render
from django.views import View
from allauth.socialaccount.models import SocialAccount
from django.http import HttpResponseForbidden  
from django.shortcuts import get_object_or_404
from .models import Club, Participation, Member, JoinRequest
from django.shortcuts import redirect
from urllib.parse import urlencode
from urllib.parse import quote
import logging
from django.core.files.storage import default_storage
import stripe
import calendar
import time
from django.conf import settings
from django.http import JsonResponse, HttpResponse
from datetime import date
from django.utils import timezone

from .tasks_emails import send_subscription_activated_emails
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.http import HttpResponseForbidden

from django.views.decorators.csrf import csrf_exempt

from .utils import add_one_month
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
import json

logger = logging.getLogger(__name__)
 

@login_required
@require_http_methods(["PATCH"])
def update_club_billing_settings(request, club_id):
    """
    Updates the club's subscription_mode or stripe_anchor_date.
    Only visible/usable by the club owner and if Stripe is connected.
    Enforces an anchor day for monthly mode (defaults to 25 if not set).
    """
    club = get_object_or_404(Club, id=club_id, is_deleted=False)

    if club.owner != request.user:
        return HttpResponseForbidden("You do not own this club")

    if not club.stripe_onboarding_completed:
        return JsonResponse({"error": "Stripeアカウントが接続されていません"}, status=400)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "無効なリクエスト"}, status=400)

    updated_fields = []

    # -------------------------
    # Update subscription_mode
    # -------------------------
    if "subscription_mode" in data:
        subscription_mode = data["subscription_mode"]
        if subscription_mode in ["regular", "monthly"]:
            club.subscription_mode = subscription_mode
            updated_fields.append("subscription_mode")
            # Ensure monthly mode always has an anchor
            if subscription_mode == "monthly" and not club.stripe_anchor_date:
                club.stripe_anchor_date = 25
                updated_fields.append("stripe_anchor_date")
        else:
            return JsonResponse({"error": "無効な課金モード"}, status=400)

    # -------------------------
    # Update stripe_anchor_date
    # -------------------------
    if "stripe_anchor_date" in data:
        anchor_date = data["stripe_anchor_date"]
        if anchor_date in [None, "", "null"]:
            # Prevent clearing anchor if monthly mode
            if club.subscription_mode == "monthly":
                return JsonResponse(
                    {"error": "月謝モードの場合、請求日は必須です"},
                    status=400
                )
            club.stripe_anchor_date = None
            updated_fields.append("stripe_anchor_date")
        else:
            try:
                anchor_day = int(anchor_date)
                if 1 <= anchor_day <= 28:
                    club.stripe_anchor_date = anchor_day
                    updated_fields.append("stripe_anchor_date")
                else:
                    return JsonResponse(
                        {"error": "請求日は1〜28の数字で指定してください"},
                        status=400
                    )
            except (ValueError, TypeError):
                return JsonResponse({"error": "無効な数字形式"}, status=400)

    if "joining_fee" in data:
        try:
            fee = int(data["joining_fee"])
            if fee < 0:
                return JsonResponse({"error": "入会金は0以上で入力してください"}, status=400)
    
            club.joining_fee = fee
            updated_fields.append("joining_fee")
    
        except (ValueError, TypeError):
            return JsonResponse({"error": "無効な入会金の形式"}, status=400)



    if updated_fields:
        club.save(update_fields=updated_fields)

    return JsonResponse({
        "success": True,
        "subscription_mode": club.subscription_mode,
        "stripe_anchor_date": club.stripe_anchor_date,
        "joining_fee": club.joining_fee,
    })

@login_required
@require_POST
def create_join_request(request, club_subdomain):
    club = get_object_or_404(Club, subdomain=club_subdomain, is_deleted=False)
    
    join_request, created = JoinRequest.objects.get_or_create(
        user=request.user,
        club=club,
        defaults={"status": JoinRequest.Status.PENDING},
    )
    
    return JsonResponse({
        "id": join_request.id,
        "status": join_request.status,
        "created": created
    })





def start_google_login(request):
    next_url = request.GET.get('next')
    if next_url:
        request.session['next_subdomain'] = next_url
    else:
        request.session['next_subdomain'] = "https://kaibaru.jp/"  
    return redirect("/account/google/login/")


class KaibaruPageView(View):
    template_name = 'kaibaru/kaibaru.html'

    def get(self, request, *args, **kwargs):
        user = ""
        has_google = False

        subdomain = request.get_host().split('.')[0]
        
        club = Club.objects.filter(subdomain=subdomain, is_deleted=False).first()

        club_data = {
            'title': club.title if club and club.title else 'ホームページ兼会員管理システム作成',
            'search_description': club.search_description if club and club.search_description else 'Wordのようにページを作成でき、会員管理システムです',
            'favicon': club.favicon.url if club and club.favicon else 'https://storage.googleapis.com/ibaru_repair/kaibarufavicon.png',
            'og_image': club.og_image.url if club and club.og_image else 'https://storage.googleapis.com/ibaru_repair/kaibarufavicon.png',
            'subdomain': subdomain or 'kaibaru',
        }


        if request.user.is_authenticated:
            user = request.user
            has_google = SocialAccount.objects.filter(user=user, provider='google').exists()

        return render(request, self.template_name, {
            'user': user,
            'has_google': has_google,
            'club_data': club_data,
            'stripe_publishable_key': settings.STRIPE_PUBLISHABLE_KEY,
        })





