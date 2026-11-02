"""link_quartzy_inventory (design §3): the Quartzy inventory mirror becomes stock."""

from io import StringIO

import pytest
from django.core.management import call_command

from apps.inventory.models import ExternalRef, StockItem
from apps.inventory.quartzy_link import link_quartzy_inventory
from apps.quartzy import sync
from apps.quartzy.models import SyncRun
from tests.quartzy.conftest import fake, fake_lab, item_types, quartzy_lab  # noqa: F401

pytestmark = pytest.mark.django_db


@pytest.fixture
def mirrored(fake, fake_lab, quartzy_lab, member, item_types):  # noqa: F811
    """Two Quartzy inventory items mirrored the way the nightly sync does it."""
    priya = fake.add_user("Priya", "Patel", email=member.email)
    fake.add_inventory_item(
        fake_lab,
        qtype="Antibodies",
        added_by=priya,
        name="GAPDH (14C10) Rabbit mAb",
        vendor="CST",
        catalog_number="2118S",
        instances=[
            {"quantity": "2", "lot_number": "0012"},
            {"quantity": "30"},
            {"quantity": "250 mL"},
        ],
    )
    stranger = fake.add_user("Old", "Member", email="old@gone.test")
    fake.add_inventory_item(fake_lab, added_by=stranger, name="Mystery box")
    run = SyncRun.objects.create(kind=SyncRun.Kind.INVENTORY, quartzy_lab=quartzy_lab)
    for payload in fake.inventory_items.values():
        sync.upsert_inventory_item(quartzy_lab, payload, run=run)


def test_instances_become_unverified_stock(lab, mirrored, member, make_location, location_types):
    room = make_location("Room 101", "room")
    freezer = make_location("Freezer -80 A", "freezer", room, temperature="-80C")
    box = make_location("Box 1", "box", freezer)
    report = link_quartzy_inventory(lab, commit=True)
    assert report.items == 2 + 1 + 1 + 1
    gapdh = StockItem.objects.filter(product__name="GAPDH (14C10) Rabbit mAb")
    assert {item.custodian for item in gapdh} == {member}
    assert {item.lifecycle for item in gapdh} == {"unverified"}
    # Every instance of the fake's first item is in "Room 101 / Freezer -80 A / Box 1".
    assert set(gapdh.values_list("location", flat=True)) == {box.pk}
    assert gapdh.filter(lot__lot_raw="0012").count() == 2  # 2 containers: 2 items
    aggregate = gapdh.get(is_aggregate=True)
    assert aggregate.container_count == 30
    volume = gapdh.get(notes__startswith="Quartzy quantity")
    assert volume.notes == "Quartzy quantity: 250 mL" and not volume.is_aggregate
    mystery = StockItem.objects.get(product__name="Mystery box")
    assert mystery.custodian == lab.pool_user  # added by someone not in the lab
    assert mystery.location.is_unsorted  # LabStock has no "Box 2"
    assert ExternalRef.objects.filter(system="quartzy_instance").count() == 5


def test_linking_twice_changes_nothing(lab, mirrored):
    link_quartzy_inventory(lab, commit=True)
    again = link_quartzy_inventory(lab, commit=True)
    assert (again.items, again.skipped) == (0, 4)
    assert StockItem.objects.count() == 5


def test_the_command_is_a_dry_run_by_default(lab, mirrored):
    out = StringIO()
    call_command("link_quartzy_inventory", lab="test-lab", stdout=out)
    assert "Dry run: 5 stock item(s), 0 already linked" in out.getvalue()
    assert StockItem.objects.count() == 0
