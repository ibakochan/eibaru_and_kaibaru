from django.contrib.auth.views import redirect_to_login
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from .service_member_transfer import accept_member_transfer, transfer_block_detail


@require_http_methods(["GET", "POST"])
def accept_member_transfer_view(request, token):
    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path())

    if request.method == "GET":
        result = _preview(request, token)
        return _render(request, result)

    result = accept_member_transfer(token=token, user=request.user)
    status = 200 if result.get("ok") else 400
    return _render(request, result, status=status)


def _preview(request, token):
    from .models import MemberTransfer

    transfer = (
        MemberTransfer.objects
        .select_related("member", "member__club", "to_user", "from_owner")
        .filter(token=token)
        .first()
    )
    if transfer is None or transfer.status != MemberTransfer.Status.PENDING:
        return {"mode": "invalid", "message": "この依頼は見つからないか、すでに完了しています。"}

    if request.user.id != transfer.to_user_id:
        return {
            "mode": "wrong_user",
            "message": (
                "この依頼は別のアカウント宛です。"
                f"{transfer.to_user.email} でログインしてください。"
            ),
            "transfer": transfer,
        }

    blocked = transfer_block_detail(transfer.member)
    if blocked:
        return {"mode": "blocked", "message": blocked, "transfer": transfer}

    club = transfer.member.club
    return {
        "mode": "ready",
        "transfer": transfer,
        "club_name": (club.title or club.subdomain) if club else "",
        "home_url": f"https://{club.subdomain}.kaibaru.jp/" if club and club.subdomain else "/",
    }


def _render(request, result, status=200):
    transfer = result.get("transfer")
    member = result.get("member") or (transfer.member if transfer else None)
    club = member.club if member else None
    context = {
        "mode": result.get("mode"),
        "message": result.get("message", ""),
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
