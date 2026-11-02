"""Locations: creating, moving (subtrees, cycles, labs), unsorted containers, paths and
temperatures."""

import pytest

from apps.core.policies import set_policy
from apps.inventory import services
from apps.inventory.models import Location, LocationEvent, LocationType, StockItem
from apps.inventory.services import CrossLab, InvalidCustodian, InvalidLocation, move_location


def problems():
    return [found for found in Location.objects.find_problems() if found]


@pytest.mark.django_db
def test_create_builds_the_tree(room, freezer, shelf, box, member):
    assert box.path_text() == "Room 210 / -80 A / Shelf 1 / Box 4"
    assert [loc.name for loc in box.ancestors()] == ["Room 210", "-80 A", "Shelf 1"]
    assert room.is_root() and box.depth == 4 and box.default_custodian == member
    assert box.created_by is None and room.numchild == 1
    assert problems() == []


@pytest.mark.django_db
def test_effective_temperature_comes_from_the_nearest_location_that_says(
    room, freezer, shelf, box, make_location
):
    assert (room.effective_temperature(), freezer.effective_temperature()) == ("", "-80C")
    assert box.effective_temperature() == "-80C"
    cold = make_location("LN2 box", "box", shelf, temperature="LN2")
    assert cold.effective_temperature() == "LN2"
    with pytest.raises(InvalidLocation, match="Unknown temperature"):
        make_location("Box 9", "box", shelf, temperature="cold")
    assert make_location("Box 10", "box", shelf, temperature="\u221220 °C").temperature == "-20C"


@pytest.mark.django_db
def test_visibility_defaults(make_location, room, lab):
    assert room.visibility == "shared"
    assert make_location("Box A", "box", room).visibility == "ask_first"  # the lab default
    set_policy("inventory.default_visibility", "restricted", lab=lab)
    assert make_location("Box B", "box", room).visibility == "restricted"
    assert make_location("Shelf 2", "shelf", room).visibility == "shared"
    assert make_location("Box C", "box", room, visibility="shared").visibility == "shared"
    assert make_location("Bag D", "bag", room, visibility="ask_first").visibility == "ask_first"


@pytest.mark.django_db
def test_structural_locations_are_always_shared(make_location, room):
    # A restricted freezer would hide everyone's stock in it; restrict the box instead.
    for visibility in ("restricted", "ask_first"):
        with pytest.raises(InvalidLocation, match="A freezer is always shared"):
            make_location("-20 C", "freezer", room, visibility=visibility)
    assert make_location("-20 C", "freezer", room, visibility="shared").visibility == "shared"
    with pytest.raises(services.InventoryError, match="Unknown visibility"):
        make_location("Box Q", "box", room, visibility="secret")


@pytest.mark.django_db
def test_a_location_changing_type_keeps_the_tree_valid(make_location, room, freezer, shelf, box):
    rack = make_location("Rack 3", "rack", shelf)
    kinds = {kind.key: kind for kind in LocationType.objects.all()}

    def change(location, key):
        services.validate_placement(
            lab=room.lab,
            parent=Location.objects.get_parent(location),
            location_type=kinds[key],
            name=location.name,
            location=location,
        )

    with pytest.raises(InvalidLocation, match=r"holds Rack 3 \(rack\), so it can't be a box"):
        change(shelf, "box")
    change(rack, "box")  # nothing inside it: a rack may turn out to be a box
    change(box, "bag")
    with pytest.raises(InvalidLocation, match="made on demand"):
        change(box, "unsorted")


@pytest.mark.django_db
def test_tags_may_be_given_as_text(make_location, room, antibody_type):
    assert make_location("Box T", "box", room, tags="antibody, ,antibody").tags == ["antibody"]
    with pytest.raises(InvalidLocation, match="Unknown item type tag"):
        make_location("Box U", "box", room, tags="antibody,unicorn")


@pytest.mark.django_db
def test_names_kinds_and_tags_are_checked(make_location, room, box, antibody_type):
    with pytest.raises(InvalidLocation, match="already has a location called"):
        make_location("room 210", "room")
    assert make_location("Room 210", "room", room).name == "Room 210"  # a different parent
    with pytest.raises(InvalidLocation, match="can't contain '/'"):
        make_location("Shelf 1/2", "shelf", room)
    with pytest.raises(InvalidLocation, match="needs a name"):
        make_location("  ", "shelf", room)
    with pytest.raises(InvalidLocation, match="can't go inside a box"):
        make_location("Shelf X", "shelf", box)
    assert make_location("Bag 1", "bag", box).location_type.key == "bag"  # a bag in a box
    with pytest.raises(InvalidLocation, match="Unknown kind"):
        make_location("Cupboard", "cupboard", room)
    with pytest.raises(InvalidLocation, match="made on demand"):
        make_location("Mine", "unsorted", room)
    tagged = make_location("Box T", "box", room, tags=["antibody", "antibody", " "])
    assert tagged.tags == ["antibody"]
    with pytest.raises(InvalidLocation, match="Unknown item type"):
        make_location("Box U", "box", room, tags=["antibody", "unicorn"])
    assert make_location("Room 211   B", "room").name == "Room 211 B"


