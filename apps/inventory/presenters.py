"""How stock looks to people: the only place visibility is decided (docs/design/mvp-a.md §2).

- ``present_item(item, viewer, *, channel)`` -> ItemView, or None when the viewer may not see it
- ``present_items(items, viewer, *, channel, listed_in=None)`` -> [ItemView, ...] (the ones
  the viewer may see)
- ``present_location(location, viewer, *, channel)`` -> LocationView or None
- ``present_locations(locations, viewer, *, channel, listed_in=None)`` -> [LocationView, ...]

Pages, emails, digests, CSVs and htmx partials render these views, never stock rows, and
templates bind them to names ending in ``_view`` (tests/inventory/test_templates.py). The
channel changes formatting only (emails, digests and CSVs get absolute links), never what is
shown.

Who sees what:
- the item's custodian, co-owners and system admins: everything;
- other current members of the lab, the PI included: everything about shared and ask-first
  items ("Ask X" for ask-first). Of a restricted item, or of anything inside a restricted
  location they aren't the custodian of, only that it exists, its product, its state and
  whom to ask: no location, position, temperature, lot, expiry, quantity or notes;
- anyone else (leavers, ended memberships, other labs, anonymous): nothing (None).

A list of what is in one location (a box's page, a freezer's with what is inside it, a
shelf's boxes) passes ``listed_in=`` that location. It then leaves out the rows that would be
redacted, with no count either: the page is itself the place those rows must not be tied to.
Lists that aren't about a place (search results, the guard's matches, what someone holds) show
redacted rows as they are: that stock exists, and whom to ask.

Whom to ask: the custodian; for lab stock (the pool user) and for stock of someone who has
left the lab, the product's steward (LabProduct.steward), else the lab's co-owner.
"""

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder

from apps.catalog.models import ItemType, LabProduct
from apps.core.models import Membership, User
from apps.core.permissions import is_co_owner, is_member

from .models import Lifecycle, Visibility, temperature_label
from .selectors import ancestor_chains, chain_text

CHANNELS = ("page", "email", "digest", "csv", "partial")
# Read away from the site, so their links carry the host.
ABSOLUTE_CHANNELS = frozenset({"email", "digest", "csv"})
POOL_ASK_NAME = "the lab's co-owner"


class _View:
    def as_json(self):
        """The view as plain JSON (dates as ISO strings), e.g. for a match snapshot."""
        return json.loads(json.dumps(asdict(self), cls=DjangoJSONEncoder))


@dataclass(frozen=True)
class ItemView(_View):
    channel: str
    short_id: str
    url: str
    version: int  # for expected_version in a form that changes the item
    product_id: int
    product_name: str
    item_type: str
    vendor: str
    catalog_number: str
    custodian_id: int
    custodian_name: str
    custodian_is_pool: bool
    custodian_has_left: bool  # a leaver or an ended membership still holding it
    viewer_is_custodian: bool
    # Whom to ask (None and "the lab's co-owner" when no one person), and "Ask Priya Patel"
    # when the viewer should ask before using it.
    ask_user_id: int | None
    ask_name: str
    ask_text: str
    visibility: str
    visibility_label: str
    # True: where it is, its lot, expiry, quantity and notes are withheld from this viewer.
    redacted: bool
    lifecycle: str
    lifecycle_label: str
    is_active: bool
    unverified: bool
    unverified_text: str  # "Probably: ask Priya Patel"
    level: str
    level_label: str
    location_code: str
    location_name: str
    location_path: str
    location_url: str
    unplaced: bool  # in the custodian's "place later" container
    temperature: str
    temperature_label: str
    position: str
    position_index: int | None
    lot: str
    expiry_date: date | None
    expiry_precision: str
    expiry_text: str  # "2027-03-14", "2027-03" or "2027"
    quantity: Decimal | None
    unit: str
    quantity_text: str  # "250 mL"
    opened_at: datetime | None
    is_aggregate: bool
    container_count: int | None
    containers_text: str  # "4 containers, not split yet"
    source: str
    source_label: str
    mta: bool
    notes: str
    created_at: datetime


