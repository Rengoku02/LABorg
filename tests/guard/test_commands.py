from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.guard.models import DuplicateCheck

pytestmark = pytest.mark.django_db


def call(*args, **options):
    out = StringIO()
    call_command(*args, stdout=out, **options)
    return out.getvalue()


def test_guard_replay_then_guard_report(lab, requested):
    requested(by="raj", at=timezone.now() - timedelta(days=40))
    requested(at=timezone.now() - timedelta(days=20))
    requested(catalog="N/A", at=timezone.now() - timedelta(days=10))
    out = call("guard_replay", lab="test-lab", months=12)
    assert "Replayed 3 request(s) of the last 12 months" in out
    assert DuplicateCheck.objects.filter(replay=True).count() == 3
    report = call("guard_report", lab="test-lab")
    assert "T1/open" in report and "not yet" in report
    assert "Replay: 3 checked, 1 can't be checked (33%): 1 placeholder" in report
    assert "Baseline (all history): 3 compared or uncheckable, 1 can't be checked" in report
    assert "Replayed 0 request(s)" in call("guard_replay", lab="test-lab")
