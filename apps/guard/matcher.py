"""The duplicate matcher (design §6): which earlier stock and requests is a new request for
the same product?

This module starts with what the guard, the health panel and the dashboard share with the
Phase 0 baseline (``apps.guard.baseline``), so that they report the same numbers: the tier
order and the reasons a request can't be checked.
"""

from django.db.models import Q

from apps.catalog.models import GenericCatalogKey, ItemType, SupplierProduct
from apps.catalog.normalization import UNKNOWN_FAMILY, cross_family_key_ok
from apps.core.policies import get_policy
from apps.quartzy.models import OrderRequest

from . import baseline

# Strongest first (design §6 "Score"): T1 > T1x > T2. T3 and T4 come in v1.
TIER_ORDER = ("T1", "T1x", "T2", "T3", "T4")

# Why a request can't be checked: the baseline's reasons, so the guard's uncheckable share
# and the baseline's agree. "unresolved" is not one of them: the request hasn't been
# resolved yet (or resolving failed) and is checked again once it is.
UNCHECKABLE_REASONS = baseline.UNCHECKABLE_REASONS
UNRESOLVED = "unresolved"


def uncheckable_reason(order_request, *, min_key_length=None):
    """Why the guard can't compare ``order_request`` on its catalog #, or None if it can.

    One of ``UNCHECKABLE_REASONS`` ("placeholder", "excel_mangled", "too_long", "generic",
    "no_vendor"), or ``UNRESOLVED`` while the request has no resolution yet.
    """
    if (
        order_request.resolution_status == OrderRequest.Resolution.UNRESOLVED
        or order_request.supplier_product_id is None
    ):
        return UNRESOLVED
    supplier_product = order_request.supplier_product
    key = supplier_product.catalog_key
    if key is None:
        catalog = supplier_product.normalized()
        return catalog.no_key_reason or (
            "placeholder" if catalog.is_placeholder else "excel_mangled"
        )
    if is_generic(supplier_product, item_type=item_type_of(order_request)):
        return "generic"
    if min_key_length is None:
        min_key_length = get_policy("guard.cross_family_min_key_length", order_request.lab)
    family = supplier_product.vendor_family
    if family.slug == UNKNOWN_FAMILY and not cross_family_key_ok(key, min_length=min_key_length):
        return "no_vendor"  # no vendor named: only a key long enough for T1x can match
    return None


def is_generic(supplier_product, *, item_type=None):
    """Does this listing's catalog # name no particular product (custom oligos, services,
    fees)? Generic listings never match on catalog # (design §6). Checked again at match
    time, because a key can be added to the stop-list after a request was resolved."""
    if supplier_product.identity == SupplierProduct.Identity.GENERIC:
        return True
    if item_type is not None and item_type.identity_rule == ItemType.IdentityRule.NONE:
        return True
    key = supplier_product.catalog_key
    if key is None:
        return False
    return GenericCatalogKey.objects.filter(
        Q(vendor_family_id=supplier_product.vendor_family_id) | Q(vendor_family__isnull=True),
        catalog_key=key,
    ).exists()


def item_type_of(order_request):
    """The request's item type: its Quartzy type's mapping, else its product's."""
    qtype = order_request.quartzy_type
    if qtype is not None and qtype.item_type_id is not None:
        return qtype.item_type
    product = order_request.product
    if product is not None and product.item_type_id is not None:
        return product.item_type
    return None
