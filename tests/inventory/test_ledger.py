"""The stock ledger: create_stock_item, apply_event, its transition table, idempotency,
stale versions, tenancy, who may hold stock, and the guards around the state fields."""

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.catalog.models import Lot, Product
from apps.catalog.services import merge_products
from apps.core.models import Membership
from apps.core.policies import set_policy
from apps.inventory import services
from apps.inventory.models import (
    STATE_FIELDS,
    LedgerError,
    Location,
    LocationType,
    StockEvent,
    StockItem,
    ledger_value,
)
from apps.inventory.presenters import present_item
from apps.inventory.selectors import active_stock
from apps.inventory.services import (
    CrossLab,
    IdempotencyConflict,
    InvalidCustodian,
    InvalidLocation,
    InvalidTransition,
    InventoryError,
    StaleVersion,
    apply_event,
    move_location,
)
from apps.quartzy.models import OrderRequest


@pytest.fixture
def other_box(make_location, freezer):
    return make_location("Box 5", "box", freezer)


def events_of(item):
    return list(StockEvent.objects.filter(stock_item=item).order_by("seq"))


# ---------------------------------------------------------------------------------------
# Creating stock


@pytest.mark.django_db
def test_create_writes_the_created_event(item, box, member):
    assert item.version == 1 and item.lifecycle == "available" and item.level == "ok"
    (event,) = events_of(item)
    assert (event.seq, event.kind, event.source) == (1, "created", "web")
    assert (event.to_location, event.to_custodian, event.user) == (box, member, member)
    assert event.state == StockItem.objects.get(pk=item.pk).ledger_state()
    assert set(event.state) == set(STATE_FIELDS)


@pytest.mark.django_db
def test_visibility_defaults_to_the_lab_policy(make_item, lab):
    assert make_item().visibility == "ask_first"
    set_policy("inventory.default_visibility", "shared", lab=lab)
    assert make_item().visibility == "shared"
    assert make_item(visibility="restricted").visibility == "restricted"
    set_policy("inventory.default_visibility", "everyone", lab=lab)  # not a visibility
    assert make_item().visibility == "ask_first"
    with pytest.raises(InventoryError, match="Unknown visibility"):
        make_item(visibility="public")


@pytest.mark.django_db
def test_mta_forces_restricted(make_item, lab):
    set_policy("inventory.default_visibility", "shared", lab=lab)
    assert make_item(mta=True, visibility="shared").visibility == "restricted"
    # A collaborator's gift without an MTA follows the default like anything else.
    assert make_item(source="collaborator").visibility == "shared"


@pytest.mark.django_db
def test_no_location_means_the_custodians_unsorted_container(make_item, member):
    frozen = make_item(location=None, temperature="-20C")
    assert frozen.location.is_unsorted and frozen.location.unsorted_owner == member
    assert frozen.location.temperature == "-20C"
    assert frozen.location.display_name == "Unsorted: Priya Patel, -20 °C"
    again = make_item(location=None, temperature="-20 °C")
    assert again.location == frozen.location
    unknown = make_item(location=None)
    assert unknown.location.display_name == "Unsorted: Priya Patel"
    with pytest.raises(InvalidLocation, match="Unknown temperature"):
        make_item(location=None, temperature="-70C")


@pytest.mark.django_db
def test_new_stock_follows_product_merges(make_item, product):
    duplicate = Product.objects.create(name="GAPDH rabbit mAb (dup)")
    merge_products(duplicate, product)
    assert make_item(product=duplicate).product == product


@pytest.mark.django_db
def test_listing_and_lot_must_be_of_the_product(make_item, supplier_product, product):
    other = Product.objects.create(name="Beta-actin")
    assert make_item(supplier_product=supplier_product).supplier_product == supplier_product
    with pytest.raises(InventoryError, match="listing of another product"):
        make_item(product=other, supplier_product=supplier_product)
    lot = Lot.objects.create(product=other, lot_raw="0012")
    with pytest.raises(InventoryError, match="another product"):
        make_item(lot=lot)


@pytest.mark.django_db
def test_initial_lifecycles(make_item):
    assert make_item(lifecycle="unverified").lifecycle == "unverified"
    opened = make_item(lifecycle="opened")
    assert opened.opened_at is not None
    when = timezone.now() - timedelta(days=30)
    assert make_item(lifecycle="unverified", opened_at=when).opened_at == when
    for lifecycle in ("empty", "discarded", "lost"):
        with pytest.raises(InvalidTransition):
            make_item(lifecycle=lifecycle)
    with pytest.raises(InvalidTransition, match="opened date"):
        make_item(lifecycle="available", opened_at=when)


@pytest.mark.django_db
def test_aggregates_need_a_container_count(make_item):
    aggregate = make_item(is_aggregate=True, container_count=4)
    assert aggregate.container_count == 4
    with pytest.raises(InventoryError, match="needs container_count"):
        make_item(is_aggregate=True)
    with pytest.raises(InventoryError, match="only for aggregates"):
        make_item(container_count=2)


