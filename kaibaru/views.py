from django.shortcuts import render
from django.views import View
from allauth.socialaccount.models import SocialAccount
from django.http import HttpResponseForbidden  
from django.shortcuts import get_object_or_404
from .models import Club, Participation, Member, Subscription, Lesson
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

from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.http import JsonResponse

from .models import Invoice
from .service_cash_invoice_payment import (
    BulkCashInvoicePaymentService,
)

logger = logging.getLogger(__name__)
 

@login_required
@require_http_methods(["PATCH"])
def update_club_billing_settings(request, club_id):
    """
    Updates the club's subscription_mode or stripe_anchor_date.
    Cash clubs can set these without a connected Stripe account.
    The joining fee still requires Stripe. Monthly mode needs an anchor
    day and defaults to 25.
    """
    club = get_object_or_404(Club, id=club_id, is_deleted=False)

    from .manager_permissions import has_permission

    if not has_permission(request.user, club, "plans"):
        return HttpResponseForbidden("You do not own this club")

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "無効なリクエスト"}, status=400)

    updated_fields = []
    billing_locked = Subscription.objects.filter(club=club).exists()

    # -------------------------
    # Update subscription_mode
    # -------------------------
    if "subscription_mode" in data:
        subscription_mode = data["subscription_mode"]
        if subscription_mode in ["regular", "monthly"]:
            if billing_locked and subscription_mode != club.subscription_mode:
                return JsonResponse(
                    {
                        "error": (
                            "会員のサブスクリプションがあるため、"
                            "課金モードと請求日は変更できません。"
                        )
                    },
                    status=400,
                )
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
            if billing_locked and club.stripe_anchor_date is not None:
                return JsonResponse(
                    {
                        "error": (
                            "会員のサブスクリプションがあるため、"
                            "課金モードと請求日は変更できません。"
                        )
                    },
                    status=400,
                )
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
                    if (
                        billing_locked
                        and anchor_day != club.stripe_anchor_date
                    ):
                        return JsonResponse(
                            {
                                "error": (
                                    "会員のサブスクリプションがあるため、"
                                    "課金モードと請求日は変更できません。"
                                )
                            },
                            status=400,
                        )
                    club.stripe_anchor_date = anchor_day
                    updated_fields.append("stripe_anchor_date")
                else:
                    return JsonResponse(
                        {"error": "請求日は1〜28の数字で指定してください"},
                        status=400
                    )
            except (ValueError, TypeError):
                return JsonResponse({"error": "無効な数字形式"}, status=400)

    if "joining_fee" in data and not club.stripe_onboarding_completed:
        return JsonResponse({"error": "Stripeアカウントが接続されていません"}, status=400)

    if "joining_fee" in data:
        try:
            fee = int(data["joining_fee"])
            from .charge_amounts import invalid_set_price

            fee_message = invalid_set_price(fee, allow_free=True)
            if fee_message:
                return JsonResponse({"error": fee_message}, status=400)
    
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







def start_google_login(request):
    next_url = request.GET.get("next") or "https://kaibaru.jp/"
    request.session["next_subdomain"] = next_url
    if request.GET.get("process") == "connect" and request.user.is_authenticated:
        return redirect("/account/google/login/?process=connect")
    return redirect(f"/account/google/login/?next={quote(next_url, safe='')}")


