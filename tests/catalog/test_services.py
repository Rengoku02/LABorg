"""Product identity services: vendor and product resolution, merges, and the admin's review
queue that drives them."""

import threading
from datetime import date
from decimal import Decimal

import pytest
from django.db import connection
from django.urls import reverse

from apps.catalog import services
from apps.catalog.models import (
    GenericCatalogKey,
    LabProduct,
    Lot,
    Product,
    ProductCode,
    ResolutionStatus,
    SupplierProduct,
    Vendor,
    VendorAlias,
    VendorFamily,
)
from apps.catalog.normalization import vendor_family_for
from apps.catalog.services import (
    MergeError,
    canonical,
    get_or_create_lot,
    merge_products,
    merge_vendor_families,
    merge_vendors,
    move_vendor,
    refresh_catalog_keys,
    resolve_supplier_product,
    resolve_vendor,
    suggest_item_type,
)
from apps.quartzy.models import OrderRequest

# What people paste into Quartzy's catalog # field instead of a catalog number.
PASTED_URL = (
    "https://www.amazon.com/Fisherbrand-Disposable-Borosilicate-Glass-Pasteur/dp/B07Q3XGZ8M/"
    "ref=sr_1_3?crid=2X9QZ1&keywords=pasteur+pipettes+9+inch&qid=1700000000&sr=8-3"
)
PASTED_LIST = ", ".join(f"R0{number}S" for number in range(101, 121))  # 20 NEB numbers


def resolve(vendor, catalog, name="", **kwargs):
    return resolve_supplier_product(
        vendor_name=vendor, catalog_number=catalog, item_name=name, **kwargs
    )


# ---------------------------------------------------------------------------------------
# resolve_vendor


@pytest.mark.django_db
def test_resolve_vendor_matches_aliases_however_they_are_typed(seeded):
    thermo = Vendor.objects.get(name="Thermo Fisher Scientific")
    assert resolve_vendor("Thermo Fisher Scientific, Inc.") == thermo
    assert resolve_vendor("  thermofisher.com ") == thermo
    biorad = Vendor.objects.get(name="Bio-Rad Laboratories")
    assert (
        resolve_vendor("BioRad") == resolve_vendor("Bio Rad") == resolve_vendor("BIO-RAD") == biorad
    )
    assert resolve_vendor("R & D Systems").name == "R&D Systems"
    assert not Vendor.objects.filter(provisional=True).exists()


@pytest.mark.django_db
def test_an_alias_added_in_the_database_matches_without_spaces(make_vendor):
    acme = make_vendor("Acme Biotech")
    VendorAlias.objects.create(alias_key="acme bio tech", vendor=acme)
    assert resolve_vendor("AcmeBioTech") == acme


@pytest.mark.django_db
def test_known_vendor_is_created_from_the_yaml_when_not_seeded():
    neb = resolve_vendor("NEB")
    assert (neb.name, neb.provisional) == ("New England Biolabs", False)
    assert (neb.family.slug, neb.family.catalog_rule, neb.family.provisional) == (
        "neb",
        "neb",
        False,
    )
    assert neb.family.seeded["catalog_rule"] == "neb"  # seed_catalog may update it later
    assert resolve_vendor("New England BioLabs, Inc.") == neb
    assert VendorFamily.objects.count() == 1


@pytest.mark.django_db
def test_unknown_vendor_becomes_provisional_once():
    acme = resolve_vendor("Acme Bio-Reagents, LLC")
    assert acme.name == "Acme Bio-Reagents, LLC" and acme.provisional
    assert acme.family.slug == "acme-bio-reagents-llc" and acme.family.provisional
    assert VendorAlias.objects.get(vendor=acme).alias_key == "acme bio reagents"
    assert resolve_vendor("ACME bio reagents") == acme
    assert resolve_vendor("Acme BioReagents") == acme  # same key without spaces
    assert (Vendor.objects.count(), VendorFamily.objects.count()) == (1, 1)


@pytest.mark.django_db
@pytest.mark.parametrize("raw", [None, "", "   ", "-", "N/A", "unknown", "TBD", "Other"])
def test_blank_or_placeholder_vendor_is_the_shared_unknown_vendor(raw):
    vendor = resolve_vendor(raw)
    assert vendor.name == services.UNKNOWN_VENDOR_NAME and vendor.provisional
    assert vendor.family.slug == services.UNKNOWN_FAMILY_SLUG and vendor.family.provisional
    # normalization (and so the Phase 0 baseline) puts these names in the same family.
    assert vendor_family_for(raw) == vendor.family.slug
    assert resolve_vendor(None) == vendor
    assert Vendor.objects.count() == 1


@pytest.mark.django_db
def test_the_same_number_from_unnamed_vendors_is_one_product():
    vendors = ["N/A", "", "Unknown"]
    results = [resolve(vendor, "2118S", "GAPDH (14C10) Rabbit mAb") for vendor in vendors]
    assert [result.status for result in results] == ["new", "exact", "exact"]
    families = {vendor_family_for(vendor) for vendor in vendors}
    assert families == {results[0].supplier_product.vendor_family.slug}
    assert services.UNKNOWN_FAMILY_SLUG == services.UNKNOWN_FAMILY == "unknown"


@pytest.mark.django_db
def test_a_name_joining_aliases_of_one_family_lands_in_that_family():
    vendor = resolve_vendor("Thermo Fisher Scientific (Gibco)")
    assert vendor.family.slug == "thermo" and not vendor.family.provisional
    assert vendor.provisional  # which brand it is stays for a person to decide


@pytest.mark.django_db
def test_new_provisional_family_never_takes_a_curated_slug():
    vendor = resolve_vendor("Thomas")  # not an alias, but "thomas" is Thomas Scientific's slug
    assert vendor.family.slug == "thomas-2"