@pytest.mark.django_db
def test_backdated_creation_and_positions(make_item):
    when = timezone.now() - timedelta(days=400)
    item = make_item(occurred_at=when, position_label="b3", quantity="250", unit="mL")
    event = item.events.get()
    assert event.occurred_at == when and event.recorded_at > when
    assert (item.position_label, item.position_index) == ("B3", 12)  # row B, column 3 of 9x9
    assert (item.quantity, event.qty_delta) == (Decimal("250"), Decimal("250"))
    free = make_item(position_label="  top shelf,   left ")
    assert (free.position_label, free.position_index) == ("top shelf, left", None)  # kept
    assert make_item(position_index=81).position_label == "I9"


@pytest.mark.django_db
def test_positions_must_exist_in_the_location(make_item, item, make_location, shelf):
    with pytest.raises(InventoryError, match="has no position 82"):
        make_item(position_index=82)  # a 9 x 9 box
    with pytest.raises(InventoryError, match="isn't position 5"):
        make_item(position_label="A1", position_index=5)
    assert make_item(position_label="a-5", position_index=5).position_label == "A5"
    with pytest.raises(InventoryError, match="has no position 1"):
        make_item(location=shelf, position_index=1)  # a shelf has no numbered positions
    assert make_item(location=shelf, position_label="left").position_label == "left"
    with pytest.raises(InventoryError, match="has no position 90"):
        apply_event(item, "moved", user=None, position_index=90)
    assert StockItem.objects.get(pk=item.pk).version == 1


@pytest.mark.django_db
def test_a_label_written_like_a_position_the_box_doesnt_have_is_refused(
    make_item, item, make_location, shelf
):
    # Box 4 is 9 x 9 (A-I, 1-9): a typo, or a 10 x 10 box registered as a 9 x 9 one.
    for label in ("J1", "a10", "Z99", "A0", "j-1"):
        with pytest.raises(InventoryError, match=f"Box 4 has no position {label}"):
            make_item(position_label=label)
        with pytest.raises(InventoryError, match=f"Box 4 has no position {label}"):
            apply_event(item, "moved", user=None, position_label=label)
    assert StockItem.objects.get(pk=item.pk).version == 1
    big = make_location("Box 10", "box_10x10", shelf)
    assert make_item(location=big, position_label="j10").position_index == 100
    with pytest.raises(InventoryError, match="Box 10 has no position K1"):
        make_item(location=big, position_label="K1")
    # Whatever isn't written like a position is a note of where it is, as before.
    for label in ("top shelf, left", "box 3, back", "AA1"):
        assert (make_item(position_label=label).position_index, label) == (None, label)
    assert make_item(location=shelf, position_label="J1").position_label == "J1"  # no scheme
    strip = LocationType.objects.create(
        key="strip", label="Strip", structural=False, position_scheme="linear", cols=8, prefix="S"
    )
    tubes = make_location("Strip 1", strip, shelf)
    assert make_item(location=tubes, position_label="s8").position_label == "S8"
    with pytest.raises(InventoryError, match="Strip 1 has no position S9"):
        make_item(location=tubes, position_label="S9")
    assert make_item(location=tubes, position_label="slot three").position_index is None


@pytest.mark.django_db
def test_quantities_are_kept_as_the_database_rounds_them(make_item):
    item = make_item(quantity="0.12345", unit="mL")
    assert item.quantity == Decimal("0.1235") and item.events.get().state["quantity"] == "0.1235"
    event = apply_event(item, "quantity_adjusted", user=None, quantity="2.00005")
    assert (item.quantity, event.qty_delta) == (Decimal("2.0001"), Decimal("1.8766"))
    assert StockItem.objects.get(pk=item.pk).quantity == Decimal("2.0001")
    assert services.ledger_drift(item) == {}
    zero = make_item(quantity="-0")
    assert str(zero.quantity) == "0.0000" and services.ledger_drift(zero) == {}
    for bad in ("-0.01", "lots", "NaN", "1E+20"):
        with pytest.raises(InventoryError, match="isn't a quantity"):
            make_item(quantity=bad)


@pytest.mark.django_db
def test_the_created_event_can_say_where_the_stock_came_from(make_item):
    item = make_item(reason="stock-sheet.csv line 4", data={"sheet_row": 4}, event_source="import")
    event = item.events.get()
    assert (event.reason, event.source, event.data["sheet_row"]) == (
        "stock-sheet.csv line 4",
        "import",
        4,
    )
    assert set(event.state) == set(STATE_FIELDS)
    with pytest.raises(InventoryError, match="reserved"):
        make_item(data={"state": {}})


# ---------------------------------------------------------------------------------------
# apply_event


