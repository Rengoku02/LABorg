"""The duplicate matcher (design §6): which earlier stock and requests is a new request for
the same product?

This module starts with what the guard, the health panel and the dashboard share with the
Phase 0 baseline (``apps.guard.baseline``), so that they report the same numbers: the tier
order and the reasons a request can't be checked.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db.models import Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.catalog.models import GenericCatalogKey, ItemType, SupplierProduct
from apps.catalog.normalization import UNKNOWN_FAMILY, cross_family_key_ok
from apps.core.dates import months_before
from apps.core.policies import get_policy
from apps.inventory.selectors import active_stock, product_family_ids
from apps.quartzy.models import OrderRequest, OrderStatusObservation

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


# ---------------------------------------------------------------------------------------
# Matching (design §6)

MATCHER_VERSION = 1
KIND_ORDER = ("stock", "open", "bought")
OPEN = "open"
BOUGHT = "bought"
STOCK = "stock"
# A RECEIVED request with no status history counts as received at a point in time only
# when it was requested this long before it (the baseline's received_days 14 plus
# assumed_delivery_days 14); until then it was probably still on its way.
REPLAY_RECEIVED_AFTER = timedelta(days=28)


@dataclass(frozen=True)
class Candidate:
    """One earlier thing a request matched, best first by ``score``."""

    tier: str
    kind: str  # stock | open | bought
    score: float
    stock_item_id: int | None
    order_request_id: int | None
    product_id: int | None
    probably: bool  # unverified stock, or an open request old enough to have arrived
    why: str
    since: datetime | None  # when the candidate was requested or recorded

    @property
    def key(self):
        if self.stock_item_id is not None:
            return (STOCK, self.stock_item_id)
        return ("request", self.order_request_id)


@dataclass(frozen=True)
class Policy:
    min_key_length: int
    min_similarity: float
    open_probably_arrived_days: int
    include_same_requester: bool

    @classmethod
    def for_lab(cls, lab):
        return cls(
            min_key_length=get_policy("guard.cross_family_min_key_length", lab),
            min_similarity=get_policy("guard.cross_family_min_name_similarity", lab),
            open_probably_arrived_days=get_policy("guard.open_probably_arrived_days", lab),
            include_same_requester=get_policy("guard.include_same_requester", lab),
        )


@dataclass(frozen=True)
class Subject:
    """The request being checked, reduced to what matching compares."""

    request: OrderRequest
    listing: SupplierProduct
    family_id: int
    family_slug: str
    key: str
    base: str | None
    mfr_family_id: int | None
    mfr_key: str | None
    product_ids: frozenset
    item_type: ItemType | None
    name: str
    quartzy_type_name: str  # casefolded; "" when the request has none
    at: datetime
    requester_id: int | None
    requester_alias_id: int | None

    @classmethod
    def of(cls, order_request):
        listing = order_request.supplier_product
        return cls(
            request=order_request,
            listing=listing,
            family_id=listing.vendor_family_id,
            family_slug=listing.vendor_family.slug,
            key=listing.catalog_key,
            base=listing.catalog_base,
            mfr_family_id=listing.mfr_family_id,
            mfr_key=listing.mfr_catalog_key,
            product_ids=frozenset(
                product_family_ids([order_request.product_id or listing.product_id])
            ),
            item_type=item_type_of(order_request),
            name=order_request.item_name or listing.product.name,
            quartzy_type_name=(
                order_request.quartzy_type.name.casefold()
                if order_request.quartzy_type_id and order_request.quartzy_type.name
                else ""
            ),
            at=request_time(order_request),
            requester_id=order_request.requested_by_id,
            requester_alias_id=_alias_id(order_request),
        )


def request_time(order_request):
    """When a request was made: Quartzy's requested_at, else when the mirror first saw it."""
    return order_request.requested_at or order_request.first_seen_at


def subject_rows():
    """OrderRequest rows with what ``find_matches`` and the checks read."""
    return OrderRequest.objects.select_related(
        "lab",
        "quartzy_lab",
        "quartzy_type__item_type",
        "product__item_type",
        "supplier_product__vendor_family",
        "supplier_product__vendor",
        "supplier_product__product",
        "requested_by_alias",
        "created_by_alias",
    )


def find_matches(order_request, *, as_of=None, replay=False):
    """The earlier stock and requests ``order_request`` is a duplicate of, best first.

    Deterministic, and pure apart from database reads. Empty when the request can't be
    checked (``uncheckable_reason``). ``as_of`` is when the check reflects (default: now).
    A replay (``replay=True``, ``as_of`` = the request's time) reads each earlier request's
    status at that moment from the status observations, else from its current status and
    age as the baseline does, and leaves stock out: LabStock's stock records didn't exist
    back then.
    """
    if uncheckable_reason(order_request) is not None:
        return []
    as_of = as_of or timezone.now()
    lab = order_request.lab
    policy = Policy.for_lab(lab)
    subject = Subject.of(order_request)
    listings = _related_listings(subject, policy)
    found = []
    if not replay:
        found += _stock_candidates(subject, listings, as_of, policy)
    found += _request_candidates(subject, listings, as_of, policy, replay=replay)
    best = {}
    for candidate in found:
        if candidate.key not in best or candidate.score > best[candidate.key].score:
            best[candidate.key] = candidate
    return sorted(
        best.values(),
        key=lambda c: (-c.score, KIND_ORDER.index(c.kind), c.stock_item_id or 0, c.key),
    )


