"""The raw Quartzy inventory mirror: items, instances, the end of the list, and deletion only on a
confirmed 404."""

import json
from datetime import UTC, date, datetime, timedelta

import pytest
import time_machine
from django.db import connection

from apps.quartzy import sync
from apps.quartzy.models import (
    QuartzyInventoryInstance,
    QuartzyInventoryItem,
    SyncError,
    SyncRun,
)
from tests.quartzy.fake_quartzy import PAST_END

pytestmark = pytest.mark.django_db

NOW = datetime(2026, 10, 27, 3, 0, tzinfo=UTC)


def inventory(api, qlab, **kwargs):
    run = SyncRun.objects.create(kind=SyncRun.Kind.INVENTORY, quartzy_lab=qlab)
    done = sync.sync_inventory(api, qlab, run, **kwargs)
    return run, done


def test_items_and_instances_are_mirrored(fake, fake_lab, quartzy_lab, api):
    item = fake.add_inventory_item(
        fake_lab,
        name="Anti-GFP antibody",
        vendor="Abcam",
        catalog_number="ab290",
        cas_number=None,
        unit_size="100 µL",
        instances=[
            {"lot_number": "GR3361", "expiration_date": "2027-03-31", "quantity": "2"},
            {"location": None, "expiration_date": "soon"},
        ],
    )
    run, done = inventory(api, quartzy_lab)
    assert done and (run.seen, run.new, run.errors) == (1, 1, 0)
    mirrored = QuartzyInventoryItem.objects.get()
    assert (mirrored.name, mirrored.vendor_name, mirrored.catalog_number) == (
        "Anti-GFP antibody",
        "Abcam",
        "ab290",
    )
    assert (mirrored.cas_number, mirrored.unit_size_raw, mirrored.quantity_raw) == (
        "",
        "100 µL",
        "1",
    )
    assert mirrored.raw_payload == item and mirrored.quartzy_lab == quartzy_lab
    first, second = mirrored.instances.order_by("pk")
    assert (first.lot_number, first.quantity_raw) == ("GR3361", "2")
    assert first.expiration_date == date(2027, 3, 31)
    assert first.location_path == "Room 101 / Freezer -80 A / Box 1"
    assert (second.location_path, second.expiration_date) == ("", None)
    assert quartzy_lab.inventory_cursor == 1  # back at the base, ready for the next pass


def test_a_used_up_instance_is_marked_not_deleted(fake, fake_lab, quartzy_lab, api):
    item = fake.add_inventory_item(fake_lab, instances=[{}, {}])
    inventory(api, quartzy_lab)
    used_up = item["instances"].pop()
    _run, _done = inventory(api, quartzy_lab)
    assert QuartzyInventoryInstance.objects.count() == 2
    gone = QuartzyInventoryInstance.objects.get(quartzy_id=used_up["id"])
    assert gone.deleted_in_quartzy_at is not None
    item["instances"].append(used_up)  # put back in Quartzy
    inventory(api, quartzy_lab)
    gone.refresh_from_db()
    assert gone.deleted_in_quartzy_at is None


def test_an_item_is_marked_deleted_only_after_its_own_404(fake, fake_lab, quartzy_lab, api):
    keep, moved, gone = (fake.add_inventory_item(fake_lab) for _ in range(3))
    with time_machine.travel(NOW, tick=False):
        inventory(api, quartzy_lab)
    # "moved" is no longer listed for this lab but still exists; "gone" was deleted.
    moved["lab"] = {"id": "another-lab", "name": "Elsewhere"}
    del fake.inventory_items[gone["id"]]
    fake.reset_calls()
    with time_machine.travel(NOW + timedelta(days=1), tick=False):
        run, done = inventory(api, quartzy_lab)
    assert done and fake.calls["/inventory-items/{id}"] == 3  # the two, then a canary
    states = dict(QuartzyInventoryItem.objects.values_list("quartzy_id", "deleted_in_quartzy_at"))
    assert states[gone["id"]] == NOW + timedelta(days=1)
    assert states[keep["id"]] is None and states[moved["id"]] is None
    assert run.status == SyncRun.Status.RUNNING and "Marked 1 inventory item" in run.note