@pytest.mark.django_db
def test_a_move_appends_an_event_and_updates_the_cache(item, box, other_box, member):
    event = apply_event(item, "moved", user=member, location=other_box, position_label="A2")
    assert (event.seq, event.kind, event.from_location, event.to_location) == (
        2,
        "moved",
        box,
        other_box,
    )
    assert event.state == {"location": other_box.pk, "position_label": "A2", "position_index": 2}
    assert (item.version, item.location, item.position_index) == (2, other_box, 2)
    fresh = StockItem.objects.get(pk=item.pk)
    assert (fresh.version, fresh.location_id, fresh.position_label) == (2, other_box.pk, "A2")
    # Moving again without a position clears the old one.
    apply_event(item, "moved", user=member, location=box)
    assert (item.position_label, item.position_index) == ("", None)
    # A position change inside the same box is a move too.
    apply_event(item, "moved", user=member, position_label="C1")
    assert (item.location, item.position_index) == (box, 19)
    with pytest.raises(InvalidTransition, match="Nothing to change"):
        apply_event(item, "moved", user=member, location=box, position_label="C1")
    assert services.ledger_drift(item) == {}


@pytest.mark.django_db
def test_an_id_can_be_passed_instead_of_the_item(item, other_box):
    apply_event(item.pk, "moved", user=None, location=other_box)
    assert StockItem.objects.get(pk=item.pk).location == other_box


def _lifecycle(make_item, lifecycle, *, level="ok"):
    """A fresh item that went through the ledger to ``lifecycle``."""
    item = make_item(lifecycle="unverified" if lifecycle == "unverified" else "available")
    if level == "low":
        apply_event(item, "marked_low", user=None)
    steps = {
        "opened": ["opened"],
        "empty": ["emptied"],
        "discarded": ["discarded"],
        "lost": ["lost"],
    }
    for kind in steps.get(lifecycle, []):
        apply_event(item, kind, user=None)
    assert item.lifecycle == lifecycle
    return item


def _changes(kind, item, *, raj, other_box):
    return {
        "moved": {"location": other_box},
        "custody_changed": {"custodian": raj},
        "visibility_changed": {
            "visibility": "shared" if item.visibility != "shared" else "restricted"
        },
        "quantity_adjusted": {"quantity": 3},
        "note": {"reason": "checked the label"},
    }.get(kind, {})


# Which kinds of event may happen in which lifecycle (the design's table, written out).
ALL = {"unverified", "available", "opened", "empty", "discarded", "lost"}
ACTIVE = {"unverified", "available", "opened"}
ALLOWED = {
    "verified": {"unverified"},
    "moved": ACTIVE | {"empty"},
    "opened": {"unverified", "available"},
    "marked_low": ACTIVE,
    "marked_ok": ACTIVE,
    "emptied": ACTIVE,
    "discarded": ALL - {"discarded"},
    "lost": ACTIVE | {"empty"},
    "found": {"lost"},
    "custody_changed": ALL - {"discarded"},
    "visibility_changed": ALL - {"discarded"},
    "quantity_adjusted": ACTIVE,
    "split": ACTIVE,
    "note": ALL,
}


def test_the_transition_table_is_the_designed_one():
    assert {kind: set(lifecycles) for kind, (lifecycles, _) in services.TRANSITIONS.items()} == (
        ALLOWED
    )


@pytest.mark.django_db
@pytest.mark.parametrize("lifecycle", sorted(ALL))
@pytest.mark.parametrize("kind", sorted(ALLOWED))
def test_each_kind_happens_only_in_its_lifecycles(kind, lifecycle, make_item, raj, other_box, lab):
    item = _lifecycle(make_item, lifecycle, level="low" if kind == "marked_ok" else "ok")
    before = (item.version, item.ledger_state())
    changes = _changes(kind, item, raj=raj, other_box=other_box)
    if lifecycle in ALLOWED[kind]:
        event = apply_event(item, kind, user=None, **changes)
        assert (event.kind, item.version) == (kind, before[0] + 1)
    else:
        with pytest.raises(InvalidTransition):
            apply_event(item, kind, user=None, **changes)
        fresh = StockItem.objects.get(pk=item.pk)
        assert (fresh.version, fresh.ledger_state()) == before
        assert fresh.events.count() == before[0]


@pytest.mark.django_db
def test_created_is_never_applied(item):
    with pytest.raises(InvalidTransition, match="create_stock_item"):
        apply_event(item, "created", user=None)
    with pytest.raises(InventoryError, match="Unknown event kind"):
        apply_event(item, "teleported", user=None)
    with pytest.raises(InvalidTransition, match="can't change"):
        apply_event(item, "opened", user=None, custodian=item.custodian)


@pytest.mark.django_db
def test_verifying_and_finding(make_item):
    imported = make_item(lifecycle="unverified", opened_at=timezone.now() - timedelta(days=9))
    apply_event(imported, "verified", user=None)
    assert imported.lifecycle == "opened"  # it was opened before anyone checked
    sealed = make_item(lifecycle="unverified")
    apply_event(sealed, "verified", user=None)
    assert sealed.lifecycle == "available"
    # Found again, it is what it was before it was lost.
    apply_event(sealed, "emptied", user=None)
    apply_event(sealed, "lost", user=None)
    apply_event(sealed, "found", user=None)
    assert sealed.lifecycle == "empty"
    never_checked = make_item(lifecycle="unverified")
    apply_event(never_checked, "lost", user=None)
    apply_event(never_checked, "found", user=None)
    assert never_checked.lifecycle == "available"  # someone has seen it now


