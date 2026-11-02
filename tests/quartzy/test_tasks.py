"""Django-Q2 tasks (synchronous in tests: Q_CLUSTER sync=True), their cadence, monitoring and
the schedules."""

import logging
from datetime import UTC, datetime, timedelta
from io import StringIO
from zoneinfo import ZoneInfo

import httpx
import pytest
import time_machine
from django.core.management import call_command
from django.utils import timezone
from django_q.brokers import get_broker
from django_q.models import Schedule
from django_q.scheduler import scheduler
from django_q.tasks import async_task, result

from apps.core.models import Notification, User
from apps.quartzy import tasks
from apps.quartzy.models import (
    OrderRequest,
    QuartzyInventoryItem,
    QuartzyLab,
    QuartzyType,
    SpecSnapshot,
    SyncRun,
)

pytestmark = pytest.mark.django_db

NEW_YORK = ZoneInfo("America/New_York")


@pytest.fixture
def pings(monkeypatch, settings):
    """Record heartbeat pings (and anything else tasks.http_get would fetch)."""
    settings.LABSTOCK_HEARTBEAT_URLS = {
        "worker": "https://hc.test/worker",
        "sync": "https://hc.test/sync",
    }
    sent = []

    def fake_get(url, *, timeout):
        sent.append(url)
        return httpx.Response(200, request=httpx.Request("GET", url))

    monkeypatch.setattr(tasks, "http_get", fake_get)
    return sent


@pytest.fixture
def owner(db):
    return User.objects.create_superuser(email="root@lab.test", full_name="System Owner")


def runs(kind):
    return list(SyncRun.objects.filter(kind=kind).order_by("pk"))


def test_tasks_run_through_django_q(fake, fake_lab, quartzy_lab, use_fake):
    fake.add_request(fake_lab)
    task_id = async_task("apps.quartzy.tasks.run_head", quartzy_lab.pk)
    assert result(task_id) == "ok"
    [run] = runs("head")
    assert (run.status, run.new) == ("ok", 1) and run.calls == fake.total_calls
    assert OrderRequest.objects.count() == 1


def test_off_hours_head_runs_wait_for_the_off_hours_interval(lab, quartzy_lab, use_fake):
    lab.time_zone = "America/New_York"
    lab.save()
    night = datetime(2026, 10, 27, 23, 0, tzinfo=NEW_YORK)
    with time_machine.travel(night, tick=False):
        assert tasks.run_head(quartzy_lab.pk) == "ok"
    for minutes, expected in ((2, "not_due"), (8, "not_due"), (10, "ok"), (12, "not_due")):
        with time_machine.travel(night + timedelta(minutes=minutes), tick=False):
            assert tasks.run_head(quartzy_lab.pk) == expected
    assert len(runs("head")) == 2  # a skipped tick records nothing


def test_lab_hours_are_in_the_labs_time_zone(lab, quartzy_lab, use_fake):
    lab.time_zone = "America/New_York"
    lab.save()
    # 10:00 UTC is 06:00 in New York: before the lab's 07:00 start.
    morning = datetime(2026, 10, 27, 10, 0, tzinfo=UTC)
    with time_machine.travel(morning, tick=False):
        assert tasks.run_head(quartzy_lab.pk) == "ok"
    with time_machine.travel(morning + timedelta(minutes=2), tick=False):
        assert tasks.run_head(quartzy_lab.pk) == "not_due"
    working = datetime(2026, 10, 27, 9, 0, tzinfo=NEW_YORK)
    for minutes in (0, 2, 4):  # every tick of the 2-minute schedule
        with time_machine.travel(working + timedelta(minutes=minutes), tick=False):
            assert tasks.run_head(quartzy_lab.pk) == "ok"


def test_a_successful_head_run_pings_the_sync_heartbeat(quartzy_lab, use_fake, pings):
    assert tasks.run_head(quartzy_lab.pk) == "ok"
    assert pings == ["https://hc.test/sync"]
    quartzy_lab.refresh_from_db()
    assert quartzy_lab.last_synced_at == runs("head")[0].finished_at


def test_no_other_run_vouches_for_the_sync(fake, fake_lab, quartzy_lab, use_fake, pings):
    fake.add_request(fake_lab)
    assert tasks.run_sweep(quartzy_lab.pk) == "ok"
    assert tasks.run_hot(quartzy_lab.pk) == "ok"  # nothing hot: no call to Quartzy at all
    assert runs("hot")[0].calls == 0
    assert tasks.run_nightly(quartzy_lab.pk) == "types ok, inventory ok"
    quartzy_lab.refresh_from_db()
    assert pings == [] and quartzy_lab.last_synced_at is None


def test_a_catalog_re_resolution_never_pings(fake, fake_lab, quartzy_lab, api, use_fake, pings):
    fake.add_request(fake_lab, qtype="Antibodies")
    assert tasks.run_head(quartzy_lab.pk) == "ok"
    pings.clear()
    assert tasks.resolve_type(QuartzyType.objects.get().pk) == "ok"
    assert runs("resolve")[0].seen == 1 and pings == []


