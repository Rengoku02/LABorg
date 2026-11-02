"""The production settings pass Django's deployment checks (plan §13 "Ops")."""

import os
import secrets
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_check_deploy_passes_under_production_settings():
    env = {
        **{key: value for key, value in os.environ.items() if key != "DJANGO_ALLOW_ASYNC_UNSAFE"},
        "DJANGO_SETTINGS_MODULE": "labstock.settings.prod",
        "DJANGO_SECRET_KEY": secrets.token_urlsafe(50),
        "DJANGO_ALLOWED_HOSTS": "labstock.example.edu",
        "LABSTOCK_SITE_URL": "https://labstock.example.edu",
        "LABSTOCK_MAIL_API_KEY": "dummy",
        "LABSTOCK_SENTRY_DSN": "",
    }
    result = subprocess.run(
        [sys.executable, "manage.py", "check", "--deploy", "--fail-level", "WARNING"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