@pytest.mark.django_db
def test_resolve_vendor_follows_vendor_merges():
    typo = resolve_vendor("Thermo Fischer")
    gibco = resolve_vendor("Gibco")
    merge_vendors(typo, gibco)
    assert resolve_vendor("Thermo Fischer") == gibco
    typo.refresh_from_db()
    assert typo.merged_into == gibco


# ---------------------------------------------------------------------------------------
# suggest_item_type


@pytest.mark.django_db
@pytest.mark.parametrize(
    "quartzy_type, key",
    [
        ("Antibody", "antibody"),
        ("Secondary Antibodies", "antibody"),
        ("Mouse Antibodies", "antibody"),
        ("Chemical", "chemical"),
        ("Chemical - Hazardous", "chemical"),
        ("Chemical Probe", "small_molecule"),
        ("Inhibitors", "small_molecule"),
        ("General Supply", "consumable"),
        ("Pipette Tips", "consumable"),
        ("Primers/Oligos", "oligo"),
        ("Cell Lines", "cell_line"),
        ("FBS", "serum"),
        ("Restriction Enzymes", "enzyme"),
        ("Cytokines & Growth Factors", "recombinant_protein"),
        ("siRNA", "sirna_sgrna"),
        ("Plasmids", "plasmid"),
        ("E. coli strains", "strain"),
        ("Cell Culture Media", "media_buffer"),
        ("Sequencing services", "service_fee"),
        ("Equipment", "equipment"),
        ("Kits", "kit"),
        ("Other", "other"),
        ("Reagent", None),
        ("Multiple", None),
        ("", None),
        (None, None),
    ],
)
def test_suggest_item_type(item_types, quartzy_type, key):
    found = suggest_item_type(quartzy_type)
    assert (found.key if found else None) == key


# ---------------------------------------------------------------------------------------
# resolve_supplier_product


@pytest.mark.django_db
def test_exact_match_whatever_the_formatting_and_brand_in_one_family():
    first = resolve("Gibco", "11965-092", "DMEM, high glucose", unit_price="25.10")
    assert (first.status, first.created) == (ResolutionStatus.NEW, True)
    again = resolve(
        "Life Technologies", "Cat# 11965092", "DMEM hi glu", unit_size="500 mL", unit_price=27
    )
    assert (again.status, again.created) == ("exact", False)
    assert again.supplier_product == first.supplier_product and again.product == first.product
    sp = SupplierProduct.objects.get()
    assert (sp.unit_size_raw, sp.last_unit_price) == ("500 mL", Decimal("27.0000"))
    assert sp.vendor.name == "Gibco"  # the first listing stays as it was
    assert Product.objects.count() == 1


@pytest.mark.django_db
def test_fisher_and_gibco_numbers_are_different_families():
    gibco = resolve("Gibco", "11965092", "DMEM")
    fisher = resolve("Fisher Scientific", "11-965-092", "Gibco DMEM")
    assert fisher.status == "new" and fisher.supplier_product != gibco.supplier_product
    assert gibco.supplier_product.catalog_key == fisher.supplier_product.catalog_key == "11965092"
    assert (
        gibco.supplier_product.vendor_family.slug,
        fisher.supplier_product.vendor_family.slug,
    ) == (
        "thermo",
        "fisher",
    )


@pytest.mark.django_db
def test_another_pack_size_is_a_base_variant_of_the_same_product():
    small = resolve("New England Biolabs", "R0101S", "EcoRI")
    large = resolve("NEB", "R0101L", "EcoRI 50,000 units")
    assert (large.status, large.created) == ("base_variant", True)
    assert large.product == small.product and large.supplier_product != small.supplier_product
    assert [sp.pack_code for sp in large.product.supplier_products.order_by("pk")] == ["S", "L"]
    assert resolve("NEB", "R0101 S").status == "exact"
    assert Product.objects.count() == 1


@pytest.mark.django_db
def test_generic_item_type_never_groups(item_types):
    oligo = suggest_item_type("Primers")
    first = resolve("IDT", "51-01-01-01", "GAPDH fwd primer", item_type=oligo)
    second = resolve("IDT", "51-01-01-01", "ACTB rev primer", item_type="oligo")
    assert first.status == second.status == "generic"
    assert first.product != second.product
    assert first.supplier_product.identity == SupplierProduct.Identity.GENERIC
    assert first.supplier_product.catalog_key == "51010101"  # kept, just never matched


@pytest.mark.django_db
def test_stop_listed_catalog_keys_are_generic():
    idt = resolve_vendor("IDT").family
    GenericCatalogKey.objects.create(vendor_family=idt, catalog_key="10006832", reason="gBlock")
    GenericCatalogKey.objects.create(catalog_key="SHIPPING", reason="any vendor's fee")
    assert resolve("IDT", "10006832", "gBlock 1").status == "generic"
    assert resolve("IDT", "10006832", "gBlock 2").status == "generic"
    assert resolve("Twist Bioscience", "10006832", "clone").status == "new"  # other family
    assert resolve("Sigma", "shipping", "Shipping & handling").status == "generic"
    assert resolve("VWR", "Shipping", "Freight").status == "generic"
    assert Product.objects.count() == 5