@pytest.mark.django_db
def test_found_can_say_where(make_item, other_box):
    item = make_item()
    apply_event(item, "lost", user=None)
    event = apply_event(item, "found", user=None, location=other_box, position_label="I9")
    assert (item.lifecycle, item.location, item.position_index) == ("available", other_box, 81)
    assert event.to_location == other_box


@pytest.mark.django_db
def test_opening_records_when(make_item):
    item = make_item()
    when = timezone.now() - timedelta(days=3)
    apply_event(item, "opened", user=None, occurred_at=when)
    assert (item.lifecycle, item.opened_at) == ("opened", when)
    assert item.events.last().occurred_at == when


@pytest.mark.django_db
def test_a_date_is_taken_as_midnight_where_the_lab_is(make_item, lab):
    # A sheet's "date" column: 2025-03-14, no time.
    lab.time_zone = "America/New_York"
    lab.save(update_fields=["time_zone"])
    there = ZoneInfo("America/New_York")
    opened = make_item(lifecycle="opened", occurred_at=date(2025, 3, 14))
    assert opened.opened_at == datetime(2025, 3, 14, tzinfo=there)
    assert opened.events.get().occurred_at == datetime(2025, 3, 14, tzinfo=there)
    sealed = make_item(lifecycle="unverified", opened_at=date(2025, 3, 1))
    assert sealed.opened_at == datetime(2025, 3, 1, tzinfo=there)
    later = make_item()
    event = apply_event(later, "opened", user=None, occurred_at=date(2025, 3, 15))
    assert later.opened_at == event.occurred_at == datetime(2025, 3, 15, tzinfo=there)
    typed = make_item(lifecycle="opened", occurred_at=datetime(2025, 3, 14, 9, 30))  # naive
    assert typed.opened_at == datetime(2025, 3, 14, 9, 30, tzinfo=there)  # the lab's 9:30
    for item in (opened, sealed, later, typed):
        assert services.ledger_drift(item) == {}
    for bad in ("2025-03-14", 20250314):
        with pytest.raises(InventoryError, match="must be a date"):
            make_item(occurred_at=bad)
        with pytest.raises(InventoryError, match="must be a date"):
            apply_event(later, "note", user=None, reason="x", occurred_at=bad)
    assert ledger_value(date(2025, 3, 14)) == "2025-03-14"


@pytest.mark.django_db
def test_emptying_zeroes_a_tracked_quantity(make_item):
    tracked = make_item(quantity="500", unit="mL")
    event = apply_event(tracked, "emptied", user=None)
    assert (tracked.quantity, event.qty_delta) == (Decimal(0), Decimal(-500))
    untracked = make_item()
    apply_event(untracked, "emptied", user=None)
    assert untracked.quantity is None


@pytest.mark.django_db
def test_quantities_and_splits(make_item):
    item = make_item(quantity="10", unit="vials")
    event = apply_event(item, "quantity_adjusted", user=None, quantity=Decimal("7.5"))
    assert (item.quantity, event.qty_delta) == (Decimal("7.5"), Decimal("-2.5"))
    with pytest.raises(InventoryError, match="isn't a quantity"):
        apply_event(item, "quantity_adjusted", user=None, quantity=-1)
    with pytest.raises(InvalidTransition, match="not an aggregate"):
        apply_event(item, "quantity_adjusted", user=None, container_count=3)
    aggregate = make_item(is_aggregate=True, container_count=4)
    split = apply_event(aggregate, "split", user=None, container_count=1, data={"into": ["X"]})
    assert aggregate.container_count == 1 and split.data["into"] == ["X"]
    assert apply_event(aggregate, "split", user=None).kind == "split"  # a record only
    with pytest.raises(InventoryError, match="reserved"):
        apply_event(aggregate, "split", user=None, data={"state": {}})


@pytest.mark.django_db
def test_an_aggregate_split_down_to_nothing_is_empty(make_item, lab, raj):
    # link_quartzy_inventory made an aggregate of 2; both containers got their own items.
    aggregate = make_item(
        is_aggregate=True, container_count=2, lifecycle="unverified", quantity="2", unit="vials"
    )
    parts = [make_item(parent=aggregate, lifecycle="unverified") for _ in range(2)]
    event = apply_event(
        aggregate, "split", user=None, container_count=0, data={"into": [p.short_id for p in parts]}
    )
    assert (aggregate.lifecycle, aggregate.container_count, aggregate.quantity) == (
        "empty",
        0,
        Decimal(0),
    )
    assert event.state == {"container_count": 0, "lifecycle": "empty", "quantity": "0.0000"}
    assert set(active_stock(lab, products=[aggregate.product])) == set(parts)
    assert present_item(aggregate, raj, channel="page").containers_text == "all split off"
    counted = make_item(is_aggregate=True, container_count=3)
    apply_event(counted, "quantity_adjusted", user=None, container_count=0, reason="none left")
    assert (counted.lifecycle, counted.quantity) == ("empty", None)
    with pytest.raises(InvalidTransition, match="empty"):
        apply_event(counted, "split", user=None, container_count=1)
    for item in (aggregate, counted):
        assert services.ledger_drift(item) == {}


