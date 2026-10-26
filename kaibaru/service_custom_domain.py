import re
import socket
import subprocess

from django.conf import settings

from .models import Club


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
        other.save(update_fields=["custom_domain", "custom_domain_status"])
    club.custom_domain = domain
    club.custom_domain_status = "pending"
    club.save(update_fields=["custom_domain", "custom_domain_status"])
    return domain


def clear_domain(club):
    club.custom_domain = None
    club.custom_domain_status = ""
    club.save(update_fields=["custom_domain", "custom_domain_status"])


def _addresses(domain):
    try:
        infos = socket.getaddrinfo(domain, None)
    except socket.gaierror:
        return set()
    return {item[4][0] for item in infos}


def publish_domain(club):
    domain = club.custom_domain
    if not domain:
        raise CustomDomainError("先にドメインを保存してください。")
    ip = target_ip()
    if ip not in _addresses(domain):
        raise CustomDomainError(
            f"DNSがまだ {ip} を指していません。Aレコードを設定してから、もう一度確認してください。"
        )
    if club.custom_domain_status == "active":
        return domain
    command = getattr(
        settings,
        "CUSTOM_DOMAIN_ISSUE_COMMAND",
        "/usr/local/sbin/kaibaru-issue-custom-cert",
    )
    try:
        result = subprocess.run(
            ["sudo", "-n", command, domain],
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
    club.custom_domain_status = "active"
    club.save(update_fields=["custom_domain_status"])
    return domain
