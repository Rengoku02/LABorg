"""Reading stock and locations (docs/design/mvp-a.md §2).

Every function is scoped to one lab and never returns another lab's rows. They return model
rows (querysets, ready to filter further); pages, emails and CSVs show them only through
``presenters``, and a list of what is in a location with ``listed_in=`` that location.

- ``active_stock(lab, *, products=None, supplier_products=None)``: what the guard checks
- ``items_by_ids(lab, ids)``: the stock items a match or a claim points at
- ``stock_held_by(person, *, lab, active_only=True)``
- ``location_contents(location, *, include_sublocations=False, active_only=True)``
- ``child_locations(location, *, include_inactive=False)``
- ``location_by_path(lab, path, *, include_inactive=False)``
- ``resolve_code(code, *, lab)``: anything typed or scanned -> a StockItem, a Location or None
- ``ancestor_chains(locations)``: the locations around each location, in one query
"""

import re
from functools import reduce
from operator import or_

from django.db.models import Q, Value
from django.db.models.functions import Replace

from apps.catalog.models import Product
from apps.catalog.services import MAX_REDIRECT_HOPS, canonical

from .models import PATH_SEPARATOR, ContainerCode, Location, StockItem
from .shortid import LOCATION_CODE_LENGTH, as_short_id, crockford_normalize

_ITEM_RELATED = (
    "product__item_type",
    "supplier_product__vendor",
    "lot",
    "custodian",
    "location__location_type",
    "location__unsorted_owner",
)
# The bench URLs (/i/<short_id>, /l/<code>), alone or inside a scanned link.
_ROUTE = re.compile(r"(?:^|/)(?P<kind>[il])/(?P<code>[A-Za-z0-9-]+)/?(?:[?#].*)?$")


def product_family_ids(products):
    """The ids of the canonical products of ``products`` (Products or ids) and of every
    product merged into them: stock of a product merged into P counts as P's."""
    ids = set()
    for product in products:
        if product is not None:
            ids.add(canonical(product if isinstance(product, Product) else Product(pk=product)).pk)
    frontier = set(ids)
    for _hop in range(MAX_REDIRECT_HOPS):
        merged = Product._base_manager.filter(merged_into__in=frontier).values_list("pk", flat=True)
        frontier = set(merged) - ids
        if not frontier:
            break
        ids |= frontier
    return ids


def active_stock(lab, *, products=None, supplier_products=None):
    """The lab's unverified, available and opened stock items.

    With ``products`` (Products or ids, redirects followed both ways) and/or
    ``supplier_products``, only the items of those products or listings (either one)."""
    items = StockItem.objects.for_lab(lab).active()
    matches = []
    if products is not None:
        matches.append(Q(product_id__in=product_family_ids(products)))
    if supplier_products is not None:
        matches.append(Q(supplier_product__in=list(supplier_products)))
    if matches:
        items = items.filter(reduce(or_, matches))
    return items.select_related(*_ITEM_RELATED).order_by("pk")


def items_by_ids(lab, ids):
    """These stock items of the lab (active or not), ready for the presenters: e.g. the
    stock a duplicate match points at."""
    return (
        StockItem.objects.for_lab(lab)
        .filter(pk__in=list(ids))
        .select_related(*_ITEM_RELATED)
        .order_by("pk")
    )


def stock_held_by(person, *, lab, active_only=True):
    """What ``person`` is the custodian of in ``lab`` (by product name)."""
    items = StockItem.objects.for_lab(lab).filter(custodian=person)
    if active_only:
        items = items.active()
    return items.select_related(*_ITEM_RELATED).order_by("product__name", "pk")


def location_contents(location, *, include_sublocations=False, active_only=True):
    """The stock in ``location`` (and everything inside it), in position order. Show it with
    ``present_items(..., listed_in=location)``, which leaves out what the viewer may not know
    is there."""
    items = StockItem.objects.for_lab(location.lab_id)
    if include_sublocations:
        items = items.filter(location__path__startswith=location.path)
    else:
        items = items.filter(location=location)
    if active_only:
        items = items.active()
    return items.select_related(*_ITEM_RELATED).order_by("position_index", "position_label", "pk")


