"""import_locations: the committed template imports cleanly, re-runs create nothing, rows
come in any order, missing parents are implied, and any error saves nothing."""

import csv
import random
from io import StringIO
from pathlib import Path

import pytest
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.core.models import Membership, User
from apps.inventory import services
from apps.inventory.importers import ImportFailed, PathIndex, import_locations, import_run
from apps.inventory.models import Location, LocationType
from apps.inventory.selectors import location_by_path

TEMPLATE = Path(settings.BASE_DIR) / "docs" / "templates" / "locations.csv"
HEADER = ["path", "kind", "temperature", "owner_email", "code", "notes"]


@pytest.fixture
def priya(lab):
    """The template's owner_email."""
    user = User.objects.create_user(email="priya@example.edu", full_name="Priya Example")
    Membership.objects.create(user=user, lab=lab)
    return user


def write_csv(tmp_path, rows, header=HEADER):
    path = Path(tmp_path) / "locations.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    return str(path)


def run(path, lab, *args):
    out = StringIO()
    call_command("import_locations", path, "--lab", lab.slug, *args, stdout=out, stderr=StringIO())
    return out.getvalue()


def tree(lab):
    return {
        location.path_text(): (
            location.location_type.key,
            location.temperature,
            location.default_custodian.email if location.default_custodian else "",
            location.notes,
        )
        for location in Location.objects.for_lab(lab)
    }


EXPECTED = {
    "Room 210": ("room", "", "", ""),
    "Room 210 / -80 A": ("freezer", "-80C", "", "shared -80 by the window"),
    "Room 210 / -80 A / Shelf 1": ("shelf", "-80C", "", ""),
    "Room 210 / -80 A / Shelf 1 / Rack 3": ("rack", "-80C", "priya@example.edu", ""),
    "Room 210 / -20 B": ("freezer", "-20C", "", ""),
    "Room 210 / Fridge 1": ("fridge", "4C", "", ""),
    "Room 214": ("room", "", "", ""),  # implied by the dewar's row
    "Room 214 / LN2 dewar": ("ln2_dewar", "LN2", "", "cell line stocks"),
}


@pytest.mark.django_db
def test_the_committed_template_imports_cleanly(lab, priya, location_types):
    out = run(str(TEMPLATE), lab)
    assert "Dry run: 8 to create, 0 already in LabStock; nothing saved." in out
    assert "would create: Room 214 (room) [implied by line 8]" in out
    assert not Location.objects.exists()

    out = run(str(TEMPLATE), lab, "--commit")
    assert "Saved: 8 location(s) created, 0 already in LabStock." in out
    assert tree(lab) == EXPECTED
    codes = dict(Location.objects.values_list("name", "code"))
    assert (codes["-80 A"], codes["-20 B"], codes["Fridge 1"], codes["LN2 dewar"]) == (
        "F80A",
        "F20B",
        "R1",
        "LN2-1",
    )
    assert len(codes["Room 210"]) == 5  # generated
    assert f"[implied by line 8, code {codes['Room 214']}]" in out
    assert not [problem for problem in Location.objects.find_problems() if problem]


@pytest.mark.django_db
def test_re_running_creates_nothing(lab, priya, location_types):
    run(str(TEMPLATE), lab, "--commit")
    before = set(Location.objects.values_list("pk", "path", "code"))
    out = run(str(TEMPLATE), lab, "--commit")
    assert "Saved: 0 location(s) created, 7 already in LabStock." in out
    assert set(Location.objects.values_list("pk", "path", "code")) == before
    assert "Dry run: 0 to create, 7 already in LabStock" in run(str(TEMPLATE), lab)


@pytest.mark.django_db
@pytest.mark.parametrize("seed", range(5))
def test_rows_may_come_in_any_order(lab, priya, location_types, tmp_path, seed):
    with TEMPLATE.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))[1:]
    random.Random(seed).shuffle(rows)
    run(write_csv(tmp_path, rows), lab, "--commit")
    assert tree(lab) == EXPECTED


