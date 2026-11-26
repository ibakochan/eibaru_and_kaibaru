from urllib.parse import quote

from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from .club_language import is_english, pick
from .models import MemberTransfer
from .service_member_transfer import (
    accept_member_transfer,
    member_plans_will_move,
    transfer_block_detail,
)
from .shown import shown


@ensure_csrf_cookie
@require_http_methods(["GET", "POST"])
def accept_member_transfer_view(request, token):
    if not request.user.is_authenticated:
        transfer = _pending_transfer(token)
        if transfer is None:
            return _render(request, {
                "mode": "invalid",
                "message": "この依頼は見つからないか、すでに完了しています。",
            })
        return _render_login(request, transfer)

    if request.method == "GET":
        result = _preview(request, token)
        return _render(request, result)

    result = accept_member_transfer(token=token, user=request.user)
    status = 200 if result.get("ok") else 400
    return _render(request, result, status=status)


def _pending_transfer(token):
    return (
        MemberTransfer.objects
        .select_related("member", "member__club")
        .filter(token=token, status=MemberTransfer.Status.PENDING)
        .first()
    )


def _render_login(request, transfer):
    club = transfer.member.club
    home_url = (
        f"https://{club.subdomain}.kaibaru.jp/"
        if club and club.subdomain
        else "https://kaibaru.jp/"
    )
    response = render(
        request,
        "kaibaru/member_transfer_login.html",
        {
            "english": is_english(club),
            "google_login_url": (
                "https://kaibaru.jp/start_google_login/?next="
                + quote(home_url, safe="")
            ),
        },
    )
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


def _preview(request, token):
    transfer = (
        MemberTransfer.objects
        .select_related("member", "member__club", "to_user", "from_owner")
        .filter(token=token)
        .first()
    )
    if transfer is None or transfer.status != MemberTransfer.Status.PENDING:
        club = transfer.member.club if transfer is not None else None
        return {
            "mode": "invalid",
            "message": pick(
                club,
                "この依頼は見つからないか、すでに完了しています。",
                "This request can't be found, or it's already finished.",
            ) if club is not None else "この依頼は見つからないか、すでに完了しています。",
            "transfer": transfer,
        }

    if request.user.id != transfer.to_user_id:
        return {
            "mode": "wrong_user",
            "message": pick(
                transfer.member.club,
                "この依頼は別のアカウント宛です。"
                f"{transfer.to_user.email} でログインしてください。",
                f"This request was sent to a different account. Log in as {transfer.to_user.email}.",
            ),
            "transfer": transfer,
        }

    blocked = transfer_block_detail(transfer.member, transfer.to_user)
    if blocked:
        return {"mode": "blocked", "message": blocked, "transfer": transfer}

    club = transfer.member.club
    return {
        "mode": "ready",
        "transfer": transfer,
        "plans_follow": member_plans_will_move(transfer.member),
        "club_name": (club.title or club.subdomain) if club else "",
        "home_url": f"https://{club.subdomain}.kaibaru.jp/" if club and club.subdomain else "/",
    }


def _render(request, result, status=200):
    transfer = result.get("transfer")
    member = result.get("member") or (transfer.member if transfer else None)
    club = member.club if member else None
    english = is_english(club) if club else False
    context = {
        "english": english,
        "mode": result.get("mode"),
        "message": shown(english, result.get("message", "")),
        "plans_follow": result.get("plans_follow", False),
        "member_name": member.full_name if member else "",
        "club_name": result.get("club_name") or ((club.title or club.subdomain) if club else ""),
        "home_url": (
            f"https://{club.subdomain}.kaibaru.jp/"
            if club and club.subdomain
            else "/"
        ),
    }
    response = render(
        request,
        "kaibaru/member_transfer.html",
        context,
        status=status,
    )
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response