@dataclass(frozen=True)
class LocationView(_View):
    channel: str
    code: str
    url: str
    name: str  # "Box 4", "Unsorted: Priya Patel, -20 °C"
    kind: str
    kind_label: str
    structural: bool
    is_unsorted: bool
    custodian_id: int | None
    custodian_name: str
    custodian_has_left: bool
    viewer_is_custodian: bool
    ask_text: str
    visibility: str
    visibility_label: str
    # True: where it is and its notes and photo are withheld from this viewer.
    redacted: bool
    path: str  # "Room 210 / -80 A / Shelf 1 / Box 4"
    parent_path: str
    temperature: str
    temperature_label: str
    tags: tuple[str, ...]  # ItemType keys
    tag_labels: tuple[str, ...]
    position_scheme: str
    rows: int | None
    cols: int | None
    notes: str
    has_photo: bool
    active: bool


@dataclass(frozen=True)
class _Access:
    user_id: int
    privileged: bool  # co-owner or system admin of the lab


def _access(viewer, lab):
    """How much of the lab's stock ``viewer`` may see; None for nothing."""
    if viewer is None or not getattr(viewer, "is_authenticated", False):
        return None
    if is_co_owner(viewer, lab):  # includes system admins
        return _Access(viewer.pk, privileged=True)
    if is_member(viewer, lab):
        return _Access(viewer.pk, privileged=False)
    return None


class _Batch:
    """Lookups shared by the views of one call: access per lab, who has left, stewards and
    tag labels, each read once."""

    def __init__(self, viewer, channel):
        if channel not in CHANNELS:
            raise ValueError(f"Unknown channel {channel!r}; use one of {', '.join(CHANNELS)}.")
        self.viewer, self.channel = viewer, channel
        self._access, self._current, self._stewards, self._tags = {}, {}, {}, {}

    def access(self, row):
        """The viewer's access to the lab of ``row`` (a stock item or a location), worked out
        once per lab: ``row.lab`` is read only for the first row of each lab."""
        if row.lab_id not in self._access:
            self._access[row.lab_id] = _access(self.viewer, row.lab)
        return self._access[row.lab_id]

    def load_holders(self, lab_id, user_ids):
        """Read in one query which of these people are current members of the lab."""
        missing = {pk for pk in user_ids if pk is not None and (lab_id, pk) not in self._current}
        if not missing:
            return
        rows = Membership.objects.filter(lab_id=lab_id, user_id__in=missing)
        current = {row.user_id for row in rows if row.is_current()}
        self._current.update({(lab_id, pk): pk in current for pk in missing})

    def has_left(self, lab_id, person):
        """Does ``person`` (not the pool user) hold the lab's stock without being a current
        member any more: a leaver, an ended membership, a placeholder?"""
        self.load_holders(lab_id, [person.pk])
        return not self._current[(lab_id, person.pk)]

    def load_stewards(self, lab_id, product_ids):
        """Read in one query the stewards of these products in the lab (LabProduct)."""
        missing = {pk for pk in product_ids if (lab_id, pk) not in self._stewards}
        if not missing:
            return
        rows = (
            LabProduct.objects.filter(lab_id=lab_id, product_id__in=missing)
            .exclude(steward=None)
            .select_related("steward")
        )
        found = {row.product_id: row.steward for row in rows}
        self._stewards.update({(lab_id, pk): found.get(pk) for pk in missing})

    def steward(self, lab_id, product_id):
        self.load_stewards(lab_id, [product_id])
        return self._stewards[(lab_id, product_id)]

    def tag_labels(self, keys):
        missing = [key for key in keys if key not in self._tags]
        if missing:
            labels = dict(ItemType.objects.filter(key__in=missing).values_list("key", "label"))
            self._tags.update({key: labels.get(key, key) for key in missing})
        return tuple(self._tags[key] for key in keys)

    def link(self, path):
        return f"{settings.SITE_URL}{path}" if self.channel in ABSOLUTE_CHANNELS else path


def _visible(rows, batch, holder_id):
    """The rows the viewer may see, with their holders' memberships read once per lab."""
    rows = [row for row in rows if batch.access(row) is not None]
    by_lab = {}
    for row in rows:
        by_lab.setdefault(row.lab_id, set()).add(holder_id(row))
    for lab_id, user_ids in by_lab.items():
        batch.load_holders(lab_id, user_ids)
    return rows


def _restricted_for(chain, access):
    """Is any of these locations restricted, and not the viewer's own?"""
    return any(
        location.visibility == Visibility.RESTRICTED and location.custodian_id != access.user_id
        for location in chain
    )


def _temperature(chain):
    return next((location.temperature for location in reversed(chain) if location.temperature), "")


def _number(value):
    return format(value.normalize(), "f")


def _expiry_text(expiry, precision):
    if expiry is None:
        return ""
    if precision == "year":
        return f"{expiry:%Y}"
    if precision == "month":
        return f"{expiry:%Y-%m}"
    return expiry.isoformat()


