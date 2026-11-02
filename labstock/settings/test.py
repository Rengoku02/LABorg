import os

os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-insecure-key")

from .base import *  # noqa: F403
from .base import DATABASES

DEBUG = False
ALLOWED_HOSTS = ["testserver", "localhost"]
SITE_URL = "https://labstock.test"
LABSTOCK_REQUIRE_STAFF_MFA = True

# Parallel test runs (e.g. several agents or CI shards) share one Postgres server, so each
# run can pick its own test database with LABSTOCK_TEST_DB_SUFFIX.
DATABASES["default"]["TEST"] = {
    "NAME": "test_labstock" + os.environ.get("LABSTOCK_TEST_DB_SUFFIX", "")
}

STATIC_ROOT = None  # WhiteNoise warns about a missing collectstatic dir otherwise
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
MAILERS = {"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
Q_CLUSTER = {**Q_CLUSTER, "sync": True}  # noqa: F405

QUARTZY_ACCESS_TOKEN = "test-token"
QUARTZY_API_BASE = "https://api.quartzy.test"
