"""Load vendor families, vendors and aliases (data/vendors.yaml) and item types
(data/item_types.yaml) into the catalog.

Safe to re-run, and it never deletes. The rule for existing rows, kept simple on purpose:
seed_catalog changes a field only while it still holds the value seed_catalog last wrote
there (each row's ``seeded`` snapshot). Anything a person changed afterwards, in the admin or
elsewhere, stays as they set it, and the command lists where the YAML disagrees.

Rows the sync created for a name the YAML didn't know yet have no snapshot. While they are
still provisional (nobody reviewed them), the seed adopts them and writes all its fields;
once reviewed they belong to people. Merged rows (redirects) are left alone.
"""

import re
from collections import Counter
from pathlib import Path

import yaml
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.catalog.models import ItemType, Vendor, VendorAlias, VendorFamily, seed_snapshot
from apps.catalog.normalization import VENDORS_YAML, build_vendor_table, normalize_vendor_name
from apps.catalog.services import (
    canonical,
    family_seed_values,
    move_vendor,
    refresh_catalog_keys,
    vendor_seed_values,
)

ITEM_TYPES_YAML = Path(__file__).resolve().parents[2] / "data" / "item_types.yaml"
_SINGULAR = {
    "families": "family",
    "vendors": "vendor",
    "aliases": "alias",
    "item types": "item type",
}


class DryRun(Exception):
    pass