@pytest.mark.django_db
@pytest.mark.parametrize("catalog", [None, "", "N/A", "TBD", "custom", "1.1965E+07"])
def test_missing_catalog_number_gets_its_own_product(catalog):
    first = resolve("Thermo Fisher", catalog, "Something")
    second = resolve("Thermo Fisher", catalog, "Something")
    assert first.status == second.status == "no_catalog"
    assert first.product != second.product and first.created
    assert first.supplier_product.catalog_key is None
    assert first.product.provisional and first.supplier_product.provisional


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("vendor", "catalog"),
    [("Amazon", PASTED_URL), ("NEB", PASTED_LIST)],
    ids=["pasted url", "list of 20 numbers"],
)
def test_a_pasted_url_or_list_of_numbers_has_no_catalog_number(vendor, catalog):
    result = resolve(vendor, catalog, "Pasteur pipettes, 9 inch")
    assert (result.status, result.created) == ("no_catalog", True)
    sp = SupplierProduct.objects.get()
    assert (sp.catalog_raw, sp.catalog_key, sp.catalog_base) == (catalog, None, None)
    assert resolve(vendor, catalog, "Pasteur pipettes, 9 inch").product != result.product


@pytest.mark.django_db
def test_a_misread_pack_code_fits_its_column():
    result = resolve("Sigma", "A2220-" + "1" * 45 + "ML", "ANTI-FLAG M2 affinity gel")
    assert result.status == "new" and result.supplier_product.pack_code == ""


@pytest.mark.django_db
def test_nul_characters_are_stripped_before_anything_is_stored():
    # Text pasted from PDFs, or UTF-16 read as UTF-8; Postgres text can't hold NUL.
    result = resolve(
        "Acme\x00 Reagents",
        "AC-\x00100",
        "Widget\x00 buffer",
        unit_size="500\x00 mL",
        currency="US\x00D",
    )
    sp = result.supplier_product
    assert (sp.catalog_raw, sp.catalog_key, sp.unit_size_raw, sp.currency) == (
        "AC-100",
        "AC100",
        "500 mL",
        "USD",
    )
    assert result.product.name == "Widget buffer"
    assert sp.vendor.name == sp.vendor.family.name == "Acme Reagents"
    assert VendorAlias.objects.get(vendor=sp.vendor).raw_example == "Acme Reagents"
    gibco = resolve("G\x00i\x00b\x00c\x00o\x00", "11965\x00092", "")
    assert gibco.supplier_product.vendor.name == "Gibco"
    assert gibco.product.name == "Gibco 11965092"
    lot = get_or_create_lot(result.product, "Lot\x00 2183-45B")
    assert (lot.lot_raw, lot.lot_key) == ("Lot 2183-45B", "218345B")


@pytest.mark.django_db
def test_atcc_hela_s3_and_mdbk_are_different_products():
    hela = resolve("ATCC", "ATCC CCL-2.2", "HeLa S3")
    mdbk = resolve("ATCC", "CCL-22", "MDBK (NBL-1)")
    assert (hela.status, mdbk.status) == ("new", "new") and hela.product != mdbk.product
    assert hela.supplier_product.catalog_key == hela.supplier_product.catalog_base == "CCL2.2"
    again = resolve("American Type Culture Collection", "ccl 2.2", "HeLa S3 cells")
    assert (again.status, again.supplier_product) == ("exact", hela.supplier_product)


@pytest.mark.django_db
def test_unknown_vendor_makes_provisional_rows():
    result = resolve("Acme Lab Supplies", "AC-100", "Widget buffer", unit_price=Decimal("9.5"))
    assert result.status == "new"
    assert result.product.name == "Widget buffer" and result.product.provisional
    assert result.supplier_product.vendor.provisional
    assert result.supplier_product.vendor_family.provisional
    assert result.supplier_product.last_unit_price == Decimal("9.5000")


@pytest.mark.django_db
def test_product_is_named_after_the_catalog_number_without_an_item_name():
    assert resolve("CST", "#2118S", "").product.name == "Cell Signaling Technology 2118S"
    assert resolve("", "XYZ-1", None).product.name == "XYZ-1"
    assert resolve("CST", "N/A", "").product.name == "Unnamed Cell Signaling Technology product"


@pytest.mark.django_db
def test_exact_match_follows_merged_products_and_fills_the_item_type(item_types):
    a = resolve("CST", "2118S", "GAPDH (14C10) Rabbit mAb")
    b = resolve("Abcam", "ab8245", "Anti-GAPDH antibody [6C5]")
    merge_products(b.product, a.product)
    again = resolve("Abcam", "ab8245", "GAPDH", item_type="antibody")
    assert (again.status, again.product) == ("exact", a.product)
    a.product.refresh_from_db()
    assert a.product.item_type.key == "antibody"


@pytest.mark.django_db
def test_exact_match_heals_a_listing_left_on_a_merged_product():
    first = resolve("CST", "2118S", "GAPDH (14C10)")
    survivor = Product.objects.create(name="GAPDH (14C10) Rabbit mAb")
    # A listing created for a product at the moment it was merged away still points at it.
    Product.objects.filter(pk=first.product.pk).update(merged_into=survivor)
    again = resolve("CST", "2118S")
    assert again.product == survivor
    again.supplier_product.refresh_from_db()
    assert again.supplier_product.product == survivor


@pytest.mark.django_db
def test_a_lost_race_for_a_new_key_ends_as_an_exact_match(monkeypatch):
    winner = resolve("Gibco", "15140122", "Pen-Strep")
    real = services._identifying
    calls = {"n": 0}

    def stale_first_read(family, **lookup):
        calls["n"] += 1
        rows = real(family, **lookup)
        return rows.none() if calls["n"] <= 2 else rows  # as if read before the winner commits

    monkeypatch.setattr(services, "_identifying", stale_first_read)
    loser = resolve("Gibco", "15140-122", "Penicillin-Streptomycin")
    assert (loser.status, loser.supplier_product) == ("exact", winner.supplier_product)
    assert Product.objects.count() == 1  # the loser's product was rolled back


