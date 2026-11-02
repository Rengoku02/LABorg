"""``signals.requests_synced``: how the duplicate guard hears about the requests the mirror took
in. The signal's docstring is the contract; these tests hold the runs to it."""

import logging
from datetime import timedelta
from io import StringIO

import pytest
import time_machine
from django.core.management import call_command
from django.db import OperationalError
from django.utils import timezone

from apps.catalog.models import GenericCatalogKey, ItemType, SupplierProduct
from apps.quartzy import sync, tasks
from apps.quartzy.models import OrderRequest, QuartzyLab, QuartzyType, SyncRun
from apps.quartzy.signals import requests_synced

pytestmark = pytest.mark.django_db


@pytest.fixture
def heard():
    """What a connected receiver hears: one ``(qlab_id, request_ids, via)`` per send."""
    sends = []

    def receiver(sender, *, qlab_id, request_ids, via, **kwargs):
        sends.append((qlab_id, request_ids, via))

    requests_synced.connect(receiver)
    yield sends
    requests_synced.disconnect(receiver)


@pytest.fixture
def committed(django_capture_on_commit_callbacks):
    """``with committed():`` runs the block's on-commit callbacks when it ends, as a commit
    would; the test's own transaction never commits."""
    return lambda: django_capture_on_commit_callbacks(execute=True)


def pks(*requests):
    """The pks of these fake requests' mirrored rows, ascending."""
    ids = [request["id"] for request in requests]
    return sorted(OrderRequest.objects.filter(quartzy_id__in=ids).values_list("pk", flat=True))


def call(*args, **options):
    out = StringIO()
    call_command(*args, stdout=out, **options)
    return out.getvalue()


# ---------------------------------------------------------------------------------------
# The 24/7 runs


def test_each_run_announces_what_it_created_or_resolved_differently_once(
    fake, fake_lab, quartzy_lab, use_fake, heard, committed
):
    requests = [fake.add_request(fake_lab) for _ in range(3)]
    with committed():
        assert tasks.run_head(quartzy_lab.pk) == "ok"
    assert heard == [(quartzy_lab.pk, pks(*requests), "head")]

    # In Quartzy one line gets another catalog #, and another is only ordered: the sweep
    # resolves one of them differently.
    fake.update_request(requests[0]["id"], catalog_number="TV-9001")
    fake.update_request(requests[1]["id"], status="ORDERED")
    heard.clear()
    with committed():
        assert tasks.run_sweep(quartzy_lab.pk) == "ok"
    assert heard == [(quartzy_lab.pk, pks(requests[0]), "sweep")]

    # Someone watches the third, which then changes too: the hot set reads it by id.
    fake.update_request(requests[2]["id"], catalog_number="TV-9002")
    OrderRequest.objects.filter(quartzy_id=requests[2]["id"]).update(
        hot_until=timezone.now() + timedelta(days=1),
        last_seen_at=timezone.now() - timedelta(hours=1),
    )
    heard.clear()
    with committed():
        assert tasks.run_hot(quartzy_lab.pk) == "ok"
    assert heard == [(quartzy_lab.pk, pks(requests[2]), "hot")]


def test_a_run_that_finds_nothing_new_announces_nothing(
    fake, fake_lab, quartzy_lab, use_fake, heard, committed
):
    fake.add_request(fake_lab)
    with committed():
        assert tasks.run_head(quartzy_lab.pk) == "ok"
    heard.clear()
    with time_machine.travel(timezone.now() + timedelta(minutes=15)), committed():
        assert tasks.run_head(quartzy_lab.pk) == "ok"
        assert tasks.run_sweep(quartzy_lab.pk) == "ok"
    assert SyncRun.objects.filter(kind="head").count() == 2 and heard == []


def test_a_run_that_fails_part_way_announces_what_it_saved(
    fake, fake_lab, quartzy_lab, use_fake, heard, committed, settings
):
    settings.QUARTZY_NEW_ITEMS_AT = "first_page"
    fake.page_size = 2
    requests = [fake.add_request(fake_lab) for _ in range(4)]
    fake.after_page(lambda: fake.fail_next(503, times=10, path="/order-requests"), page=1)
    with committed():
        assert tasks.run_head(quartzy_lab.pk) == "failed"
    # Newest first: page 1 held the two newest, and they are saved.
    assert heard == [(quartzy_lab.pk, pks(*requests[2:]), "head")]


@pytest.mark.django_db(transaction=True)
def test_a_receiver_that_raises_is_logged_and_changes_nothing(
    fake, fake_lab, quartzy_lab, use_fake, heard, caplog
):
    def broken(sender, **kwargs):
        raise RuntimeError("the guard's queue is down")

    fake.add_request(fake_lab)
    requests_synced.connect(broken)
    try:
        # No test transaction here: the signal is sent on the spot, inside the run.
        with caplog.at_level(logging.ERROR):
            assert tasks.run_head(quartzy_lab.pk) == "ok"
    finally:
        requests_synced.disconnect(broken)
    run = SyncRun.objects.get(kind="head")
    assert (run.status, run.new, run.errors) == ("ok", 1, 0)
    assert OrderRequest.objects.count() == 1
    assert len(heard) == 1  # the other receivers still hear it
    assert any("the guard's queue is down" in message for message in caplog.messages)


# ---------------------------------------------------------------------------------------
# What is never announced