@pytest.mark.django_db
def test_paths_match_whatever_the_case_and_spacing(lab, location_types, tmp_path, room):
    report = import_locations(
        write_csv(tmp_path, [["room 210", "room", "", "", "", ""],
                             ["ROOM 210/  Bench   2", "bench", "rt", "", "b-2", ""]]),
        lab=lab, commit=True,
    )  # fmt: skip
    assert [plan.path for plan, _ in report.existing] == ["room 210"]
    bench = Location.objects.get(name="Bench 2")
    assert (bench.path_text(), bench.temperature, bench.code) == ("Room 210 / Bench 2", "RT", "B-2")


@pytest.mark.django_db
def test_implied_parents_take_their_kind_from_their_name(lab, location_types, tmp_path):
    path = write_csv(tmp_path, [["Room 5 / Freezer 3 / Shelf 2 / Box 4", "box", "", "", "", ""]])
    report = import_locations(path, lab=lab, commit=True)
    assert [(plan.path, plan.kind.key, plan.implied_by) for plan in report.created] == [
        ("Room 5", "room", 2),
        ("Room 5 / Freezer 3", "freezer", 2),
        ("Room 5 / Freezer 3 / Shelf 2", "shelf", 2),
        ("Room 5 / Freezer 3 / Shelf 2 / Box 4", "box", None),
    ]
    with pytest.raises(ImportFailed) as failed:
        import_locations(
            write_csv(tmp_path, [["Room 6 / Big Thing / Box 1", "box", "", "", "", ""]]), lab=lab
        )
    assert failed.value.errors == [
        "line 2: Room 6 / Big Thing isn't in LabStock, and its kind can't be told from its name; "
        "give it a row in locations.csv"
    ]


@pytest.mark.django_db
def test_a_path_index_finds_paths_and_makes_the_missing_parts(lab, box):
    """What import_stock --create-locations builds on."""
    paths = PathIndex(lab)
    assert paths.get("room 210 / -80 A / shelf 1 / box 4") == box
    assert paths.get(("Room 210", "-80 a")) == box.ancestors()[1]
    assert paths.get("Room 210 / Box 4") is None
    assert paths.ensure(" Room 210/-80 A/Shelf 1/Box 4 ") == box and paths.created == []
    made = paths.ensure("Room 210 / -20 B / Rack 2 / Box 9", line=7)
    assert made.path_text() == "Room 210 / -20 B / Rack 2 / Box 9"
    assert [
        (plan.path, plan.kind.key, plan.temperature, plan.implied_by) for plan in paths.created
    ] == [
        ("Room 210 / -20 B", "freezer", "-20C", 7),
        ("Room 210 / -20 B / Rack 2", "rack", "", 7),
        ("Room 210 / -20 B / Rack 2 / Box 9", "box", "", 7),
    ]
    assert PathIndex(lab).get("Room 210 / -20 B / Rack 2 / Box 9") == made
    with pytest.raises(services.InventoryError, match="kind can't be told"):
        paths.ensure("Room 210 / Big Thing")
    with pytest.raises(services.InventoryError, match="empty"):
        paths.ensure(" / ")


@pytest.mark.django_db
def test_an_import_run_saves_only_a_clean_commit(lab, location_types):
    def run_once(*, commit, fail=False):
        with import_run(commit=commit) as errors:
            services.create_location(
                lab=lab, parent=None, name="Room 1", location_type="room", user=None
            )
            if fail:
                errors.append("line 3: something is wrong")

    run_once(commit=False)
    assert not Location.objects.exists()
    with pytest.raises(ImportFailed) as failed:
        run_once(commit=True, fail=True)
    assert failed.value.errors == ["line 3: something is wrong"]
    assert not Location.objects.exists()
    run_once(commit=True)
    assert Location.objects.get().name == "Room 1"


@pytest.mark.django_db
def test_existing_locations_are_kept_and_differences_reported(lab, location_types, room, tmp_path):
    rows = [["Room 210", "freezer", "4C", "", "ZZ9", "a note"]]
    report = import_locations(write_csv(tmp_path, rows), lab=lab, commit=True)
    ((_, differences),) = report.existing
    assert differences == [
        "kind is room in LabStock",
        "temperature is blank in LabStock",
        f"code is {room.code} in LabStock",
        "notes is blank in LabStock",
    ]
    assert Location.objects.get(pk=room.pk).location_type.key == "room"
    out = run(write_csv(tmp_path, rows), lab)
    assert "exists: Room 210 [line 2]; kept: kind is room in LabStock; temperature" in out