def _containers_text(item):
    if not item.is_aggregate:
        return ""
    count = item.container_count or 0
    if count == 0:
        return "all split off"  # its containers are stock items of their own now
    return f"{count} container{'s' if count != 1 else ''}, not split yet"


def _listed(views, listed_in):
    """The views a list of what is in ``listed_in`` shows: a redacted row there would tell
    where it is."""
    return views if listed_in is None else [view for view in views if not view.redacted]


def present_item(item, viewer, *, channel):
    """The ItemView of ``item`` for ``viewer`` (a User, AnonymousUser or None), or None."""
    views = present_items([item], viewer, channel=channel)
    return views[0] if views else None


def present_items(items, viewer, *, channel, listed_in=None):
    """ItemViews of the items ``viewer`` may see, in order; the others are left out. Access,
    paths and memberships are looked up once for all of them (pass rows from ``selectors``,
    which select the related rows a view reads).

    ``listed_in``: the Location these items are listed as the contents of
    (``selectors.location_contents``); the rows redacted for the viewer are then left out
    too, since the list would tell where they are."""
    batch = _Batch(viewer, channel)
    items = _visible(list(items), batch, lambda item: item.custodian_id)
    _load_stewards(items, batch)
    chains = ancestor_chains({item.location_id: item.location for item in items}.values())
    views = [
        _item_view(item, batch.access(item), chains[item.location_id], batch) for item in items
    ]
    return _listed(views, listed_in)


def _load_stewards(items, batch):
    """Stewards are asked about lab stock and the stock of people who have left: read them
    for all such items at once (the memberships are loaded already)."""
    by_lab = {}
    for item in items:
        custodian = item.custodian
        if custodian.kind == User.Kind.POOL or batch.has_left(item.lab_id, custodian):
            by_lab.setdefault(item.lab_id, set()).add(item.product_id)
    for lab_id, product_ids in by_lab.items():
        batch.load_stewards(lab_id, product_ids)


def _ask(item, custodian, batch):
    """Who to ask about an item: its custodian. For lab stock, and for stock of someone who
    has left, the product's steward, else (None) the lab's co-owner."""
    if custodian.kind != User.Kind.POOL and not batch.has_left(item.lab_id, custodian):
        return custodian, str(custodian)
    steward = batch.steward(item.lab_id, item.product_id)
    return (steward, str(steward)) if steward else (None, POOL_ASK_NAME)


def _item_view(item, access, chain, batch):
    custodian = item.custodian
    is_pool = custodian.kind == User.Kind.POOL
    viewer_is_custodian = custodian.pk == access.user_id
    redacted = not (access.privileged or viewer_is_custodian) and (
        item.visibility == Visibility.RESTRICTED or _restricted_for(chain, access)
    )
    asked, ask_name = _ask(item, custodian, batch)
    viewer_is_asked = asked is not None and asked.pk == access.user_id
    unverified = item.lifecycle == Lifecycle.UNVERIFIED
    must_ask = redacted or item.visibility != Visibility.SHARED or unverified
    ask_text = f"Ask {ask_name}" if must_ask and not viewer_is_asked else ""
    unverified_text = ""
    if unverified:
        unverified_text = "Not checked yet" if viewer_is_asked else f"Probably: ask {ask_name}"
    product, supplier_product, lot = item.product, item.supplier_product, item.lot
    expiry, precision = item.expiry_date, item.expiry_precision
    if expiry is None and lot is not None and lot.expiry is not None:
        expiry, precision = lot.expiry, lot.expiry_precision
    location = item.location
    shown = not redacted
    temperature = _temperature(chain) if shown else ""
    return ItemView(
        channel=batch.channel,
        short_id=item.short_id,
        url=batch.link(f"/i/{item.short_id}"),
        version=item.version,
        product_id=product.pk,
        product_name=product.name,
        item_type=str(product.item_type) if product.item_type_id else "",
        vendor=str(supplier_product.vendor) if supplier_product else "",
        catalog_number=supplier_product.catalog_raw if supplier_product else "",
        custodian_id=custodian.pk,
        custodian_name=str(custodian),
        custodian_is_pool=is_pool,
        custodian_has_left=not is_pool and batch.has_left(item.lab_id, custodian),
        viewer_is_custodian=viewer_is_custodian,
        ask_user_id=asked.pk if asked is not None else None,
        ask_name=ask_name,
        ask_text=ask_text,
        visibility=item.visibility,
        visibility_label=item.get_visibility_display(),
        redacted=redacted,
        lifecycle=item.lifecycle,
        lifecycle_label=item.get_lifecycle_display(),
        is_active=item.is_active,
        unverified=unverified,
        unverified_text=unverified_text,
        level=item.level,
        level_label=item.get_level_display(),
        location_code=location.code if shown else "",
        location_name=location.display_name if shown else "",
        location_path=chain_text(chain) if shown else "",
        location_url=batch.link(f"/l/{location.code}") if shown else "",
        unplaced=location.is_unsorted and shown,
        temperature=temperature,
        temperature_label=temperature_label(temperature),
        position=item.position_label if shown else "",
        position_index=item.position_index if shown else None,
        lot=(lot.lot_raw or lot.lot_key) if lot and shown else "",
        expiry_date=expiry if shown else None,
        expiry_precision=precision if shown else "",
        expiry_text=_expiry_text(expiry, precision) if shown else "",
        quantity=item.quantity if shown else None,
        unit=item.unit if shown else "",
        quantity_text=(
            f"{_number(item.quantity)} {item.unit}".strip()
            if shown and item.quantity is not None
            else ""
        ),
        opened_at=item.opened_at if shown else None,
        is_aggregate=item.is_aggregate,
        # How many containers is a quantity too.
        container_count=item.container_count if shown else None,
        containers_text=_containers_text(item) if shown else "",
        source=item.source,
        source_label=item.get_source_display(),
        mta=item.mta,
        notes=item.notes if shown else "",
        created_at=item.created_at,
    )


