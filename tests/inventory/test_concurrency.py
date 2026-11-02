"""Two writers at once, on real connections: optimistic versions, no lost events, race-safe
creation of unsorted containers and top-level locations, moves that keep the tree whole, and
locations that are never retired with stock going into them."""

import threading
import uuid

import pytest
from django.db import connection, transaction

from apps.inventory import selectors, services
from apps.inventory.admin import LocationAdminForm
from apps.inventory.models import Location, LocationEvent, StockEvent, StockItem
from apps.inventory.presenters import present_item
from apps.inventory.services import InvalidLocation, StaleVersion, apply_event


def run_together(*targets):
    """Run each target in its own thread (so its own connection), started together; return
    what each returned or raised, in order."""
    barrier = threading.Barrier(len(targets))
    results = [None] * len(targets)

    def worker(index, target):
        try:
            barrier.wait(timeout=10)
            results[index] = target()
        except Exception as exc:  # reported to the test through results
            results[index] = exc
        finally:
            connection.close()

    threads = [
        threading.Thread(target=worker, args=(index, target))
        for index, target in enumerate(targets)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads)
    return results


@pytest.mark.django_db(transaction=True)
def test_with_the_same_version_exactly_one_writer_wins(make_item, make_location, freezer):
    item = make_item()
    boxes = [make_location(f"Box {n}", "box", freezer) for n in (7, 8)]
    version = item.version

    def move_to(box):
        return lambda: apply_event(
            item.pk, "moved", user=None, location=box, expected_version=version
        )

    results = run_together(move_to(boxes[0]), move_to(boxes[1]))
    winners = [result for result in results if isinstance(result, StockEvent)]
    losers = [result for result in results if isinstance(result, StaleVersion)]
    assert len(winners) == 1 and len(losers) == 1, results
    fresh = StockItem.objects.get(pk=item.pk)
    assert fresh.version == 2 and fresh.location_id == winners[0].to_location_id
    assert StockEvent.objects.filter(stock_item=item).count() == 2
    assert services.ledger_drift(fresh) == {}


@pytest.mark.django_db(transaction=True)
def test_without_a_version_both_writers_apply_and_nothing_is_lost(
    make_item, make_location, freezer, raj, member
):
    item = make_item()
    boxes = [make_location(f"Box {n}", "box", freezer) for n in (7, 8)]

    def mover():
        for step in range(10):
            apply_event(item.pk, "moved", user=member, location=boxes[step % 2])
        return "moved"

    def annotator():
        for step in range(10):
            apply_event(item.pk, "note", user=raj, reason=f"note {step}")
            apply_event(
                item.pk, "visibility_changed", user=raj,
                visibility="shared" if step % 2 == 0 else "ask_first",
            )  # fmt: skip
        return "noted"

    assert run_together(mover, annotator) == ["moved", "noted"]
    fresh = StockItem.objects.get(pk=item.pk)
    events = list(StockEvent.objects.filter(stock_item=item).order_by("seq"))
    assert [event.seq for event in events] == list(range(1, 32))
    assert fresh.version == 31
    assert sorted(e.reason for e in events if e.kind == "note") == sorted(
        f"note {step}" for step in range(10)
    )
    assert (fresh.location_id, fresh.visibility) == (boxes[1].pk, "ask_first")
    assert services.ledger_drift(fresh) == {}


@pytest.mark.django_db(transaction=True)
def test_unsorted_containers_are_created_once_under_a_race(lab, member, location_types):
    def fetch():
        return services.unsorted_location(lab, member, "-20C")

    results = run_together(*[fetch] * 4)
    assert all(isinstance(result, Location) for result in results), results
    assert len({result.pk for result in results}) == 1
    assert Location.objects.filter(is_unsorted=True).count() == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_top_level_locations_get_their_own_paths(lab, location_types):
    def room(number):
        return lambda: services.create_location(
            lab=lab, parent=None, name=f"Room {number}", location_type="room", user=None
        )

    results = run_together(*[room(number) for number in range(4)])
    assert all(isinstance(result, Location) for result in results), results
    assert len({result.path for result in results}) == 4
    assert all(not problems for problems in Location.objects.find_problems())