@pytest.mark.django_db
def test_marking_low_and_ok(make_item):
    item = make_item()
    apply_event(item, "marked_low", user=None)
    assert item.level == "low"
    with pytest.raises(InvalidTransition, match="already marked low"):
        apply_event(item, "marked_low", user=None)
    apply_event(item, "marked_ok", user=None)
    with pytest.raises(InvalidTransition, match="isn't marked low"):
        apply_event(item, "marked_ok", user=None)


@pytest.mark.django_db
def test_a_discarded_item_takes_only_notes(make_item, other_box, raj):
    item = make_item()
    apply_event(item, "discarded", user=None, reason="expired")
    for kind, changes in [
        ("moved", {"location": other_box}),
        ("custody_changed", {"custodian": raj}),
        ("visibility_changed", {"visibility": "shared"}),
        ("found", {}),
        ("opened", {}),
    ]:
        with pytest.raises(InvalidTransition, match="discarded"):
            apply_event(item, kind, user=None, **changes)
    assert apply_event(item, "note", user=None, reason="bottle kept for the lot #").seq == 3
    with pytest.raises(InvalidTransition, match="needs some text"):
        apply_event(item, "note", user=None)


# ---------------------------------------------------------------------------------------
# Versions and idempotency


@pytest.mark.django_db
def test_a_stale_version_is_rejected(item, other_box, box):
    apply_event(item, "moved", user=None, location=other_box, expected_version=1)
    with pytest.raises(StaleVersion) as raised:
        apply_event(item, "moved", user=None, location=box, expected_version=1)
    assert (raised.value.expected, raised.value.actual) == (1, 2)
    assert StockItem.objects.get(pk=item.pk).location == other_box
    assert item.events.count() == 2


@pytest.mark.django_db
def test_a_repeated_client_uuid_returns_the_first_event(item, other_box, box):
    request_id = uuid.uuid4()
    first = apply_event(item, "moved", user=None, location=other_box, client_uuid=request_id)
    stale_copy = StockItem.objects.get(pk=item.pk)
    apply_event(item, "moved", user=None, location=box)  # something else happened since
    again = apply_event(item, "moved", user=None, location=other_box, client_uuid=str(request_id))
    assert again.pk == first.pk
    assert item.events.count() == 3 and item.location == box
    # The retry also wins over a stale expected_version: it already happened.
    replay = apply_event(
        stale_copy, "moved", user=None, location=other_box, client_uuid=request_id,
        expected_version=1,
    )  # fmt: skip
    assert replay.pk == first.pk and stale_copy.location == box and stale_copy.version == 3


@pytest.mark.django_db
def test_a_client_uuid_is_bound_to_one_item_and_kind(make_item, other_box):
    item, other = make_item(), make_item()
    request_id = uuid.uuid4()
    apply_event(item, "moved", user=None, location=other_box, client_uuid=request_id)
    with pytest.raises(IdempotencyConflict):
        apply_event(other, "moved", user=None, location=other_box, client_uuid=request_id)
    with pytest.raises(IdempotencyConflict):
        apply_event(item, "opened", user=None, client_uuid=request_id)
    with pytest.raises(IdempotencyConflict):
        make_item(client_uuid=request_id)
    with pytest.raises(InventoryError, match="client uuid"):
        apply_event(item, "opened", user=None, client_uuid="not-a-uuid")


@pytest.mark.django_db
def test_create_is_idempotent_on_client_uuid(make_item, other_lab):
    request_id = uuid.uuid4()
    first = make_item(client_uuid=request_id)
    assert make_item(client_uuid=request_id).pk == first.pk
    assert StockItem.objects.count() == 1


# ---------------------------------------------------------------------------------------
# Who may hold stock, and tenancy


@pytest.mark.django_db
def test_custody_goes_to_current_members_and_the_pool(
    item, member, raj, pool, leaver, outsider, make_member
):
    event = apply_event(item, "custody_changed", user=None, custodian=raj)
    assert (event.from_custodian, event.to_custodian, item.custodian) == (member, raj, raj)
    apply_event(item, "custody_changed", user=None, custodian=pool)
    assert item.custodian == pool
    ended = make_member("ended@lab.test", end_date=timezone.localdate() - timedelta(days=1))
    for nobody in (leaver, ended, outsider):
        with pytest.raises(InvalidCustodian):
            apply_event(item, "custody_changed", user=None, custodian=nobody)
    with pytest.raises(InvalidTransition, match="already has"):
        apply_event(item, "custody_changed", user=None, custodian=pool)
    with pytest.raises(InvalidTransition, match="Say who"):
        apply_event(item, "custody_changed", user=None)