@pytest.mark.django_db(transaction=True)
def test_concurrent_resolution_creates_one_supplier_product():
    resolve_vendor("Gibco")  # vendor rows exist; the race is on the product
    barrier = threading.Barrier(4)
    results, errors = [], []

    def worker():
        try:
            barrier.wait(timeout=10)
            results.append(resolve("Gibco", "25200-056", "Trypsin-EDTA"))
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert len({r.supplier_product.pk for r in results}) == 1
    assert SupplierProduct.objects.count() == Product.objects.count() == 1
    assert sorted(r.status for r in results) == ["exact", "exact", "exact", "new"]


# ---------------------------------------------------------------------------------------
# Lots


@pytest.mark.django_db
def test_get_or_create_lot_normalizes_and_follows_redirects():
    kept, merged = Product.objects.create(name="FBS"), Product.objects.create(name="FBS dup")
    merge_products(merged, kept)
    lot = get_or_create_lot(merged, "Lot# 2183-45B")
    assert (lot.product, lot.lot_key, lot.expiry) == (kept, "218345B", None)
    same = get_or_create_lot(kept, "218345b", expiry=date(2027, 3, 1), expiry_precision="month")
    assert same == lot
    lot.refresh_from_db()
    assert (lot.expiry, lot.expiry_precision) == (date(2027, 3, 1), "month")
    assert get_or_create_lot(kept, "N/A") is None and get_or_create_lot(kept, None) is None


@pytest.mark.django_db
def test_a_lot_too_long_to_be_a_lot_number_gets_no_lot():
    product = Product.objects.create(name="Pasteur pipettes")
    assert get_or_create_lot(product, PASTED_URL) is None
    assert not Lot.objects.exists()


# ---------------------------------------------------------------------------------------
# merge_products


def points_at(product):
    """Rows anywhere that still reference ``product`` (every reverse relation)."""
    found = {}
    for rel in Product._meta.related_objects:
        model = rel.related_model
        if rel.many_to_many:
            continue
        count = model._base_manager.filter(**{rel.field.name: product}).count()
        if count:
            found[f"{model._meta.label}.{rel.field.name}"] = count
    return found


@pytest.mark.django_db
def test_merge_moves_everything_and_leaves_a_redirect(lab, curator, make_vendor):
    cst, abcam = make_vendor("CST", catalog_rule="cst"), make_vendor("Abcam")
    dst = Product.objects.create(name="GAPDH (14C10) Rabbit mAb", synonyms=["GAPDH XP"])
    src = Product.objects.create(
        name="Anti-GAPDH", synonyms=["gapdh xp"], rrid="AB_2107445", attributes={"host": "rabbit"}
    )
    SupplierProduct.objects.create(product=dst, vendor=cst, catalog_raw="2118S")
    moved_sp = SupplierProduct.objects.create(product=src, vendor=abcam, catalog_raw="ab9485")
    lot = Lot.objects.create(product=src, lot_raw="GR3214-1")
    lab_product = LabProduct.objects.create(lab=lab, product=src, notes="in the -20 B box")
    older = Product.objects.create(name="GAPDH old", merged_into=src)

    merge_products(src, dst, user=curator)

    src.refresh_from_db()
    dst.refresh_from_db()
    assert src.merged_into == dst and Product.objects.filter(pk=src.pk).exists()
    assert points_at(src) == {}
    assert moved_sp in dst.supplier_products.all()
    lot.refresh_from_db()
    lab_product.refresh_from_db()
    older.refresh_from_db()
    assert lot.product == lab_product.product == dst
    assert older.merged_into == dst  # redirect chains stay one hop long
    assert dst.synonyms == ["GAPDH XP", "Anti-GAPDH"]
    assert dst.rrid == "AB_2107445" and dst.attributes == {"host": "rabbit"}
    assert src.history.first().history_user == curator
    assert moved_sp.history.first().history_user == curator
    assert canonical(src) == dst


@pytest.mark.django_db
def test_merge_keeps_the_target_lot_when_both_have_the_lot(make_vendor):
    dst, src = Product.objects.create(name="FBS"), Product.objects.create(name="FBS, US origin")
    kept = Lot.objects.create(product=dst, lot_raw="2183-45B")
    dropped = Lot.objects.create(product=src, lot_raw="Lot# 218345b", expiry=date(2028, 1, 31))
    Lot.objects.create(product=src, lot_raw="9999")
    merge_products(src, dst)
    assert not Lot.objects.filter(pk=dropped.pk).exists()
    kept.refresh_from_db()
    assert kept.expiry == date(2028, 1, 31)  # filled from the dropped twin
    assert sorted(dst.lots.values_list("lot_key", flat=True)) == ["218345B", "9999"]
    assert Lot.history.filter(id=dropped.pk, history_type="-").exists()


@pytest.mark.django_db
def test_merge_combines_lab_products_of_the_same_lab(lab, member):
    dst, src = Product.objects.create(name="Taq"), Product.objects.create(name="Taq polymerase")
    LabProduct.objects.create(lab=lab, product=dst, synonyms=["taq"], notes="shared box")
    LabProduct.objects.create(
        lab=lab,
        product=src,
        min_stock_containers=2,
        synonyms=["Taq", "hot taq"],
        notes="ask Priya",
        steward=member,
    )
    merge_products(src, dst)
    merged = LabProduct.objects.get()
    assert merged.product == dst and merged.min_stock_containers == 2
    assert merged.synonyms == ["taq", "hot taq"] and merged.steward == member
    assert merged.notes == "shared box\n\nask Priya"


@pytest.mark.django_db
def test_merge_refuses_itself_and_loops():
    a, b, c = (Product.objects.create(name=name) for name in "abc")
    with pytest.raises(MergeError, match="itself"):
        merge_products(a, a)
    merge_products(a, b)
    with pytest.raises(MergeError, match="loop"):
        merge_products(b, a)  # a already redirects to b
    with pytest.raises(MergeError, match="already redirects"):
        merge_products(a, c)
    b.refresh_from_db()
    assert b.merged_into is None