@pytest.mark.django_db(transaction=True)
def test_a_create_retried_at_the_same_moment_makes_one_item(make_item):
    request_id = uuid.uuid4()

    def create():
        return make_item(client_uuid=request_id)

    results = run_together(create, create)
    assert all(isinstance(result, StockItem) for result in results), results
    assert results[0].pk == results[1].pk
    assert StockItem.objects.count() == 1
    assert StockEvent.objects.filter(client_uuid=request_id).count() == 1


@pytest.mark.django_db(transaction=True)
def test_two_people_adding_the_same_box_at_once_make_one(lab, shelf):
    def add(name, parent, kind):
        def create():
            fresh = parent and Location.objects.get(pk=parent.pk)  # one copy per thread
            return services.create_location(
                lab=lab, parent=fresh, name=name, location_type=kind, user=None
            )

        return create

    for name, parent, kind in (("Box 9", shelf, "box"), ("Room 300", None, "room")):
        results = run_together(add(name, parent, kind), add(name.upper(), parent, kind))
        made = [result for result in results if isinstance(result, Location)]
        refused = [result for result in results if isinstance(result, InvalidLocation)]
        assert len(made) == 1 and len(refused) == 1, results
        assert "already has a location called" in str(refused[0])
        assert Location.objects.filter(name__iexact=name).count() == 1
    assert all(not problems for problems in Location.objects.find_problems())


@pytest.mark.django_db(transaction=True)
def test_a_code_taken_by_another_lab_at_the_same_moment_is_refused_cleanly(
    lab, other_lab, location_types
):
    def add(which):
        return lambda: services.create_location(
            lab=which, parent=None, name="Room 1", location_type="room", user=None, code="RM-1"
        )

    results = run_together(add(lab), add(other_lab))
    made = [result for result in results if isinstance(result, Location)]
    refused = [result for result in results if isinstance(result, InvalidLocation)]
    assert len(made) == 1 and len(refused) == 1, results
    assert "already in use" in str(refused[0])
    assert Location.objects.filter(code="RM-1").count() == 1


def tree_problems():
    return [found for found in Location.objects.find_problems() if found]


def fresh(location):
    """This thread's own copy of a location, read now."""
    return Location.objects.get(pk=location.pk)


@pytest.mark.django_db(transaction=True)
def test_locations_added_inside_a_subtree_while_it_moves_move_with_it(
    lab, location_types, member, raj, product
):
    def add(name, kind, parent=None, **fields):
        return services.create_location(
            lab=lab, parent=parent, name=name, location_type=kind, user=None, **fields
        )

    room = add("Room 1", "room")
    first, second = add("-80 A", "freezer", room), add("-80 B", "freezer", room)
    shelf = add("Shelf 1", "shelf", first)
    rack = add("Rack 3", "rack", shelf)
    secret = add("Box quokka", "box", shelf, default_custodian=member, visibility="restricted")
    added, release = threading.Event(), threading.Event()

    def importer():
        # import_locations --commit adds every location in one transaction.
        with transaction.atomic():
            box = add("Box 9", "box", fresh(rack))
            bag = add("Bag 1", "bag", fresh(secret), visibility="shared")
            added.set()
            release.wait(10)
        return box, bag

    def mover():
        added.wait(10)
        threading.Timer(1.0, release.set).start()  # the import commits while the move waits
        return services.move_location(fresh(shelf), fresh(second), user=None)

    added_rows, event = run_together(importer, mover)
    assert isinstance(added_rows, tuple), added_rows
    assert isinstance(event, LocationEvent), event
    box, bag = added_rows
    assert fresh(box).path_text() == "Room 1 / -80 B / Shelf 1 / Rack 3 / Box 9"
    assert fresh(bag).path_text() == "Room 1 / -80 B / Shelf 1 / Box quokka / Bag 1"
    assert tree_problems() == []
    # Stock put in the bag is still inside the restricted box, so still hidden from Raj.
    item = services.create_stock_item(
        lab=lab, product=product, custodian=member, user=None, source="purchased",
        location=fresh(bag), visibility="shared",
    )  # fmt: skip
    view = present_item(item, raj, channel="page")
    assert view.redacted and view.location_path == ""