def test_a_head_run_with_no_call_to_quartzy_does_not_ping(
    quartzy_lab, use_fake, pings, monkeypatch
):
    monkeypatch.setattr(tasks.sync, "head_probe", lambda client, qlab, run: None)
    assert tasks.run_head(quartzy_lab.pk) == "ok"
    quartzy_lab.refresh_from_db()
    assert pings == [] and quartzy_lab.last_synced_at is None


def test_one_lab_falling_behind_keeps_the_sync_heartbeat_quiet(
    fake, lab, quartzy_lab, use_fake, pings
):
    other = QuartzyLab.objects.create(lab=lab, quartzy_id=fake.add_lab("Teaching")["id"])
    assert tasks.run_head(quartzy_lab.pk) == "ok"
    assert pings == []  # the other lab has never synced, so the switch must not be fed
    assert tasks.run_head(other.pk) == "ok"
    assert pings == ["https://hc.test/sync"]


def test_a_refused_token_emails_the_system_owners_once_a_day(
    fake, quartzy_lab, use_fake, pings, owner
):
    fake.fail_next(401, times=1000)  # the token was revoked
    day = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)
    with time_machine.travel(day):
        assert tasks.run_head(quartzy_lab.pk) == "failed"
        assert tasks.run_sweep(quartzy_lab.pk) == "failed"
    [alert] = Notification.objects.filter(kind=Notification.Kind.SYSTEM, recipient=owner)
    assert "refused" in alert.context["title"] and "401" in alert.context["message"]
    assert alert.period_key.endswith("2026-10-27")
    with time_machine.travel(day + timedelta(days=1)):
        assert tasks.run_head(quartzy_lab.pk) == "failed"
    assert Notification.objects.filter(kind=Notification.Kind.SYSTEM).count() == 2
    assert pings == []


def test_the_worker_heartbeat(pings, settings, monkeypatch):
    assert tasks.heartbeat() == "pinged" and pings == ["https://hc.test/worker"]

    def down(url, *, timeout):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(tasks, "http_get", down)
    assert tasks.heartbeat() == "failed"  # logged, never raised
    settings.LABSTOCK_HEARTBEAT_URLS = {"worker": "", "sync": ""}
    assert tasks.heartbeat() == "skipped"


def test_an_aborted_hot_run_does_not_ping(fake, fake_lab, quartzy_lab, use_fake, pings):
    requests = [fake.add_request(fake_lab) for _ in range(10)]
    assert tasks.run_sweep(quartzy_lab.pk) == "ok"
    an_hour_ago = timezone.now() - timedelta(hours=1)
    OrderRequest.objects.update(
        hot_until=timezone.now() + timedelta(days=1), last_seen_at=an_hour_ago
    )
    for request in requests[:5]:
        fake.delete_request(request["id"])
    pings.clear()
    assert tasks.run_hot(quartzy_lab.pk) == "aborted"
    assert pings == [] and not OrderRequest.objects.filter(deleted_in_quartzy_at__isnull=False)


def test_a_long_retry_after_backs_the_lab_off(fake, quartzy_lab, use_fake):
    fake.fail_next(429, retry_after=120)
    assert tasks.run_sweep(quartzy_lab.pk) == "failed"
    [run] = runs("sweep")
    assert "429" in run.note and fake.slept == []  # no waiting inside the task
    quartzy_lab.refresh_from_db()
    assert quartzy_lab.backoff_until > timezone.now() + timedelta(seconds=100)
    assert tasks.run_sweep(quartzy_lab.pk) == "backoff" and len(runs("sweep")) == 1
    with time_machine.travel(timezone.now() + timedelta(minutes=3)):
        assert tasks.run_sweep(quartzy_lab.pk) == "ok"


def test_a_refused_token_fails_the_run_loudly(fake, quartzy_lab, use_fake, pings, caplog):
    fake.fail_next(401, path="/order-requests")
    with caplog.at_level(logging.ERROR, logger="apps.quartzy.tasks"):
        assert tasks.run_sweep(quartzy_lab.pk) == "failed"
    assert any("401" in message for message in caplog.messages)
    assert pings == [] and "QuartzyAuthError" in runs("sweep")[0].note


def test_nightly_syncs_types_then_the_inventory_in_chunks(
    fake, fake_lab, quartzy_lab, use_fake, monkeypatch
):
    monkeypatch.setattr(tasks, "INVENTORY_PAGES_PER_TASK", 2)
    fake.page_size = 2
    fake.add_type(fake_lab, "Antibodies")
    items = [fake.add_inventory_item(fake_lab) for _ in range(5)]
    old = SyncRun.objects.create(kind="head", started_at=timezone.now() - timedelta(days=90))

    assert tasks.run_nightly(quartzy_lab.pk) == "types ok, inventory ok"
    assert QuartzyType.objects.get().name == "Antibodies"
    # Two pages per task: pages 1-2, then 3 and the empty page 4 that ends the pass.
    assert [run.pages for run in runs("inventory")] == [2, 2]
    assert set(QuartzyInventoryItem.objects.values_list("quartzy_id", flat=True)) == {
        item["id"] for item in items
    }
    quartzy_lab.refresh_from_db()
    assert quartzy_lab.inventory_cursor == 1
    assert not SyncRun.objects.filter(pk=old.pk).exists()  # pruned


