"""seed_catalog loads vendors.yaml and item_types.yaml, re-runs safely, keeps people's edits."""

from io import StringIO
from pathlib import Path

import pytest
import yaml
from django.core.management import call_command

from apps.catalog.management.commands.seed_catalog import ITEM_TYPES_YAML
from apps.catalog.models import ItemType, Vendor, VendorAlias, VendorFamily
from apps.catalog.normalization import VENDORS_YAML, load_vendor_table, normalize_vendor_name
from apps.catalog.services import resolve_supplier_product, resolve_vendor


def seed(*args, **options):
    out = StringIO()
    call_command("seed_catalog", *args, stdout=out, **options)
    return out.getvalue()


def counts():
    return (
        VendorFamily.objects.count(),
        Vendor.objects.count(),
        VendorAlias.objects.count(),
        ItemType.objects.count(),
    )


def history_counts():
    return (VendorFamily.history.count(), Vendor.history.count())


def write_yaml(tmp_path, name, data):
    path = Path(tmp_path) / name
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return str(path)


def load(path):
    return yaml.safe_load(Path(path).read_text())


@pytest.mark.django_db
def test_seed_loads_everything_in_the_yaml_files():
    seed()
    table = load_vendor_table()
    vendors = load(VENDORS_YAML)
    item_types = load(ITEM_TYPES_YAML)["item_types"]
    assert VendorFamily.objects.count() == len(table.families)
    assert Vendor.objects.count() == sum(len(f["vendors"]) for f in vendors["families"])
    assert ItemType.objects.count() == len(item_types)
    thermo = VendorFamily.objects.get(slug="thermo")
    assert (thermo.name, thermo.catalog_rule, thermo.is_distributor) == (
        "Thermo Fisher Scientific",
        "thermo",
        False,
    )
    assert VendorFamily.objects.get(slug="fisher").is_distributor
    assert not VendorFamily.objects.filter(provisional=True).exists()
    assert Vendor.objects.get(name="Gibco").family == thermo
    # Every alias is stored under its normalize_vendor_name key, with the name as typed.
    rnd = VendorAlias.objects.get(alias_key=normalize_vendor_name("R&D Systems"))
    assert rnd.alias_key == "r and d systems" and rnd.vendor.name == "R&D Systems"
    assert VendorAlias.objects.get(alias_key="gibco brl").raw_example == "GIBCO BRL"
    every_key = {
        normalize_vendor_name(raw)
        for family in vendors["families"]
        for vendor in family["vendors"]
        for raw in [vendor["name"], *(vendor.get("aliases") or [])]
    }
    assert set(VendorAlias.objects.values_list("alias_key", flat=True)) == every_key


@pytest.mark.django_db
def test_seed_is_idempotent():
    seed()
    before, history_before = counts(), history_counts()
    output = seed()
    assert counts() == before
    assert history_counts() == history_before  # nothing re-saved
    assert "0 created" in output and "updated" not in output


@pytest.mark.django_db
def test_dry_run_saves_nothing():
    output = seed(dry_run=True)
    assert counts() == (0, 0, 0, 0)
    assert "Dry run" in output and "Families: " in output


@pytest.mark.django_db
def test_seed_updates_untouched_fields_but_keeps_peoples_edits(tmp_path):
    seed()
    # A person edits some rows in the admin.
    abcam = VendorFamily.objects.get(slug="abcam")
    abcam.name = "Abcam (Danaher)"
    abcam.save()
    antibody = ItemType.objects.get(key="antibody")
    antibody.lookback_months = 36
    antibody.save()

    # vendors.yaml and item_types.yaml change later.
    vendors = load(VENDORS_YAML)
    for family in vendors["families"]:
        if family["slug"] == "abcam":
            family["name"] = "Abcam plc"  # people renamed it: kept
            family["catalog_rule"] = "cst"  # untouched since the seed: updated
    item_types = load(ITEM_TYPES_YAML)
    for entry in item_types["item_types"]:
        if entry["key"] == "antibody":
            entry["lookback_months"] = 18  # people changed it: kept
            entry["quartzy_type_keywords"].append("immunoreagent")  # untouched: updated
    output = seed(
        vendors=write_yaml(tmp_path, "vendors.yaml", vendors),
        item_types=write_yaml(tmp_path, "item_types.yaml", item_types),
    )

    abcam.refresh_from_db()
    antibody.refresh_from_db()
    assert abcam.name == "Abcam (Danaher)" and abcam.catalog_rule == "cst"
    assert antibody.lookback_months == 36
    assert "immunoreagent" in antibody.quartzy_type_keywords
    assert "family abcam: kept people's name" in output
    assert "item type antibody: kept people's lookback_months" in output


@pytest.mark.django_db
def test_seed_moves_a_vendor_the_yaml_moved_and_resyncs_its_supplier_products(tmp_path):
    seed()
    resolution = resolve_supplier_product(
        vendor_name="Biotium", catalog_number="31000", item_name="GelRed"
    )
    vendors = load(VENDORS_YAML)
    for family in vendors["families"]:
        if family["slug"] == "biotium":
            family["vendors"] = []
        if family["slug"] == "thermo":
            family["vendors"].append({"name": "Biotium", "aliases": []})
    seed(vendors=write_yaml(tmp_path, "vendors.yaml", vendors))
    resolution.supplier_product.refresh_from_db()
    assert Vendor.objects.get(name="Biotium").family.slug == "thermo"
    assert resolution.supplier_product.vendor_family.slug == "thermo"