def load_yaml(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as exc:
        raise CommandError(f"Can't read {path}: {exc}") from exc


def item_type_values(data, path):
    """Validate item_types.yaml; return ``[(key, field values), ...]`` in file order."""
    entries = data.get("item_types") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise CommandError(f"{path}: expected a list under 'item_types'")
    rules, modes = set(ItemType.IdentityRule.values), set(ItemType.TrackingMode.values)
    result, seen = [], set()
    for position, entry in enumerate(entries, start=1):
        key = entry.get("key") if isinstance(entry, dict) else None
        if not isinstance(key, str) or not re.fullmatch(r"[a-z0-9_]+", key) or key in seen:
            raise CommandError(f"{path}: item type #{position} has a bad or repeated key")
        seen.add(key)
        values = {
            "label": entry.get("label") or key.replace("_", " ").capitalize(),
            "tier": entry.get("tier"),
            "durable": entry.get("durable", False),
            "lookback_months": entry.get("lookback_months", 0),
            "in_scope": entry.get("in_scope", True),
            "identity_rule": entry.get("identity_rule", ItemType.IdentityRule.CATALOG.value),
            "tracking_mode": entry.get("tracking_mode", ItemType.TrackingMode.CONTAINER.value),
            "product_schema": entry.get("product_schema") or {},
            "instance_schema": entry.get("instance_schema") or {},
            "quartzy_type_keywords": entry.get("quartzy_type_keywords") or [],
            "sort_order": entry.get("sort_order", position * 10),
        }
        checks = [
            (type(values["tier"]) is int and 1 <= values["tier"] <= 5, "tier must be 1-5"),
            (type(values["lookback_months"]) is int and values["lookback_months"] >= 0,
             "lookback_months must be a whole number"),
            (isinstance(values["durable"], bool) and isinstance(values["in_scope"], bool),
             "durable and in_scope must be true or false"),
            (values["identity_rule"] in rules, f"identity_rule must be one of {sorted(rules)}"),
            (values["tracking_mode"] in modes, f"tracking_mode must be one of {sorted(modes)}"),
            (all(isinstance(word, str) and word.strip()
                 for word in values["quartzy_type_keywords"]),
             "quartzy_type_keywords must be a list of words"),
        ]  # fmt: skip
        for ok, message in checks:
            if not ok:
                raise CommandError(f"{path}: {key}: {message}")
        result.append((key, values))
    return result


def _unreviewed(obj):
    """Created by the sync (no snapshot) and not reviewed yet."""
    return not obj.seeded and obj.provisional


def _differs(obj, values):
    """Fields whose value differs from ``values`` (after apply_seed: the ones people own)."""
    differs = []
    for name, value in values.items():
        current = getattr(obj, obj._meta.get_field(name).attname)
        if current != (value.pk if hasattr(value, "_meta") else value):
            differs.append(name)
    return differs


class Command(BaseCommand):
    help = "Load vendors.yaml and item_types.yaml into the catalog (idempotent, never deletes)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true", help="Report what would change; save nothing."
        )
        parser.add_argument("--vendors", default=str(VENDORS_YAML), help="vendors.yaml to load")
        parser.add_argument(
            "--item-types", default=str(ITEM_TYPES_YAML), help="item_types.yaml to load"
        )

    def handle(self, *args, dry_run, vendors, item_types, **options):
        vendor_data = load_yaml(vendors)
        try:
            table = build_vendor_table(vendor_data)
        except (KeyError, TypeError, ValueError) as exc:
            raise CommandError(f"{vendors}: {exc}") from exc
        types = item_type_values(load_yaml(item_types), item_types)
        self.verbosity = options.get("verbosity", 1)
        self.counts, self.notes = Counter(), []
        try:
            with transaction.atomic():
                self._seed_vendors(vendor_data, table)
                for key, values in types:
                    self._seed_item_type(key, values)
                if dry_run:
                    raise DryRun
        except DryRun:
            pass
        self._report(dry_run)

    # -----------------------------------------------------------------------------------

    def _seed_vendors(self, data, table):
        for family_data in data["families"]:
            family = self._seed_family(table.families[family_data["slug"]])
            for vendor_data in family_data["vendors"]:
                vendor = self._seed_vendor(vendor_data["name"], family)
                for raw in [vendor_data["name"], *(vendor_data.get("aliases") or [])]:
                    self._seed_alias(str(raw), vendor)

    def _seed_family(self, info):
        values = family_seed_values(info)
        family = VendorFamily.objects.filter(slug=info.slug).first()
        if family is None:
            family = VendorFamily.objects.create(
                slug=info.slug, **values, seeded=seed_snapshot(values)
            )
            self._did("families", "created", info.slug)
            return family
        if family.merged_into_id is not None:
            return canonical(family)
        changed = family.apply_seed(values, adopt=_unreviewed(family))
        if changed:
            family.save()
            self._did("families", "updated", f"{info.slug}: {', '.join(changed)}")
            if "catalog_rule" in changed:
                refresh_catalog_keys(family=family)
        self._kept("families", info.slug, _differs(family, values))
        return canonical(family)

    def _seed_vendor(self, name, family):
        values = vendor_seed_values(family)
        vendor = Vendor.objects.filter(name__iexact=name).order_by("pk").first()
        if vendor is None:
            vendor = Vendor.objects.create(name=name, **values, seeded=seed_snapshot(values))
            self._did("vendors", "created", name)
            return vendor
        if vendor.merged_into_id is None:
            changed = vendor.apply_seed(values, adopt=_unreviewed(vendor))
            if "family" in changed:
                move_vendor(vendor, family)  # saves the vendor and its supplier products
            elif changed:
                vendor.save()
            if changed:
                self._did("vendors", "updated", f"{name}: {', '.join(changed)}")
            self._kept("vendors", name, _differs(vendor, values))
        return canonical(vendor)

    def _seed_alias(self, raw, vendor):
        key = normalize_vendor_name(raw)
        alias = VendorAlias.objects.filter(alias_key=key).select_related("vendor").first()
        if alias is None:
            VendorAlias.objects.create(alias_key=key, raw_example=raw[:200], vendor=vendor)
            self._did("aliases", "created", f"{key} -> {vendor}")
        elif canonical(alias.vendor).pk != vendor.pk:
            self.counts["aliases", "kept"] += 1
            self.notes.append(f"alias {key!r} points to {alias.vendor}; vendors.yaml says {vendor}")

    def _seed_item_type(self, key, values):
        item_type = ItemType.objects.filter(key=key).first()
        if item_type is None:
            ItemType.objects.create(key=key, **values, seeded=seed_snapshot(values))
            self._did("item types", "created", key)
            return
        changed = item_type.apply_seed(values)
        if changed:
            item_type.save()
            self._did("item types", "updated", f"{key}: {', '.join(changed)}")
        self._kept("item types", key, _differs(item_type, values))

    # -----------------------------------------------------------------------------------

    def _did(self, kind, verb, detail):
        self.counts[kind, verb] += 1
        if self.verbosity >= 2:
            self.stdout.write(f"{verb} {_SINGULAR[kind]}: {detail}")

    def _kept(self, kind, label, fields):
        if fields:
            self.counts[kind, "kept"] += 1
            self.notes.append(f"{_SINGULAR[kind]} {label}: kept people's {', '.join(fields)}")

    def _report(self, dry_run):
        for kind in _SINGULAR:
            parts = [
                f"{self.counts[kind, verb]} {verb}"
                for verb in ("created", "updated", "kept")
                if self.counts[kind, verb] or verb == "created"
            ]
            self.stdout.write(f"{kind.capitalize()}: {', '.join(parts)}")
        if self.notes and self.verbosity >= 1:
            self.stdout.write("Left as people set them (vendors.yaml/item_types.yaml differ):")
            for note in self.notes:
                self.stdout.write(f"  {note}")
        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run: nothing saved."))
        else:
            self.stdout.write(self.style.SUCCESS("Catalog seeded."))