@pytest.mark.django_db
def test_location_types_missing_from_the_database_come_from_the_yaml(lab):
    assert not LocationType.objects.exists()
    box = services.create_location(
        lab=lab, parent=None, name="Box 1", location_type="room", user=None
    )
    assert box.location_type.label == "Room" and LocationType.objects.count() == 1


@pytest.mark.django_db
def test_parents_must_be_in_the_lab_active_and_real(
    make_location, room, other_lab, location_types, raj, member, outsider
):
    theirs = services.create_location(
        lab=other_lab, parent=None, name="Room 9", location_type="room", user=None
    )
    with pytest.raises(CrossLab):
        make_location("Shelf 1", "shelf", theirs)
    unsorted = services.unsorted_location(room.lab, member, "-20C")
    with pytest.raises(InvalidLocation, match="unsorted container"):
        make_location("Box 1", "box", unsorted)
    room.active = False
    room.save(update_fields=["active"])
    with pytest.raises(InvalidLocation, match="no longer in use"):
        make_location("Shelf 3", "shelf", room)
    with pytest.raises(InvalidCustodian):
        make_location("Room 300", "room", default_custodian=outsider)
    assert make_location("Room 301", "room", default_custodian=raj).default_custodian == raj


@pytest.mark.django_db
def test_moving_a_box_moves_its_contents(
    make_location, room, freezer, shelf, box, make_item, member
):
    item = make_item(position_label="A1")
    other_freezer = make_location("-20 B", "freezer", room, temperature="-20C")
    event = move_location(shelf, other_freezer, user=member, reason="defrosting -80 A")
    assert (event.location, event.from_parent, event.to_parent) == (shelf, freezer, other_freezer)
    assert (event.user, event.reason, event.lab) == (member, "defrosting -80 A", room.lab)
    box.refresh_from_db()
    assert box.path_text() == "Room 210 / -20 B / Shelf 1 / Box 4"
    assert box.effective_temperature() == "-20C"
    fresh = StockItem.objects.get(pk=item.pk)
    assert (fresh.location, fresh.position_label, fresh.version) == (box, "A1", 1)  # unchanged
    assert shelf.path.startswith(other_freezer.path)  # the caller's copy is refreshed
    assert Location.objects.get(pk=freezer.pk).numchild == 0
    assert problems() == []


@pytest.mark.django_db
def test_moves_to_the_top_level_and_back(make_location, room, freezer, member):
    event = move_location(freezer, None, user=member)
    assert (event.from_parent, event.to_parent) == (room, None)
    assert freezer.is_root() and freezer.path_text() == "-80 A"
    move_location(freezer, room, user=member)
    assert freezer.path_text() == "Room 210 / -80 A"
    assert LocationEvent.objects.filter(location=freezer).count() == 2
    assert problems() == []


@pytest.mark.django_db
def test_moves_that_make_no_sense_are_refused(
    make_location, room, freezer, shelf, box, other_lab, location_types, member
):
    with pytest.raises(InvalidLocation, match="inside itself"):
        move_location(freezer, freezer, user=None)
    with pytest.raises(InvalidLocation, match="inside itself"):
        move_location(freezer, box, user=None)
    with pytest.raises(InvalidLocation, match="already"):
        move_location(shelf, freezer, user=None)
    with pytest.raises(InvalidLocation, match="already"):
        move_location(room, None, user=None)
    with pytest.raises(InvalidLocation, match="can't go inside a box"):
        move_location(make_location("Shelf 2", "shelf", room), box, user=None)
    theirs = services.create_location(
        lab=other_lab, parent=None, name="Room 9", location_type="room", user=None
    )
    with pytest.raises(CrossLab):
        move_location(freezer, theirs, user=None)
    unsorted = services.unsorted_location(room.lab, member, "4C")
    with pytest.raises(InvalidLocation, match="can't move"):
        move_location(unsorted, room, user=None)
    with pytest.raises(InvalidLocation, match="unsorted container"):
        move_location(box, unsorted, user=None)
    make_location("Shelf 1", "shelf", room)
    with pytest.raises(InvalidLocation, match="already has a location called 'Shelf 1'"):
        move_location(shelf, room, user=None)
    assert LocationEvent.objects.count() == 0
    assert problems() == []


@pytest.mark.django_db
def test_unsorted_containers(lab, member, raj, pool, leaver, outsider, location_types):
    cold = services.unsorted_location(lab, member, "-20C")
    assert cold.is_unsorted and cold.is_root() and cold.visibility == "shared"
    assert cold.location_type.key == "unsorted" and len(cold.code) == 5
    assert str(cold) == cold.path_text() == "Unsorted: Priya Patel, -20 °C"
    assert services.unsorted_location(lab, member, "-20 C") == cold
    assert services.unsorted_location(lab, member, "4C") != cold
    assert services.unsorted_location(lab, raj, "-20C") != cold
    assert services.unsorted_location(lab, member, "").display_name == "Unsorted: Priya Patel"
    assert services.unsorted_location(lab, member, None).temperature == ""
    assert services.unsorted_location(lab, pool, "RT").unsorted_owner == pool
    assert services.unsorted_location(lab, leaver, "LN2").unsorted_owner == leaver
    with pytest.raises(InvalidCustodian):
        services.unsorted_location(lab, outsider, "-20C")
    with pytest.raises(InvalidLocation):
        services.unsorted_location(lab, member, "freezing")
    assert problems() == []


