"""Catalog models: constraints, derived fields, history and Postgres search."""

import pytest
from django.contrib.postgres.search import SearchQuery, TrigramSimilarity
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction

from apps.catalog import normalization
from apps.catalog.models import (
    GenericCatalogKey,
    ItemType,
    LabProduct,
    Lot,
    Product,
    ProductCode,
    SupplierProduct,
    Vendor,
    VendorAlias,
    VendorFamily,
    validate_catalog_rule,
)

pytestmark = pytest.mark.django_db


def raises_integrity_error():
    """pytest.raises(IntegrityError) inside a savepoint, so the test can keep using the DB."""

    class _Raises:
        def __enter__(self):
            self.atomic = transaction.atomic()
            self.atomic.__enter__()
            self.raises = pytest.raises(IntegrityError)
            return self.raises.__enter__()

        def __exit__(self, *exc):
            suppressed = self.raises.__exit__(*exc)
            self.atomic.__exit__(IntegrityError, IntegrityError(), None)
            return suppressed

    return _Raises()


# ---------------------------------------------------------------------------------------
# SupplierProduct


def test_supplier_product_derives_family_and_keys(make_vendor):
    gibco = make_vendor("Gibco", family="thermo", catalog_rule="thermo")
    product = Product.objects.create(name="DMEM, high glucose")
    sp = SupplierProduct.objects.create(product=product, vendor=gibco, catalog_raw="11965-092")
    assert sp.vendor_family == gibco.family
    assert (sp.catalog_key, sp.catalog_base, sp.pack_code) == ("11965092", "11965", "092")
    placeholder = SupplierProduct.objects.create(product=product, vendor=gibco, catalog_raw="N/A")
    assert (placeholder.catalog_key, placeholder.catalog_base) == (None, None)


def test_changing_the_vendor_changes_the_derived_family(make_vendor, make_supplier_product):
    gibco = make_vendor("Gibco", family="thermo", catalog_rule="thermo")
    fisher = make_vendor("Fisher Scientific", family="fisher", catalog_rule="fisher")
    sp = make_supplier_product(gibco, "11965092")
    sp.vendor = fisher
    sp.save(update_fields=["vendor"])
    sp.refresh_from_db()
    assert sp.vendor_family == fisher.family
    assert sp.catalog_base == "11965092"  # Fisher's rule doesn't split Gibco numbers


def test_identifying_key_is_unique_within_a_family(make_vendor, make_supplier_product):
    gibco = make_vendor("Gibco", family="thermo", catalog_rule="thermo")
    invitrogen = make_vendor("Invitrogen", family="thermo")
    make_supplier_product(gibco, "11965092")
    with raises_integrity_error():
        make_supplier_product(invitrogen, "11965-092")  # same family, same key


def test_same_key_is_fine_across_families_generic_or_missing(make_vendor, make_supplier_product):
    gibco = make_vendor("Gibco", family="thermo", catalog_rule="thermo")
    fisher = make_vendor("Fisher Scientific", family="fisher")
    make_supplier_product(gibco, "11965092")
    make_supplier_product(fisher, "11-965-092")
    idt = make_vendor("IDT", family="idt")
    make_supplier_product(idt, "51-01-01-01", identity=SupplierProduct.Identity.GENERIC)
    make_supplier_product(idt, "51-01-01-01", identity=SupplierProduct.Identity.GENERIC)
    make_supplier_product(idt, "")
    make_supplier_product(idt, "TBD")
    assert SupplierProduct.objects.filter(catalog_key="11965092").count() == 2
    assert SupplierProduct.objects.filter(catalog_key__isnull=True).count() == 2


def test_clean_explains_a_duplicate_catalog_number(make_vendor, make_supplier_product):
    gibco = make_vendor("Gibco", family="thermo", catalog_rule="thermo")
    first = make_supplier_product(gibco, "11965092", name="DMEM")
    duplicate = SupplierProduct(product=first.product, vendor=gibco, catalog_raw="11965-092")
    with pytest.raises(ValidationError, match="Merge the products instead"):
        duplicate.full_clean()
    other_pack = SupplierProduct(product=first.product, vendor=gibco, catalog_raw="11965-084")
    other_pack.full_clean()  # no clash


def test_manufacturer_key_is_normalized(make_vendor, make_supplier_product):
    fisher = make_vendor("Fisher Scientific", family="fisher")
    sp = make_supplier_product(fisher, "11-965-092", mfr_catalog_key="Cat# 11965-092")
    assert sp.mfr_catalog_key == "11965092"


