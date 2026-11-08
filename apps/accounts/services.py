import hashlib
import re
import secrets
from datetime import timedelta

from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.db import IntegrityError, connection, transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .mail import absolute_url, send_account_mail
from .models import EmailVerification, User

VERIFICATION_LIFETIME = timedelta(hours=24)
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
VERIFIED_EMAIL_CONSTRAINT = "unique_verified_account_email"


class AccountResetTokenGenerator(PasswordResetTokenGenerator):
    def _make_hash_value(self, user, timestamp):
        return f"{super()._make_hash_value(user, timestamp)}:{user.email_verified_at!s}"


account_reset_token_generator = AccountResetTokenGenerator()


def normalize_email(value):
    return value.strip().lower()


def _digest(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def token_digest(token):
    return _digest(token) if TOKEN_PATTERN.fullmatch(token) else None


def _lock_identity(namespace, value):
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [f"accounts:{namespace}:{value}"])


def _email_in_use(email, *, except_user=None):
    users = User.objects.filter(email__iexact=email, registration_pending=False)
    return users.exclude(pk=except_user).exists() if except_user is not None else users.exists()


def _send_verification(*, user, address):
    token = secrets.token_urlsafe(32)
    url = absolute_url(reverse("verify_email_token", kwargs={"token": token}))
    if not url or not send_account_mail(
        "Подтверждение адреса Enactus OS",
        "Подтвердите адрес электронной почты по ссылке. Она действительна 24 часа.\n"
        f"{url}\n\nЕсли вы не запрашивали подтверждение, проигнорируйте это письмо.",
        address,
    ):
        return False
    EmailVerification.objects.update_or_create(
        user=user,
        defaults={
            "target_email": address, "token_digest": _digest(token),
            "expires_at": timezone.now() + VERIFICATION_LIFETIME,
        },
    )
    return True


@transaction.atomic
def register_pending(user):
    _lock_identity("username", user.username.lower())
    if User.objects.filter(username__iexact=user.username).exists():
        return "username_taken"
    user.email = normalize_email(user.email)
    _lock_identity("email", user.email)
    if _email_in_use(user.email):
        return "email_taken"
    user.is_active = False
    user.registration_pending = True
    user.save()
    return "sent" if _send_verification(user=user, address=user.email) else "unavailable"


@transaction.atomic
def resend_pending(email, username):
    email = normalize_email(email)
    user = User.objects.select_for_update().filter(
        email__iexact=email, username__iexact=username,
        registration_pending=True, is_active=False,
    ).first()
    if user is not None:
        _lock_identity("email", email)
        if not _email_in_use(email, except_user=user.pk):
            _send_verification(user=user, address=email)


@transaction.atomic
def request_email_change(*, user_id, email, password):
    user = User.objects.select_for_update().filter(pk=user_id, is_active=True, registration_pending=False).first()
    if user is None or not user.check_password(password):
        return "invalid_password"
    email = normalize_email(email)
    _lock_identity("email", email)
    if _email_in_use(email, except_user=user.pk):
        return "email_taken"
    if user.email_verified_at is not None and normalize_email(user.email) == email:
        return "unchanged"
    return "sent" if _send_verification(user=user, address=email) else "unavailable"


def _eligible_verification(row, actor_id):
    user = row.user
    if actor_id is not None and actor_id != user.pk:
        return False
    if user.registration_pending:
        return not user.is_active and normalize_email(user.email) == row.target_email
    return user.is_active


def pending_verification(digest, *, actor_id=None):
    if not digest:
        return None
    row = EmailVerification.objects.select_related("user").filter(
        token_digest=digest, expires_at__gt=timezone.now(),
    ).first()
    return row if row is not None and _eligible_verification(row, actor_id) else None


@transaction.atomic
def confirm_verification(digest, *, actor_id=None, password=""):
    row = pending_verification(digest, actor_id=actor_id)
    if row is None:
        return "invalid"
    user = User.objects.select_for_update().get(pk=row.user_id)
    row = EmailVerification.objects.select_for_update().filter(
        user=user, token_digest=digest, expires_at__gt=timezone.now(),
    ).first()
    if row is None:
        return "invalid"
    row.user = user
    if not _eligible_verification(row, actor_id):
        return "invalid"
    if user.registration_pending and not user.check_password(password):
        return "invalid_password"

    email = row.target_email
    _lock_identity("email", email)
    if _email_in_use(email, except_user=user.pk):
        return "invalid"
    user.email = email
    user.email_verified_at = timezone.now()
    if user.registration_pending:
        user.registration_pending = False
        user.is_active = True
    try:
        with transaction.atomic():
            user.save(update_fields=["email", "email_verified_at", "registration_pending", "is_active"])
    except IntegrityError as exc:
        if getattr(getattr(exc.__cause__, "diag", None), "constraint_name", None) != VERIFIED_EMAIL_CONSTRAINT:
            raise
        return "invalid"
    row.delete()
    return "verified"


def eligible_for_reset(user):
    return (
        user is not None and user.is_active and not user.registration_pending
        and user.email_verified_at is not None and user.has_usable_password()
    )


def send_password_reset(user):
    token = account_reset_token_generator.make_token(user)
    path = reverse("password_reset_confirm", kwargs={
        "uidb64": urlsafe_base64_encode(force_bytes(user.pk)), "token": token,
    })
    url = absolute_url(path)
    if url:
        send_account_mail(
            "Восстановление пароля Enactus OS",
            f"Чтобы установить новый пароль, откройте ссылку:\n{url}\n\n"
            "Если вы не запрашивали восстановление, проигнорируйте это письмо.",
            user.email,
        )


def invalidate_pending_email(user):
    EmailVerification.objects.filter(user=user).delete()
