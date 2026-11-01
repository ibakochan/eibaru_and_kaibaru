import logging
import re
import socket
import subprocess

import stripe
from django.conf import settings
from django.core.mail import EmailMessage

from .models import Club, PlatformClub, PlatformSubscription

logger = logging.getLogger(__name__)


class CustomDomainError(Exception):
    def __init__(self, message):
        self.message = message
        super().__init__(message)


_HOST = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$"
)
_BLOCKED = (
    "kaibaru.jp",
    "eibaru.jp",
)


def target_ip():
    return getattr(settings, "CUSTOM_DOMAIN_TARGET_IP", "13.158.230.76")


def normalize_domain(value):
    raw = (value or "").strip().lower()
    raw = re.sub(r"^https?://", "", raw)
    raw = raw.split("/")[0].split("?")[0].split("#")[0]
    raw = raw.rstrip(".")
    if ":" in raw:
        raise CustomDomainError("ポート番号は入れないでください。")
    try:
        raw = raw.encode("idna").decode("ascii")
    except UnicodeError:
        raise CustomDomainError("このドメイン名は使えません。")
    if not _HOST.match(raw):
        raise CustomDomainError("ドメインの形が正しくありません。")
    if raw in _BLOCKED or raw.endswith(".kaibaru.jp") or raw.endswith(".eibaru.jp"):
        raise CustomDomainError("Kaibaru と eibaru のドメインは指定できません。")
    return raw


def assign_domain(club, value):
    domain = normalize_domain(value)
    other = Club.all_objects.filter(custom_domain=domain).exclude(pk=club.pk).first()
    if other and not other.is_deleted:
        raise CustomDomainError("このドメインは別のクラブで使われています。")
    if other:
        other.custom_domain = None
        other.custom_domain_status = ""
        other.scheduled_for_domain_cert = False
        other.save(update_fields=[
            "custom_domain",
            "custom_domain_status",
            "scheduled_for_domain_cert",
        ])
    club.custom_domain = domain
    club.custom_domain_status = "pending"
    club.scheduled_for_domain_cert = True
    club.save(update_fields=[
        "custom_domain",
        "custom_domain_status",
        "scheduled_for_domain_cert",
    ])
    return domain


def clear_domain(club):
    owner_id = club.owner_id
    club.custom_domain = None
    club.custom_domain_status = ""
    club.scheduled_for_domain_cert = False
    club.save(update_fields=[
        "custom_domain",
        "custom_domain_status",
        "scheduled_for_domain_cert",
    ])
    try:
        sync_custom_domain_fee(owner_id)
    except CustomDomainError:
        logger.exception(
            "custom domain fee sync failed after clear club=%s",
            club.pk,
        )


def _addresses(domain):
    try:
        infos = socket.getaddrinfo(domain, None)
    except socket.gaierror:
        return set()
    return {item[4][0] for item in infos}


def domain_points_here(domain):
    return target_ip() in _addresses(domain)


def check_domain(club):
    """Look up DNS only. The hourly task issues the certificate."""
    domain = club.custom_domain
    if not domain:
        raise CustomDomainError("先にドメインを保存してください。")
    ip = target_ip()
    if ip not in _addresses(domain):
        raise CustomDomainError(
            f"DNSがまだ {ip} を指していません。Aレコードを設定してから、もう一度確認してください。"
        )
    return domain


