import importlib.util

import pytest
from django.core.management import call_command

from apps.core.models import Lab, Membership, User

# The browser tests need the "e2e" dependency group (pytest-playwright); without it they
# aren't collected at all (CI's main job), so a plain `pytest` still runs everywhere.
collect_ignore = [] if importlib.util.find_spec("pytest_playwright") else ["e2e"]


@pytest.fixture
def lab(db):
    call_command("bootstrap_lab", name="Test Lab", slug="test-lab", verbosity=0)
    return Lab.objects.get(slug="test-lab")


@pytest.fixture
def make_member(lab):
    """Create an active member of the test lab: make_member("priya@x.edu", role="co_owner")."""
    counter = {"n": 0}

    def _make(email=None, *, full_name=None, role=Membership.Role.MEMBER, **membership):
        counter["n"] += 1
        email = email or f"member{counter['n']}@lab.test"
        user = User.objects.create_user(email=email, full_name=full_name or email.split("@")[0])
        Membership.objects.create(user=user, lab=lab, role=role, **membership)
        return user

    return _make


@pytest.fixture
def member(make_member):
    return make_member("priya@lab.test", full_name="Priya Patel")


@pytest.fixture
def member_client(client, member):
    client.force_login(member)
    return client
