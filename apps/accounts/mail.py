import logging

from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)


def absolute_url(path):
    if not path.startswith("/") or path.startswith("//") or "\\" in path:
        raise ValueError("Expected a local account URL.")
    return settings.ACCOUNT_PUBLIC_ORIGIN.rstrip("/") + path


def send_account_mail(subject, message, recipient):
    if settings.EMAIL_BACKEND == "django.core.mail.backends.dummy.EmailBackend":
        logger.warning("Account mail disabled.")
        return False
    try:
        delivered = send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [recipient])
    except Exception:
        # Transport exceptions can include private addresses and message payloads.
        logger.warning("Account mail transport failed.")
        return False
    if delivered != 1:
        logger.warning("Account mail transport did not accept the message.")
    return delivered == 1