@pytest.mark.django_db
def test_errors_name_their_line_and_nothing_is_saved(
    lab, priya, outsider, other_lab, location_types, tmp_path
):
    services.create_location(
        lab=other_lab, parent=None, name="Room 1", location_type="room", user=None, code="TAKEN"
    )
    rows = [
        ["Room 210", "room", "", "", "", ""],  # fine on its own
        ["Room 210 / Cupboard", "cupboard", "", "", "", ""],
        ["Room 210 / Fridge 9", "fridge", "warm", "", "", ""],
        ["Room 210 / Fridge 8", "fridge", "", "nobody@example.edu", "", ""],
        ["Room 210 / Fridge 7", "fridge", "", "olu@other.test", "", ""],
        ["Room 210 / Fridge 6", "fridge", "", "", "SHELF1", ""],
        ["Room 210 / Fridge 5", "fridge", "", "", "R 1", ""],
        ["Room 210 / Fridge 4", "fridge", "", "", "DUP", ""],
        ["Room 210 / Fridge 3", "fridge", "", "", "DUP", ""],
        ["room 210 / fridge 4", "fridge", "", "", "", ""],
        [" / ", "room", "", "", "", ""],
        ["Room 210 / Shelf 1/2", "", "", "", "", ""],
        ["Room 210 / Unsorted", "unsorted", "", "", "", ""],
    ]
    with pytest.raises(ImportFailed) as failed:
        import_locations(write_csv(tmp_path, rows), lab=lab, commit=True)
    errors = failed.value.errors
    for line, words in [
        (3, "unknown kind 'cupboard'"),
        (4, "Unknown temperature 'warm'"),
        (5, "no one in Test Lab has the email nobody@example.edu"),
        (6, "no one in Test Lab has the email olu@other.test"),
        (7, "reads like a stock item's 6-character id"),
        (8, "isn't a valid code"),
        (10, "the code DUP is already on line 9"),
        (11, "room 210 / fridge 4 is already on line 9"),
        (12, "the path is empty"),
        (13, "give a kind"),
        (14, "give a kind"),
    ]:
        assert any(error.startswith(f"line {line}: ") and words in error for error in errors), (
            line,
            errors,
        )
    assert not any(error.startswith("line 2:") for error in errors)
    assert not Location.objects.for_lab(lab).exists()

    # A code another lab uses is caught when the location is created; still nothing saved.
    rows = [
        ["Room 210", "room", "", "", "", ""],
        ["Room 210 / Fridge 1", "fridge", "", "", "taken", ""],
    ]
    with pytest.raises(ImportFailed) as failed:
        import_locations(write_csv(tmp_path, rows), lab=lab, commit=True)
    assert failed.value.errors == [
        "line 3: The code TAKEN is already in use; codes are unique in LabStock."
    ]
    assert not Location.objects.for_lab(lab).exists()


@pytest.mark.django_db
def test_the_command_reports_errors_and_missing_columns(lab, location_types, tmp_path):
    bad = write_csv(tmp_path, [["Room 1", "spaceship", "", "", "", ""]])
    with pytest.raises(CommandError, match="1 error"):
        run(bad, lab, "--commit")
    no_kind = write_csv(tmp_path, [["Room 1"]], header=["path"])
    with pytest.raises(CommandError, match="1 error"):
        run(no_kind, lab)
    with pytest.raises(ImportFailed, match="1 error"):
        import_locations(no_kind, lab=lab)
    with pytest.raises(CommandError, match="No lab"):
        call_command("import_locations", bad, "--lab", "nope")
    with pytest.raises(CommandError, match="Can't read"):
        run(str(Path(tmp_path) / "missing.csv"), lab)
    assert not Location.objects.exists()


