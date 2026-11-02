"""Load the location types (data/location_types.yaml) into the inventory.

Safe to re-run, and it never deletes. As with seed_catalog, an existing row's field changes
only while it still holds the value seed_inventory last wrote there (its ``seeded``
snapshot); anything a person changed afterwards in the admin stays as they set it, and the
command lists where the YAML disagrees.
"""

import re
from collections import Counter
from pathlib import Path

import yaml
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.catalog.models import seed_snapshot
from apps.inventory.models import LocationType

LOCATION_TYPES_YAML = Path(__file__).resolve().parents[2] / "data" / "location_types.yaml"


class DryRun(Exception):
    pass


def load_yaml(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as exc:
        raise CommandError(f"Can't read {path}: {exc}") from exc


def location_type_values(data, path):
    """Validate location_types.yaml; return ``[(key, field values), ...]`` in file order."""
    entries = data.get("location_types") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise CommandError(f"{path}: expected a list under 'location_types'")
    schemes = set(LocationType.PositionScheme.values)
    result, seen = [], set()
    for position, entry in enumerate(entries, start=1):
        key = entry.get("key") if isinstance(entry, dict) else None
        if not isinstance(key, str) or not re.fullmatch(r"[a-z0-9_]+", key) or key in seen:
            raise CommandError(f"{path}: location type #{position} has a bad or repeated key")
        seen.add(key)
        values = {
            "label": entry.get("label") or key.replace("_", " ").capitalize(),
            "structural": entry.get("structural", True),
            "position_scheme": entry.get("position_scheme", LocationType.PositionScheme.NONE.value),
            "rows": entry.get("rows"),
            "cols": entry.get("cols"),
            "prefix": entry.get("prefix") or "",
            "sort_order": entry.get("sort_order", position * 10),
        }
        grid = values["position_scheme"] == LocationType.PositionScheme.GRID
        checks = [
            (isinstance(values["structural"], bool), "structural must be true or false"),
            (values["position_scheme"] in schemes,
             f"position_scheme must be one of {sorted(schemes)}"),
            (all(values[side] is None or (type(values[side]) is int and values[side] >= 1)
                 for side in ("rows", "cols")),
             "rows and cols must be whole numbers"),
            (not grid or (values["rows"] and values["cols"] and values["rows"] <= 26),
             "a grid needs rows (at most 26, lettered A-Z) and cols"),
            (type(values["sort_order"]) is int and values["sort_order"] >= 0,
             "sort_order must be a whole number"),
        ]  # fmt: skip
        for ok, message in checks:
            if not ok:
                raise CommandError(f"{path}: {key}: {message}")
        result.append((key, values))
    return result


def seeded_location_type(key):
    """The YAML's field values for ``key``, or None when the YAML doesn't know it."""
    values = dict(location_type_values(load_yaml(LOCATION_TYPES_YAML), LOCATION_TYPES_YAML))
    return values.get(key)


def _differs(obj, values):
    return [name for name, value in values.items() if getattr(obj, name) != value]


class Command(BaseCommand):
    help = "Load location_types.yaml into the inventory (idempotent, never deletes)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true", help="Report what would change; save nothing."
        )
        parser.add_argument(
            "--location-types",
            default=str(LOCATION_TYPES_YAML),
            help="location_types.yaml to load",
        )

    def handle(self, *args, dry_run, location_types, **options):
        types = location_type_values(load_yaml(location_types), location_types)
        self.verbosity = options.get("verbosity", 1)
        self.counts, self.notes = Counter(), []
        try:
            with transaction.atomic():
                for key, values in types:
                    self._seed(key, values)
                if dry_run:
                    raise DryRun
        except DryRun:
            pass
        self._report(dry_run)

    def _seed(self, key, values):
        location_type = LocationType.objects.filter(key=key).first()
        if location_type is None:
            LocationType.objects.create(key=key, **values, seeded=seed_snapshot(values))
            self._did("created", key)
            return
        changed = location_type.apply_seed(values)
        if changed:
            location_type.save()
            self._did("updated", f"{key}: {', '.join(changed)}")
        kept = _differs(location_type, values)
        if kept:
            self.counts["kept"] += 1
            self.notes.append(f"location type {key}: kept people's {', '.join(kept)}")

    def _did(self, verb, detail):
        self.counts[verb] += 1
        if self.verbosity >= 2:
            self.stdout.write(f"{verb} location type: {detail}")

    def _report(self, dry_run):
        parts = [
            f"{self.counts[verb]} {verb}"
            for verb in ("created", "updated", "kept")
            if self.counts[verb] or verb == "created"
        ]
        self.stdout.write(f"Location types: {', '.join(parts)}")
        if self.notes and self.verbosity >= 1:
            self.stdout.write("Left as people set them (location_types.yaml differs):")
            for note in self.notes:
                self.stdout.write(f"  {note}")
        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run: nothing saved."))
        else:
            self.stdout.write(self.style.SUCCESS("Inventory seeded."))
