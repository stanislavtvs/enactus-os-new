import os
import re

from django.core.exceptions import ImproperlyConfigured

from .settings import *


PUBLIC_HOST = os.environ.get("DJANGO_PUBLIC_HOST", "").lower()
hostname_label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
if len(PUBLIC_HOST) > 253 or not re.fullmatch(rf"{hostname_label}(?:\.{hostname_label})+", PUBLIC_HOST):
    raise ImproperlyConfigured("DJANGO_PUBLIC_HOST must be an explicit HTTPS hostname.")
if len(SECRET_KEY) < 50 or len(set(SECRET_KEY)) < 6 or SECRET_KEY.startswith("django-insecure-"):
    raise ImproperlyConfigured("DJANGO_SECRET_KEY must be a strong production key.")

DEBUG = False
ALLOWED_HOSTS = [PUBLIC_HOST]
CSRF_TRUSTED_ORIGINS = [f"https://{PUBLIC_HOST}"]
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31536000
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

STATIC_ROOT = BASE_DIR / "collected_static"
MIDDLEWARE = [MIDDLEWARE[0], "whitenoise.middleware.WhiteNoiseMiddleware", *MIDDLEWARE[1:]]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
