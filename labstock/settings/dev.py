import os

os.environ.setdefault("DJANGO_SECRET_KEY", "dev-only-insecure-key-do-not-use-in-production")

from .base import *  # noqa: F403
from .base import env, env_bool, env_list

DEBUG = True
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
LABSTOCK_REQUIRE_STAFF_MFA = env_bool("LABSTOCK_REQUIRE_STAFF_MFA", False)

# Mailpit from compose.dev.yaml: http://localhost:8025
MAILERS = {
    "default": {
        "BACKEND": "django.core.mail.backends.smtp.EmailBackend",
        "OPTIONS": {"host": env("LABSTOCK_SMTP_HOST", "localhost"), "port": 1025},
    }
}
