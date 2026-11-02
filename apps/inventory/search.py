"""Search before you buy (docs/design/mvp-a.md §10.2).

``search(lab, viewer, q)`` reads what someone typed or pasted: a vendor product page
(``apps.catalog.vendor_urls``), a catalog # ("2118S", "CST 2118S", "11965-092"), or a name.
It answers with what the lab has, has on order and bought before, grouped, through the
presenters: a restricted item shows that it exists and whom to ask, never where it is.

Each search is logged without its text (``SearchLog``: product ids and counts only), for the
aggregate search-before-buy rate; ``purge_search_logs`` deletes rows after
``search.log_retention_days``.
"""

import re
from dataclasses import dataclass, field
from datetime import timedelta

from django.contrib.postgres.search import TrigramSimilarity
from django.db.models import Q
from django.utils import timezone

from apps.catalog.models import LabProduct, Product, SupplierProduct, VendorFamily
from apps.catalog.normalization import (
    UNKNOWN_FAMILY,
    cross_family_key_ok,
    normalize_catalog,
    vendor_family_for,
)
from apps.catalog.vendor_urls import parse_vendor_url
from apps.core.dates import months_before
from apps.core.models import Lab
from apps.core.policies import get_policy
from apps.quartzy.models import OrderRequest
from apps.quartzy.presenters import present_requests, with_related

from .models import Location, SearchLog, Visibility
from .presenters import present_items, present_locations
from .selectors import active_stock, product_family_ids

MAX_PRODUCTS = 25
_HAS_DIGIT = re.compile(r"\d")


@dataclass(frozen=True)
class SearchResult:
    query_kind: str  # url | catalog | name | "" (nothing to search)
    vendor: str = ""  # the vendor family a URL or a "CST 2118S" named
    catalog_raw: str = ""
    products: tuple = ()  # (id, name)
    in_lab: tuple = ()  # ItemViews, shared first
    probably: tuple = ()  # ItemViews of unverified stock: "Probably: ask X"
    on_order: tuple = ()  # RequestViews
    bought_before: tuple = ()  # RequestViews
    boxes: tuple = ()  # LocationViews
    words: str = ""

    @property
    def found_anything(self):
        return bool(self.in_lab or self.probably or self.on_order or self.bought_before)


@dataclass
class _Query:
    kind: str
    family: str | None = None
    catalog: str | None = None
    words: str = ""
    product_ids: set = field(default_factory=set)


def read_query(q):
    """What kind of search ``q`` is: ("url" | "catalog" | "name", family, catalog, words)."""
    q = " ".join((q or "").split())[:300]
    if not q:
        return _Query("")
    url = parse_vendor_url(q)
    if url is not None:
        return _Query("url", url.family, url.catalog_raw, url.words)
    tokens = q.split()
    if len(tokens) >= 2 and _HAS_DIGIT.search(tokens[-1]):
        family = vendor_family_for(" ".join(tokens[:-1]))
        if family and family != UNKNOWN_FAMILY:
            return _Query("catalog", family, tokens[-1], q)
    if len(tokens) <= 3 and _HAS_DIGIT.search(q) and len(q) <= 40:
        # "2118S", "11965-092", "2118 S": a catalog #, else (nothing found) a name.
        return _Query("catalog", None, q, q)
    return _Query("name", None, None, q)


def search(lab, viewer, q, *, log=True, now=None):
    now = now or timezone.now()
    query = read_query(q)
    if not query.kind:
        return SearchResult("")
    if query.catalog:
        query.product_ids |= _by_catalog(query.family, query.catalog)
    if not query.product_ids and query.words:
        query.product_ids |= _by_name(lab, query.words)
    family_ids = product_family_ids(query.product_ids) if query.product_ids else set()
    items = present_items(
        active_stock(lab, products=family_ids) if family_ids else [], viewer, channel="page"
    )
    shared_first = sorted(items, key=lambda view: view.visibility != Visibility.SHARED)
    in_lab = tuple(view for view in shared_first if not view.unverified)
    probably = tuple(view for view in shared_first if view.unverified)
    requests = OrderRequest.objects.for_lab(lab).filter(product_id__in=family_ids)
    on_order = present_requests(
        with_related(requests.open().order_by("-requested_at")), viewer, channel="page"
    )
    since = months_before(now, get_policy("search.bought_before_months", lab))
    bought = (
        requests.live()
        .filter(status=OrderRequest.Status.RECEIVED, requested_at__gte=since)
        .order_by("-requested_at")
    )
    bought_before = present_requests(with_related(bought[:20]), viewer, channel="page")
    boxes = ()
    if query.kind == "name" and len(query.words) >= 3:
        boxes = tuple(_boxes(lab, viewer, query.words))
    names = dict(Product.objects.filter(pk__in=query.product_ids).values_list("pk", "name"))
    result = SearchResult(
        query_kind=query.kind,
        vendor=query.family or "",
        catalog_raw=query.catalog or "",
        products=tuple(sorted(names.items(), key=lambda pair: pair[1].casefold())),
        in_lab=in_lab,
        probably=probably,
        on_order=tuple(on_order),
        bought_before=tuple(bought_before),
        boxes=boxes,
        words=query.words,
    )
    if log and getattr(viewer, "is_authenticated", False):
        _log(lab, viewer, query, result, now)
    return result


