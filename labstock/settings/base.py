"""Settings shared by every environment. Environment-specific modules import from here."""

import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from django.utils.csp import CSP

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def env(name, default=None, *, required=False):
    value = os.environ.get(name, default)
    if required and not value:
        raise ImproperlyConfigured(f"Environment variable {name} is required")
    return value


def env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name, default):
    raw = os.environ.get(name)
    return default if raw in (None, "") else int(raw)


def env_list(name, default=""):
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


SECRET_KEY = env("DJANGO_SECRET_KEY", required=True)
DEBUG = False
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS")

# The permanent public base URL (see plan §2 "Hostname"). Every emailed link and, later,
# every QR label is built from it, so it must not change once mail has gone out.
SITE_URL = env("LABSTOCK_SITE_URL", "http://localhost:8000").rstrip("/")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "allauth",
    "allauth.account",
    "allauth.mfa",
    "django_q",
    "simple_history",
    "treebeard",
    "apps.core",
    "apps.catalog",
    "apps.quartzy",
    "apps.inventory",
    "apps.guard",
    "apps.bench",
    "apps.reports",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "simple_history.middleware.HistoryRequestMiddleware",
    "apps.core.middleware.CurrentLabMiddleware",
    "apps.core.middleware.StaffMFARequiredMiddleware",
]

ROOT_URLCONF = "labstock.urls"
WSGI_APPLICATION = "labstock.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.template.context_processors.csp",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.labstock",
                "apps.quartzy.context_processors.quartzy",
            ],
        },
    },
]

DATABASES = {
    "default": dj_database_url.config(
        env="DATABASE_URL",
        default="postgres://labstock:labstock@localhost:54329/labstock",
        conn_max_age=60,
        conn_health_checks=True,
    )
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# A database cache keeps allauth's rate-limit counters consistent across Gunicorn workers
# without adding Redis. Create the table with `manage.py createcachetable`.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "labstock_cache",
    }
}

AUTH_USER_MODEL = "core.User"
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Accounts are provisioned from the lab roster; nobody can sign up (plan §12).
# Members sign in with an emailed code; there are no member passwords.
ACCOUNT_ADAPTER = "apps.core.adapters.LabAccountAdapter"
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*"]
ACCOUNT_USER_MODEL_USERNAME_FIELD = None
ACCOUNT_EMAIL_VERIFICATION = "none"
ACCOUNT_LOGIN_BY_CODE_ENABLED = True
ACCOUNT_SESSION_REMEMBER = True
ACCOUNT_EMAIL_SUBJECT_PREFIX = "[LabStock] "
ACCOUNT_LOGOUT_REDIRECT_URL = "/"
LOGIN_URL = "account_login"
LOGIN_REDIRECT_URL = "/"
SESSION_COOKIE_AGE = 60 * 60 * 24 * 90
MFA_SUPPORTED_TYPES = ["totp", "recovery_codes"]
MFA_TOTP_ISSUER = "LabStock"
ALLAUTH_TRUSTED_PROXY_COUNT = env_int("ALLAUTH_TRUSTED_PROXY_COUNT", 0)
LABSTOCK_REQUIRE_STAFF_MFA = env_bool("LABSTOCK_REQUIRE_STAFF_MFA", True)

LANGUAGE_CODE = "en-us"
TIME_ZONE = env("LABSTOCK_TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_URL = "/media/"
MEDIA_ROOT = Path(env("LABSTOCK_MEDIA_ROOT", str(BASE_DIR / "media")))
# Box-lid photos. Media is never served publicly; a login-checked view streams it.
LABSTOCK_MAX_PHOTO_BYTES = env_int("LABSTOCK_MAX_PHOTO_MB", 10) * 1024 * 1024
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# Strict CSP everywhere, including dev and tests (plan §6): scripts only from our origin
# or with the per-request nonce, no eval, no inline styles.
SECURE_CSP = {
    "default-src": [CSP.SELF],
    "script-src": [CSP.SELF, CSP.NONCE],
    "style-src": [CSP.SELF],
    "img-src": [CSP.SELF, "data:"],
    "connect-src": [CSP.SELF],
    "font-src": [CSP.SELF],
    "object-src": [CSP.NONE],
    "base-uri": [CSP.SELF],
    "form-action": [CSP.SELF],
    "frame-ancestors": [CSP.NONE],
}
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# Email: Django 6.1 MAILERS (EMAIL_* settings are deprecated). Environments override this.
MAILERS = {"default": {"BACKEND": "django.core.mail.backends.console.EmailBackend"}}
# Two senders keep action-needed mail separate from digests (plan §2 "Email").
LABSTOCK_ALERTS_FROM = env("LABSTOCK_ALERTS_FROM", "LabStock <alerts@labstock.invalid>")
LABSTOCK_NEWS_FROM = env("LABSTOCK_NEWS_FROM", "LabStock <news@labstock.invalid>")
DEFAULT_FROM_EMAIL = LABSTOCK_ALERTS_FROM
SERVER_EMAIL = LABSTOCK_ALERTS_FROM

# Django-Q2: timeout must stay below retry, or a slow task is handed out twice (plan §6).
# Task arguments are ids only; Q2 stores them in an admin-visible table.
Q_CLUSTER = {
    "name": "labstock",
    "orm": "default",
    "workers": 2,
    "timeout": 600,
    "retry": 900,
    "max_attempts": 2,
    "ack_failures": True,
    "catch_up": False,
    "save_limit": 1000,
}

# Quartzy (read-only mirror). The page-base, insertion end and money unit are measured by
# scripts/quartzy_probe.py in Phase 0 and then pinned here through the environment.
QUARTZY_API_BASE = env("QUARTZY_API_BASE", "https://api.quartzy.com")
QUARTZY_ACCESS_TOKEN = env("QUARTZY_ACCESS_TOKEN", "")
QUARTZY_PAGE_BASE = env_int("QUARTZY_PAGE_BASE", 1)
QUARTZY_NEW_ITEMS_AT = env("QUARTZY_NEW_ITEMS_AT", "unknown")  # first_page | last_page | unknown
QUARTZY_MONEY_UNIT = env("QUARTZY_MONEY_UNIT", "auto")  # minor | major | auto
QUARTZY_HTTP_TIMEOUT = env_int("QUARTZY_HTTP_TIMEOUT", 20)
# The Quartzy labs to mirror when the sync first finds them; any other lab the token sees
# (a collaborator's, say) is recorded but left inactive. Empty: only a lone first lab is.
QUARTZY_LAB_IDS = env_list("QUARTZY_LAB_IDS")

# Heartbeat URLs for the external dead-man's switch (healthchecks.io or self-hosted).
LABSTOCK_HEARTBEAT_URLS = {
    "worker": env("LABSTOCK_HEARTBEAT_WORKER_URL", ""),
    "sync": env("LABSTOCK_HEARTBEAT_SYNC_URL", ""),
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"redact_tokens": {"()": "apps.core.logging.RedactActionTokens"}},
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "plain",
            "filters": ["redact_tokens"],
        }
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django.db.backends": {"level": "WARNING"},
        "httpx": {"level": "WARNING"},
    },
}