def _related_listings(subject, policy):
    """Identifying listings that could be the same product: the same key or base in the
    family, the same key elsewhere (T1x), and manufacturer-key links."""
    match = Q(pk=subject.listing.pk)
    match |= Q(vendor_family_id=subject.family_id, catalog_key=subject.key)
    if subject.base:
        match |= Q(vendor_family_id=subject.family_id, catalog_base=subject.base)
    if cross_family_key_ok(subject.key, min_length=policy.min_key_length):
        match |= Q(catalog_key=subject.key)
    match |= Q(mfr_family_id=subject.family_id, mfr_catalog_key=subject.key)
    if subject.mfr_family_id and subject.mfr_key:
        match |= Q(vendor_family_id=subject.mfr_family_id, catalog_key=subject.mfr_key)
        match |= Q(mfr_family_id=subject.mfr_family_id, mfr_catalog_key=subject.mfr_key)
    rows = (
        SupplierProduct.objects.filter(match)
        .exclude(identity=SupplierProduct.Identity.GENERIC)
        .select_related("vendor_family", "vendor")
    )
    return {row.pk: row for row in rows}


def _tier(subject, listing, product_id, other_type, other_name, policy, other_quartzy_type=None):
    """(tier, why) for a candidate with this listing (or None) and product, else None."""
    if listing is not None and listing.identity == SupplierProduct.Identity.GENERIC:
        return None
    if listing is not None and listing.catalog_key:
        label = f"{listing.vendor} {listing.catalog_raw}".strip()
        same_family = listing.vendor_family_id == subject.family_id
        if same_family and listing.catalog_key == subject.key:
            if subject.family_slug != UNKNOWN_FAMILY:
                return "T1", f"Same catalog # ({label})"
            # No vendor named on either: the key may still be another vendor's (design §6).
            if _related(subject, other_type, other_name, policy, other_quartzy_type):
                return "T1x", f"Same catalog #, no vendor named ({label})"
            return None
        if _manufacturer_link(subject, listing):
            return "T1", f"Same product, manufacturer's # ({label})"
        if same_family and subject.base and listing.catalog_base == subject.base:
            return "T2", f"Same product, other pack size ({label})"
        if (
            not same_family
            and listing.catalog_key == subject.key
            and cross_family_key_ok(subject.key, min_length=policy.min_key_length)
            and _related(subject, other_type, other_name, policy, other_quartzy_type)
        ):
            return "T1x", f"Same catalog # at another vendor ({label})"
    if product_id is not None and product_id in subject.product_ids:
        return "T2", "Same product, another listing"
    return None


def _manufacturer_link(subject, listing):
    """A distributor SKU and the manufacturer's own number for it, either way round."""
    if subject.mfr_family_id and subject.mfr_key:
        if (listing.vendor_family_id, listing.catalog_key) == (
            subject.mfr_family_id,
            subject.mfr_key,
        ):
            return True
        if (listing.mfr_family_id, listing.mfr_catalog_key) == (
            subject.mfr_family_id,
            subject.mfr_key,
        ):
            return True
    return (listing.mfr_family_id, listing.mfr_catalog_key) == (subject.family_id, subject.key)


def _related(subject, other_type, other_name, policy, other_quartzy_type=None):
    """T1x needs more than a shared number: the same item type (or, while types aren't
    mapped, the same Quartzy type, as the baseline compares them), or similar names."""
    if subject.item_type is not None and other_type is not None:
        if subject.item_type.pk == other_type.pk:
            return True
    if subject.quartzy_type_name and other_quartzy_type:
        if subject.quartzy_type_name == other_quartzy_type.casefold():
            return True
    return baseline.trigram_similarity(subject.name, other_name or "") >= policy.min_similarity


def _same_person(subject, user_id, alias_id):
    if subject.requester_id is not None and user_id is not None:
        return subject.requester_id == user_id
    return subject.requester_alias_id is not None and subject.requester_alias_id == alias_id


def _alias_id(order_request):
    alias = order_request.requested_by_alias_id or order_request.created_by_alias_id
    return alias


def _score(tier, kind, since, as_of):
    """Tier first (T1 > T1x > T2), then kind (stock > open > bought), then recency."""
    days = max((as_of - since).total_seconds() / 86400, 0) if since else 365
    recency = 1 / (1 + days / 30)
    tier_rank = len(TIER_ORDER) - TIER_ORDER.index(tier)
    kind_rank = len(KIND_ORDER) - KIND_ORDER.index(kind)
    return tier_rank * 100 + kind_rank * 10 + round(recency, 4)


