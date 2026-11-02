"""The matcher (design §6): tiers, candidates, look-back, ordering."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.catalog.models import SupplierProduct
from apps.catalog.services import merge_products, resolve_vendor
from apps.guard.matcher import find_matches
from apps.inventory.services import apply_event
from apps.quartzy.models import OrderRequest

pytestmark = pytest.mark.django_db


def summary(order_request, **kwargs):
    """[(tier, kind, what)] of the request's matches, best first."""
    rows = []
    for candidate in find_matches(order_request, **kwargs):
        if candidate.stock_item_id:
            rows.append((candidate.tier, candidate.kind, f"stock {candidate.stock_item_id}"))
        else:
            other = OrderRequest.objects.get(pk=candidate.order_request_id)
            rows.append((candidate.tier, candidate.kind, other.catalog_number_raw))
    return rows


def ago(**delta):
    return timezone.now() - timedelta(**delta)


# ---------------------------------------------------------------------------------------
# Tiers


def test_t1_the_same_catalog_number_on_order_by_someone_else(requested):
    requested(by="raj", at=ago(days=3))
    mine = requested(by="priya")
    assert summary(mine) == [("T1", "open", "2118S")]
    (match,) = find_matches(mine)
    assert match.why == "Same catalog # (Cell Signaling Technology 2118S)"


def test_t1_ignores_how_the_number_is_typed(requested):
    requested(by="raj", qtype="Cell Culture", vendor="Gibco", catalog="11965-092", at=ago(days=3))
    mine = requested(qtype="Cell Culture", vendor="Thermo Fisher", catalog="11965092")
    assert [row[:2] for row in summary(mine)] == [("T1", "open")]


def test_t2_another_pack_size_of_the_same_product(requested):
    requested(by="raj", catalog="2118L", at=ago(days=3))
    mine = requested(catalog="2118S")
    assert summary(mine) == [("T2", "open", "2118L")]


def test_t1x_the_same_long_number_at_another_vendor_needs_a_related_item(requested):
    requested(
        by="raj",
        qtype="Cell Culture",
        vendor="VWR",
        catalog="11965092",
        name="DMEM high glucose",
        at=ago(days=3),
    )
    mine = requested(
        qtype="Cell Culture", vendor="Thermo Fisher", catalog="11965-092", name="DMEM, 500 mL"
    )
    assert [row[:2] for row in summary(mine)] == [("T1x", "open")]


def test_t1x_needs_a_long_enough_key(requested):
    requested(by="raj", vendor="Abcam", catalog="ab8245", at=ago(days=3))
    mine = requested(vendor="Sigma", catalog="AB8245")
    assert summary(mine) == []  # 6 characters: too short to compare across vendors


def test_t1x_needs_the_same_type_or_similar_names(requested):
    requested(
        by="raj",
        qtype="Lab Supplies",
        vendor="VWR",
        catalog="10012345",
        name="Pipette tips 200 uL",
        at=ago(days=3),
    )
    mine = requested(qtype="Antibodies", vendor="Abcam", catalog="10012345", name="Anti-CD3")
    assert summary(mine) == []


def test_no_vendor_named_on_either_side_counts_as_t1x(requested):
    requested(by="raj", vendor="", catalog="11965092", name="DMEM high glucose", at=ago(days=3))
    mine = requested(vendor="N/A", catalog="11965092", name="DMEM high glucose 500 mL")
    assert [row[:2] for row in summary(mine)] == [("T1x", "open")]


def test_a_manufacturer_number_links_a_distributor_listing(requested):
    fisher = requested(
        by="raj",
        qtype="Cell Culture",
        vendor="Fisher Scientific",
        catalog="11-965-118",
        name="DMEM 10 x 500 mL",
        at=ago(days=3),
    )
    listing = fisher.supplier_product
    listing.mfr_family = resolve_vendor("Gibco").family  # thermo, made on first use
    listing.mfr_catalog_key = "11965092"
    listing.save()
    mine = requested(qtype="Cell Culture", vendor="Gibco", catalog="11965-092", name="DMEM")
    assert summary(mine) == [("T1", "open", "11-965-118")]
    assert find_matches(mine)[0].why.startswith("Same product, manufacturer's #")


def test_products_merged_in_the_catalog_match_as_the_same_product(requested):
    other = requested(
        by="raj", vendor="Qiagen", catalog="74104", name="RNeasy Mini Kit", at=ago(days=3)
    )
    mine = requested(vendor="Qiagen", catalog="74106", name="RNeasy Mini Kit (250)")
    assert summary(mine) == []  # Qiagen numbers carry no pack code
    merge_products(mine.product, other.product)
    mine.refresh_from_db()
    assert summary(mine) == [("T2", "open", "74104")]
    assert find_matches(mine)[0].why == "Same product, another listing"