class KaibaruPageView(View):
    template_name = 'kaibaru/kaibaru.html'

    def get(self, request, *args, **kwargs):
        user = ""
        has_google = False

        from .custom_domain_hosts import is_platform_host

        host = request.get_host().split(":")[0].lower().rstrip(".")
        if is_platform_host(host):
            subdomain = host.split(".")[0]
            club = Club.objects.filter(subdomain=subdomain, is_deleted=False).first()
        else:
            club = Club.objects.filter(
                custom_domain=host,
                custom_domain_status="active",
                is_deleted=False,
            ).first()
            subdomain = club.subdomain if club else ""

        custom_domain = ""
        if club and club.custom_domain_status == "active":
            custom_domain = (club.custom_domain or "").strip().lower().rstrip(".")

        club_data = {
            'title': club.title if club and club.title else 'ホームページ兼会員管理システム作成',
            'search_description': club.search_description if club and club.search_description else 'Wordのようにページを作成でき、会員管理システムです',
            'favicon': club.favicon.url if club and club.favicon else 'https://storage.googleapis.com/ibaru_repair/kaibarufavicon.png',
            'og_image': club.og_image.url if club and club.og_image else 'https://storage.googleapis.com/ibaru_repair/kaibarufavicon.png',
            'subdomain': subdomain or 'kaibaru',
            'custom_domain': custom_domain,
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





@login_required
@require_POST
def apply_cash_reservation_action(request):
    """Owner marks a cash lesson/event reservation paid, unpaid, or no-show."""
    from .cash_reservations import (
        ACTIONS,
        KIND_EVENT,
        KIND_LESSON,
        CashReservationError,
        apply_cash_action,
    )

    try:
        data = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "無効なリクエストです。"}, status=400)

    kind = str(data.get("kind") or "").strip().lower()
    action = str(data.get("action") or "").strip().lower()
    reservation_id = data.get("reservation_id")

    if kind not in (KIND_LESSON, KIND_EVENT):
        return JsonResponse({"error": "予約の種類が正しくありません。"}, status=400)
    if action not in ACTIONS:
        return JsonResponse({"error": "この操作は利用できません。"}, status=400)
    try:
        reservation_id = int(reservation_id)
    except (TypeError, ValueError):
        return JsonResponse({"error": "予約が見つかりません。"}, status=404)

    try:
        payload = apply_cash_action(
            kind=kind,
            reservation_id=reservation_id,
            action=action,
            user=request.user,
        )
    except CashReservationError as exc:
        return JsonResponse({"error": exc.message}, status=exc.status_code)

    return JsonResponse(payload)


@login_required
@require_POST
def bulk_mark_cash_invoices_paid(request):

    data = json.loads(request.body)

    invoice_ids = data.get("invoice_ids", [])


    if not invoice_ids:
        return JsonResponse(
            {
                "error": "No invoices selected"
            },
            status=400
        )


    from .manager_permissions import has_permission, manages_own_invoice

    invoices = (
        Invoice.objects
        .filter(
            id__in=invoice_ids,
            status="open",
            subscription__billing_method__in=[
                "cash",
                "manual",
                "bank_transfer",
            ],
        )
        .select_related(
            "subscription",
            "club",
            "payer",
        )
    )
    allowed = []
    for invoice in invoices:
        if not has_permission(request.user, invoice.club, "member_payments"):
            return JsonResponse({"error": "許可なし"}, status=403)
        if manages_own_invoice(request.user, invoice):
            return JsonResponse(
                {"error": "自分の請求は確認できません。"},
                status=403,
            )
        allowed.append(invoice)
    invoices = allowed


    result = (
        BulkCashInvoicePaymentService
        .mark_paid_bulk(
            invoices=invoices
        )
    )


    return JsonResponse(result)