@pytest.mark.django_db
def test_seed_turns_on_the_atcc_rule_and_rekeys_existing_atcc_listings(tmp_path):
    # A database seeded before the atcc rule existed, where CCL-2.2 (HeLa S3) became "CCL22".
    vendors = load(VENDORS_YAML)
    for family in vendors["families"]:
        if family["slug"] == "atcc":
            family.pop("catalog_rule", None)
    seed(vendors=write_yaml(tmp_path, "vendors.yaml", vendors))
    hela = resolve_supplier_product(
        vendor_name="ATCC", catalog_number="CCL-2.2", item_name="HeLa S3"
    ).supplier_product
    assert hela.catalog_key == "CCL22"

    output = seed(verbosity=2)

    hela.refresh_from_db()
    assert VendorFamily.objects.get(slug="atcc").catalog_rule == "atcc"
    assert "updated family: atcc: catalog_rule" in output
    assert hela.catalog_key == hela.catalog_base == "CCL2.2"
    mdbk = resolve_supplier_product(vendor_name="ATCC", catalog_number="CCL-22", item_name="MDBK")
    assert mdbk.status == "new" and mdbk.product != hela.product


@pytest.mark.django_db
def test_seed_leaves_vendors_people_moved_where_they_put_them(tmp_path):
    seed()
    vendor = Vendor.objects.get(name="Olink")
    vendor.family = VendorFamily.objects.get(slug="qiagen")  # wrong, but a person did it
    vendor.save()
    output = seed()
    vendor.refresh_from_db()
    assert vendor.family.slug == "qiagen"
    assert "vendor Olink: kept people's family" in output


@pytest.mark.django_db
def test_seed_after_the_sync_reuses_what_it_created():
    # The sync can meet vendors before anyone runs the seed: it creates them from the YAML.
    gibco = resolve_vendor("GIBCO BRL")
    acme = resolve_vendor("Acme Lab Supplies")
    seed()
    assert Vendor.objects.filter(name="Gibco").count() == 1
    assert resolve_vendor("Gibco") == gibco
    assert VendorFamily.objects.filter(slug="thermo").count() == 1
    acme.refresh_from_db()
    assert acme.provisional and acme.family.provisional  # unknown to the YAML: still for review


@pytest.mark.django_db
def test_seed_adopts_unreviewed_rows_the_sync_made_before_the_yaml_knew_them():
    family = VendorFamily.objects.create(slug="neb", name="NEB", provisional=True)
    vendor = Vendor.objects.create(name="New England Biolabs", family=family, provisional=True)
    reviewed = VendorFamily.objects.create(slug="cst", name="CST (checked)", provisional=False)
    seed()
    family.refresh_from_db()
    vendor.refresh_from_db()
    reviewed.refresh_from_db()
    assert (family.name, family.catalog_rule, family.provisional) == (
        "New England Biolabs",
        "neb",
        False,
    )
    assert not vendor.provisional and vendor.seeded
    assert (reviewed.name, reviewed.catalog_rule) == ("CST (checked)", "")  # a person's row


@pytest.mark.django_db
def test_seed_reports_aliases_that_point_elsewhere():
    seed()
    alias = VendorAlias.objects.get(alias_key="serotec")
    alias.vendor = Vendor.objects.get(name="Abcam")  # a person re-pointed it
    alias.save()
    output = seed()
    alias.refresh_from_db()
    assert alias.vendor.name == "Abcam"
    assert "alias 'serotec' points to Abcam" in output


@pytest.mark.django_db
@pytest.mark.parametrize(
    "entry, message",
    [
        ({"key": "x", "tier": 0}, "tier must be 1-5"),
        ({"key": "x", "tier": 1, "identity_rule": "gtin"}, "identity_rule"),
        ({"key": "x", "tier": 1, "lookback_months": -1}, "lookback_months"),
        ({"key": "Bad Key", "tier": 1}, "bad or repeated key"),
    ],
)
def test_bad_item_types_yaml_is_refused(tmp_path, entry, message):
    from django.core.management.base import CommandError

    path = write_yaml(tmp_path, "item_types.yaml", {"item_types": [entry]})
    with pytest.raises(CommandError, match=message):
        seed(item_types=path)
    assert counts() == (0, 0, 0, 0)


def test_item_types_yaml_follows_the_plan_tiers():
    entries = {entry["key"]: entry for entry in load(ITEM_TYPES_YAML)["item_types"]}
    tier1 = {
        "antibody",
        "kit",
        "enzyme",
        "small_molecule",
        "recombinant_protein",
        "sirna_sgrna",
        "serum",
    }
    for key in tier1:
        entry = entries[key]
        assert (entry["tier"], entry["durable"], entry["lookback_months"], entry["in_scope"]) == (
            1,
            True,
            24,
            True,
        ), key
    assert entries["cell_line"]["tier"] == 2
    assert entries["oligo"]["tier"] == 3 and entries["oligo"]["identity_rule"] == "none"
    assert entries["plasmid"]["tier"] == entries["strain"]["tier"] == 4
    assert entries["chemical"]["tier"] == 5
    assert entries["media_buffer"]["lookback_months"] == 3
    assert entries["media_buffer"]["in_scope"] is False
    assert entries["consumable"]["lookback_months"] == 0
    assert entries["service_fee"]["identity_rule"] == "none"
    assert entries["service_fee"]["tracking_mode"] == "status_only"
    assert entries["equipment"]["in_scope"] is False
    assert entries["lab_made_shared"]["identity_rule"] == "none"
    assert "other" in entries
    assert "antibod" in entries["antibody"]["quartzy_type_keywords"]
    assert {"general supply", "consumable", "plastic", "glove", "tip"} <= set(
        entries["consumable"]["quartzy_type_keywords"]
    )
    assert all(entry["quartzy_type_keywords"] for entry in entries.values())