def child_locations(location, *, include_inactive=False):
    """The locations directly inside ``location``, in tree order. Show them with
    ``present_locations(..., listed_in=location)``, which leaves out a restricted box the
    viewer may not place there."""
    children = Location.objects.filter(
        lab_id=location.lab_id, depth=location.depth + 1, path__startswith=location.path
    )
    if not include_inactive:
        children = children.active()
    return children.select_related("location_type", "default_custodian", "unsorted_owner")


def split_path(text):
    """The parts of a path as typed ("Room 210 / -80 A"), whitespace collapsed."""
    return tuple(" ".join(part.split()) for part in str(text or "").split("/") if part.strip())


def location_by_path(lab, path, *, include_inactive=False):
    """The lab's location whose path is ``path`` ("room 210 / -80 a" finds "Room 210 / -80 A"),
    or None. Unsorted containers have no path. Active locations win over inactive ones."""
    parts = split_path(path)
    if not parts:
        return None
    wanted = [part.casefold() for part in parts]
    candidates = (
        Location.objects.for_lab(lab)
        .physical()
        .filter(depth=len(parts), name__iexact=parts[-1])
        .order_by("-active", "path")
    )
    if not include_inactive:
        candidates = candidates.active()
    for candidate in candidates:
        names = [loc.name.casefold() for loc in ancestor_chains([candidate])[candidate.pk]]
        if names == wanted:
            return candidate
    return None


def resolve_code(code, *, lab):
    """The lab's StockItem or Location that a typed or scanned code means, or None.

    Understands short ids as people copy them ("7k3m-9q", "7K3MOQ"), location codes in any
    case (and without their hyphens when that is unambiguous), container barcodes, and the
    bench links /i/<short_id> and /l/<code>, alone or in a full URL. Another lab's rows are
    never returned."""
    text = str(code or "").strip()
    if not text or lab is None:
        return None
    route = _ROUTE.search(text)
    if route:
        if route["kind"] == "i":
            return _item_by_short_id(route["code"], lab)
        return _location_by_code(route["code"], lab)
    return (
        _item_by_short_id(text, lab)
        or _location_by_code(text, lab)
        or _item_by_container_code(text, lab)
    )


def _item_by_short_id(text, lab):
    short_id = as_short_id(text)
    if short_id is None:
        return None
    return StockItem.objects.for_lab(lab).filter(short_id=short_id).first()


def _location_by_code(text, lab):
    code = re.sub(r"\s+", "", text).upper()
    locations = Location.objects.for_lab(lab)
    found = locations.filter(code=code).first()
    if found is not None:
        return found
    # Generated codes are Crockford characters, which read I and L as 1 and O as 0.
    crockford = crockford_normalize(text)
    if len(crockford) == LOCATION_CODE_LENGTH and crockford != code:
        found = locations.filter(code=crockford).first()
        if found is not None:
            return found
    compact = code.replace("-", "")
    if not compact:
        return None
    matches = list(
        locations.annotate(compact=Replace("code", Value("-"), Value(""))).filter(compact=compact)[
            :2
        ]
    )
    return matches[0] if len(matches) == 1 else None


def _item_by_container_code(text, lab):
    row = (
        ContainerCode.objects.filter(code=text, stock_item__lab=lab)
        .select_related("stock_item")
        .first()
    )
    return row.stock_item if row else None


def ancestor_chains(locations):
    """``{location pk: [outermost, ..., location]}`` for each location, in one query."""
    locations = [location for location in locations if location is not None]
    steplen = Location.steplen
    prefixes = {
        location.path[:end]
        for location in locations
        for end in range(steplen, len(location.path), steplen)
    }
    above = {}
    if prefixes:
        rows = Location.objects.filter(path__in=prefixes).select_related("unsorted_owner")
        above = {row.path: row for row in rows}
    return {
        location.pk: [
            *(
                above[location.path[:end]]
                for end in range(steplen, len(location.path), steplen)
                if location.path[:end] in above
            ),
            location,
        ]
        for location in locations
    }


def chain_text(chain):
    """ "Room 210 / -80 A / Shelf 1 / Box 4" for a chain from ``ancestor_chains``."""
    return PATH_SEPARATOR.join(location.display_name for location in chain)
