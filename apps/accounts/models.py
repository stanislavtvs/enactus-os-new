from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower


class User(AbstractUser):
    """Глобальная учётная запись платформы."""

    email_verified_at = models.DateTimeField(null=True, blank=True)
    registration_pending = models.BooleanField(default=False)

    class Meta(AbstractUser.Meta):
        constraints = [
            models.UniqueConstraint(
                Lower("email"),
                condition=Q(email_verified_at__isnull=False),
                name="unique_verified_account_email",
            ),
            models.CheckConstraint(
                condition=Q(email_verified_at__isnull=True) | Q(email__regex=r"[^[:space:]]"),
                name="verified_account_email_present",
            ),
            models.CheckConstraint(
                condition=Q(registration_pending=False)
                | Q(is_active=False, email_verified_at__isnull=True),
                name="pending_account_inactive_unverified",
            ),
        ]


class EmailVerification(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="email_verification",
    )
    target_email = models.EmailField(max_length=254)
    token_digest = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(token_digest__regex=r"^[0-9a-f]{64}$"),
                name="email_verification_digest_valid",
            ),
        ]


class AuthThrottle(models.Model):
    key = models.CharField(max_length=64, primary_key=True)
    window_started_at = models.DateTimeField()
    count = models.PositiveIntegerField(default=0)