def test_the_backfill_announces_nothing(
    fake, fake_lab, lab, api, use_fake, item_types, heard, committed
):
    fake.add_type(fake_lab, "Antibodies")
    for _ in range(3):
        fake.add_request(fake_lab, qtype="Antibodies")
    with committed():
        sync.full_backfill(api, lab)
        call("quartzy_sync", lab="test-lab", backfill=True)
    assert OrderRequest.objects.count() == 3 and heard == []


@pytest.fixture
def sequencing(fake, fake_lab, lab, api, item_types):
    """Three generic requests: "Sequencing Reagents" is suggested as a service."""
    fake.add_type(fake_lab, "Sequencing Reagents")
    for n in range(3):
        fake.add_request(
            fake_lab,
            qtype="Sequencing Reagents",
            vendor_name="Illumina",
            catalog_number="20024907",
            item_name="NextSeq 1000/2000 P2 Reagents (300 Cycles)",
            requested_by=fake.add_user(f"P{n}", "Person"),
        )
    sync.full_backfill(api, lab)
    assert set(OrderRequest.objects.values_list("resolution_status", flat=True)) == {"generic"}
    return QuartzyType.objects.get()


def map_type(qtype, key):
    qtype.item_type = ItemType.objects.get(key=key)
    qtype.save()


def test_a_mapping_change_announces_the_requests_resolved_differently(
    sequencing, heard, committed, monkeypatch
):
    monkeypatch.setattr(tasks, "RESOLVE_PER_TASK", 2)
    ids = sorted(OrderRequest.objects.values_list("pk", flat=True))
    with committed():
        map_type(sequencing, "kit")
    qlab = sequencing.quartzy_lab_id
    # One send per resolve_type chunk, each with the ids it resolved differently.
    assert heard == [(qlab, ids[:2], "resolve"), (qlab, ids[2:], "resolve")]


def test_a_re_resolution_that_changes_nothing_announces_nothing(sequencing, heard, committed):
    with committed():
        map_type(sequencing, "oligo")  # identifies nothing either: the same generic lines
    assert SyncRun.objects.filter(kind=SyncRun.Kind.RESOLVE).exists() and heard == []


def test_a_failed_re_resolution_that_moves_a_request_announces_it(
    sequencing, heard, committed, monkeypatch
):
    # A line an overlapping re-resolution once left "generic" without rows. Resolving it again
    # fails, so it is "unresolved" now (tried again daily): another resolution. The others
    # keep theirs.
    broken = OrderRequest.objects.order_by("pk").first()
    OrderRequest.objects.filter(pk=broken.pk).update(supplier_product=None, product=None)

    def down(**kwargs):
        raise OperationalError("deadlock detected")

    monkeypatch.setattr(sync, "resolve_supplier_product", down)
    with committed():
        call("quartzy_resolve", "--all", lab="test-lab")
    statuses = dict(OrderRequest.objects.values_list("pk", "resolution_status"))
    assert statuses.pop(broken.pk) == "unresolved" and set(statuses.values()) == {"generic"}
    assert heard == [(sequencing.quartzy_lab_id, [broken.pk], "resolve")]


def test_requests_deleted_in_quartzy_or_of_an_inactive_lab_are_not_announced(
    sequencing, heard, committed
):
    first, *rest = OrderRequest.objects.order_by("pk")
    OrderRequest.objects.filter(pk=first.pk).update(deleted_in_quartzy_at=timezone.now())
    with committed():
        map_type(sequencing, "kit")
    assert heard == [(sequencing.quartzy_lab_id, [row.pk for row in rest], "resolve")]

    # The Quartzy lab is switched off (say, a collaborator's mirrored by mistake): its rows are
    # still resolved again, but hidden, so nothing is announced.
    heard.clear()
    QuartzyLab.objects.filter(pk=sequencing.quartzy_lab_id).update(is_active=False)
    with committed():
        map_type(sequencing, "service_fee")
    assert set(OrderRequest.objects.values_list("resolution_status", flat=True)) == {"generic"}
    assert heard == []


@pytest.fixture
def grouped_oligos(fake, fake_lab, quartzy_lab, api, run):
    """Five custom oligos on IDT's shared catalog # 51-01-01-01, grouped as one product."""
    for n in range(5):
        request = fake.add_request(
            fake_lab, vendor_name="IDT", catalog_number="51-01-01-01", item_name=f"primer {n}"
        )
        sync.upsert_order_request(
            quartzy_lab, api.order_request(request["id"]), via="poll", run=run
        )
    listing = SupplierProduct.objects.get()
    # The catalog fix: the number is one that identifies nothing.
    GenericCatalogKey.objects.create(vendor_family=listing.vendor_family, catalog_key="51-01-01-01")
    return listing


def test_quartzy_resolve_announces_once_per_run_and_a_dry_run_never(
    grouped_oligos, quartzy_lab, heard, committed, monkeypatch
):
    monkeypatch.setattr("apps.quartzy.management.commands.quartzy_resolve.BATCH", 2)
    with committed() as callbacks:
        out = call("quartzy_resolve", "--all", "--dry-run", lab="test-lab")
    assert "5 of 5 requests, 5 changed" in out and callbacks == [] and heard == []

    with committed():
        call("quartzy_resolve", "--all", lab="test-lab")
    ids = sorted(OrderRequest.objects.values_list("pk", flat=True))
    assert heard == [(quartzy_lab.pk, ids, "resolve")]
