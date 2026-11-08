from datetime import timedelta

from django.contrib.auth.forms import PasswordResetForm
from django.db import transaction
from django.utils import timezone

from .models import PasswordResetRequest
from .services import eligible_for_reset, send_password_reset


def enqueue_password_reset(email):
    # Known and unknown addresses take the same persistence path.
    PasswordResetRequest.objects.create(email=email, available_at=timezone.now())


@transaction.atomic
def process_next_password_reset():
    now = timezone.now()
    pending = (
        PasswordResetRequest.objects.select_for_update(skip_locked=True)
        .filter(available_at__lte=now).order_by("available_at", "pk").first()
    )
    if pending is None:
        return False
    if pending.created_at <= now - timedelta(minutes=15):
        pending.delete()
        return True

    user = next((
        candidate for candidate in PasswordResetForm().get_users(pending.email)
        if eligible_for_reset(candidate) and candidate.email_verified_at <= pending.created_at
    ), None)
    if user is None or send_password_reset(user) or pending.attempts >= 2:
        pending.delete()
    else:
        pending.attempts += 1
        pending.available_at = now + timedelta(seconds=30 * pending.attempts)
        pending.save(update_fields=["attempts", "available_at"])
    return True
