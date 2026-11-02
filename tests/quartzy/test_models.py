"""The mirror models' own rules: open/closed, outside purchases, tenancy helpers, and a Quartzy
lab's tenant following it."""

from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.db.models import RestrictedError
from django.utils import timezone

from apps.core.models import Lab, PersonAlias
from apps.quartzy.models import (
    OrderRequest,
    QuartzyInventoryItem,
    QuartzyLab,
    QuartzyType,
    SyncRun,
)

pytestmark = pytest.mark.django_db


def make_request(qlab, **fields):
    defaults = {
        "lab": qlab.lab,
        "quartzy_lab": qlab,
        "quartzy_id": f"q-{OrderRequest.objects.count()}",
    }
    return OrderRequest.objects.create(**{**defaults, **fields})


def test_open_means_neither_closed_nor_deleted(quartzy_lab):
    open_one = make_request(quartzy_lab, status="ORDERED")
    make_request(quartzy_lab, status="RECEIVED")
    make_request(quartzy_lab, status="CANCELLED")
    make_request(quartzy_lab, status="CREATED", deleted_in_quartzy_at=timezone.now())
    assert list(OrderRequest.objects.open()) == [open_one] and open_one.is_open
    assert OrderRequest.objects.live().count() == 3
    assert OrderRequest.objects.for_lab(quartzy_lab.lab).count() == 4


def test_outside_purchases_need_no_quartzy_id_but_mirrored_requests_do(lab, quartzy_lab):
    OrderRequest.objects.create(lab=lab, source="outside", item_name="FBS from a neighbour")
    OrderRequest.objects.create(lab=lab, source="gift", item_name="Plasmid from a collaborator")
    assert OrderRequest.objects.filter(quartzy_id__isnull=True).count() == 2
    with pytest.raises(IntegrityError), transaction.atomic():
        OrderRequest.objects.create(lab=lab, source="quartzy", item_name="no id")
    make_request(quartzy_lab, quartzy_id="dup")
    with pytest.raises(IntegrityError), transaction.atomic():
        make_request(quartzy_lab, quartzy_id="dup")


def test_requester_alias_falls_back_to_created_by(quartzy_lab):
    creator = PersonAlias.objects.create(kind="quartzy_user_id", value="u-1")
    request = make_request(quartzy_lab, created_by_alias=creator)
    assert request.requester_alias == creator
    request.requested_by_alias = PersonAlias.objects.create(kind="quartzy_user_id", value="u-2")
    assert request.requester_alias == request.requested_by_alias


@pytest.mark.parametrize(("flag", "enabled"), [(None, True), (True, True), (False, False)])
def test_a_missing_uses_instances_counts_as_migrated(lab, flag, enabled):
    qlab = QuartzyLab.objects.create(lab=lab, quartzy_id="lab-1", uses_instances=flag)
    assert qlab.instances_enabled is enabled
    assert list(QuartzyLab.objects.for_lab(lab).active()) == [qlab]


def test_quartzy_type_ids_are_unique_per_lab(lab, quartzy_lab):
    other = QuartzyLab.objects.create(lab=lab, quartzy_id="lab-2")
    QuartzyType.objects.create(quartzy_lab=quartzy_lab, quartzy_id="t-1", name="Antibodies")
    QuartzyType.objects.create(quartzy_lab=other, quartzy_id="t-1", name="Antibodies")
    with pytest.raises(IntegrityError), transaction.atomic():
        QuartzyType.objects.create(quartzy_lab=quartzy_lab, quartzy_id="t-1")


def test_a_quartzy_lab_with_requests_cannot_be_deleted_by_itself(quartzy_lab):
    make_request(quartzy_lab)
    with pytest.raises(RestrictedError):
        quartzy_lab.delete()
    quartzy_lab.lab.delete()  # deleting the whole lab still cascades
    assert not OrderRequest.objects.exists()