@pytest.mark.django_db
def test_people_who_left_still_hold_what_they_had(make_item, leaver, outsider, other_lab, pool):
    # An import of a departed member's sheet: they hold it until it is handed over.
    theirs = make_item(custodian=leaver, location=None, temperature="LN2", source="import")
    assert theirs.location.unsorted_owner == leaver
    colder = services.unsorted_location(theirs.lab, leaver, "-80C")
    apply_event(theirs, "moved", user=None, location=colder)
    apply_event(theirs, "custody_changed", user=None, custodian=pool)
    assert theirs.location.unsorted_owner == pool
    with pytest.raises(InvalidCustodian):
        make_item(custodian=outsider)
    with pytest.raises(InvalidCustodian):
        make_item(custodian=other_lab.pool_user)


@pytest.mark.django_db
def test_handing_over_unplaced_stock_moves_it_to_the_new_unsorted_container(make_item, raj, box):
    unplaced = make_item(location=None, temperature="-20C")
    event = apply_event(unplaced, "custody_changed", user=None, custodian=raj)
    assert unplaced.location.unsorted_owner == raj and unplaced.location.temperature == "-20C"
    assert event.to_location == unplaced.location
    placed = make_item()
    apply_event(placed, "custody_changed", user=None, custodian=raj)
    assert placed.location == box  # a real place doesn't change with custody
    handed = make_item(location=None, temperature="4C")
    apply_event(handed, "custody_changed", user=None, custodian=raj, location=box)
    assert handed.location == box
    assert services.ledger_drift(unplaced) == services.ledger_drift(handed) == {}


@pytest.mark.django_db
def test_stock_stays_inside_its_lab(make_item, other_lab, member, location_types, lab):
    item = make_item()
    elsewhere = services.create_location(
        lab=other_lab, parent=None, name="Room 9", location_type="room", user=None
    )
    with pytest.raises(CrossLab):
        apply_event(item, "moved", user=None, location=elsewhere)
    with pytest.raises(CrossLab):
        make_item(location=elsewhere)
    their_request = OrderRequest.objects.create(lab=other_lab, source="outside")
    with pytest.raises(CrossLab):
        make_item(source_order_request=their_request)
    ours = OrderRequest.objects.create(lab=lab, source="outside")
    assert make_item(source_order_request=ours).source_order_request == ours
    their_item = services.create_stock_item(
        lab=other_lab, product=item.product, custodian=other_lab.pool_user, user=None,
        source="gift",
    )  # fmt: skip
    with pytest.raises(CrossLab):
        make_item(parent=their_item)
    assert make_item(parent=item).parent == item


@pytest.mark.django_db
def test_refusals_about_another_labs_rows_dont_name_them(
    make_item, lab, other_lab, outsider, leaver, shelf, location_types
):
    """The messages are shown to people: one about another lab's place or person says only
    that it is another lab's, so trying ids one after another reads no names."""
    theirs = services.create_location(
        lab=other_lab, parent=None, name="Olu's cold room", location_type="room", user=None
    )
    their_unsorted = services.unsorted_location(other_lab, outsider, "-80C")  # "Olu Other"
    item = make_item()
    for error, attempt in [
        (CrossLab, lambda: apply_event(item, "moved", user=None, location=theirs)),
        (CrossLab, lambda: apply_event(item, "moved", user=None, location=their_unsorted)),
        (CrossLab, lambda: make_item(location=theirs)),
        (CrossLab, lambda: services.create_location(
            lab=lab, parent=theirs, name="Box 1", location_type="box", user=None)),
        (CrossLab, lambda: move_location(shelf, theirs, user=None)),
        (InvalidCustodian, lambda: apply_event(
            item, "custody_changed", user=None, custodian=outsider)),
        (InvalidCustodian, lambda: make_item(custodian=outsider)),
        (InvalidCustodian, lambda: services.unsorted_location(lab, outsider, "-20C")),
        (InvalidCustodian, lambda: services.create_location(
            lab=lab, parent=None, name="Room 9", location_type="room", user=None,
            default_custodian=outsider)),
    ]:  # fmt: skip
        with pytest.raises(error) as raised:
            attempt()
        message = str(raised.value)
        assert "Olu" not in message and "cold room" not in message, message
        assert "another lab" in message or "isn't in Test Lab" in message, message
    # Our own lab's people are still named.
    with pytest.raises(InvalidCustodian, match="Lee Left can't take stock of Test Lab"):
        apply_event(item, "custody_changed", user=None, custodian=leaver)
    assert StockItem.objects.get(pk=item.pk).version == 1


@pytest.mark.django_db
def test_someone_elses_unsorted_container_is_not_a_place(make_item, raj, member):
    rajs = services.unsorted_location(member.memberships.get().lab, raj, "-20C")
    with pytest.raises(InvalidLocation, match="only Raj Rao's stock"):
        make_item(location=rajs)
    item = make_item()
    with pytest.raises(InvalidLocation, match="only Raj Rao's stock"):
        apply_event(item, "moved", user=None, location=rajs)