@pytest.mark.django_db
def test_merge_is_idempotent_and_resolves_the_target_first():
    a, b, c = (Product.objects.create(name=name) for name in "abc")
    merge_products(a, b)
    history = Product.history.count()
    merge_products(a, b)
    assert Product.history.count() == history
    merge_products(b, c)
    merge_products(Product.objects.create(name="d"), a)  # a -> c
    assert Product.objects.get(name="d").merged_into == c


# ---------------------------------------------------------------------------------------
# Vendors: merge, move, family merge, rule changes


@pytest.mark.django_db
def test_merge_vendors_moves_listings_and_aliases_into_the_target_family():
    typo = resolve("Thermo Fischer", "11965-092", "DMEM").supplier_product
    assert (typo.vendor_family.provisional, typo.catalog_base) == (True, "11965092")
    gibco = resolve_vendor("Gibco")
    assert merge_vendors(typo.vendor, gibco) == 0
    typo.refresh_from_db()
    assert typo.vendor == gibco and typo.vendor_family.slug == "thermo"
    assert (typo.catalog_base, typo.pack_code) == ("11965", "092")  # thermo's pack rule now
    assert VendorAlias.objects.get(alias_key="thermo fischer").vendor == gibco
    assert resolve("Thermo Fischer", "11965092").status == "exact"
    assert resolve("Gibco", "11965084", "DMEM 1 L").status == "base_variant"


@pytest.mark.django_db
def test_merge_vendors_folds_listings_the_target_family_already_has():
    official = resolve("Gibco", "11965092", "DMEM", unit_price="25").supplier_product
    stray = resolve("Gibbco", "11965-092", "DMEM high glucose", unit_size="500 mL")
    ProductCode.objects.create(code="00888888888888", supplier_product=stray.supplier_product)
    ProductCode.objects.create(code="00777777777777", supplier_product=stray.supplier_product)
    ProductCode.objects.create(code="00777777777777", supplier_product=official)
    folded = merge_vendors(stray.supplier_product.vendor, official.vendor)
    assert folded == 1
    assert not SupplierProduct.objects.filter(pk=stray.supplier_product.pk).exists()
    official.refresh_from_db()
    assert official.unit_size_raw == "500 mL"  # filled from the folded listing
    assert sorted(official.codes.values_list("code", flat=True)) == [
        "00777777777777",
        "00888888888888",
    ]
    stray.product.refresh_from_db()
    assert stray.product.merged_into == official.product
    assert SupplierProduct.history.filter(id=stray.supplier_product.pk, history_type="-").exists()


@pytest.mark.django_db
def test_unknown_vendor_cannot_be_merged():
    unknown = resolve_vendor("")
    with pytest.raises(MergeError, match="Unknown vendor"):
        merge_vendors(unknown, resolve_vendor("Gibco"))
    with pytest.raises(MergeError, match="Unknown vendor"):
        merge_vendor_families(resolve_vendor("Abcam").family, unknown.family)


@pytest.mark.django_db
def test_merge_vendors_refuses_itself_and_is_idempotent():
    gibco, typo = resolve_vendor("Gibco"), resolve_vendor("Gibko")
    with pytest.raises(MergeError):
        merge_vendors(gibco, gibco)
    merge_vendors(typo, gibco)
    assert merge_vendors(typo, gibco) == 0


@pytest.mark.django_db
def test_move_vendor_brings_its_listings_under_the_new_familys_rule():
    # "NEB Japan" isn't in vendors.yaml, so it starts in its own family with no pack rule.
    sp = resolve("NEB Japan K.K.", "R0101S", "EcoRI").supplier_product
    assert (sp.vendor_family.provisional, sp.catalog_base) == (True, "R0101S")
    neb = resolve("NEB", "R0101L", "EcoRI large").supplier_product
    assert move_vendor(sp.vendor, neb.vendor_family) == 0
    sp.refresh_from_db()
    assert (
        sp.vendor_family.slug == "neb" and Vendor.objects.get(pk=sp.vendor_id).family.slug == "neb"
    )
    assert (sp.catalog_base, sp.pack_code) == ("R0101", "S")
    assert resolve("NEB", "R0101S").supplier_product == sp


@pytest.mark.django_db
def test_merge_vendor_families():
    sp = resolve("Gibbco Labs", "11965092", "DMEM").supplier_product
    stray_family = sp.vendor_family
    GenericCatalogKey.objects.create(vendor_family=stray_family, catalog_key="FEE1")
    GenericCatalogKey.objects.create(vendor_family=stray_family, catalog_key="FEE2")
    thermo = resolve_vendor("Gibco").family
    GenericCatalogKey.objects.create(vendor_family=thermo, catalog_key="FEE2")
    fisher_sp = resolve("Fisher Scientific", "11-965-092", "DMEM (Fisher)").supplier_product
    fisher_sp.mfr_family, fisher_sp.mfr_catalog_key = stray_family, "11965092"
    fisher_sp.save()

    merge_vendor_families(stray_family, thermo)

    sp.refresh_from_db()
    fisher_sp.refresh_from_db()
    stray_family.refresh_from_db()
    assert sp.vendor_family == thermo and sp.vendor.family == thermo
    assert sp.catalog_base == "11965"
    assert fisher_sp.mfr_family == thermo
    assert sorted(thermo.generic_keys.values_list("catalog_key", flat=True)) == ["FEE1", "FEE2"]
    assert stray_family.merged_into == thermo
    assert canonical(stray_family) == thermo