@pytest.mark.django_db
def test_grid_and_linear_positions(location_types):
    box = LocationType.objects.get(key="box")
    assert [box.position_index(label) for label in ("A1", "a9", "B1", "i9", "B-3")] == [
        1,
        9,
        10,
        81,
        12,
    ]
    assert [box.position_index(label) for label in ("J1", "A10", "A0", "", "top")] == [None] * 5
    assert [box.position_label(index) for index in (1, 10, 81, 82, 0)] == ["A1", "B1", "I9", "", ""]
    big = LocationType.objects.get(key="box_10x10")
    assert (big.position_index("J10"), big.position_label(100)) == (100, "J10")
    strip = LocationType.objects.create(
        key="strip", label="Strip", position_scheme="linear", cols=8, prefix="S"
    )
    assert [strip.position_index(label) for label in ("S1", "s8", "S9", "1")] == [1, 8, None, None]
    assert strip.position_label(3) == "S3"
    assert LocationType.objects.get(key="shelf").position_index("A1") is None
    # Written like a position, whether or not the type has it: the services refuse "J1" in
    # a 9 x 9 box, and keep "top" as free text.
    assert [box.looks_like_position(label) for label in ("J1", "a-10", "I9", "AA1", "top")] == [
        True,
        True,
        True,
        False,
        False,
    ]
    assert [strip.looks_like_position(label) for label in ("S9", "s 0", "S²", "slot 1")] == [
        True,
        True,
        False,
        False,
    ]
    assert not LocationType.objects.get(key="shelf").looks_like_position("A1")


@pytest.mark.django_db
def test_a_registered_box_keeps_its_lid_photo(
    make_location, shelf, member, raj, settings, tmp_path
):
    from django.core.files.uploadedfile import SimpleUploadedFile

    from apps.inventory.presenters import present_location

    settings.MEDIA_ROOT = tmp_path
    lid = SimpleUploadedFile("lid.jpg", b"\xff\xd8\xff not really a jpeg", "image/jpeg")
    box = make_location("Box 12", "box", shelf, default_custodian=member, photo=lid)
    assert box.photo.name.startswith("location-photos/") and box.photo.read().startswith(b"\xff")
    assert present_location(box, raj, channel="page").has_photo
    assert not make_location("Box 13", "box", shelf).photo


@pytest.mark.django_db
def test_a_retired_location_is_checked_only_when_it_comes_back(make_location, room, shelf):
    old = make_location("Box 9", "box", shelf)
    old.active = False
    old.save(update_fields=["active"])
    make_location("Box 9", "box", shelf)  # its replacement
    box_type = LocationType.objects.get(key="box")

    def check(location, *, active, parent=shelf):
        services.validate_placement(
            lab=room.lab, parent=parent, location_type=box_type, name=location.name,
            location=location, active=active,
        )  # fmt: skip

    check(old, active=False)  # its notes can still be edited
    with pytest.raises(InvalidLocation, match="already has a location called 'Box 9'"):
        check(old, active=True)  # brought back into use: two Box 9s in use
    shelf.active = False  # the shelf is retired after its boxes
    shelf.save(update_fields=["active"])
    check(old, active=False)
    with pytest.raises(InvalidLocation, match="Shelf 1 is no longer in use"):
        check(old, active=True)  # nothing comes back into use inside a retired place
    other = make_location("Shelf 2", "shelf", room)
    lone = make_location("Box 12", "box", other)
    lone.active = False
    lone.save(update_fields=["active"])
    with pytest.raises(InvalidLocation, match="Shelf 1 is no longer in use"):
        check(lone, active=False)  # moving it, even retired, into a retired shelf


@pytest.mark.django_db
def test_a_move_cancelled_to_break_a_deadlock_asks_to_try_again(
    room, freezer, shelf, make_location, monkeypatch
):
    import psycopg
    from django.db import OperationalError

    from apps.inventory.models import LocationManager

    other = make_location("-20 B", "freezer", room)

    def cancelled(*args, **kwargs):
        # What Django raises when Postgres picks this transaction to break a deadlock.
        raise OperationalError("deadlock detected") from psycopg.errors.DeadlockDetected()

    monkeypatch.setattr(LocationManager, "move", cancelled)
    with pytest.raises(InvalidLocation, match="Someone else is changing these locations"):
        move_location(shelf, other, user=None)

    def broken(*args, **kwargs):
        raise OperationalError("server closed the connection") from psycopg.OperationalError()

    monkeypatch.setattr(LocationManager, "move", broken)
    with pytest.raises(OperationalError, match="server closed"):
        move_location(shelf, other, user=None)
    assert LocationEvent.objects.count() == 0
    assert Location.objects.get(pk=shelf.pk).path.startswith(freezer.path)
