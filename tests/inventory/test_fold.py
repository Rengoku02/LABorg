"""Folding an item's events in seq order reproduces its cached state, over many random
event sequences (valid and invalid), from every starting point create_stock_item allows."""

import random
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.inventory import services
from apps.inventory.models import StockEvent, StockItem
from apps.inventory.services import InventoryError, apply_event

KINDS = [*sorted(services.TRANSITIONS), "created"]
SEEDS = range(60)
# seed -> the kinds its walk applied, and "refused <kind>" for those it had refused.
WALKED = {}


@pytest.fixture
def world(lab, make_location, room, freezer, box, member, raj, pool, leaver):
    fridge = make_location("Fridge 1", "fridge", room, temperature="4C")
    return {
        "places": [
            box,
            make_location("Box 5", "box_10x10", freezer),
            make_location("Bag 1", "bag", fridge),
            fridge,
        ],
        "people": [member, raj, pool, leaver],
        "temperatures": ["", "RT", "4C", "-20C", "-80C", "LN2"],
    }


def _random_item(rng, make_item, world):
    custodian = rng.choice(world["people"])
    fields = {
        "custodian": custodian,
        "lifecycle": rng.choice(["unverified", "available", "opened"]),
        "visibility": rng.choice([None, "shared", "ask_first", "restricted"]),
        "mta": rng.random() < 0.1,
        "quantity": rng.choice([None, "1", "250", "0.5"]),
        "unit": rng.choice(["", "mL", "vials"]),
        "occurred_at": timezone.now() - timedelta(days=rng.randint(0, 900)),
    }
    if rng.random() < 0.3:
        fields.update(location=None, temperature=rng.choice(world["temperatures"]))
    else:
        fields.update(location=rng.choice(world["places"]), position_label=rng.choice(["", "A1"]))
    if rng.random() < 0.2:
        fields.update(is_aggregate=True, container_count=rng.randint(1, 6))
    return make_item(**fields)


def _random_kind(rng, item):
    """Mostly a kind the item's lifecycle allows (discarding rarely, so histories run on),
    sometimes any kind at all, which is then usually refused."""
    if rng.random() < 0.2:
        return rng.choice(KINDS)
    allowed = [
        kind
        for kind, (lifecycles, _) in services.TRANSITIONS.items()
        if item.lifecycle in lifecycles and (kind != "discarded" or rng.random() < 0.15)
    ]
    return rng.choice(allowed)


def _random_changes(rng, kind, item, world):
    """Plausible arguments for ``kind``; some are invalid on purpose."""
    changes = {}
    if kind in ("moved", "verified", "found", "custody_changed") and rng.random() < 0.7:
        if rng.random() < 0.25:
            owner = item.custodian if rng.random() < 0.8 else rng.choice(world["people"])
            temperature = rng.choice(world["temperatures"])
            changes["location"] = services.unsorted_location(item.lab, owner, temperature)
        else:
            changes["location"] = rng.choice(world["places"])
        if rng.random() < 0.5:
            changes["position_label"] = rng.choice(["A1", "B3", "J10", "top", ""])
    if kind == "moved" and not changes:
        changes["position_label"] = rng.choice(["C2", "D4"])
    if kind == "custody_changed":
        changes["custodian"] = rng.choice(world["people"])
    if kind == "visibility_changed":
        changes["visibility"] = rng.choice(["shared", "ask_first", "restricted"])
    if kind in ("quantity_adjusted", "split"):
        if rng.random() < 0.7:
            changes["quantity"] = rng.choice([None, Decimal("0"), Decimal("2.25"), 7, "-1"])
        if rng.random() < 0.3:
            changes["unit"] = rng.choice(["", "mL", "uL"])
        if rng.random() < 0.3:
            changes["container_count"] = rng.randint(0, 5)
    return changes


@pytest.mark.django_db
@pytest.mark.parametrize("seed", SEEDS)
def test_folding_events_reproduces_the_cached_state(seed, make_item, world):
    rng = random.Random(seed)
    item = _random_item(rng, make_item, world)
    applied, seen = 0, set()
    for _step in range(rng.randint(10, 30)):
        kind = _random_kind(rng, item)
        changes = _random_changes(rng, kind, item, world)
        before = StockItem.objects.get(pk=item.pk)
        when = (
            timezone.now() - timedelta(hours=rng.randint(0, 2000)) if rng.random() < 0.3 else None
        )
        try:
            apply_event(
                item, kind, user=rng.choice(world["people"][:2]), occurred_at=when,
                reason="note" if kind == "note" else "", **changes,
            )  # fmt: skip
            applied += 1
            seen.add(kind)
        except InventoryError:
            seen.add(f"refused {kind}")
            after = StockItem.objects.get(pk=item.pk)
            assert (after.version, after.ledger_state()) == (before.version, before.ledger_state())

    cached = StockItem.objects.get(pk=item.pk)
    events = list(StockEvent.objects.filter(stock_item=cached).order_by("seq"))
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    assert cached.version == len(events) == applied + 1
    assert services.fold_events(events) == cached.ledger_state()
    assert services.ledger_drift(cached) == {}
    _check_columns(events)
    WALKED[seed] = seen


def _check_columns(events):
    """The from/to columns agree with the state the events carry."""
    location = custodian = quantity = None
    for event in events:
        state = event.state
        if "location" in state:
            assert event.to_location_id == state["location"]
            assert event.from_location_id == location
            location = state["location"]
        else:
            assert event.to_location_id is None and event.from_location_id is None
        if "custodian" in state:
            assert (event.from_custodian_id, event.to_custodian_id) == (
                custodian,
                state["custodian"],
            )
            custodian = state["custodian"]
        if "quantity" in state:
            new = Decimal(state["quantity"]) if state["quantity"] is not None else None
            if new is None and quantity is None:
                assert event.qty_delta is None
            else:
                assert event.qty_delta == (new or 0) - (quantity or 0)
            quantity = new


def test_the_walks_cover_every_kind():
    """The random walks above really exercise every kind of event, and refusals too."""
    if set(WALKED) != set(SEEDS):
        pytest.skip("checks the walks above; run them in the same session")
    seen = set().union(*WALKED.values())
    assert set(services.TRANSITIONS) <= seen
    assert {"refused created", "refused moved", "refused custody_changed"} <= seen