@login_required
@require_http_methods(["GET", "PUT", "PATCH"])
def todo_checklist(request):
    from .models import TodoChecklist

    row, _ = TodoChecklist.objects.get_or_create(user=request.user)
    if request.method == "GET":
        return JsonResponse(
            {
                "checked": row.checked if isinstance(row.checked, dict) else {},
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
        )

    try:
        data = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "無効なリクエストです。"}, status=400)

    checked = data.get("checked")
    if not isinstance(checked, dict):
        return JsonResponse({"error": "checked must be an object."}, status=400)

    cleaned = {}
    for key, value in checked.items():
        if not isinstance(key, str):
            continue
        if len(key) > 200:
            continue
        cleaned[key] = bool(value)

    row.checked = cleaned
    row.save(update_fields=["checked", "updated_at"])
    return JsonResponse(
        {
            "checked": row.checked,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
    )


def _json_body(request):
    if request.content_type and "application/json" in request.content_type:
        try:
            return json.loads(request.body.decode() or "{}")
        except json.JSONDecodeError:
            return {}
    return request.POST


@login_required
@require_POST
def generate_attendance_qr(request):
    from .manager_permissions import has_permission
    from .service_lesson_attendance import generate_qr_token

    body = _json_body(request)
    club = Club.objects.filter(
        id=body.get("club_id") or request.POST.get("club_id"),
        is_deleted=False,
    ).first()
    if club is None:
        return JsonResponse({"detail": "クラブが見つかりません。"}, status=404)
    if club.owner_id != request.user.id and not has_permission(request.user, club, "lessons"):
        return JsonResponse({"detail": "許可なし"}, status=403)
    token = generate_qr_token(club)
    return JsonResponse({"attendance_qr_token": token})


def attendance_qr_status(request):
    from .service_lesson_attendance import _account_members, lesson_accepts_member, open_checkin_lessons

    code = request.GET.get("code") or ""
    club = Club.objects.filter(attendance_qr_token=code, is_deleted=False).first()
    if club is None or not club.attendance_qr_enabled:
        return JsonResponse({"detail": "このQRコードは使えません。"}, status=404)
    lessons = []
    members = []
    if request.user.is_authenticated:
        mine = list(_account_members(request.user, club))
        members = [{"id": member.id, "full_name": member.full_name} for member in mine]
        for lesson in open_checkin_lessons(club):
            if any(lesson_accepts_member(lesson, member) for member in mine):
                lessons.append({
                    "id": lesson.id,
                    "title": lesson.title,
                    "start_time": lesson.start_time.strftime("%H:%M"),
                    "end_time": lesson.end_time.strftime("%H:%M"),
                })
    return JsonResponse({
        "club_name": club.name,
        "enabled": True,
        "logged_in": request.user.is_authenticated,
        "lessons": lessons,
        "members": members,
    })


@login_required
@require_POST
def attendance_qr_checkin(request):
    from .service_lesson_attendance import (
        _account_members,
        check_in_member,
        lesson_is_open_for_checkin,
    )

    body = _json_body(request)
    club = Club.objects.filter(attendance_qr_token=body.get("code") or "", is_deleted=False).first()
    if club is None or not club.attendance_qr_enabled:
        return JsonResponse({"detail": "このQRコードは使えません。"}, status=404)
    member = _account_members(request.user, club).filter(id=body.get("member_id")).first()
    lesson = Lesson.objects.filter(id=body.get("lesson_id"), club=club).first()
    if member is None or lesson is None:
        return JsonResponse({"detail": "出席できません。"}, status=400)
    if not lesson_is_open_for_checkin(lesson, timezone.localtime()):
        return JsonResponse({"detail": "今はこのレッスンの出席時間ではありません。"}, status=400)
    try:
        _participation, created = check_in_member(lesson, member)
    except ValueError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)
    return JsonResponse({"ok": True, "already": not created, "member_name": member.full_name})


@login_required
def my_lesson_schedule(request):
    from .service_lesson_attendance import _account_members

    club = Club.objects.filter(subdomain=request.GET.get("subdomain"), is_deleted=False).first()
    if club is None:
        return JsonResponse({"detail": "クラブが見つかりません。"}, status=404)
    today = timezone.localdate()
    members = []
    for member in _account_members(request.user, club).prefetch_related("expected_lessons", "lesson_absences"):
        lessons = []
        for lesson in member.expected_lessons.all().order_by("weekday", "start_time"):
            lessons.append({
                "id": lesson.id,
                "title": lesson.title,
                "weekday": lesson.weekday,
                "start_time": lesson.start_time.strftime("%H:%M"),
                "end_time": lesson.end_time.strftime("%H:%M"),
                "occurs_on": lesson.occurs_on.isoformat() if lesson.occurs_on else None,
            })
        absences = [
            {
                "id": row.id,
                "date": row.date.isoformat(),
                "lesson_id": row.lesson_id,
                "reason": row.reason,
            }
            for row in member.lesson_absences.filter(date__gte=today).order_by("date", "id")
        ]
        if lessons or absences:
            members.append({
                "id": member.id,
                "full_name": member.full_name,
                "lessons": lessons,
                "absences": absences,
            })
    return JsonResponse({"members": members})


