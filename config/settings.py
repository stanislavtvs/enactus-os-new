import os
import stat
from ipaddress import ip_network
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from django.core.exceptions import ImproperlyConfigured


def _env_flag(name, default):
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    if value.lower() in {"true", "1"}:
        return True
    if value.lower() in {"false", "0"}:
        return False
    raise ImproperlyConfigured(f"{name} must be true or false.")


def _env_int(name, default, maximum):
    try:
        value = int(os.environ.get(name) or default)
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} must be an integer.") from exc
    if not 1 <= value <= maximum:
        raise ImproperlyConfigured(f"{name} must be between 1 and {maximum}.")
    return value


def _local_account_origin():
    origin = os.environ.get("ACCOUNT_PUBLIC_ORIGIN", "http://127.0.0.1:8002")
    try:
        parsed = urlsplit(origin)
        if (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and parsed.username is None
            and parsed.port != 0
            and parsed.path in {"", "/"}
            and not any(char.isspace() or char in "\\?#" for char in origin)
        ):
            return origin.rstrip("/")
    except ValueError:
        pass
    raise ImproperlyConfigured("ACCOUNT_PUBLIC_ORIGIN must be an HTTP or HTTPS origin.")


def _trusted_account_proxies():
    networks = []
    for value in os.environ.get("ACCOUNT_TRUSTED_PROXY_NETWORKS", "").split(","):
        if not value.strip():
            continue
        try:
            network = ip_network(value.strip())
        except ValueError as exc:
            raise ImproperlyConfigured("ACCOUNT_TRUSTED_PROXY_NETWORKS must contain valid CIDRs.") from exc
        if network.prefixlen == 0:
            raise ImproperlyConfigured("ACCOUNT_TRUSTED_PROXY_NETWORKS cannot trust every address.")
        networks.append(network)
    return tuple(networks)


def _prepare_local_mail_directory(path):
    directory = Path(path)
    try:
        directory.mkdir(mode=0o700, exist_ok=True)
        metadata = directory.lstat()
    except OSError as exc:
        raise ImproperlyConfigured("Could not prepare the local mail directory.") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.geteuid()
    ):
        raise ImproperlyConfigured("The local mail directory must be owned by the app user with mode 0700.")


BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]
DEBUG = os.environ.get("DJANGO_DEBUG", "false").lower() in {"1", "true"}
ALLOWED_HOSTS = os.environ.get(
    "DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1"
).split(",")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "apps.accounts.apps.AccountsConfig",
    "apps.organizations.apps.OrganizationsConfig",
    "apps.departments.apps.DepartmentsConfig",
    "apps.access.apps.AccessConfig",
    "apps.projects.apps.ProjectsConfig",
    "apps.tasks.apps.TasksConfig",
    "apps.calendar_app.apps.CalendarConfig",
    "apps.notifications.apps.NotificationsConfig",
    "apps.audit.apps.AuditConfig",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.organizations.workspaces.WorkspaceNavigationMiddleware",
    "apps.organizations.module_views.ModuleAvailabilityMiddleware",
    "apps.audit.context.AuditOperationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.organizations.workspaces.workspace_navigation",
            ],
        },
    },
]
WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "config.db.backends.postgresql",
        "NAME": os.environ["POSTGRES_DB"],
        "USER": os.environ["POSTGRES_USER"],
        "PASSWORD": os.environ["POSTGRES_PASSWORD"],
        "HOST": os.environ.get("POSTGRES_HOST", "db"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        "TEST": {"NAME": f"test_enactus_{uuid4().hex}"},
    },
}

AUTH_USER_MODEL = "accounts.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": f"django.contrib.auth.password_validation.{validator}"}
    for validator in (
        "UserAttributeSimilarityValidator",
        "MinimumLengthValidator",
        "CommonPasswordValidator",
        "NumericPasswordValidator",
    )
]
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "login"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
PASSWORD_RESET_TIMEOUT = 3600
ACCOUNT_PUBLIC_ORIGIN = _local_account_origin()
ACCOUNT_TRUSTED_PROXY_NETWORKS = _trusted_account_proxies()

EMAIL_HOST = os.environ.get("SMTP_HOST", "").strip()
EMAIL_HOST_USER = os.environ.get("SMTP_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
EMAIL_USE_SSL = _env_flag("SMTP_USE_SSL", False)
EMAIL_USE_TLS = _env_flag("SMTP_USE_TLS", not EMAIL_USE_SSL)
EMAIL_PORT = _env_int("SMTP_PORT", 465 if EMAIL_USE_SSL else 587, 65535)
EMAIL_TIMEOUT = _env_int("SMTP_TIMEOUT", 10, 120)
SMTP_FROM_EMAIL = os.environ.get("SMTP_FROM_EMAIL", "").strip()
DEFAULT_FROM_EMAIL = SMTP_FROM_EMAIL or "enactus@localhost"
EMAIL_FILE_PATH = "/tmp/enactus-mail"
if EMAIL_HOST:
    if EMAIL_USE_TLS and EMAIL_USE_SSL:
        raise ImproperlyConfigured("SMTP_USE_TLS and SMTP_USE_SSL cannot both be enabled.")
    if bool(EMAIL_HOST_USER) != bool(EMAIL_HOST_PASSWORD):
        raise ImproperlyConfigured("Set both SMTP_USER and SMTP_PASSWORD for authenticated SMTP.")
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
else:
    EMAIL_BACKEND = "django.core.mail.backends.filebased.EmailBackend"
    if os.environ.get("DJANGO_SETTINGS_MODULE") == __name__:
        _prepare_local_mail_directory(EMAIL_FILE_PATH)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"account_tokens": {"()": "apps.accounts.logging.AccountTokenFilter"}},
    "formatters": {
        "django.server": {
            "()": "django.utils.log.ServerFormatter",
            "format": "[{server_time}] {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "filters": ["account_tokens"]},
        "django.server": {
            "class": "logging.StreamHandler",
            "formatter": "django.server",
            "filters": ["account_tokens"],
        },
    },
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "django.server": {"handlers": ["django.server"], "level": "INFO", "propagate": False},
    },
}

LANGUAGE_CODE = "ru"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
