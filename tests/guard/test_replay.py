"""The replay over history agrees with the Phase 0 baseline (design §7 "Replay")."""

import json
from pathlib import Path

import pytest
from django.test import override_settings

from apps.guard import baseline
from apps.guard.matcher import uncheckable_reason
from apps.guard.models import DuplicateCheck
from apps.guard.records import request_record
from apps.guard.replay import replay
from apps.quartzy import sync
from apps.quartzy.models import OrderRequest, OrderStatusObservation, QuartzyLab, SyncRun

pytestmark = pytest.mark.django_db
SAMPLE = Path(__file__).resolve().parents[1] / "fixtures" / "baseline" / "export-sample"


@pytest.fixture
def history(lab, item_types):
    """The synthetic export mirrored into the lab, as a backfill writes it."""
    manifest = json.loads((SAMPLE / "manifest.json").read_text())
    labs = {row["id"]: row for row in json.loads((SAMPLE / "labs.json").read_text())}
    with override_settings(QUARTZY_MONEY_UNIT=manifest["probe_facts"]["money_unit"]):
        for quartzy_id in manifest["lab_ids"]:
            qlab = QuartzyLab.objects.create(
                lab=lab, quartzy_id=quartzy_id, name=labs[quartzy_id]["name"]
            )
            run = SyncRun.objects.create(kind=SyncRun.Kind.BACKFILL, quartzy_lab=qlab)
            for line in (SAMPLE / "order_requests.jsonl").read_text().splitlines():
                payload = json.loads(line)
                if payload["lab"]["id"] == quartzy_id:
                    sync.upsert_order_request(
                        qlab, payload, via=OrderStatusObservation.Via.BACKFILL, run=run
                    )
        yield manifest


def pairs_found(lab):
    """{(later quartzy id, earlier quartzy id): tier} of the replay's matches."""
    found = {}
    for check in DuplicateCheck.objects.filter(lab=lab, replay=True).select_related(
        "order_request"
    ):
        for match in check.matches.select_related("candidate_order_request"):
            other = match.candidate_order_request
            found[(check.order_request.quartzy_id, other.quartzy_id)] = match.tier
    return found


EXPORTED_AT = "2026-10-20T08:00:00+00:00"


def test_the_replay_finds_every_baseline_pair_it_should(lab, history):
    from datetime import datetime

    run = replay(lab, months=24, now=datetime.fromisoformat(EXPORTED_AT))
    assert run.requests == OrderRequest.objects.count()  # all within 24 months
    found = pairs_found(lab)
    result = baseline.find_clusters(baseline.load_requests(SAMPLE))
    rows = {row.quartzy_id: row for row in OrderRequest.objects.select_related("quartzy_type")}
    explained = []
    for pair in result.pairs:
        later = rows[pair.later.id]
        tier = found.get((pair.later.id, pair.earlier.id))
        if tier is not None:
            assert tier == pair.tier, (pair.later.catalog.raw, pair.bucket)
            continue
        # The guard compares only what it can: oligos are generic by type (never matched on
        # catalog #) and chemicals are out of scope. The baseline counts both.
        check = DuplicateCheck.objects.get(order_request=later, replay=True)
        assert (check.status, check.uncheckable_reason) in {
            ("uncheckable", "generic"),
            ("out_of_scope", ""),
        }, (pair.later.catalog.raw, pair.bucket)
        explained.append(later.quartzy_type.name)
    assert sorted(set(explained)) == ["Chemicals", "Oligos"]
    # The baseline keeps one pair per request (the earlier request that best explains it);
    # the guard lists every match. Beyond that, what only the guard finds is older
    # stock-in-a-box: received further back than the baseline's 90-day window, within the
    # antibody type's 24-month look-back.
    baseline_pairs = {(p.later.id, p.earlier.id) for p in result.pairs}
    paired_laters = {p.later.id for p in result.pairs}
    beyond_window = 0
    for later_id, earlier_id in set(found) - baseline_pairs:
        later, earlier = rows[later_id], rows[earlier_id]
        # The baseline leaves cancelled requests out; the replay checks them as they were
        # when made (a cancelled duplicate is one somebody caught).
        if later_id in paired_laters or later.status == "CANCELLED":
            continue
        assert earlier.status == "RECEIVED"
        assert (later.requested_at - earlier.requested_at).days > 90
        beyond_window += 1
    assert beyond_window > 0


def test_the_guard_and_the_baseline_agree_on_what_cant_be_checked(lab, history):
    for row in OrderRequest.objects.select_related(
        "quartzy_type__item_type", "supplier_product__vendor_family"
    ):
        expected = baseline._uncheckable_reason(request_record(row), frozenset())
        guard = uncheckable_reason(row)
        item_type = row.quartzy_type.item_type if row.quartzy_type else None
        if guard == "generic" and item_type is not None and item_type.identity_rule == "none":
            continue  # generic by type (oligos, services): the baseline knows no types
        assert guard == expected, (row.catalog_number_raw, row.vendor_name_raw)


def test_replaying_again_skips_what_was_replayed(lab, history):
    from datetime import datetime

    now = datetime.fromisoformat(EXPORTED_AT)
    first = replay(lab, months=24, now=now)
    second = replay(lab, months=24, now=now)
    assert first.requests > 0 and second.requests == 0
