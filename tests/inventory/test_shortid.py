"""Short ids and location codes: Crockford characters, what people type, uniqueness."""

import re

import pytest
from django.db import IntegrityError, connection, transaction

from apps.inventory import services, shortid
from apps.inventory.models import Location, StockItem

CROCKFORD_6 = re.compile(r"^[0-9A-HJKMNP-TV-Z]{6}$")


def test_generated_codes_use_only_crockford_characters():
    ids = {shortid.new_short_id() for _ in range(500)}
    assert all(CROCKFORD_6.fullmatch(value) for value in ids)
    assert len(ids) > 490  # random, not a sequence
    codes = {shortid.new_location_code() for _ in range(200)}
    assert all(len(code) == 5 and set(code) <= set(shortid.CROCKFORD) for code in codes)
    assert not set(shortid.CROCKFORD) & set("ILOU")


@pytest.mark.parametrize(
    ("typed", "short_id"),
    [
        ("7K3M9Q", "7K3M9Q"),
        ("7k3m9q", "7K3M9Q"),
        (" 7K3-M9Q ", "7K3M9Q"),
        ("7K3 M9Q", "7K3M9Q"),
        ("7K3MOQ", "7K3M0Q"),  # O read as 0
        ("I0LABC", "101ABC"),  # I and L read as 1
        ("il-oo-11", "110011"),
        ("7K3M9", None),  # too short
        ("7K3M9QX", None),  # too long
        ("7K3M9U", None),  # U is not Crockford
        ("7K3M9!", None),
        ("", None),
        (None, None),
    ],
)
def test_short_ids_are_read_the_way_people_type_them(typed, short_id):
    assert shortid.as_short_id(typed) == short_id


@pytest.mark.parametrize(
    ("code", "ok"),
    [
        ("F80A", True),
        ("LN2-1", True),
        ("R1", True),
        ("A", True),
        ("BOX-12", True),  # 5 characters once the hyphen is gone
        ("UNIT12", True),  # 6 characters, but U is never in a short id
        ("ABCDEFGHJKMN", True),  # 12
        ("ABCDEFGHJKMNP", False),  # 13
        ("F80 A", False),
        ("F80_A", False),
        ("Ä1", False),
        ("7K3M9Q", False),  # exactly 6 Crockford characters: reads as a stock item id
        ("SHELF1", False),  # reads as SHE1F1
        ("OLDBOX", False),  # reads as 01DB0X
        ("7K3-M9Q", False),  # hyphens are ignored when reading an id
    ],
)
def test_hand_made_location_codes(code, ok):
    assert (shortid.location_code_problem(code) is None) is ok


@pytest.mark.django_db
def test_short_ids_are_stored_in_upper_case_and_unique(make_item):
    items = [make_item() for _ in range(5)]
    assert all(CROCKFORD_6.fullmatch(item.short_id) for item in items)
    assert len({item.short_id for item in items}) == 5
    assert StockItem.objects.get(short_id=items[0].short_id) == items[0]


@pytest.mark.django_db
def test_the_database_refuses_a_short_id_that_is_not_crockford(item):
    for bad in ("abcdef", "7K3M9U", "7K3M9"):
        with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE inventory_stockitem SET short_id = %s WHERE id = %s", [bad, item.pk]
            )


@pytest.mark.django_db
def test_a_short_id_collision_draws_a_new_one(make_item, monkeypatch):
    first = make_item()
    drawn = iter([first.short_id, first.short_id, "ZZZZZZ"])
    monkeypatch.setattr(shortid, "new_short_id", lambda: next(drawn))
    second = make_item()
    assert second.short_id == "ZZZZZZ"


@pytest.mark.django_db
def test_a_short_id_taken_between_the_check_and_the_insert_is_retried(make_item, monkeypatch):
    first = make_item()
    drawn = iter([first.short_id, "YYYYYY"])
    monkeypatch.setattr(shortid, "new_short_id", lambda: next(drawn))
    # The pre-insert check misses the clash once, as it would when another transaction
    # inserts the same id at the same moment; the unique index catches it.
    real_filter = StockItem._base_manager.filter
    misses = {"left": 1}

    def stale_filter(*args, **kwargs):
        rows = real_filter(*args, **kwargs)
        if kwargs.get("short_id") == first.short_id and misses["left"]:
            misses["left"] -= 1
            return rows.none()
        return rows

    monkeypatch.setattr(StockItem._base_manager, "filter", stale_filter)
    assert make_item().short_id == "YYYYYY"


@pytest.mark.django_db
def test_blank_location_codes_are_generated(make_location):
    room = make_location("Room 210", "room")
    assert len(room.code) == 5 and set(room.code) <= set(shortid.CROCKFORD)


@pytest.mark.django_db
def test_location_codes_are_unique_case_insensitively_across_labs(
    make_location, other_lab, location_types
):
    make_location("Room 210", "room", code="f80a")
    assert Location.objects.get().code == "F80A"
    with pytest.raises(services.InvalidLocation, match="already in use"):
        services.create_location(
            lab=other_lab, parent=None, name="Room 1", location_type="room", user=None, code="F80a"
        )
    with pytest.raises(services.InvalidLocation, match="reads like"):
        make_location("Room 211", "room", code="shelf1")
    with pytest.raises(services.InvalidLocation, match="isn't a valid code"):
        make_location("Room 212", "room", code="F80 A")


@pytest.mark.django_db
def test_the_database_keeps_location_codes_upper_case(make_location):
    room = make_location("Room 210", "room", code="F80A")
    with pytest.raises(IntegrityError), transaction.atomic():
        Location.objects.filter(pk=room.pk).update(code="f80a")
    with pytest.raises(ValueError, match="reads like"):
        room.code = "SHELF1"
        room.save()