def test_manufacturer_key_follows_the_manufacturers_rule(make_vendor, make_supplier_product):
    fisher = make_vendor("Fisher Scientific", family="fisher")
    atcc = VendorFamily.objects.create(slug="atcc", name="ATCC", catalog_rule="atcc")
    sp = make_supplier_product(fisher, "50-238-3201", mfr_family=atcc, mfr_catalog_key="CCL-2.2")
    assert sp.mfr_catalog_key == "CCL2.2"  # as ATCC's own listing of HeLa S3 is keyed
    sp.mfr_family = None
    sp.save(update_fields=["mfr_family"])
    sp.refresh_from_db()
    assert sp.mfr_catalog_key == "CCL22"


def test_keys_too_long_to_be_catalog_numbers_fit_their_columns(make_vendor, make_supplier_product):
    pasted = ", ".join(f"R0{number}S" for number in range(101, 121))  # 20 numbers, 158 chars
    fisher = make_vendor("Fisher Scientific", family="fisher")
    sp = make_supplier_product(fisher, pasted, mfr_catalog_key=pasted)
    assert (sp.catalog_key, sp.catalog_base, sp.mfr_catalog_key) == (None, None, None)
    generic = GenericCatalogKey.objects.create(catalog_key=pasted)
    assert generic.catalog_key == pasted[:100]  # never matched: keys are alphanumeric
    with pytest.raises(ValidationError):
        GenericCatalogKey(catalog_key=pasted).full_clean()
    lot = Lot.objects.create(product=sp.product, lot_raw=pasted)
    assert lot.lot_key == ""
    for model, name in [
        (SupplierProduct, "catalog_key"),
        (SupplierProduct, "catalog_base"),
        (SupplierProduct, "mfr_catalog_key"),
        (GenericCatalogKey, "catalog_key"),
        (Lot, "lot_key"),
    ]:
        assert model._meta.get_field(name).max_length >= normalization.MAX_KEY_LENGTH


# ---------------------------------------------------------------------------------------
# Other tables


def test_product_codes_are_not_unique(make_vendor, make_supplier_product):
    vendor = make_vendor("Corning")
    first, second = make_supplier_product(vendor, "3516"), make_supplier_product(vendor, "3506")
    ProductCode.objects.create(code="0 0709-0127 3516 5", supplier_product=first)
    ProductCode.objects.create(code="00709012735165", supplier_product=second)
    ProductCode.objects.create(code="00709012735165", supplier_product=second)
    assert ProductCode.objects.filter(code="00709012735165").count() == 3


def test_lot_is_unique_per_product_and_keyed_from_the_raw_lot():
    product = Product.objects.create(name="FBS")
    lot = Lot.objects.create(product=product, lot_raw="Lot# 2183-45B")
    assert lot.lot_key == "218345B"
    Lot.objects.create(product=Product.objects.create(name="other"), lot_key="218345B")
    with raises_integrity_error():
        Lot.objects.create(product=product, lot_raw="2183 45b")


def test_lab_product_is_unique_per_lab(lab):
    product = Product.objects.create(name="Anti-GAPDH")
    LabProduct.objects.create(lab=lab, product=product, synonyms=["the GAPDH ab"])
    assert LabProduct.objects.for_lab(lab).get().synonyms == ["the GAPDH ab"]
    with raises_integrity_error():
        LabProduct.objects.create(lab=lab, product=product)


def test_generic_key_is_normalized_and_unique_even_without_a_family():
    GenericCatalogKey.objects.create(catalog_key="51-01-01-01", reason="IDT custom DNA oligo")
    assert GenericCatalogKey.objects.get().catalog_key == "51010101"
    with raises_integrity_error():
        GenericCatalogKey.objects.create(catalog_key="51010101")  # NULL family twice
    idt = VendorFamily.objects.create(slug="idt", name="IDT")
    GenericCatalogKey.objects.create(vendor_family=idt, catalog_key="51010101")
    with pytest.raises(ValidationError):
        GenericCatalogKey(catalog_key="N/A").full_clean()


def test_a_familys_generic_key_is_keyed_under_its_rule():
    atcc = VendorFamily.objects.create(slug="atcc", name="ATCC", catalog_rule="atcc")
    GenericCatalogKey.objects.create(vendor_family=atcc, catalog_key="CCL-2.2")
    GenericCatalogKey.objects.create(catalog_key="CCL-2.2")  # every family: no rule
    keys = GenericCatalogKey.objects.order_by("vendor_family").values_list("catalog_key", flat=True)
    assert list(keys) == ["CCL2.2", "CCL22"]