@login_required
@require_POST
def save_lesson_absence(request):
    from datetime import date as date_cls

    from .service_lesson_attendance import member_can_manage, save_absences

    body = _json_body(request)
    member = Member.objects.filter(id=body.get("member_id")).select_related("club").first()
    if member is None or not member_can_manage(request.user, member):
        return JsonResponse({"detail": "許可なし"}, status=403)
    lesson = None
    if not body.get("whole_day"):
        lesson = Lesson.objects.filter(id=body.get("lesson_id"), club_id=member.club_id).first()
        if lesson is None:
            return JsonResponse({"detail": "レッスンが見つかりません。"}, status=404)
    try:
        dates = [date_cls.fromisoformat(value) for value in body.get("dates") or []]
    except ValueError:
        return JsonResponse({"detail": "日付が正しくありません。"}, status=400)
    if not dates:
        return JsonResponse({"detail": "日を選んでください。"}, status=400)
    try:
        rows = save_absences(member=member, dates=dates, lesson=lesson, reason=body.get("reason") or "")
    except ValueError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)
    return JsonResponse({"ids": [row.id for row in rows]})


@login_required
@require_POST
def delete_lesson_absence(request):
    from .models import LessonAbsence
    from .service_lesson_attendance import member_can_manage

    body = _json_body(request)
    row = LessonAbsence.objects.filter(id=body.get("absence_id")).select_related("member").first()
    if row is None or not member_can_manage(request.user, row.member):
        return JsonResponse({"detail": "許可なし"}, status=403)
    if row.date < timezone.localdate():
        return JsonResponse({"detail": "過去の欠席は取り消せません。"}, status=400)
    row.delete()
    return JsonResponse({"ok": True})


@login_required
@require_POST
def lesson_day_off(request):
    from datetime import date as date_cls

    from .manager_permissions import has_permission
    from .service_lesson_attendance import cancel_club_day, uncancel_club_day

    body = _json_body(request)
    club = Club.objects.filter(subdomain=body.get("subdomain"), is_deleted=False).first()
    if club is None:
        return JsonResponse({"detail": "クラブが見つかりません。"}, status=404)
    if club.owner_id != request.user.id and not has_permission(request.user, club, "lessons"):
        return JsonResponse({"detail": "許可なし"}, status=403)
    try:
        day = date_cls.fromisoformat(body.get("date") or "")
    except ValueError:
        return JsonResponse({"detail": "日付が正しくありません。"}, status=400)
    if body.get("action") == "uncancel":
        removed = uncancel_club_day(club, day)
        return JsonResponse({"removed": removed})
    count = cancel_club_day(club, day, body.get("note") or "")
    return JsonResponse({"canceled": count})


def lesson_absences_today(request, lesson_id):
    from .manager_permissions import has_permission
    from .models import LessonAbsence

    lesson = Lesson.objects.filter(id=lesson_id).select_related("club").first()
    if lesson is None:
        return JsonResponse({"detail": "レッスンが見つかりません。"}, status=404)
    user = request.user
    if not user.is_authenticated:
        return JsonResponse({"detail": "許可なし"}, status=403)
    instructor_here = Member.objects.filter(club=lesson.club, is_instructor=True).filter(
        models_user_or_owner(user)
    ).exists()
    if lesson.club.owner_id != user.id and not has_permission(user, lesson.club, "lessons") and not instructor_here:
        return JsonResponse({"detail": "許可なし"}, status=403)
    day = timezone.localdate()
    rows = LessonAbsence.objects.filter(date=day).filter(
        models_absence_q(lesson)
    ).select_related("member")
    return JsonResponse({
        "absences": [
            {
                "member_id": row.member_id,
                "full_name": row.member.full_name,
                "whole_day": row.lesson_id is None,
                "reason": row.reason,
            }
            for row in rows
        ]
    })


@login_required
def member_absence_history(request):
    from .manager_permissions import has_permission
    from .service_lesson_attendance import absence_history

    member = Member.objects.filter(id=request.GET.get("member_id")).select_related("club").first()
    if member is None:
        return JsonResponse({"detail": "会員が見つかりません。"}, status=404)
    user = request.user
    instructor_here = Member.objects.filter(club=member.club, is_instructor=True).filter(
        models_user_or_owner(user)
    ).exists()
    if (
        member.club.owner_id != user.id
        and not has_permission(user, member.club, "lessons")
        and not has_permission(user, member.club, "members")
        and not instructor_here
    ):
        return JsonResponse({"detail": "許可なし"}, status=403)
    return JsonResponse(absence_history(member))


def models_user_or_owner(user):
    from django.db.models import Q

    return Q(user=user) | Q(owner=user)


def models_absence_q(lesson):
    from django.db.models import Q

    return Q(lesson=lesson) | Q(lesson__isnull=True, member__expected_lessons=lesson)