@pytest.mark.django_db
def test_refresh_catalog_keys_after_a_rule_change():
    from django.core.management import call_command

    small = resolve("Zymo Research", "D4013", "DNA Clean & Concentrator").supplier_product
    large = resolve("Zymo", "D4014", "DNA Clean & Concentrator 50").supplier_product
    family = small.vendor_family
    family.catalog_rule = "neb"  # letter + 4 digits + size letter: D4013 vs D4014 still differ
    family.save()
    assert refresh_catalog_keys(family=family) == (0, 0)
    family.catalog_rule = "takara"
    family.save()
    call_command("refresh_catalog_keys", family=family.slug, verbosity=0)
    small.refresh_from_db()
    assert (small.catalog_key, small.catalog_base) == ("D4013", "D4013")
    assert large.product != small.product


@pytest.mark.django_db
def test_refresh_catalog_keys_gives_atcc_listings_keyed_before_the_dot_rule_their_dot():
    from django.core.management import call_command

    hela = resolve("ATCC", "CCL-2.2", "HeLa S3").supplier_product
    # As the code before the atcc rule keyed it.
    SupplierProduct.objects.filter(pk=hela.pk).update(catalog_key="CCL22", catalog_base="CCL22")
    assert refresh_catalog_keys(family=hela.vendor_family) == (1, 0)
    hela.refresh_from_db()
    assert (hela.catalog_key, hela.catalog_base) == ("CCL2.2", "CCL2.2")
    SupplierProduct.objects.filter(pk=hela.pk).update(catalog_key="CCL22", catalog_base="CCL22")
    call_command("refresh_catalog_keys", verbosity=0)
    hela.refresh_from_db()
    assert hela.catalog_key == "CCL2.2"
    assert resolve("ATCC", "CCL-22", "MDBK").status == "new"


@pytest.mark.django_db
def test_refreshing_a_family_also_rekeys_manufacturer_keys_in_it():
    hela = resolve("ATCC", "CCL-2.2", "HeLa S3").supplier_product
    fisher = resolve("Fisher Scientific", "50-238-3201", "HeLa S3 via Fisher").supplier_product
    fisher.mfr_family, fisher.mfr_catalog_key = hela.vendor_family, "CCL-2.2"
    fisher.save()
    assert fisher.mfr_catalog_key == "CCL2.2"
    # Someone takes the rule off the family (the admin then refreshes it).
    VendorFamily.objects.filter(pk=hela.vendor_family_id).update(catalog_rule="")
    assert refresh_catalog_keys(family=hela.vendor_family) == (2, 0)
    hela.refresh_from_db()
    fisher.refresh_from_db()
    assert (hela.catalog_key, fisher.mfr_catalog_key) == ("CCL22", "CCL22")


# ---------------------------------------------------------------------------------------
# retire_orphans: what a re-resolved request leaves behind

NOTHING_RETIRED = {
    "deleted": 0,
    "kept_curated": 0,
    "in_use": 0,
    "products_merged": 0,
    "products_deleted": 0,
    "products_kept_curated": 0,
    "products_kept_for_redirects": 0,
    "products_in_use": 0,
}


@pytest.mark.django_db
def test_retire_orphans_deletes_unused_provisional_rows(curator):
    old = resolve("Amazon", "N/A", "Sharpie").supplier_product
    counts = services.retire_orphans([old], user=curator)
    assert counts == {**NOTHING_RETIRED, "deleted": 1, "products_deleted": 1}
    assert not SupplierProduct.objects.exists() and not Product.objects.exists()
    deleted = SupplierProduct.history.get(id=old.pk, history_type="-")
    assert deleted.history_user == curator
    assert Product.history.get(id=old.product_id, history_type="-").history_user == curator


@pytest.mark.django_db
def test_retire_orphans_merges_the_old_product_into_the_replacement():
    old = resolve("Amazon", "N/A", "Sharpie").supplier_product
    new = resolve("Amazon", "N/A", "Sharpie markers, black")
    counts = services.retire_orphans([old], replacement=new.product)
    assert counts == {**NOTHING_RETIRED, "deleted": 1, "products_merged": 1}
    assert not SupplierProduct.objects.filter(pk=old.pk).exists()
    assert canonical(Product.objects.get(pk=old.product_id)) == new.product  # the id still works
    new.product.refresh_from_db()
    assert new.product.synonyms == ["Sharpie"]


@pytest.mark.django_db
def test_retire_orphans_skips_supplier_products_still_in_use(lab):
    requested = resolve("Amazon", "N/A", "Sharpie").supplier_product
    OrderRequest.objects.create(
        lab=lab, source=OrderRequest.Source.OUTSIDE, supplier_product=requested
    )
    barcoded = resolve("Corning", "3516", "6-well plates").supplier_product
    ProductCode.objects.create(code="00709012735165", supplier_product=barcoded)
    replacement = Product.objects.create(name="Markers")
    counts = services.retire_orphans([requested, barcoded], replacement=replacement)
    assert counts == {**NOTHING_RETIRED, "in_use": 2}
    assert SupplierProduct.objects.count() == 2
    assert Product.objects.filter(merged_into__isnull=True).count() == 3


@pytest.mark.django_db
def test_retire_orphans_keeps_products_still_in_use(lab):
    pack = resolve("NEB", "R0101S", "EcoRI").supplier_product
    resolve("NEB", "R0101L", "EcoRI 50,000 units")  # the product's other pack size
    with_lot = resolve("CST", "N/A", "Rabbit mAb").supplier_product
    Lot.objects.create(product=with_lot.product, lot_raw="0012")
    with_notes = resolve("Abcam", "N/A", "Mouse mAb").supplier_product
    LabProduct.objects.create(lab=lab, product=with_notes.product, notes="-20 B")
    requested = resolve("IDT", "N/A", "Primer mix").supplier_product
    OrderRequest.objects.create(lab=lab, source=OrderRequest.Source.GIFT, product=requested.product)
    replacement = Product.objects.create(name="Something else")
    listings = [pack, with_lot, with_notes, requested]
    counts = services.retire_orphans(listings, replacement=replacement)
    assert counts == {**NOTHING_RETIRED, "deleted": 4, "products_in_use": 4}
    kept = Product.objects.filter(pk__in=[sp.product_id for sp in listings])
    assert kept.filter(merged_into__isnull=True).count() == 4


