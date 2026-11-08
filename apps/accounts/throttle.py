from datetime import timedelta
from ipaddress import ip_address, ip_network

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac

from .models import AuthThrottle


IP_LIMITS = {
    "register": 12, "login": 30, "resend": 16, "verify": 40,
    "reset": 16, "reset_confirm": 40, "change_email": 12, "password_change": 15,
}
MAIL_ACTIONS = frozenset({"register", "resend", "reset", "change_email"})


def _client_ip(request):
    try:
        peer = ip_address(request.META.get("REMOTE_ADDR", ""))
    except ValueError:
        return "unknown"
    if any(
        peer in ip_network(cidr, strict=False)
        for cidr in getattr(settings, "ACCOUNT_TRUSTED_PROXY_NETWORKS", ())
    ):
        try:
            return str(ip_address(request.META.get("HTTP_X_REAL_IP", "")))
        except ValueError:
            pass
    return str(peer)


def _key(*parts):
    return salted_hmac("enactus.accounts.throttle", "\0".join(parts), algorithm="sha256").hexdigest()


def _consume(key, limit, now):
    bucket, _ = AuthThrottle.objects.select_for_update().get_or_create(
        key=key, defaults={"window_started_at": now, "count": 0},
    )
    if bucket.window_started_at <= now - timedelta(hours=1):
        bucket.window_started_at = now
        bucket.count = 0
    if bucket.count >= limit:
        return False
    bucket.count += 1
    bucket.save(update_fields=["window_started_at", "count"])
    return True


@transaction.atomic
def allow(request, action, identity=None):
    now = timezone.now()
    ip = _client_ip(request)
    if not _consume(_key("ip", action, ip), IP_LIMITS[action], now):
        return False
    if identity and action in MAIL_ACTIONS:
        return _consume(_key("mail", identity.strip().casefold()), 5, now)
    if identity and action == "login":
        return _consume(_key("login", ip, identity.strip().casefold()), 10, now)
    return True
