"""The checks pipeline (design §7) in shadow mode: statuses, arming, late, suppression,
idempotency, and that nothing is ever sent."""

import threading
from datetime import timedelta

import pytest
from django.db import connection
from django.utils import timezone

from apps.catalog.models import GenericCatalogKey
from apps.core.models import Notification
from apps.guard import checks, matcher
from apps.guard.checks import check_request
from apps.guard.models import CheckRun, DuplicateCheck, DuplicateMatch, PairSuppression
from apps.quartzy.models import OrderRequest

pytestmark = pytest.mark.django_db
Status = DuplicateCheck.Status


def ago(**delta):
    return timezone.now() - timedelta(**delta)


def test_an_armed_request_with_a_match_is_recorded_in_shadow(requested, armed):
    requested(by="raj", at=ago(days=2))
    mine = requested(at=ago(minutes=5))
    check = check_request(mine.pk)
    assert (check.status, check.shadow, check.best_tier, check.late) == (
        Status.SHADOW_ONLY,
        True,
        "T1",
        False,
    )
    assert check.quartzy_native_would_fire_raw and check.quartzy_native_would_fire_norm
    (match,) = check.matches.all()
    assert (match.tier, match.candidate_kind, match.suppressed) == ("T1", "open", False)
    assert match.candidate_snapshot["requester"] == "Raj Rao"
    assert not Notification.objects.exists()  # shadow mode never mails anyone


def test_the_native_warning_only_fires_for_the_same_number_as_typed(requested, armed):
    requested(by="raj", vendor="Gibco", catalog="11965-092", qtype="Cell Culture", at=ago(days=2))
    mine = requested(vendor="Gibco", catalog="11965092", qtype="Cell Culture")
    check = check_request(mine.pk)
    assert check.status == Status.SHADOW_ONLY
    assert not check.quartzy_native_would_fire_raw  # Quartzy misses the formatting variant
    assert check.quartzy_native_would_fire_norm


def test_before_arming_a_match_is_recorded_as_unarmed(requested, quartzy_lab):
    requested(by="raj", at=ago(days=2))
    check = check_request(requested().pk)
    assert check.status == Status.UNARMED and check.matches.count() == 1


def test_a_request_seen_hours_after_it_was_made_is_late(requested, armed):
    requested(by="raj", at=ago(days=2))
    mine = requested(at=ago(hours=7))
    check = check_request(mine.pk)
    assert (check.status, check.late) == (Status.LATE, True)


def test_statuses_without_matches(requested, armed, item_types):
    assert check_request(requested().pk).status == Status.NO_MATCH
    placeholder = check_request(requested(catalog="TBD").pk)
    assert (placeholder.status, placeholder.uncheckable_reason) == (
        Status.UNCHECKABLE,
        "placeholder",
    )
    buffers = requested(qtype="Buffers", vendor="Sigma", catalog="P4417", name="PBS tablets")
    assert check_request(buffers.pk).status == Status.OUT_OF_SCOPE


def test_suppressed_by_the_requesters_not_the_same_answer(requested, armed, member):
    raj_req = requested(by="raj", at=ago(days=2))
    mine = requested()
    PairSuppression.objects.create(
        lab=mine.lab,
        subject_product=mine.product,
        candidate_product=raj_req.product,
        scope="requester",
        requester=member,
        expires_at=timezone.now() + timedelta(days=30),
    )
    check = check_request(mine.pk)
    assert check.status == Status.SUPPRESSED
    (match,) = check.matches.all()
    assert (match.suppressed, match.suppression_reason) == (True, "pair")


def test_recurring_purchases_are_suppressed_unless_two_are_open(requested, armed, policy):
    policy("guard.recurring_per_year", 3)
    for days in (200, 100):
        requested(by="raj", status="RECEIVED", at=ago(days=days))
    requested(by="sam", status="ORDERED", at=ago(days=1))
    check = check_request(requested().pk)
    kinds = {m.candidate_kind: (m.suppressed, m.suppression_reason) for m in check.matches.all()}
    assert kinds == {"bought": (True, "recurring"), "open": (False, "")}
    assert check.status == Status.SHADOW_ONLY  # the open one still counts


def test_checking_twice_makes_one_check(requested, armed):
    requested(by="raj", at=ago(days=2))
    mine = requested()
    run = CheckRun.objects.create(lab=mine.lab, via="manual")
    first = check_request(mine.pk, run=run)
    again = check_request(mine.pk, run=run)
    assert first.pk == again.pk and DuplicateMatch.objects.count() == 1
    run.refresh_from_db()
    assert run.checks_created == 1


def test_an_unresolved_request_is_checked_again_once_resolved(requested, armed):
    requested(by="raj", at=ago(days=2))
    mine = requested()
    OrderRequest.objects.filter(pk=mine.pk).update(resolution_status="unresolved")
    early = check_request(mine.pk)
    assert (early.status, early.uncheckable_reason) == (Status.UNCHECKABLE, "unresolved")
    assert check_request(mine.pk).pk == early.pk  # still unresolved: unchanged
    OrderRequest.objects.filter(pk=mine.pk).update(resolution_status="exact")
    redone = check_request(mine.pk)
    assert redone.pk == early.pk and redone.status == Status.SHADOW_ONLY
    assert redone.matches.count() == 1


def test_a_key_stop_listed_later_makes_the_next_check_uncheckable(requested, armed):
    requested(by="raj", at=ago(days=2))
    GenericCatalogKey.objects.create(catalog_key="2118S")
    check = check_request(requested().pk)
    assert (check.status, check.uncheckable_reason) == (Status.UNCHECKABLE, "generic")


def test_labels_carry_over_to_a_new_matcher_version(requested, armed, member, monkeypatch):
    requested(by="raj", at=ago(days=2))
    mine = requested()
    (match,) = check_request(mine.pk).matches.all()
    match.label, match.labelled_by, match.labelled_at = "true_dup", member, timezone.now()
    match.save()
    monkeypatch.setattr(checks, "MATCHER_VERSION", 2)
    monkeypatch.setattr(matcher, "MATCHER_VERSION", 2)
    newer = check_request(mine.pk)
    assert newer.matcher_version == 2
    (carried,) = newer.matches.all()
    assert (carried.label, carried.labelled_by) == ("true_dup", member)


@pytest.mark.django_db(transaction=True)
def test_concurrent_checks_of_one_request_make_one_check(requested, armed):
    requested(by="raj", at=ago(days=2))
    mine = requested()
    errors, barrier = [], threading.Barrier(3)

    def work():
        try:
            barrier.wait()
            check_request(mine.pk)
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=work) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert DuplicateCheck.objects.filter(order_request=mine).count() == 1
    assert DuplicateMatch.objects.count() == 1
