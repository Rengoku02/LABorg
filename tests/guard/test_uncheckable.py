"""The guard's reasons a request can't be checked are the baseline's (plan §9, design §6)."""

import itertools

import pytest

from apps.catalog.models import GenericCatalogKey
from apps.catalog.services import resolve_supplier_product
from apps.core.policies import set_policy
from apps.guard import baseline
from apps.guard.matcher import TIER_ORDER, UNRESOLVED, uncheckable_reason
from apps.quartzy.models import OrderRequest

_ids = itertools.count(1)


@pytest.fixture
def request_for(lab):
    def _make(vendor, catalog, name="Thing"):
        resolution = resolve_supplier_product(
            vendor_name=vendor, catalog_number=catalog, item_name=name
        )
        return OrderRequest.objects.create(
            lab=lab,
            quartzy_id=f"q-{next(_ids)}",
            item_name=name,
            vendor_name_raw=vendor,
            catalog_number_raw=catalog,
            supplier_product=resolution.supplier_product,
            product=resolution.product,
            resolution_status=resolution.status,
        )

    return _make


def test_the_baseline_ranks_tiers_as_the_guard_does():
    assert baseline.TIERS == TIER_ORDER[:3] == ("T1", "T1x", "T2")


@pytest.mark.django_db
def test_reasons(request_for, lab):
    assert uncheckable_reason(request_for("CST", "2118S")) is None
    assert uncheckable_reason(request_for("CST", "N/A")) == "placeholder"
    assert uncheckable_reason(request_for("Thermo Fisher", "1.1965E+07")) == "excel_mangled"
    assert uncheckable_reason(request_for("", "123")) == "no_vendor"
    assert uncheckable_reason(request_for("N/A", "11965092")) is None  # long enough for T1x
    pending = OrderRequest.objects.create(lab=lab, quartzy_id="q-pending", item_name="x")
    assert uncheckable_reason(pending) == UNRESOLVED


@pytest.mark.django_db
def test_a_key_added_to_the_stop_list_later_makes_requests_generic(request_for):
    oligo = request_for("IDT", "51-01-01-01")
    assert uncheckable_reason(oligo) is None
    GenericCatalogKey.objects.create(catalog_key="51-01-01-01")  # every family
    assert uncheckable_reason(oligo) == "generic"


@pytest.mark.django_db
def test_the_no_vendor_key_length_follows_the_policy(request_for, lab):
    short = request_for("", "123456")
    assert uncheckable_reason(short) == "no_vendor"
    set_policy("guard.cross_family_min_key_length", 6, lab=lab)
    assert uncheckable_reason(short) is None