@pytest.mark.django_db
def test_a_dry_run_rolls_back_location_types_too(lab, priya):
    # Nothing seeded: the import creates the types it needs, and the dry run undoes them.
    run(str(TEMPLATE), lab)
    assert not Location.objects.exists()
    assert not LocationType.objects.exists()


@pytest.mark.django_db
def test_an_import_creates_only_the_location_types_it_uses(lab, tmp_path):
    import_locations(write_csv(tmp_path, [["Room 1 / Fridge 2", "fridge", "", "", "", ""]]),
                     lab=lab, commit=True)  # fmt: skip
    assert set(LocationType.objects.values_list("key", flat=True)) == {"room", "fridge"}


@pytest.mark.django_db
def test_freezers_and_fridges_named_by_temperature_are_implied(lab, location_types, tmp_path):
    rows = [
        ["Room 210 / -80 A / Shelf 1", "shelf", "", "", "", ""],
        ["Room 210 / 4C door / Bag 2", "bag", "", "", "", ""],
        ["4 West / Bench 1", "bench", "", "", "", ""],
    ]
    report = import_locations(write_csv(tmp_path, rows), lab=lab, commit=True)
    implied = {
        plan.path: (plan.kind.key, plan.temperature, plan.implied_by)
        for plan in report.created
        if plan.implied_by
    }
    assert implied == {
        "4 West": ("room", "", 4),  # a top-level part is a room, whatever its name
        "Room 210": ("room", "", 2),
        "Room 210 / -80 A": ("freezer", "-80C", 2),
        "Room 210 / 4C door": ("fridge", "4C", 3),
    }
    shelf = location_by_path(lab, "Room 210 / -80 A / Shelf 1")
    assert shelf.effective_temperature() == "-80C"


@pytest.mark.django_db
def test_a_file_that_isnt_utf8_names_the_line(lab, location_types, tmp_path):
    path = Path(tmp_path) / "locations.csv"
    # Excel's plain "CSV" on Windows writes cp1252: the en dash is byte 0x96.
    path.write_bytes("path,kind\nRoom 210,room\nRoom 211 \u2013 annex,room\n".encode("cp1252"))
    with pytest.raises(ImportFailed) as failed:
        import_locations(str(path), lab=lab)
    assert failed.value.errors == [
        "line 3: the file isn't UTF-8 text; save it as 'CSV UTF-8' and try again"
    ]
    with pytest.raises(CommandError, match="1 error"):
        run(str(path), lab)
    assert not Location.objects.exists()


@pytest.mark.django_db
def test_a_top_level_part_is_a_room_whatever_its_first_word(lab, location_types, tmp_path):
    rows = [
        ["Freezer room B / -80 A", "freezer", "-80C", "", "", ""],
        ["Box room / Shelf 1", "shelf", "", "", "", ""],
        ["Bench area / Bench 1", "bench", "", "", "", ""],
    ]
    report = import_locations(write_csv(tmp_path, rows), lab=lab, commit=True)
    implied = {
        plan.path: (plan.kind.key, plan.temperature) for plan in report.created if plan.implied_by
    }
    assert implied == {
        "Freezer room B": ("room", ""),
        "Box room": ("room", ""),
        "Bench area": ("room", ""),
    }
    assert location_by_path(lab, "Box room / Shelf 1").location_type.key == "shelf"


@pytest.mark.django_db
def test_a_path_index_prefers_the_location_in_use(lab, make_location, shelf):
    # Box 9 was retired and replaced; later the new one was retired and the old one is back.
    first = make_location("Box 9", "box", shelf)
    first.active = False
    first.save(update_fields=["active"])
    second = make_location("Box 9", "box", shelf)
    second.active = False
    second.save(update_fields=["active"])
    first.active = True
    first.save(update_fields=["active"])
    path = "Room 210 / -80 A / Shelf 1 / Box 9"
    assert location_by_path(lab, path) == first
    assert PathIndex(lab).get(path) == first
    assert PathIndex(lab).ensure(path) == first
    first.active = False
    first.save(update_fields=["active"])  # both retired: still found, as location_by_path does
    assert PathIndex(lab).get(path) == location_by_path(lab, path, include_inactive=True)