def issue_custom_certificate(domain):
    """
    Ask the server script to issue the certificate.
    /usr/bin/sudo is absolute because the Celery worker's PATH
    does not include sudo. -n never sends a password. The server
    allows this one script without a password; that rule is not stored here.
    """
    command = getattr(
        settings,
        "CUSTOM_DOMAIN_ISSUE_COMMAND",
        "/usr/local/sbin/kaibaru-issue-custom-cert",
    )
    try:
        result = subprocess.run(
            ["/usr/bin/sudo", "-n", command, domain],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CustomDomainError(f"証明書の発行に失敗しました。{exc}")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        if len(detail) > 400:
            detail = detail[-400:]
        raise CustomDomainError(
            "証明書の発行に失敗しました。" + (f" {detail}" if detail else "")
        )


def _notify_domain_ready(club):
    owner = getattr(club, "owner", None)
    email = (getattr(owner, "email", "") or "").strip()
    if not email:
        return
    club_name = club.title or club.subdomain or "クラブ"
    domain = club.custom_domain
    try:
        EmailMessage(
            subject=f"【{club_name}】独自ドメインの準備ができました",
            body=(
                f"{domain} を{club_name}のアドレスとして公開しました。\n\n"
                f"https://{domain}\n"
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[email],
        ).send()
    except Exception:
        logger.exception(
            "custom domain ready email failed club=%s domain=%s",
            club.pk,
            domain,
        )


def domain_fee_quantities(count):
    """One full-price domain, then one quantity per domain after that."""
    primary = getattr(
        settings,
        "STRIPE_CUSTOM_DOMAIN_PRICE_ID",
        "price_1UWdj7EuyKDnirofTYIVi0AP",
    )
    extra_price = getattr(
        settings,
        "STRIPE_CUSTOM_DOMAIN_EXTRA_PRICE_ID",
        "price_1UWdlAEuyKDnirofQaW0HMGv",
    )
    if count <= 0:
        return {}
    quantities = {primary: 1}
    extra = count - 1
    if extra:
        quantities[extra_price] = extra
    return quantities


def _price_id_of(item):
    price = item.get("price")
    if isinstance(price, str):
        return price
    if isinstance(price, dict) and price.get("id"):
        return price["id"]
    details = (item.get("pricing") or {}).get("price_details") or {}
    return details.get("price")


def _platform_subscription_id(owner_id, club):
    link = (
        PlatformClub.objects
        .filter(club=club)
        .select_related("platform_subscription")
        .first()
    )
    if link and link.platform_subscription.stripe_subscription_id:
        return link.platform_subscription.stripe_subscription_id
    platform = PlatformSubscription.objects.filter(user_id=owner_id).first()
    if platform and platform.stripe_subscription_id:
        return platform.stripe_subscription_id
    if club is not None and club.stripe_subscription_id:
        return club.stripe_subscription_id
    return None


def _live_domain_count(owner_id, include_club_id=None):
    ids = set(
        Club.objects.filter(
            owner_id=owner_id,
            custom_domain_status="active",
        ).values_list("id", flat=True)
    )
    if include_club_id:
        ids.add(include_club_id)
    return len(ids)


def sync_custom_domain_fee(owner_id, club=None, *, include_club_id=None):
    """
    Set the owner's domain items to the number of live domains.
    The first domain uses the full price. Each domain after that
    is one quantity on the secondary price. Quantity is written
    as the expected total, so a retry does not add another item.
    """
    subscription_id = _platform_subscription_id(owner_id, club)
    if not subscription_id:
        raise CustomDomainError("プラットフォームの契約がないため、ドメイン料金を追加できません。")
    count = _live_domain_count(owner_id, include_club_id)
    desired = domain_fee_quantities(count)
    prices = (
        getattr(settings, "STRIPE_CUSTOM_DOMAIN_PRICE_ID", "price_1UWdj7EuyKDnirofTYIVi0AP"),
        getattr(
            settings,
            "STRIPE_CUSTOM_DOMAIN_EXTRA_PRICE_ID",
            "price_1UWdlAEuyKDnirofQaW0HMGv",
        ),
    )
    stripe.api_key = settings.STRIPE_SECRET_KEY
    listed = stripe.SubscriptionItem.list(subscription=subscription_id, limit=100)
    existing = {}
    extras = []
    for item in listed.auto_paging_iter():
        price_id = _price_id_of(item)
        if price_id not in prices:
            continue
        if price_id in existing:
            extras.append(item)
        else:
            existing[price_id] = item
    changes = []
    for price_id in prices:
        quantity = desired.get(price_id, 0)
        item = existing.get(price_id)
        if quantity <= 0:
            if item:
                changes.append({"id": item["id"], "deleted": True})
            continue
        if item:
            if item["quantity"] != quantity:
                changes.append({"id": item["id"], "quantity": quantity})
        else:
            changes.append({"price": price_id, "quantity": quantity})
    for extra in extras:
        changes.append({"id": extra["id"], "deleted": True})
    if not changes:
        return
    stripe.Subscription.modify(
        subscription_id,
        items=changes,
        proration_behavior="none",
        idempotency_key=f"domain-fee-{subscription_id}-{count}"[:255],
    )


def publish_domain(club):
    """Issue a certificate when DNS already points here. Used by the hourly task."""
    domain = check_domain(club)
    if club.custom_domain_status == "active":
        if club.scheduled_for_domain_cert:
            club.scheduled_for_domain_cert = False
            club.save(update_fields=["scheduled_for_domain_cert"])
        return domain
    issue_custom_certificate(domain)
    try:
        sync_custom_domain_fee(club.owner_id, club, include_club_id=club.pk)
    except CustomDomainError:
        raise
    except stripe.error.StripeError as exc:
        raise CustomDomainError(f"ドメイン料金の追加に失敗しました。{exc}")
    club.custom_domain_status = "active"
    club.scheduled_for_domain_cert = False
    club.save(update_fields=["custom_domain_status", "scheduled_for_domain_cert"])
    _notify_domain_ready(club)
    return domain
