"""How checks get run: the sync's signal, the chunked task, the safety net, the schedule."""

from datetime import timedelta

import pytest
from django.utils import timezone
from django_q.models import Schedule

from apps.core import schedules
from apps.guard import tasks
from apps.guard.models import CheckRun, DuplicateCheck
from apps.quartzy import tasks as quartzy_tasks
from apps.quartzy.models import OrderRequest

pytestmark = pytest.mark.django_db


@pytest.fixture
def committed(django_capture_on_commit_callbacks):
    return lambda: django_capture_on_commit_callbacks(execute=True)


def test_a_head_run_gets_its_new_requests_checked(
    fake, fake_lab, quartzy_lab, use_fake, people, item_types, armed, committed
):
    now = timezone.now()
    for who, minutes in (("raj", 30), ("priya", 1)):
        fake.add_request(
            fake_lab,
            qtype="Antibodies",
            requested_by=people[who],
            vendor_name="CST",
            catalog_number="2118S",
            item_name="GAPDH (14C10) Rabbit mAb",
            requested_at=(now - timedelta(minutes=minutes)).isoformat(),
        )
    with committed():
        assert quartzy_tasks.run_head(quartzy_lab.pk) == "ok"
    checks = DuplicateCheck.objects.order_by("order_request__requested_at")
    assert [check.status for check in checks] == ["no_match", "shadow_only"]
    (run,) = CheckRun.objects.all()
    assert (run.via, run.requests, run.finished_at is not None) == ("head", 2, True)


def test_check_requests_works_in_chunks_within_one_run(requested, armed, monkeypatch):
    monkeypatch.setattr(tasks, "CHECKS_PER_TASK", 2)
    ids = [requested(by="raj", catalog=f"21{n}S").pk for n in range(10, 15)]
    assert tasks.check_requests(ids, "head") == "2 checked, 3 queued"  # the rest ran inline
    (run,) = CheckRun.objects.all()
    assert run.requests == 5 and run.finished_at is not None
    assert DuplicateCheck.objects.filter(run=run).count() == 5


def test_the_safety_net_checks_what_the_signal_missed(requested, armed, quartzy_lab):
    missed = requested()
    old = requested(by="raj", catalog="2119S")
    OrderRequest.objects.filter(pk=old.pk).update(first_seen_at=timezone.now() - timedelta(days=3))
    assert tasks.safety_net() == "1 checked"
    assert list(DuplicateCheck.objects.values_list("order_request_id", flat=True)) == [missed.pk]
    assert CheckRun.objects.get().via == "safety_net"
    assert tasks.safety_net() == "0 checked"


def test_the_safety_net_skips_unarmed_requests(requested, quartzy_lab):
    requested()
    assert tasks.safety_net() == "0 checked"


def test_the_safety_net_redoes_checks_made_before_resolution(requested, armed):
    raj = requested(by="raj", at=timezone.now() - timedelta(days=1))
    mine = requested()
    OrderRequest.objects.filter(pk=mine.pk).update(resolution_status="unresolved")
    tasks.check_requests([raj.pk, mine.pk], "head")
    OrderRequest.objects.filter(pk=mine.pk).update(resolution_status="exact")
    assert tasks.safety_net() == "1 checked"
    assert DuplicateCheck.objects.get(order_request=mine).status == "shadow_only"


def test_the_guard_schedules_its_safety_net():
    assert "guard." in schedules.providers()
    schedules.ensure_all()
    net = Schedule.objects.get(name="guard.safety_net")
    assert (net.func, net.schedule_type, net.minutes) == (
        "apps.guard.tasks.safety_net",
        Schedule.MINUTES,
        10,
    )
