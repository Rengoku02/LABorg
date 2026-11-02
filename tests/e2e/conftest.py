"""Browser end-to-end tests (plan §13 "E2E"): Playwright against pytest-django's live server,
at phone size, with the strict CSP on, failing on any CSP violation.

    uv run --group e2e pytest -m e2e

They need the ``e2e`` dependency group and a Chromium: the one pinned Playwright downloads
(``uv run --group e2e playwright install chromium``), or LABSTOCK_CHROMIUM / the container's
/opt/pw-browsers/chromium.
"""

import os
from pathlib import Path

import pytest
from django.test import Client

from tests.bench.conftest import bought, make_item, qlab, qtypes  # noqa: F401
from tests.catalog.conftest import item_types  # noqa: F401
from tests.guard.conftest import (  # noqa: F401
    armed,
    fake,
    fake_lab,
    mirror,
    people,
    policy,
    quartzy_lab,
    requested,
    stock,
)
from tests.inventory.conftest import (  # noqa: F401
    box,
    freezer,
    location_types,
    make_location,
    raj,
    room,
    shelf,
)

CHROMIUM = os.environ.get("LABSTOCK_CHROMIUM", "/opt/pw-browsers/chromium")


@pytest.fixture(scope="session", autouse=True)
def _async_unsafe_ok():
    """Playwright's sync API runs an event loop, and the tests still use the database
    directly. Set before the browser starts (a session fixture, autouse: first), and only
    while browser tests run; tests/test_deploy_check.py strips it for its subprocess."""
    before = os.environ.get("DJANGO_ALLOW_ASYNC_UNSAFE")
    os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
    yield
    if before is None:
        os.environ.pop("DJANGO_ALLOW_ASYNC_UNSAFE", None)
    else:
        os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = before


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args):
    if Path(CHROMIUM).exists():
        return {**browser_type_launch_args, "executable_path": CHROMIUM}
    return browser_type_launch_args


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    return {
        **browser_context_args,
        "viewport": {"width": 390, "height": 844},
        "is_mobile": True,
        "has_touch": True,
    }


@pytest.fixture
def csp_violations(page):
    """Console messages about the Content Security Policy (the test asserts there are none)."""
    found = []
    page.on(
        "console",
        lambda message: (
            found.append(message.text) if "Content Security Policy" in message.text else None
        ),
    )
    return found


@pytest.fixture
def sign_in(page, live_server):
    """sign_in(user): the browser has that user's session (as allauth would give it)."""

    def _sign_in(user):
        client = Client()
        client.force_login(user)
        session = client.cookies["sessionid"].value
        page.context.add_cookies([{"name": "sessionid", "value": session, "url": live_server.url}])

    return _sign_in


@pytest.fixture
def local(live_server):
    """local(url): an email link (https://labstock.test/...) on the live server."""
    return lambda url: url.replace("https://labstock.test", live_server.url)
