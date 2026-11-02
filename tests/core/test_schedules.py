from datetime import UTC, datetime
from io import StringIO
from zoneinfo import ZoneInfo

import pytest
from django.core.management import call_command
from django_q.models import Schedule

from apps.core import schedules


@pytest.fixture
def registry(monkeypatch):
    """An empty provider registry for the test; the apps' own providers come back after."""
    monkeypatch.setattr(schedules, "_PROVIDERS", {})
    return schedules._PROVIDERS


def minutes(func, every):
    return {"func": func, "schedule_type": Schedule.MINUTES, "minutes": every}


def test_prefixes_must_end_with_a_dot_and_never_overlap(registry):
    schedules.register("guard.", dict)
    schedules.register("guard.", dict)  # re-registering the same prefix is fine
    with pytest.raises(ValueError, match="ends with a dot"):
        schedules.register("reports", dict)
    with pytest.raises(ValueError, match="overlaps"):
        schedules.register("guard.alerts.", dict)


@pytest.mark.django_db
def test_a_prefix_never_touches_another_prefixs_schedules(registry):
    wanted = {"guard.safety_net": minutes("apps.guard.tasks.safety_net", 10)}
    schedules.register("guard.", lambda: wanted)
    schedules.register("quartzy.", dict)  # wants nothing
    Schedule.objects.create(name="quartzy.stale", func="x", schedule_type=Schedule.MINUTES)
    Schedule.objects.create(name="other.keep", func="x", schedule_type=Schedule.MINUTES)
    result = schedules.ensure_all()
    assert result == {"guard.": (1, 0, 0), "quartzy.": (0, 0, 1)}
    names = set(Schedule.objects.values_list("name", flat=True))
    assert names == {"guard.safety_net", "other.keep"}
    assert schedules.ensure_all() == {"guard.": (0, 0, 0), "quartzy.": (0, 0, 0)}
    wanted["guard.safety_net"]["minutes"] = 5
    assert schedules.ensure_all()["guard."] == (0, 1, 0)


@pytest.mark.django_db
def test_sync_prefix_refuses_names_outside_it(registry):
    with pytest.raises(ValueError, match="not under"):
        schedules.sync_prefix("guard.", {"reports.tick": minutes("x", 60)})


@pytest.mark.django_db
def test_first_run_is_used_only_when_creating(registry):
    first = datetime(2030, 1, 1, 8, tzinfo=UTC)
    wanted = {"reports.tick": {**schedules.hourly("apps.reports.tasks.tick"), "first_run": first}}
    schedules.sync_prefix("reports.", wanted)
    tick = Schedule.objects.get(name="reports.tick")
    assert tick.next_run == first and tick.schedule_type == Schedule.HOURLY
    wanted["reports.tick"]["first_run"] = datetime(2031, 1, 1, tzinfo=UTC)
    assert schedules.sync_prefix("reports.", wanted) == (0, 0, 0)


@pytest.mark.django_db
def test_setup_schedules_reports_each_prefix(registry):
    schedules.register("guard.", lambda: {"guard.a": minutes("x", 10)})
    schedules.register("reports.", dict)
    call_command("setup_schedules", stdout=(out := StringIO()))
    lines = out.getvalue().splitlines()
    assert lines[0] == "guard. 1 created, 0 updated, 0 removed."
    assert lines[1] == "reports. 0 created, 0 updated, 0 removed."
    assert lines[2].endswith("Schedules: 1 created, 0 updated, 0 removed.")


def test_the_apps_register_their_providers():
    # Registered in AppConfig.ready(); later milestones add guard., inventory. and reports.
    assert "quartzy." in schedules.providers()


@pytest.mark.django_db
def test_lab_local_times(lab):
    lab.time_zone = "America/New_York"
    now = datetime(2026, 11, 2, 12, 30, tzinfo=UTC)  # 07:30 in New York
    assert schedules.local_now(lab, now).hour == 7
    assert schedules.next_local(lab, 8, now) == datetime(
        2026, 11, 2, 8, tzinfo=ZoneInfo("America/New_York")
    )
    assert schedules.next_local(lab, 3, now).day == 3  # 03:00 has passed: tomorrow
    lab.time_zone = "Not/AZone"
    assert schedules.local_now(lab, now).hour == 12  # an unknown zone reads as UTC