@pytest.mark.django_db
def test_a_request_resolved_again_and_again_leaves_one_canonical_product():
    # Its catalog # is filled in ("N/A" -> "SH-1234"), then a typo fixed ("SH-12345"): each
    # time it gets a product of its own, and the one it left redirects to the new one.
    first = resolve("Amazon", "N/A", "Sharpie fine point x12")
    second = resolve("Amazon", "SH-1234", "Sharpie fine point x12")
    services.retire_orphans([first.supplier_product], replacement=second.product)
    third = resolve("Amazon", "SH-12345", "Sharpie fine point x12")
    counts = services.retire_orphans([second.supplier_product], replacement=third.product)
    assert counts == {**NOTHING_RETIRED, "deleted": 1, "products_merged": 1}
    assert list(Product.objects.filter(merged_into=None)) == [third.product]
    for old in (first.product, second.product):
        old.refresh_from_db()
        assert old.merged_into == third.product  # still one hop to the product in use
    third.product.refresh_from_db()
    assert third.product.provisional


@pytest.mark.django_db
def test_without_a_replacement_the_products_that_redirect_to_one_go_with_it(curator):
    # Requests that left their product for several (split up again) give it no replacement.
    # The products merged into it earlier would redirect nowhere, so they are deleted too.
    old = resolve("Qiagen", "N/A", "Mini kit").supplier_product
    merged = Product.objects.create(name="Mini kit (dup)", merged_into=old.product)
    older = Product.objects.create(name="Mini kit, 50", merged_into=merged)  # a longer chain
    counts = services.retire_orphans([old], user=curator)
    assert counts == {**NOTHING_RETIRED, "deleted": 1, "products_deleted": 3}
    assert not Product.objects.exists()
    assert Product.history.get(id=older.pk, history_type="-").history_user == curator


@pytest.mark.django_db
def test_without_a_replacement_a_reviewed_or_used_redirect_keeps_its_product(lab):
    old = resolve("Qiagen", "N/A", "Mini kit").supplier_product
    reviewed = Product.objects.create(
        name="Mini kit (dup)", provisional=False, merged_into=old.product
    )
    counts = services.retire_orphans([old])
    assert counts == {**NOTHING_RETIRED, "deleted": 1, "products_kept_for_redirects": 1}
    assert canonical(reviewed) == Product.objects.get(pk=old.product_id)
    # A redirect something still points at, though nothing should.
    used = resolve("Qiagen", "N/A", "Maxi kit").supplier_product
    stale = Product.objects.create(name="Maxi kit (dup)", merged_into=used.product)
    OrderRequest.objects.create(lab=lab, source=OrderRequest.Source.GIFT, product=stale)
    counts = services.retire_orphans([used])
    assert counts == {**NOTHING_RETIRED, "deleted": 1, "products_kept_for_redirects": 1}
    assert canonical(stale) == Product.objects.get(pk=used.product_id)
    # A reviewed one stays reviewed, replacement or not.
    reviewed = resolve("Qiagen", "N/A", "Midi kit").supplier_product
    Product.objects.filter(pk=reviewed.product_id).update(provisional=False)
    Product.objects.create(name="Midi kit (dup)", merged_into=reviewed.product)
    counts = services.retire_orphans([reviewed], replacement=Product.objects.create(name="Kits"))
    assert counts == {**NOTHING_RETIRED, "deleted": 1, "products_kept_curated": 1}
    assert Product.objects.get(pk=reviewed.product_id).merged_into is None


@pytest.mark.django_db
def test_a_replacement_retired_meanwhile_counts_as_none():
    # Two re-resolutions overlap: the second moved the request on from its new product and
    # retired that one before the first retired what the request had left.
    old = resolve("Amazon", "N/A", "Sharpie").supplier_product
    gone = resolve("Amazon", "N/A", "Sharpie markers")
    services.retire_orphans([gone.supplier_product])
    counts = services.retire_orphans([old], replacement=gone.product)
    assert counts == {**NOTHING_RETIRED, "deleted": 1, "products_deleted": 1}
    assert not Product.objects.exists() and not SupplierProduct.objects.exists()


@pytest.mark.django_db
def test_retire_orphans_never_deletes_or_merges_reviewed_rows():
    reviewed_listing = resolve("Amazon", "N/A", "Sharpie").supplier_product
    SupplierProduct.objects.filter(pk=reviewed_listing.pk).update(provisional=False)
    reviewed_product = resolve("Amazon", "N/A", "Pens").supplier_product
    Product.objects.filter(pk=reviewed_product.product_id).update(provisional=False)
    replacement = Product.objects.create(name="Markers")
    counts = services.retire_orphans([reviewed_listing, reviewed_product], replacement=replacement)
    assert counts == {
        **NOTHING_RETIRED,
        "kept_curated": 1,
        "deleted": 1,
        "products_kept_curated": 1,
    }
    assert SupplierProduct.objects.filter(pk=reviewed_listing.pk).exists()
    assert not SupplierProduct.objects.filter(pk=reviewed_product.pk).exists()
    assert Product.objects.filter(merged_into__isnull=True).count() == 3