def present_location(location, viewer, *, channel):
    """The LocationView of ``location`` for ``viewer``, or None."""
    views = present_locations([location], viewer, channel=channel)
    return views[0] if views else None


def present_locations(locations, viewer, *, channel, listed_in=None):
    """LocationViews of the locations ``viewer`` may see, in order, with access, paths and
    memberships looked up once (select_related ``location_type``, ``default_custodian`` and
    ``unsorted_owner``, as ``selectors.child_locations`` does).

    ``listed_in``: the Location these are listed as the contents of (a shelf's boxes,
    ``selectors.child_locations(shelf)``); a restricted box the viewer may not place there,
    and anything inside one, is then left out."""
    batch = _Batch(viewer, channel)
    locations = _visible(list(locations), batch, lambda location: location.custodian_id)
    chains = ancestor_chains(locations)
    views = [
        _location_view(location, batch.access(location), chains[location.pk], batch)
        for location in locations
    ]
    return _listed(views, listed_in)


def _location_view(location, access, chain, batch):
    custodian = location.unsorted_owner if location.is_unsorted else location.default_custodian
    viewer_is_custodian = custodian is not None and custodian.pk == access.user_id
    has_left = (
        custodian is not None
        and custodian.kind != User.Kind.POOL
        and batch.has_left(location.lab_id, custodian)
    )
    redacted = not (access.privileged or viewer_is_custodian) and _restricted_for(chain, access)
    must_ask = redacted or location.visibility != Visibility.SHARED
    ask_text = ""
    if custodian is not None and must_ask and not viewer_is_custodian:
        lab_stock = custodian.kind == User.Kind.POOL or has_left
        ask_text = f"Ask {POOL_ASK_NAME if lab_stock else custodian}"
    kind = location.location_type
    shown = not redacted
    temperature = _temperature(chain) if shown else ""
    tags = tuple(location.tags or ())
    return LocationView(
        channel=batch.channel,
        code=location.code,
        url=batch.link(f"/l/{location.code}"),
        name=location.display_name,
        kind=kind.key,
        kind_label=kind.label,
        structural=kind.structural,
        is_unsorted=location.is_unsorted,
        custodian_id=custodian.pk if custodian else None,
        custodian_name=str(custodian) if custodian else "",
        custodian_has_left=has_left,
        viewer_is_custodian=viewer_is_custodian,
        ask_text=ask_text,
        visibility=location.visibility,
        visibility_label=location.get_visibility_display(),
        redacted=redacted,
        path=chain_text(chain) if shown else "",
        parent_path=chain_text(chain[:-1]) if shown else "",
        temperature=temperature,
        temperature_label=temperature_label(temperature),
        tags=tags,
        tag_labels=batch.tag_labels(tags),
        position_scheme=kind.position_scheme,
        rows=kind.rows,
        cols=kind.cols,
        notes=location.notes if shown else "",
        has_photo=bool(location.photo) and shown,
        active=location.active,
    )
