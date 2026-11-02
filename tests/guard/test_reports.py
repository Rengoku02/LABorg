"""Precision per tier and the go-live gate (plan §9), and the uncheckable share."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.guard.checks import check_request
from apps.guard.models import DuplicateMatch
from apps.guard.reports import precision_report, uncheckable_share, wilson_low

pytestmark = pytest.mark.django_db


def label(match, value, user):
    match.label, match.labelled_by, match.labelled_at = value, user, timezone.now()
    match.save()


def test_precision_and_the_gate_follow_the_labels(lab, requested, armed, member, policy):
    policy("guard.go_live_min_labelled", 3)
    requested(by="raj", at=timezone.now() - timedelta(days=30))
    checks = []
    for days in (4, 3, 2, 1):
        checks.append(check_request(requested(at=timezone.now() - timedelta(days=days)).pk))
    matches = [check.matches.get(candidate_kind="open") for check in checks]
    for match, value in zip(matches, ["true_dup", "true_dup", "true_dup"], strict=False):
        label(match, value, member)
    rows = {row.name: row for row in precision_report(lab)}
    assert set(rows) == {"T1", "T1/open"}
    t1_open = rows["T1/open"]
    assert (t1_open.matches, t1_open.labelled, t1_open.true_dup) >= (4, 3, 3)
    assert t1_open.precision == 1.0 and t1_open.gate_ok
    label(matches[3], "false", member)
    t1 = {row.name: row for row in precision_report(lab)}["T1"]
    assert (t1.labelled, t1.precision, t1.gate_ok) == (4, 0.75, False)  # under 0.85


def test_a_pair_labelled_in_replay_and_live_counts_once(lab, requested, armed, member):
    requested(by="raj", at=timezone.now() - timedelta(days=3))
    mine = requested()
    live = check_request(mine.pk)
    label(live.matches.get(), "true_dup", member)
    replayed = check_request(mine.pk, replay=True)
    assert replayed.matches.get().label == "true_dup"  # carried over
    (row,) = [r for r in precision_report(lab) if r.name == "T1"]
    assert (row.matches, row.labelled) == (2, 1)


def test_suppressed_matches_are_left_out_of_precision(lab, requested, armed, member, policy):
    policy("guard.recurring_per_year", 2)
    requested(by="raj", status="RECEIVED", at=timezone.now() - timedelta(days=100))
    check = check_request(requested().pk)
    assert DuplicateMatch.objects.get(duplicate_check=check).suppressed
    assert precision_report(lab) == []


def test_uncheckable_share_by_reason(lab, requested, armed):
    for catalog in ("2118S", "N/A", "TBD"):
        check_request(requested(catalog=catalog).pk)
    share = uncheckable_share(lab)
    assert (share["checked"], share["uncheckable"]) == (3, 2)
    assert share["by_reason"]["placeholder"] == 2 and share["share"] == pytest.approx(2 / 3)


def test_wilson_lower_bound():
    assert wilson_low(0, 0) is None
    assert 0.6 < wilson_low(28, 30) < 0.85  # 93% observed, but 30 labels can't promise more
    assert wilson_low(30, 30) > 0.88
