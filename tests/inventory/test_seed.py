"""seed_inventory loads location_types.yaml, re-runs safely and keeps people's edits."""

from io import StringIO
from pathlib import Path

import pytest
import yaml
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.inventory.management.commands.seed_inventory import LOCATION_TYPES_YAML
from apps.inventory.models import LocationType

DESIGNED = [
    "room", "freezer", "fridge", "ln2_dewar", "cabinet", "bench", "shelf", "rack", "drawer",
    "box", "box_10x10", "bag", "unsorted",
]  # fmt: skip


def seed(*args):
    out = StringIO()
    call_command("seed_inventory", *args, stdout=out)
    return out.getvalue()


def write_yaml(tmp_path, data):
    path = Path(tmp_path) / "location_types.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return str(path)


@pytest.mark.django_db
def test_seed_loads_the_designed_location_types():
    assert "Location types: 13 created" in seed()
    assert list(LocationType.objects.values_list("key", flat=True)) == DESIGNED
    box = LocationType.objects.get(key="box")
    assert (box.structural, box.position_scheme, box.rows, box.cols) == (False, "grid", 9, 9)
    big = LocationType.objects.get(key="box_10x10")
    assert (big.rows, big.cols, big.capacity) == (10, 10, 100)
    structural = set(LocationType.objects.filter(structural=True).values_list("key", flat=True))
    assert structural == set(DESIGNED) - {"box", "box_10x10", "bag", "unsorted"}
    assert LocationType.objects.get(key="freezer").position_scheme == "none"


@pytest.mark.django_db
def test_reseeding_changes_nothing_and_keeps_peoples_edits(tmp_path):
    seed()
    shelf = LocationType.objects.get(key="shelf")
    shelf.label = "Shelf (co-owner's word)"
    shelf.save()
    out = seed()
    assert "0 created" in out and "1 kept" in out and "kept people's label" in out
    assert LocationType.objects.get(key="shelf").label == "Shelf (co-owner's word)"
    assert LocationType.objects.count() == 13

    data = yaml.safe_load(LOCATION_TYPES_YAML.read_text())
    for entry in data["location_types"]:
        if entry["key"] in ("fridge", "shelf"):
            entry["label"] = entry["label"] + " (new)"
    data["location_types"].append({"key": "cryo_cane", "label": "Cryo cane", "structural": False})
    out = seed("--location-types", write_yaml(tmp_path, data))
    assert "1 created" in out and "1 updated" in out
    assert LocationType.objects.get(key="fridge").label == "Fridge (new)"  # still the seed's
    assert LocationType.objects.get(key="shelf").label == "Shelf (co-owner's word)"


@pytest.mark.django_db
def test_a_dry_run_saves_nothing():
    assert "Dry run: nothing saved." in seed("--dry-run")
    assert not LocationType.objects.exists()


@pytest.mark.parametrize(
    "entries",
    [
        [{"key": "Bad Key"}],
        [{"key": "box"}, {"key": "box"}],
        [{"key": "box", "position_scheme": "grid", "rows": 9}],
        [{"key": "box", "position_scheme": "grid", "rows": 27, "cols": 9}],
        [{"key": "box", "position_scheme": "spiral"}],
        [{"key": "box", "structural": "yes"}],
        [{"key": "box", "rows": 0}],
    ],
)
@pytest.mark.django_db
def test_a_bad_yaml_is_refused(tmp_path, entries):
    with pytest.raises(CommandError):
        seed("--location-types", write_yaml(tmp_path, {"location_types": entries}))
    assert not LocationType.objects.exists()