def _by_catalog(family, catalog):
    """Canonical product ids whose listings carry this catalog # (any formatting): the same
    key in the named family (or any family, for a key long enough to compare across
    vendors), the same base (other pack sizes), and manufacturer keys."""
    rule = None
    if family:
        row = VendorFamily.objects.filter(slug=family).first()
        rule = row.pack_rule if row else None
    norm = normalize_catalog(catalog, family, rule=rule)
    if norm.key is None:
        return set()
    match = Q(catalog_key=norm.key) | Q(mfr_catalog_key=norm.key)
    if family:
        match = (Q(vendor_family__slug=family) & (match | Q(catalog_base=norm.base))) | Q(
            mfr_catalog_key=norm.key
        )
        if cross_family_key_ok(norm.key):
            match |= Q(catalog_key=norm.key)
    elif norm.base and norm.base != norm.key:
        match |= Q(catalog_base=norm.base)
    rows = SupplierProduct.objects.filter(match).exclude(identity=SupplierProduct.Identity.GENERIC)
    return {pk for pk in rows.values_list("product_id", flat=True)[:MAX_PRODUCTS]}


def _by_name(lab, words):
    """Canonical product ids named like ``words``: similar names, or a lab's synonym."""
    similar = (
        Product.objects.canonical()
        .annotate(similarity=TrigramSimilarity("name", words))
        .filter(Q(similarity__gte=0.3) | Q(name__icontains=words))
        .order_by("-similarity")
        .values_list("pk", flat=True)[:MAX_PRODUCTS]
    )
    found = set(similar)
    synonyms = LabProduct.objects.for_lab(lab).filter(synonyms__icontains=words)
    found |= set(synonyms.values_list("product_id", flat=True)[:MAX_PRODUCTS])
    return found


def _boxes(lab, viewer, words):
    rows = (
        Location.objects.for_lab(lab)
        .physical()
        .filter(active=True, location_type__structural=False)
        .filter(Q(name__icontains=words) | Q(tags__icontains=words.lower()))
        .select_related("location_type", "default_custodian", "unsorted_owner")
        .order_by("path")[:10]
    )
    return present_locations(list(rows), viewer, channel="page")


def _log(lab, user, query, result, now):
    key = ""
    if query.catalog:
        key = normalize_catalog(query.catalog, query.family).key or ""
    SearchLog.objects.create(
        lab=lab,
        user=user,
        at=now,
        kind=query.kind,
        vendor_family=query.family or "",
        catalog_key=key[:100],
        product_ids=sorted(query.product_ids),
        in_lab=len(result.in_lab) + len(result.probably),
        on_order=len(result.on_order),
        bought_before=len(result.bought_before),
    )


def purge_search_logs(now=None):
    """Delete search logs older than ``search.log_retention_days`` (per lab, daily)."""
    now = now or timezone.now()
    deleted = 0
    for lab_id in SearchLog.objects.values_list("lab_id", flat=True).distinct():
        lab = Lab.objects.get(pk=lab_id)
        days = get_policy("search.log_retention_days", lab)
        count, _ = SearchLog.objects.filter(lab=lab, at__lt=now - timedelta(days=days)).delete()
        deleted += count
    return deleted


def searched_before(order_request, *, days=None):
    """Did the requester search for this product in the days before the request? None when
    it can't be told (no requester or product)."""
    person, product = order_request.requested_by_id, order_request.product_id
    when = order_request.requested_at or order_request.first_seen_at
    if person is None or product is None or when is None:
        return None
    days = days or get_policy("search.searched_before_days", order_request.lab)
    family = sorted(product_family_ids([product]))
    return SearchLog.objects.filter(
        lab=order_request.lab,
        user_id=person,
        at__gte=when - timedelta(days=days),
        at__lte=when,
        product_ids__overlap=family,
    ).exists()
