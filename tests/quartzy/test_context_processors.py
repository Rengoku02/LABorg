"""The site-wide banner while the Quartzy sync is behind (plan §6 "Monitoring")."""

from datetime import timedelta

import pytest
import time_machine
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory
from django.utils import timezone

from apps.core.models import Lab, User
from apps.quartzy import context_processors
from apps.quartzy.models import QuartzyLab, SyncRun

pytestmark = pytest.mark.django_db

BANNER = "Quartzy sync is behind"


def head_ok(qlab, minutes_ago):
    moment = timezone.now() - timedelta(minutes=minutes_ago)
    SyncRun.objects.create(
        kind="head", quartzy_lab=qlab, status="ok", started_at=moment, finished_at=moment
    )


def test_members_see_the_banner_while_the_sync_is_behind(member_client, quartzy_lab, clear_cache):
    head_ok(quartzy_lab, 45)
    response = member_client.get("/")
    content = response.content.decode()
    assert BANNER in content and "Duplicate checks may be late." in content
    assert "last update 45\xa0minutes ago" in content
    assert "style=" not in content and content.count("<script") == 1  # strict CSP


def test_a_lab_that_never_synced_says_so(member_client, quartzy_lab, clear_cache):
    assert "Quartzy sync is behind (no update yet)" in member_client.get("/").content.decode()


def test_no_banner_while_the_sync_is_current(member_client, quartzy_lab, clear_cache):
    head_ok(quartzy_lab, 5)
    assert BANNER not in member_client.get("/").content.decode()


def test_no_banner_for_another_labs_sync_or_for_non_members(
    client, member_client, lab, quartzy_lab, clear_cache
):
    head_ok(quartzy_lab, 5)
    jones = Lab.objects.create(name="Jones Lab", slug="jones")
    QuartzyLab.objects.create(lab=jones, quartzy_id="jones-q")  # never synced
    assert BANNER not in member_client.get("/").content.decode()
    outsider = User.objects.create_user(email="visitor@elsewhere.test", full_name="Visitor")
    client.force_login(outsider)
    assert BANNER not in client.get("/").content.decode()
    request = RequestFactory().get("/")
    request.user, request.lab = AnonymousUser(), lab
    assert context_processors.quartzy(request) == {}


def test_the_check_is_one_query_then_cached_for_a_minute(
    lab, quartzy_lab, clear_cache, django_assert_num_queries
):
    head_ok(quartzy_lab, 45)
    with django_assert_num_queries(1):
        assert context_processors.sync_behind(lab) is not None
    head_ok(quartzy_lab, 0)  # caught up meanwhile
    with django_assert_num_queries(0):
        assert context_processors.sync_behind(lab) is not None
    with time_machine.travel(timezone.now() + timedelta(seconds=61)):
        assert context_processors.sync_behind(lab) is None