def test_generic_and_uncheckable_requests_never_match(requested):
    requested(by="raj", qtype="Oligos", vendor="IDT", catalog="51-01-01-01", at=ago(days=3))
    oligo = requested(qtype="Oligos", vendor="IDT", catalog="51-01-01-01")
    assert summary(oligo) == []
    requested(by="raj", catalog="N/A", at=ago(days=3))
    assert summary(requested(catalog="N/A")) == []


# ---------------------------------------------------------------------------------------
# Candidates: stock, open requests, bought before


def test_stock_in_the_lab_matches_whoever_holds_it(requested, stock, raj):
    item = stock(custodian=raj, visibility="restricted")
    mine = requested()
    assert summary(mine) == [("T1", "stock", f"stock {item.pk}")]


def test_unverified_stock_is_a_probable_match(requested, stock, raj):
    stock(custodian=raj, lifecycle="unverified", location=None, temperature="-20C")
    (match,) = find_matches(requested())
    assert match.probably


def test_empty_stock_is_not_a_match(requested, stock, raj):
    item = stock(custodian=raj)
    apply_event(item, "emptied", user=raj)
    assert summary(requested()) == []


def test_a_request_whose_stock_is_listed_counts_once_as_the_stock(requested, stock, raj):
    earlier = requested(by="raj", status="RECEIVED", at=ago(days=60))
    item = stock(custodian=raj, source_order_request=earlier)
    assert summary(requested()) == [("T1", "stock", f"stock {item.pk}")]


def test_bought_before_looks_back_as_far_as_the_type_says(requested):
    requested(by="raj", status="RECEIVED", at=ago(days=300), catalog="2118S")
    requested(by="raj", status="RECEIVED", at=ago(days=800), catalog="2118L")  # > 24 months
    mine = requested()
    assert summary(mine) == [("T1", "bought", "2118S")]


def test_consumables_only_count_while_on_order(requested):
    kw = {"qtype": "Lab Supplies", "vendor": "Corning", "catalog": "4488", "name": "Tips"}
    requested(by="raj", status="RECEIVED", at=ago(days=20), **kw)
    assert summary(requested(**kw)) == []  # look-back 0 for consumables
    requested(by="sam", status="ORDERED", at=ago(days=2), **kw)
    assert [row[1] for row in summary(requested(**kw))] == ["open"]


def test_an_old_open_request_is_probably_arrived(requested):
    requested(by="raj", status="ORDERED", at=ago(days=70))
    (match,) = find_matches(requested())
    assert (match.kind, match.probably) == ("open", True)


def test_cancelled_and_later_requests_are_not_candidates(requested):
    requested(by="raj", status="CANCELLED", at=ago(days=3))
    mine = requested(at=ago(days=1))
    requested(by="sam", at=ago(hours=1))  # made after mine
    assert summary(mine) == []


def test_the_requesters_own_requests_are_skipped_unless_the_policy_says(requested, policy):
    requested(by="priya", at=ago(days=3))
    mine = requested(by="priya")
    assert summary(mine) == []
    policy("guard.include_same_requester", True)
    assert [row[:2] for row in summary(mine)] == [("T1", "open")]


def test_candidates_come_best_first(requested, stock, raj):
    requested(by="raj", catalog="2118L", at=ago(days=1))  # T2 open
    requested(by="sam", status="RECEIVED", at=ago(days=100))  # T1 bought
    requested(by="raj", at=ago(days=5))  # T1 open
    item = stock(custodian=raj)  # T1 stock
    assert summary(requested()) == [
        ("T1", "stock", f"stock {item.pk}"),
        ("T1", "open", "2118S"),
        ("T1", "bought", "2118S"),
        ("T2", "open", "2118L"),
    ]


def test_matching_is_deterministic(requested, stock, raj):
    for days in (1, 2, 3):
        requested(by="raj", at=ago(days=days))
    stock(custodian=raj)
    mine = requested()
    assert find_matches(mine) == find_matches(mine)


def test_generic_listings_are_left_out_even_with_the_same_key(requested):
    requested(by="raj", at=ago(days=3))
    SupplierProduct.objects.filter(catalog_key="2118S").update(identity="generic")
    mine = requested(catalog="2118 S")
    assert summary(mine) == []