def test_vendor_alias_keys_are_unique(make_vendor):
    vendor = make_vendor("Bio-Rad")
    VendorAlias.objects.create(alias_key="bio rad", vendor=vendor)
    with raises_integrity_error():
        VendorAlias.objects.create(alias_key="bio rad", vendor=make_vendor("Other"))


def test_vendor_names_are_unique(make_vendor):
    make_vendor("Acme")
    with raises_integrity_error():
        Vendor.objects.create(name="Acme", family=VendorFamily.objects.get(slug="acme"))


@pytest.mark.parametrize("tier", [0, 6])
def test_item_type_tier_must_be_1_to_5(tier):
    with raises_integrity_error():
        ItemType.objects.create(key="bad", label="Bad", tier=tier)


def test_catalog_rule_must_name_a_pack_rule():
    validate_catalog_rule("")
    validate_catalog_rule("neb")
    with pytest.raises(ValidationError):
        validate_catalog_rule("nope")
    family = VendorFamily(slug="x", name="X", catalog_rule="nope")
    assert family.pack_rule is None
    family.catalog_rule = "neb"
    assert family.pack_rule == "neb"


def test_apply_seed_changes_only_fields_the_seed_still_owns():
    values = {"name": "Thermo Fisher", "catalog_rule": "", "is_distributor": False}
    family = VendorFamily(slug="thermo")
    family.apply_seed(values)
    family.save()
    family.name = "Thermo (edited by a person)"
    changed = family.apply_seed(
        {**values, "name": "Thermo Fisher Scientific", "catalog_rule": "thermo"}
    )
    assert changed == ["catalog_rule"]
    assert family.name == "Thermo (edited by a person)" and family.catalog_rule == "thermo"
    assert family.seeded == {
        "name": "Thermo Fisher",
        "catalog_rule": "thermo",
        "is_distributor": False,
    }


def test_history_is_recorded(make_vendor, make_supplier_product):
    sp = make_supplier_product(make_vendor("NEB"), "R0101S", name="EcoRI")
    sp.product.name = "EcoRI-HF"
    sp.product.save()
    assert [h.name for h in sp.product.history.order_by("history_date")] == ["EcoRI", "EcoRI-HF"]
    assert sp.history.count() == 1
    assert Vendor.history.count() == 1 and VendorFamily.history.count() == 1
    lot = Lot.objects.create(product=sp.product, lot_raw="10012345")
    assert lot.history.count() == 1


def test_canonical_queryset_hides_redirects():
    kept = Product.objects.create(name="kept")
    Product.objects.create(name="merged", merged_into=kept)
    assert list(Product.objects.canonical()) == [kept]


# ---------------------------------------------------------------------------------------
# Postgres search


def test_pg_trgm_is_installed_and_trigram_similarity_works():
    with connection.cursor() as cursor:
        cursor.execute("SELECT extname FROM pg_extension WHERE extname = 'pg_trgm'")
        assert cursor.fetchone() == ("pg_trgm",)
        cursor.execute(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'catalog_product_name_trgm'"
        )
        assert "gin_trgm_ops" in cursor.fetchone()[0]
    Product.objects.create(name="Anti-GAPDH antibody [6C5] - Loading Control")
    Product.objects.create(name="DMEM, high glucose, GlutaMAX")
    ranked = (
        Product.objects.annotate(similarity=TrigramSimilarity("name", "gapdh antibody"))
        .filter(similarity__gt=0.2)
        .order_by("-similarity")
    )
    assert [p.name for p in ranked] == ["Anti-GAPDH antibody [6C5] - Loading Control"]
    assert Product.objects.filter(name__trigram_similar="DMEM high glucose").count() == 1


def test_full_text_search_covers_name_and_synonyms():
    product = Product.objects.create(
        name="Penicillin-Streptomycin (10,000 U/mL)", synonyms=["Pen Strep", "P/S 100x"]
    )
    Product.objects.create(name="Trypsin-EDTA (0.25%), phenol red")
    assert list(Product.objects.filter(search_vector=SearchQuery("penicillin"))) == [product]
    assert list(Product.objects.filter(search_vector=SearchQuery("strep"))) == [product]
    product.synonyms = ["antibiotic mix"]
    product.save()
    assert list(Product.objects.filter(search_vector=SearchQuery("antibiotics"))) == [product]
    assert not Product.objects.filter(search_vector=SearchQuery("pen strep")).exists()