@pytest.mark.django_db
def test_retired_locations_take_no_stock(make_item, other_box):
    other_box.active = False
    other_box.save(update_fields=["active"])
    with pytest.raises(InvalidLocation, match="no longer in use"):
        make_item(location=other_box)
    item = make_item()
    with pytest.raises(InvalidLocation, match="no longer in use"):
        apply_event(item, "moved", user=None, location=Location.objects.get(pk=other_box.pk))


@pytest.mark.django_db
def test_mta_stock_stays_restricted(make_item):
    item = make_item(mta=True)
    with pytest.raises(InvalidTransition, match="MTA"):
        apply_event(item, "visibility_changed", user=None, visibility="shared")
    # The database refuses MTA material that isn't restricted, however it is written.
    shared = make_item(visibility="shared")
    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("UPDATE inventory_stockitem SET mta = true WHERE id = %s", [shared.pk])


@pytest.mark.django_db
def test_the_mta_and_aggregate_flags_never_change(make_item, member):
    """Dropping the MTA flag would let MTA stock be shared, and an item made an aggregate
    behind the ledger would have no container count: neither flag is written after creation."""
    item = make_item(mta=True)
    items = StockItem.objects.filter(pk=item.pk)
    for kwargs in ({"mta": False}, {"is_aggregate": True}):
        with pytest.raises(LedgerError):
            items.update(**kwargs)
    copy = StockItem.objects.get(pk=item.pk)
    copy.mta = False
    with pytest.raises(LedgerError, match="mta"):
        copy.save()
    with pytest.raises(LedgerError):
        StockItem.objects.bulk_update([copy], ["mta"])
    copy = StockItem.objects.get(pk=item.pk)
    copy.is_aggregate = True
    with pytest.raises(LedgerError, match="is_aggregate"):
        copy.save()
    with pytest.raises(InvalidTransition, match="MTA"):
        apply_event(item, "visibility_changed", user=member, visibility="shared")
    fresh = StockItem.objects.get(pk=item.pk)
    assert (fresh.mta, fresh.visibility, fresh.is_aggregate, fresh.container_count) == (
        True,
        "restricted",
        False,
        None,
    )
    assert list(fresh.events.values_list("kind", flat=True)) == ["created"]
    fresh.notes = "kept in the MTA box"  # other fields save as before, the flags untouched
    fresh.save()
    assert StockItem.objects.get(pk=item.pk).mta is True


# ---------------------------------------------------------------------------------------
# Guards around the state fields


@pytest.mark.django_db
def test_save_refuses_state_changes(item, raj, other_box):
    for field, value in [
        ("custodian", raj),
        ("location", other_box),
        ("lifecycle", "lost"),
        ("visibility", "shared"),
        ("position_label", "A1"),
        ("quantity", Decimal(1)),
        ("version", 9),
        ("lab_id", 999),
        ("short_id", "ZZZZZZ"),
    ]:
        copy = StockItem.objects.get(pk=item.pk)
        setattr(copy, field, value)
        with pytest.raises(LedgerError):
            copy.save()
    copy = StockItem.objects.get(pk=item.pk)
    copy.notes = "1:1000 for WB"
    copy.save()
    assert StockItem.objects.get(pk=item.pk).notes == "1:1000 for WB"


@pytest.mark.django_db
def test_a_stale_copy_never_writes_old_state_back(item, other_box):
    stale = StockItem.objects.get(pk=item.pk)
    apply_event(item, "moved", user=None, location=other_box)
    stale.notes = "edited from an old page"
    stale.save()
    fresh = StockItem.objects.get(pk=item.pk)
    assert (fresh.location, fresh.version, fresh.notes) == (other_box, 2, "edited from an old page")
    assert services.ledger_drift(fresh) == {}


@pytest.mark.django_db
def test_the_callers_copy_can_be_saved_after_an_event(item, other_box):
    apply_event(item, "moved", user=None, location=other_box)
    item.notes = "moved to make room"
    item.save()
    item.refresh_from_db()
    item.notes = "and again"
    item.save()
    deferred = StockItem.objects.only("notes").get(pk=item.pk)
    deferred.notes = "only notes loaded"
    deferred.save()
    assert deferred.location == other_box  # loaded on access, then saved without change
    deferred.save()


@pytest.mark.django_db
def test_stock_is_created_only_through_the_service(lab, product, member, box):
    values = {
        "lab": lab, "product": product, "custodian": member, "location": box,
        "visibility": "shared", "lifecycle": "available", "source": "purchased",
    }  # fmt: skip
    with pytest.raises(LedgerError, match="create_stock_item"):
        StockItem.objects.create(**values)
    with pytest.raises(LedgerError, match="create_stock_item"):
        StockItem.objects.bulk_create([StockItem(**values)])
    with pytest.raises(LedgerError, match="create_stock_item"):
        StockItem(**values).save()