def test_tasks_skip_inactive_labs_and_a_missing_token(quartzy_lab, use_fake, settings):
    assert tasks.run_sweep(quartzy_lab.pk + 1000) == "inactive"
    settings.QUARTZY_ACCESS_TOKEN = ""
    assert tasks.run_sweep(quartzy_lab.pk) == "no_token"
    quartzy_lab.is_active = False
    quartzy_lab.save()
    assert tasks.run_head(quartzy_lab.pk) == "inactive"
    assert tasks.run_nightly(quartzy_lab.pk) == "inactive"
    assert not SyncRun.objects.exists()


def schedule_names():
    return set(Schedule.objects.values_list("name", flat=True))


def test_setup_schedules_is_idempotent_and_follows_active_labs(lab, quartzy_lab):
    lab.time_zone = "America/New_York"
    lab.save()
    out = StringIO()
    call_command("setup_schedules", stdout=out)
    pk = quartzy_lab.pk
    jobs = ("head", "sweep", "hot", "nightly")
    assert schedule_names() == {
        "quartzy.heartbeat",
        "quartzy.spec_diff",
        *(f"quartzy.{job}.{pk}" for job in jobs),
    }
    head = Schedule.objects.get(name=f"quartzy.head.{pk}")
    assert (head.func, head.args, head.schedule_type, head.minutes, head.repeats) == (
        "apps.quartzy.tasks.run_head",
        str(pk),
        Schedule.MINUTES,
        2,
        -1,
    )
    nightly = Schedule.objects.get(name=f"quartzy.nightly.{pk}")
    assert nightly.schedule_type == Schedule.DAILY
    assert nightly.next_run.astimezone(NEW_YORK).hour == 3
    assert "6 created" in out.getvalue()

    call_command("setup_schedules", stdout=(out := StringIO()))
    assert "0 created, 0 updated, 0 removed" in out.getvalue() and Schedule.objects.count() == 6

    Schedule.objects.filter(name=f"quartzy.head.{pk}").update(minutes=7)  # edited by hand
    call_command("setup_schedules", stdout=(out := StringIO()))
    assert "1 updated" in out.getvalue()
    assert Schedule.objects.get(name=f"quartzy.head.{pk}").minutes == 2

    quartzy_lab.is_active = False
    quartzy_lab.save()
    call_command("setup_schedules", stdout=(out := StringIO()))
    assert "4 removed" in out.getvalue()
    assert schedule_names() == {"quartzy.heartbeat", "quartzy.spec_diff"}


def test_the_scheduler_dispatches_the_stored_schedules(
    fake, fake_lab, quartzy_lab, use_fake, pings
):
    fake.add_request(fake_lab)
    tasks.ensure_schedules()
    Schedule.objects.update(next_run=timezone.now() - timedelta(minutes=1))
    pings.clear()
    scheduler(broker=get_broker())  # runs every due schedule synchronously
    for kind in ("head", "sweep", "hot", "types", "inventory"):
        assert [run.status for run in runs(kind)] == ["ok"], kind
    assert OrderRequest.objects.count() == 1
    assert "https://hc.test/worker" in pings


def spec_response(content):
    def fake_get(url, *, timeout):
        assert url == tasks.SPEC_URL
        return httpx.Response(200, content=content, request=httpx.Request("GET", url))

    return fake_get


def test_spec_diff_compares_with_the_last_stored_spec(lab, monkeypatch):
    owner = User.objects.create_superuser(email="root@lab.test", full_name="System Owner")
    baseline = tasks.SPEC_BASELINE.read_bytes()
    monkeypatch.setattr(tasks, "http_get", spec_response(baseline))
    assert tasks.spec_diff() == "unchanged" and not SpecSnapshot.objects.exists()

    changed = baseline + b"\n# a new endpoint\n"
    monkeypatch.setattr(tasks, "http_get", spec_response(changed))
    assert tasks.spec_diff() == "changed"
    snapshot = SpecSnapshot.objects.get()
    assert snapshot.content.endswith("# a new endpoint\n")
    alert = Notification.objects.get(kind=Notification.Kind.SYSTEM, recipient=owner)
    assert alert.context["title"] == "Quartzy's API spec changed"

    assert tasks.spec_diff() == "unchanged"  # now compared with the stored copy
    assert SpecSnapshot.objects.count() == 1 and Notification.objects.count() == 1


def test_spec_diff_survives_an_unreachable_docs_site(monkeypatch):
    def down(url, *, timeout):
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(tasks, "http_get", down)
    assert tasks.spec_diff() == "unavailable"