@pytest.mark.django_db(transaction=True)
def test_moving_a_shelf_and_a_box_out_of_it_at_once_doesnt_deadlock(lab, location_types):
    def add(name, kind, parent=None):
        return services.create_location(
            lab=lab, parent=parent, name=name, location_type=kind, user=None
        )

    def move(location, parent):
        return lambda: services.move_location(fresh(location), fresh(parent), user=None)

    for attempt in range(4):
        room = add(f"Room {attempt}", "room")
        first, second = add("-80 A", "freezer", room), add("-80 B", "freezer", room)
        shelf, other_shelf = add("Shelf 1", "shelf", first), add("Shelf 2", "shelf", second)
        box = add("Box 4", "box", shelf)
        results = run_together(move(box, other_shelf), move(shelf, second))
        assert all(isinstance(result, LocationEvent) for result in results), results
        assert fresh(box).path_text() == f"Room {attempt} / -80 B / Shelf 2 / Box 4"
        assert fresh(shelf).path_text() == f"Room {attempt} / -80 B / Shelf 1"
    assert tree_problems() == []


def retire_in_the_admin(box):
    """What the admin's change form does when "active" is unticked: check, then save."""
    box = fresh(box)
    form = LocationAdminForm(
        data={
            "lab": box.lab_id, "name": box.name, "location_type": box.location_type_id,
            "code": box.code, "temperature": "", "visibility": box.visibility, "tags": "",
            "notes": "", "parent": Location.objects.get_parent(box).pk,
        },  # "active" left out: unticked
        instance=box,
    )  # fmt: skip
    if not form.is_valid():
        return " ".join(message for messages in form.errors.values() for message in messages)
    form.save(commit=False).save(update_fields=["active"])
    return "retired"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("first", ["the stock moves in", "the box is retired"])
def test_a_box_is_never_retired_with_stock_going_into_it(
    first, make_item, make_location, freezer, member
):
    item = make_item()
    box = make_location("Box 7", "box", freezer)
    started, release = threading.Event(), threading.Event()

    def move_in():
        return apply_event(item.pk, "moved", user=member, location=fresh(box))

    def holding(step):
        def run():
            with transaction.atomic():  # one request: it commits at the end
                result = step()
                started.set()
                release.wait(10)
            return result

        return run

    def after(step):
        def run():
            started.wait(10)
            threading.Timer(1.0, release.set).start()  # the first commits while this waits
            return step()

        return run

    if first == "the stock moves in":
        moved, retired = run_together(holding(move_in), after(lambda: retire_in_the_admin(box)))
        assert isinstance(moved, StockEvent), moved
        assert "Box 7 still holds 1 active stock item(s)" in retired
    else:
        retired, moved = run_together(holding(lambda: retire_in_the_admin(box)), after(move_in))
        assert retired == "retired"
        assert isinstance(moved, InvalidLocation) and "no longer in use" in str(moved), moved
    box = fresh(box)
    assert box.active or not selectors.location_contents(box).exists()
    assert services.ledger_drift(StockItem.objects.get(pk=item.pk)) == {}


@pytest.mark.django_db(transaction=True)
def test_moving_stock_between_boxes_while_their_shelf_moves_doesnt_deadlock(
    lab, location_types, make_item
):
    def add(name, kind, parent=None):
        return services.create_location(
            lab=lab, parent=parent, name=name, location_type=kind, user=None
        )

    def move_stock(item, box):
        return lambda: apply_event(item.pk, "moved", user=None, location=fresh(box))

    def move_place(location, parent):
        return lambda: services.move_location(fresh(location), fresh(parent), user=None)

    for attempt in range(4):
        room = add(f"Room {attempt}", "room")
        first, second = add("-80 A", "freezer", room), add("-80 B", "freezer", room)
        shelf = add("Shelf 1", "shelf", first)
        older, newer = add("Box 1", "box", shelf), add("Box 2", "box", shelf)
        item = make_item(location=newer)  # it moves from the newer box to the older one
        results = run_together(move_stock(item, older), move_place(shelf, second))
        assert [type(result) for result in results] == [StockEvent, LocationEvent], results
        assert StockItem.objects.get(pk=item.pk).location.path_text() == (
            f"Room {attempt} / -80 B / Shelf 1 / Box 1"
        )
    assert tree_problems() == []