@pytest.mark.django_db
def test_retire_orphans_never_retires_the_replacement_or_a_redirect():
    old = resolve("Amazon", "N/A", "Sharpie").supplier_product
    counts = services.retire_orphans([old], replacement=old.product)
    assert counts == {**NOTHING_RETIRED, "deleted": 1, "products_in_use": 1}
    assert Product.objects.filter(pk=old.product_id, merged_into=None).exists()
    # A listing made for a product while it was being merged away.
    late = resolve("Amazon", "N/A", "Pens").supplier_product
    survivor = Product.objects.create(name="Pens, blue")
    Product.objects.filter(pk=late.product_id).update(merged_into=survivor)
    assert services.retire_orphans([late]) == {**NOTHING_RETIRED, "deleted": 1}
    assert Product.objects.get(pk=late.product_id).merged_into == survivor


@pytest.mark.django_db
def test_retire_orphans_is_idempotent():
    old = resolve("Amazon", "N/A", "Sharpie").supplier_product
    new = resolve("Amazon", "N/A", "Sharpie markers")
    services.retire_orphans([old], replacement=new.product)
    history = (SupplierProduct.history.count(), Product.history.count())
    again = services.retire_orphans([old, None], replacement=new.product)
    assert again == NOTHING_RETIRED
    assert (SupplierProduct.history.count(), Product.history.count()) == history
    assert services.retire_orphans([]) == NOTHING_RETIRED


# ---------------------------------------------------------------------------------------
# Admin: the review queue


@pytest.mark.django_db
def test_review_queue_lists_filters_and_finds_catalog_numbers(curator_client):
    resolve("Gibco", "11965-092", "DMEM, high glucose")
    resolve("NEB", "R0101S", "EcoRI")
    url = reverse("admin:catalog_product_changelist")
    response = curator_client.get(url, {"review": "needed"})
    assert response.status_code == 200 and b"EcoRI" in response.content
    found = curator_client.get(url, {"q": "11965092"})
    assert b"DMEM, high glucose" in found.content and b"EcoRI" not in found.content
    worded = curator_client.get(url, {"q": "glucose"})
    assert b"DMEM, high glucose" in worded.content
    resolve("ATCC", "ATCC CCL-2.2", "HeLa S3")
    dotted = curator_client.get(url, {"q": "CCL2.2"})  # keyed "CCL2.2" under ATCC's rule
    assert b"HeLa S3" in dotted.content
    for name in ("vendor", "vendorfamily", "supplierproduct", "itemtype", "genericcatalogkey"):
        assert curator_client.get(reverse(f"admin:catalog_{name}_changelist")).status_code == 200


@pytest.mark.django_db
def test_admin_merge_action_asks_which_to_keep_then_merges(curator_client, curator):
    keep, dup = Product.objects.create(name="EcoRI"), Product.objects.create(name="EcoRI-HF")
    url = reverse("admin:catalog_product_changelist")
    selected = {"action": "merge_selected", "_selected_action": [keep.pk, dup.pk]}
    page = curator_client.post(url, {**selected, "index": 0})
    assert page.status_code == 200 and b"Pick the one to keep" in page.content
    done = curator_client.post(url, {**selected, "apply": "Merge", "keep": keep.pk})
    assert done.status_code == 302
    dup.refresh_from_db()
    assert dup.merged_into == keep and dup.history.first().history_user == curator


@pytest.mark.django_db
def test_admin_mark_reviewed(curator_client, make_vendor):
    sp = SupplierProduct.objects.create(
        product=Product.objects.create(name="X"), vendor=make_vendor("V"), catalog_raw="X1"
    )
    url = reverse("admin:catalog_product_changelist")
    data = {"action": "mark_reviewed", "_selected_action": [sp.product_id], "index": 0}
    assert curator_client.post(url, data).status_code == 302
    sp.refresh_from_db()
    sp.product.refresh_from_db()
    assert not sp.product.provisional and not sp.provisional


def inline_data(prefix, rows, fields):
    """POST data for an admin inline showing ``rows``: ``fields(row)`` gives each form's values."""
    data = {
        f"{prefix}-TOTAL_FORMS": str(len(rows)),
        f"{prefix}-INITIAL_FORMS": str(len(rows)),
        f"{prefix}-MIN_NUM_FORMS": "0",
        f"{prefix}-MAX_NUM_FORMS": "1000",
    }
    for index, row in enumerate(rows):
        for name, value in {"id": row.pk, **fields(row)}.items():
            data[f"{prefix}-{index}-{name}"] = value
    return data


@pytest.mark.django_db
def test_admin_moving_a_vendor_to_another_family_moves_its_listings(curator_client):
    sp = resolve("Biotium", "31000", "GelRed").supplier_product
    vendor, neb = sp.vendor, resolve_vendor("NEB").family
    data = {"name": vendor.name, "family": neb.pk}
    data |= inline_data(
        "aliases",
        list(vendor.aliases.all()),
        lambda a: {"vendor": vendor.pk, "alias_key": a.alias_key, "raw_example": a.raw_example},
    )
    response = curator_client.post(reverse("admin:catalog_vendor_change", args=[vendor.pk]), data)
    assert response.status_code == 302, response.content[:2000]
    sp.refresh_from_db()
    assert sp.vendor_family == neb


@pytest.mark.django_db
def test_admin_changing_a_pack_rule_rekeys_the_familys_listings(curator_client):
    sp = resolve("Zymo Research", "R1100-50", "DNA/RNA Shield 50 mL").supplier_product
    assert (sp.catalog_base, sp.pack_code) == ("R1100", "50")
    family = sp.vendor_family
    data = {"slug": family.slug, "name": family.name, "catalog_rule": ""}  # no pack rule
    data |= inline_data("vendors", list(family.vendors.all()), lambda v: {"family": family.pk})
    url = reverse("admin:catalog_vendorfamily_change", args=[family.pk])
    response = curator_client.post(url, data)
    assert response.status_code == 302, response.content[:2000]
    sp.refresh_from_db()
    assert (sp.catalog_key, sp.catalog_base, sp.pack_code) == ("R110050", "R110050", "")