def test_nul_characters_in_an_item_are_removed_before_it_is_stored(
    fake, fake_lab, quartzy_lab, api
):
    fake.add_inventory_item(
        fake_lab,
        name="Anti-GFP\x00 antibody",
        instances=[
            {
                "lot_number": "GR\x003361",
                "location": {"name": "Box\x00 1", "pathNames": ["Room 101", "Freezer\x00 A"]},
            }
        ],
    )
    fake.add_inventory_item(fake_lab)
    run, done = inventory(api, quartzy_lab)
    assert done and (run.new, run.errors) == (2, 0)
    item = QuartzyInventoryItem.objects.get(name="Anti-GFP antibody")
    instance = item.instances.get()
    assert instance.lot_number == "GR3361"
    assert instance.location_path == "Room 101 / Freezer A / Box 1"
    assert "\\u0000" not in json.dumps([item.raw_payload, instance.raw_payload])
    assert inventory(api, quartzy_lab)[0].changed == 0  # nothing new on the next pass


def test_a_database_error_on_one_item_mid_page_becomes_its_sync_error(
    fake, fake_lab, quartzy_lab, api, monkeypatch
):
    items = [fake.add_inventory_item(fake_lab) for _ in range(3)]
    doomed = items[1]
    real = sync._sync_instances

    def failing(item, instances, raw_instances, now):
        real(item, instances, raw_instances, now)
        if item.quartzy_id == doomed["id"]:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 / 0")

    monkeypatch.setattr(sync, "_sync_instances", failing)
    run, done = inventory(api, quartzy_lab)
    assert done and (run.seen, run.new, run.errors) == (3, 2, 1)
    assert set(QuartzyInventoryItem.objects.values_list("quartzy_id", flat=True)) == {
        items[0]["id"],
        items[2]["id"],
    }
    assert not QuartzyInventoryInstance.objects.filter(item__quartzy_id=doomed["id"]).exists()
    error = SyncError.objects.get()
    assert (error.kind, error.quartzy_id) == ("upsert", doomed["id"])
    assert "division by zero" in error.message


@pytest.mark.parametrize("past_end", PAST_END)
def test_inventory_runs_in_chunks_from_a_cursor(fake, fake_lab, quartzy_lab, api, past_end):
    fake.page_size, fake.past_end = 2, past_end
    items = [fake.add_inventory_item(fake_lab) for _ in range(5)]
    _run, done = inventory(api, quartzy_lab, max_pages=2)
    assert not done and quartzy_lab.inventory_cursor == 3
    assert QuartzyInventoryItem.objects.count() == 4
    run, done = inventory(api, quartzy_lab, max_pages=2)  # page 3, then page 4 past the end
    assert done and quartzy_lab.inventory_cursor == 1 and run.errors == 0
    assert set(QuartzyInventoryItem.objects.values_list("quartzy_id", flat=True)) == {
        item["id"] for item in items
    }


def test_one_item_quartzy_refuses_by_id_does_not_stop_the_others(fake, fake_lab, quartzy_lab, api):
    refused, gone, kept = (fake.add_inventory_item(fake_lab) for _ in range(3))
    with time_machine.travel(NOW, tick=False):
        inventory(api, quartzy_lab)
    for item in (refused, gone, kept):  # none listed any more; two still exist
        item["lab"] = {"id": "another-lab", "name": "Elsewhere"}
    del fake.inventory_items[gone["id"]]
    fake.add_inventory_item(fake_lab)  # the canary: listed by this very pass
    QuartzyInventoryItem.objects.filter(quartzy_id=refused["id"]).update(
        last_seen_at=NOW - timedelta(days=1)
    )
    fake.fail_next(403, path="/inventory-items/{id}")  # the oldest suspect, read first
    with time_machine.travel(NOW + timedelta(days=1)):
        run, done = inventory(api, quartzy_lab)
    states = dict(QuartzyInventoryItem.objects.values_list("quartzy_id", "deleted_in_quartzy_at"))
    assert done and states[gone["id"]] is not None
    assert states[refused["id"]] is None and states[kept["id"]] is None
    error = SyncError.objects.get()
    assert (error.kind, error.quartzy_id) == ("read", refused["id"]) and run.errors == 1
    refused_row = QuartzyInventoryItem.objects.get(quartzy_id=refused["id"])
    assert refused_row.by_id_checked_at is not None  # so the next pass reads it last
