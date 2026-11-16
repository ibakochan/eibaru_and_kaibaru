"""Club-level visitor booking finish rules (salon / lesson / trial / event)."""

CONFIRM_EMAIL_LINK = "email_link"
CONFIRM_INSTANT = "instant"
AUTH_GUEST = "guest"
AUTH_LOGIN_REQUIRED = "login_required"

CONFIRM_CHOICES = (
    (CONFIRM_EMAIL_LINK, "Email link"),
    (CONFIRM_INSTANT, "Instant"),
)
AUTH_CHOICES = (
    (AUTH_GUEST, "Guest OK"),
    (AUTH_LOGIN_REQUIRED, "Login required"),
)

LOGIN_REQUIRED_MESSAGE = (
    "予約にはログインが必要です。"
    "Kaibaruアカウントでログインしてからお申し込みください。"
)


def visitor_booking_confirm(club):
    value = getattr(club, "visitor_booking_confirm", None) or CONFIRM_EMAIL_LINK
    if value in (CONFIRM_EMAIL_LINK, CONFIRM_INSTANT):
        return value
    return CONFIRM_EMAIL_LINK


def visitor_booking_auth(club):
    value = getattr(club, "visitor_booking_auth", None) or AUTH_GUEST
    if value in (AUTH_GUEST, AUTH_LOGIN_REQUIRED):
        return value
    return AUTH_GUEST


def is_instant_confirm(club):
    return visitor_booking_confirm(club) == CONFIRM_INSTANT


def assert_visitor_booking_auth(club, user):
    if visitor_booking_auth(club) != AUTH_LOGIN_REQUIRED:
        return
    if user is None or not getattr(user, "is_authenticated", False):
        raise ValueError(LOGIN_REQUIRED_MESSAGE)


def create_result_from_begin(
    *,
    begin_result,
    reservation_id,
    reservation_ids=None,
):
    ids = list(reservation_ids or [reservation_id])
    checkout_url = begin_result.get("redirect_url") or begin_result.get(
        "checkout_url"
    )
    if checkout_url:
        return {
            "success": True,
            "email_sent": False,
            "requires_checkout": True,
            "paid": False,
            "checkout_url": checkout_url,
            "reservation_id": ids[0],
            "reservation_ids": ids,
        }

    is_cash = bool(begin_result.get("is_cash"))
    payload = {
        "success": True,
        "email_sent": False,
        "requires_checkout": False,
        "paid": not is_cash,
        "reservation_id": ids[0],
        "reservation_ids": ids,
    }
    if is_cash:
        payload["payment_method"] = "cash"
    return payload
