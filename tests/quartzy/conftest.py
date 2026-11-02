"""Fixtures for the Quartzy mirror tests: a FakeQuartzy, a mirrored Quartzy lab, and the
wiring that makes tasks, commands and views talk to the fake.

test_client.py and test_probe.py define their own ``fake``; theirs wins in those modules.
"""

import time

import pytest
from allauth.mfa.models import Authenticator
from django.core.cache import cache
from django.db import connection

from apps.catalog.management.commands.seed_catalog import (
    ITEM_TYPES_YAML,
    item_type_values,
    load_yaml,
)
from apps.catalog.models import ItemType, seed_snapshot
from apps.core.models import User
from apps.quartzy import sync
from apps.quartzy.models import QuartzyLab, SyncRun
from tests.quartzy.fake_quartzy import FakeQuartzy


@pytest.fixture
def fake():
    return FakeQuartzy()


@pytest.fixture
def fake_lab(fake):
    return fake.add_lab("Smith Lab")


@pytest.fixture
def quartzy_lab(lab, fake_lab):
    return QuartzyLab.objects.create(lab=lab, quartzy_id=fake_lab["id"], name=fake_lab["name"])


@pytest.fixture
def api(fake):
    """A QuartzyClient wired to the fake; retry waits go to fake.slept."""
    with fake.client() as client:
        yield client


@pytest.fixture
def make_run(quartzy_lab):
    def _make(kind=SyncRun.Kind.HEAD, qlab=None):
        return SyncRun.objects.create(kind=kind, quartzy_lab=qlab or quartzy_lab)

    return _make


@pytest.fixture
def run(make_run):
    return make_run()


@pytest.fixture
def use_fake(fake, monkeypatch):
    """Point sync.client_from_settings (used by tasks, commands and views) at the fake.
    Returns the list of clients made, so a test can count them."""
    made = []

    def factory(**overrides):
        client = fake.client(**overrides)
        made.append(client)
        return client

    monkeypatch.setattr(sync, "client_from_settings", factory)
    return made


@pytest.fixture
def no_quartzy(monkeypatch):
    """Fail the test if anything builds a Quartzy client."""

    def refuse(**overrides):
        raise AssertionError("Quartzy was called")

    monkeypatch.setattr(sync, "client_from_settings", refuse)


@pytest.fixture
def lock_waited():
    """``assert lock_waited()``: wait until a connection to the test database waits for a lock,
    e.g. a thread's run blocked on a row another thread holds; False after ``timeout`` s."""

    def _wait(timeout=10):
        deadline = time.monotonic() + timeout
        with connection.cursor() as cursor:
            while time.monotonic() < deadline:
                cursor.execute(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
                if cursor.fetchone()[0]:
                    return True
                time.sleep(0.01)
        return False

    return _wait


@pytest.fixture
def clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def antibody_type(db):
    return ItemType.objects.create(
        key="antibody", label="Antibody", tier=1, quartzy_type_keywords=["antibod"]
    )


@pytest.fixture
def oligo_type(db):
    return ItemType.objects.create(
        key="oligo",
        label="Oligo",
        tier=3,
        identity_rule=ItemType.IdentityRule.NONE,
        quartzy_type_keywords=["oligo", "primer"],
    )


@pytest.fixture
def item_types(db):
    """The item types of item_types.yaml, as seed_catalog loads them (without the vendors)."""
    for key, values in item_type_values(load_yaml(ITEM_TYPES_YAML), ITEM_TYPES_YAML):
        ItemType.objects.create(key=key, **values, seeded=seed_snapshot(values))


@pytest.fixture
def staff(db):
    """Staff with TOTP set up, so the staff-MFA middleware lets them through."""
    user = User.objects.create_user(email="owner@lab.test", full_name="Sys Owner", is_staff=True)
    Authenticator.objects.create(user=user, type=Authenticator.Type.TOTP, data={})
    return user


@pytest.fixture
def admin_user_client(client, db):
    """A client logged in as a superuser with TOTP set up, for the admin."""
    user = User.objects.create_superuser(email="root@lab.test", full_name="Root")
    Authenticator.objects.create(user=user, type=Authenticator.Type.TOTP, data={})
    client.force_login(user)
    return client