def _stock_candidates(subject, listings, as_of, policy):
    items = active_stock(
        subject.request.lab, products=subject.product_ids, supplier_products=list(listings)
    )
    found = []
    for item in items:
        if not policy.include_same_requester and _same_person(subject, item.custodian_id, None):
            continue
        listing = listings.get(item.supplier_product_id) or item.supplier_product
        product = item.product
        tier = _tier(subject, listing, product.pk, product.item_type, product.name, policy)
        if tier is None:
            continue
        since = item.created_at
        found.append(
            Candidate(
                tier=tier[0],
                kind=STOCK,
                score=_score(tier[0], STOCK, since, as_of),
                stock_item_id=item.pk,
                order_request_id=None,
                product_id=product.pk,
                probably=item.lifecycle == "unverified",
                why=tier[1],
                since=since,
            )
        )
    return found


def _request_candidates(subject, listings, as_of, policy, *, replay):
    request = subject.request
    rows = (
        OrderRequest.objects.for_lab(request.lab)
        .live()
        .exclude(pk=request.pk)
        .filter(Q(supplier_product_id__in=list(listings)) | Q(product_id__in=subject.product_ids))
        .annotate(at=Coalesce("requested_at", "first_seen_at"))
        .filter(Q(at__lt=subject.at) | Q(at=subject.at, pk__lt=request.pk))
        .select_related(
            "supplier_product__vendor_family",
            "supplier_product__vendor",
            "product__item_type",
            "quartzy_type__item_type",
        )
    )
    rows = list(rows)
    states = states_at(rows, subject.at) if replay else {}
    stocked = _stocked_requests(request.lab, rows) if not replay else set()
    lookback = _lookback_start(subject, as_of)
    found = []
    for row in rows:
        if row.pk in stocked:
            continue  # its stock is a candidate of its own
        user_id, alias_id = row.requested_by_id, _alias_id(row)
        if not policy.include_same_requester and _same_person(subject, user_id, alias_id):
            continue
        status = states.get(row.pk, row.status) if replay else row.status
        if status is None or status == OrderRequest.Status.CANCELLED:
            continue
        since = row.at
        if status == OrderRequest.Status.RECEIVED:
            if lookback is None or since < lookback:
                continue
            kind, probably = BOUGHT, False
        else:
            kind = OPEN
            probably = (as_of - since) > timedelta(days=policy.open_probably_arrived_days)
        listing = listings.get(row.supplier_product_id) or row.supplier_product
        name = row.item_name or (row.product.name if row.product_id else "")
        qtype = row.quartzy_type.name if row.quartzy_type_id else None
        tier = _tier(subject, listing, row.product_id, item_type_of(row), name, policy, qtype)
        if tier is None:
            continue
        found.append(
            Candidate(
                tier=tier[0],
                kind=kind,
                score=_score(tier[0], kind, since, as_of),
                stock_item_id=None,
                order_request_id=row.pk,
                product_id=row.product_id,
                probably=probably,
                why=tier[1],
                since=since,
            )
        )
    return found


def _lookback_start(subject, as_of):
    """The start of the "bought before" window: the item type's look-back (plan §9:
    24 months for durable tier-1 types; 0 = only open requests count), None for none."""
    months = subject.item_type.lookback_months if subject.item_type else 0
    if not months:
        return None
    return months_before(as_of, months)


def _stocked_requests(lab, rows):
    """Which of these requests have stock that is still active (it is matched as stock)."""
    from apps.inventory.models import StockItem

    ids = [row.pk for row in rows]
    if not ids:
        return set()
    return set(
        StockItem.objects.for_lab(lab)
        .active()
        .filter(source_order_request_id__in=ids)
        .values_list("source_order_request_id", flat=True)
    )


def states_at(rows, moment):
    """Each request's status at ``moment`` for a replay: the last status observation the
    polling made by then; without one, its current status, with a RECEIVED one counted as
    received only if it was requested REPLAY_RECEIVED_AFTER before ``moment``, and a
    CANCELLED one left out (None), as the baseline does."""
    observed = {}
    observations = (
        OrderStatusObservation.objects.filter(
            order_request_id__in=[row.pk for row in rows], observed_at__lte=moment
        )
        .exclude(via=OrderStatusObservation.Via.BACKFILL)
        .order_by("order_request_id", "observed_at", "pk")
    )
    for observation in observations:
        observed[observation.order_request_id] = observation.to_status
    states = {}
    for row in rows:
        if row.pk in observed:
            states[row.pk] = observed[row.pk]
        elif row.status == OrderRequest.Status.CANCELLED:
            states[row.pk] = None
        elif row.status == OrderRequest.Status.RECEIVED:
            received = moment - row.at >= REPLAY_RECEIVED_AFTER
            states[row.pk] = row.status if received else OrderRequest.Status.ORDERED
        else:
            states[row.pk] = row.status
    return states