def test_run_notes_accumulate_and_stay_bounded(quartzy_lab):
    run = SyncRun(kind=SyncRun.Kind.HEAD, quartzy_lab=quartzy_lab)
    run.add_note("first")
    run.add_note("second")
    assert run.note == "first\nsecond"
    run.add_note("x" * 5000)
    assert len(run.note) == 4000
    assert str(run).startswith("Head probe")


def test_run_notes_keep_one_line_per_count(quartzy_lab):
    run = SyncRun(kind=SyncRun.Kind.SWEEP, quartzy_lab=quartzy_lab)
    run.add_count("Stale payloads skipped")
    run.add_note("Skipped page 3: GET /order-requests returned 500")
    run.add_count("Stale payloads skipped", 2)
    assert run.note == (
        "Skipped page 3: GET /order-requests returned 500\nStale payloads skipped: 3"
    )


def test_stale_labs_have_no_successful_head_run_in_30_minutes(lab, quartzy_lab):
    now = timezone.now()
    assert list(QuartzyLab.objects.stale(now)) == [quartzy_lab]  # never synced
    for minutes, status in ((45, "ok"), (5, "failed")):
        moment = now - timedelta(minutes=minutes)
        SyncRun.objects.create(
            kind="head",
            quartzy_lab=quartzy_lab,
            status=status,
            started_at=moment,
            finished_at=moment,
        )
    assert list(QuartzyLab.objects.stale(now)) == [quartzy_lab]  # a failure doesn't count
    assert QuartzyLab.objects.with_last_head_ok().get().last_head_ok == now - timedelta(minutes=45)
    SyncRun.objects.create(kind="head", quartzy_lab=quartzy_lab, status="ok", finished_at=now)
    assert not QuartzyLab.objects.stale(now).exists()
    QuartzyLab.objects.create(lab=lab, quartzy_id="old-lab", is_active=False)
    assert not QuartzyLab.objects.stale(now).exists()  # inactive labs aren't expected to sync


def test_rows_of_a_quartzy_lab_that_is_not_mirrored_are_hidden(lab, quartzy_lab):
    ours = make_request(quartzy_lab, status="ORDERED")
    theirs_lab = QuartzyLab.objects.create(lab=lab, quartzy_id="collaborator", is_active=False)
    make_request(theirs_lab, status="ORDERED", item_name="their private order")
    outside = OrderRequest.objects.create(lab=lab, source="outside", item_name="FBS")
    QuartzyInventoryItem.objects.create(quartzy_lab=quartzy_lab, quartzy_id="i-1", name="ours")
    QuartzyInventoryItem.objects.create(quartzy_lab=theirs_lab, quartzy_id="i-2", name="theirs")
    assert set(OrderRequest.objects.for_lab(lab)) == {ours, outside}
    assert set(OrderRequest.objects.live()) == {ours, outside}
    assert set(OrderRequest.objects.for_lab(lab).open()) == {ours, outside}
    assert OrderRequest.objects.count() == 3  # hidden, not gone
    assert list(QuartzyInventoryItem.objects.for_lab(lab).values_list("name", flat=True)) == [
        "ours"
    ]
    assert list(QuartzyInventoryItem.objects.live().values_list("name", flat=True)) == ["ours"]
    theirs_lab.is_active = True
    theirs_lab.save()
    assert OrderRequest.objects.for_lab(lab).count() == 3


def test_requests_follow_their_quartzy_lab_to_another_lab(lab, quartzy_lab):
    request = make_request(quartzy_lab)
    outside = OrderRequest.objects.create(lab=lab, source="outside", item_name="FBS")
    jones = Lab.objects.create(name="Jones Lab", slug="jones")
    quartzy_lab.lab = jones
    quartzy_lab.save()
    request.refresh_from_db()
    outside.refresh_from_db()
    assert request.lab == jones and outside.lab == lab
    assert list(OrderRequest.objects.for_lab(jones)) == [request]
    assert list(OrderRequest.objects.for_lab(lab)) == [outside]