@pytest.mark.django_db
def test_querysets_refuse_state_writes(item, raj, other_box, lab):
    items = StockItem.objects.filter(pk=item.pk)
    for kwargs in (
        {"custodian": raj},
        {"custodian_id": raj.pk},
        {"location": other_box},
        {"location_id": other_box.pk},
        {"lifecycle": "lost"},
        {"version": 5},
        {"lab": lab},
        {"short_id": "ZZZZZZ"},
        {"mta": True},
        {"is_aggregate": True},
    ):
        with pytest.raises(LedgerError):
            items.update(**kwargs)
    with pytest.raises(LedgerError):
        lab.stock_items.update(lifecycle="lost")
    with pytest.raises(LedgerError):
        StockItem._base_manager.filter(pk=item.pk).update(lifecycle="lost")
    item.lifecycle = "lost"
    with pytest.raises(LedgerError):
        StockItem.objects.bulk_update([item], ["lifecycle"])
    item.refresh_from_db()
    assert item.lifecycle == "available"
    # Other fields are fine: merges repoint products and lots this way.
    assert items.update(notes="bulk note") == 1
    item.notes = "one note"
    assert StockItem.objects.bulk_update([item], ["notes"]) == 1


@pytest.mark.django_db
def test_events_are_append_only_in_the_orm(item, member):
    event = item.events.get()
    event.reason = "rewritten"
    with pytest.raises(LedgerError, match="append-only"):
        event.save()
    with pytest.raises(LedgerError, match="append-only"):
        event.save(update_fields=["reason"])
    with pytest.raises(LedgerError, match="never deleted"):
        event.delete()
    events = StockEvent.objects.filter(pk=event.pk)
    with pytest.raises(LedgerError, match="never deleted"):
        events.delete()
    with pytest.raises(LedgerError, match="append-only"):
        events.update(reason="x")
    with pytest.raises(LedgerError, match="append-only"):
        StockEvent.objects.bulk_update([event], ["reason"])
    with pytest.raises(LedgerError, match="apply_event"):
        StockEvent.objects.create(lab=item.lab, stock_item=item, seq=2, kind="note")
    with pytest.raises(LedgerError, match="apply_event"):
        StockEvent.objects.bulk_create([StockEvent(lab=item.lab, stock_item=item, seq=2)])
    # Retention pseudonymizes who acted, and only that.
    assert events.update(user=None) == 1
    event = StockEvent.objects.get(pk=event.pk)
    event.user = member
    event.save(update_fields=["user"])
    assert StockEvent.objects.get(pk=event.pk).user == member


@pytest.mark.django_db
def test_deleting_a_user_keeps_their_events(item, make_member, lab):
    helper = make_member("helper@lab.test")
    event = apply_event(item, "note", user=helper, reason="counted")
    Membership.objects.filter(user=helper).delete()
    helper.delete()
    assert StockEvent.objects.get(pk=event.pk).user is None


@pytest.mark.django_db
def test_a_position_can_be_given_as_a_label_or_a_number(item, other_box):
    apply_event(item, "moved", user=None, position_index=10)
    assert (item.position_label, item.position_index) == ("B1", 10)
    apply_event(item, "moved", user=None, position_label="C3")
    assert (item.position_label, item.position_index) == ("C3", 21)
    apply_event(item, "moved", user=None, position_label="")
    assert (item.position_label, item.position_index) == ("", None)
    with pytest.raises(InventoryError, match="1 or more"):
        apply_event(item, "moved", user=None, position_index=0)
    with pytest.raises(InvalidTransition, match="always somewhere"):
        apply_event(item, "moved", user=None, location=None)


@pytest.mark.django_db
def test_a_create_racing_on_its_client_uuid_returns_the_winner(make_item, monkeypatch):
    request_id = uuid.uuid4()
    winner = make_item(client_uuid=request_id)
    real = services._created_by_uuid
    calls = {"n": 0}

    def missed_once(client_uuid, lab):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(client_uuid, lab)  # as if read before it

    monkeypatch.setattr(services, "_created_by_uuid", missed_once)
    assert make_item(client_uuid=request_id).pk == winner.pk
    assert calls["n"] == 2 and StockItem.objects.count() == 1


@pytest.mark.django_db
def test_deleting_a_request_unlinks_its_stock_and_drops_its_claims(make_item, lab, member):
    from apps.inventory.models import HistoryClaim

    request = OrderRequest.objects.create(lab=lab, source="outside")
    item = make_item(source_order_request=request)
    claim = HistoryClaim.objects.create(lab=lab, order_request=request, person=member)
    claim.stock_items.add(item)
    OrderRequest.objects.filter(pk=request.pk).delete()  # as quartzy_purge_lab does
    fresh = StockItem.objects.get(pk=item.pk)
    assert fresh.source_order_request is None and fresh.version == 1
    assert not HistoryClaim.objects.exists()
    assert services.ledger_drift(fresh) == {}
