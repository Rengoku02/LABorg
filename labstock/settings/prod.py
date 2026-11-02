from .base import *  # noqa: F403
from .base import SITE_URL, env, env_int

DEBUG = False

# Behind Caddy: trust its X-Forwarded-Proto and exactly one proxy hop.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
ALLAUTH_TRUSTED_PROXY_COUNT = env_int("ALLAUTH_TRUSTED_PROXY_COUNT", 1)
CSRF_TRUSTED_ORIGINS = [SITE_URL]
ACCOUNT_DEFAULT_HTTP_PROTOCOL = "https"
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_SSL_REDIRECT = True
SECURE_REDIRECT_EXEMPT = [r"^healthz$"]  # container healthchecks call plain HTTP
SECURE_HSTS_SECONDS = env_int("LABSTOCK_HSTS_SECONDS", 60 * 60 * 24 * 30)
# The app usually lives on a university subdomain: we can't promise HTTPS for sibling
# subdomains or submit the parent domain to the preload list, so these stay off.
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SILENCED_SYSTEM_CHECKS = ["security.W005", "security.W021"]

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

# Mail goes out through an ESP's HTTPS API via django-anymail (works from any host).
# Example for Postmark: LABSTOCK_MAIL_BACKEND=anymail.backends.postmark.EmailBackend and
# LABSTOCK_MAIL_API_KEY=<server token>. The option name differs per ESP.
_mail_backend = env("LABSTOCK_MAIL_BACKEND", "anymail.backends.postmark.EmailBackend")
_mail_options = {}
if env("LABSTOCK_MAIL_API_KEY"):
    _option_name = env("LABSTOCK_MAIL_API_KEY_OPTION", "server_token")
    _mail_options[_option_name] = env("LABSTOCK_MAIL_API_KEY")
MAILERS = {"default": {"BACKEND": _mail_backend, "OPTIONS": _mail_options}}
