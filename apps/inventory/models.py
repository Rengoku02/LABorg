"""Locations, stock items and the stock ledger (plan §7 "Inventory"; docs/design/mvp-a.md §1).

The app is the lab's record of physical stock (plan §2). Where an item is, who holds it and
what state it is in (its *state fields*) change only through ``services.apply_event``, which
appends a ``StockEvent`` and updates the cached fields in one transaction, so folding an
item's events in ``seq`` order gives back its state. ``StockItem.save()`` and the stock
querysets refuse every other write to those fields, and the database refuses to change or
delete an event (migration 0002).

Locations are one treebeard tree for all labs; each lab's rooms are roots. Every person also
gets an "unsorted" container per temperature, a root of its own, for stock that has no place
yet ("place later").
"""

import re
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from functools import cache

from django.conf import settings
from django.db import IntegrityError, models, transaction
from django.db.models import Q
from django.utils import timezone
from treebeard.mp_tree import MP_Node, MP_NodeManager, MP_NodeQuerySet

from apps.catalog.models import Seeded

from . import shortid

PATH_SEPARATOR = " / "
_FRESH_CODE_ATTEMPTS = 10


class LedgerError(Exception):
    """Stock state written outside ``services.apply_event``, or a stock event changed or
    deleted. Always a programming error, never something a person did."""


class Temperature(models.TextChoices):
    ROOM = "RT", "RT"
    FRIDGE = "4C", "4 °C"
    FREEZER = "-20C", "-20 °C"
    DEEP_FREEZER = "-80C", "-80 °C"
    LN2 = "LN2", "LN2"


_TEMPERATURE_ALIASES = {
    "RT": "RT",
    "ROOMTEMP": "RT",
    "ROOMTEMPERATURE": "RT",
    "AMBIENT": "RT",
    "4": "4C",
    "+4": "4C",
    "4C": "4C",
    "+4C": "4C",
    "-20": "-20C",
    "-20C": "-20C",
    "-80": "-80C",
    "-80C": "-80C",
    "LN2": "LN2",
    "LIQUIDNITROGEN": "LN2",
}


def parse_temperature(text):
    """The ``Temperature`` value for a temperature as typed ("-20 °C", "rt", "ln2"); "" for a
    blank. Raises ValueError for anything else."""
    raw = str(text or "").strip()
    if not raw:
        return ""
    compact = re.sub(r"[\s°º]", "", raw.upper().replace("\u2212", "-"))
    if compact not in _TEMPERATURE_ALIASES:
        raise ValueError(f"Unknown temperature {raw!r}; use RT, 4C, -20C, -80C or LN2.")
    return _TEMPERATURE_ALIASES[compact]


def temperature_label(value):
    return Temperature(value).label if value else ""


class Visibility(models.TextChoices):
    """Who sees what (docs/design/mvp-a.md §2); only ``presenters`` applies it."""

    SHARED = "shared", "Shared"
    ASK_FIRST = "ask_first", "Ask first"
    # Others see that it exists, its product and whom to ask, never where it is.
    RESTRICTED = "restricted", "Restricted"


def _save_with_fresh_code(obj, field, generate, save):
    """Run ``save`` (an insert) with a random code in ``obj.<field>``, drawing a new code when
    another row has it, even one inserted between the check and the insert."""
    taken = type(obj)._base_manager.filter
    for _attempt in range(_FRESH_CODE_ATTEMPTS):
        code = generate()
        if taken(**{field: code}).exists():
            continue
        setattr(obj, field, code)
        try:
            with transaction.atomic():
                save()
        except IntegrityError:
            if taken(**{field: code}).exists():
                continue
            raise
        return
    raise IntegrityError(f"No free {type(obj).__name__}.{field} after {_FRESH_CODE_ATTEMPTS} tries")


# ---------------------------------------------------------------------------------------
# Locations


class LocationType(Seeded):
    """A kind of place (seeded from data/location_types.yaml by ``manage.py seed_inventory``).

    Structural ones (rooms, freezers, shelves, racks) hold other locations; the others (boxes,
    bags) hold stock, in positions when they have a scheme.
    """

    class PositionScheme(models.TextChoices):
        GRID = "grid", "Grid (A1, A2 … row by row)"
        LINEAR = "linear", "Numbered (1, 2, 3 …)"
        NONE = "none", "No positions"

    key = models.SlugField(max_length=40, unique=True)
    label = models.CharField(max_length=100)
    structural = models.BooleanField(default=True)
    position_scheme = models.CharField(
        max_length=10, choices=PositionScheme.choices, default=PositionScheme.NONE
    )
    rows = models.PositiveSmallIntegerField(null=True, blank=True)
    cols = models.PositiveSmallIntegerField(null=True, blank=True)
    # A linear scheme labels its positions prefix + number ("S1", "S2"...).
    prefix = models.CharField(max_length=10, blank=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "key"]
        constraints = [
            models.CheckConstraint(
                condition=~Q(position_scheme="grid") | Q(rows__gte=1, rows__lte=26, cols__gte=1),
                name="inventory_locationtype_grid_has_size",
            ),
        ]

    def __str__(self):
        return self.label

    @property
    def capacity(self):
        """How many positions there are; None when unlimited or there are none."""
        if self.position_scheme == self.PositionScheme.GRID:
            return self.rows * self.cols
        if self.position_scheme == self.PositionScheme.LINEAR:
            return self.cols or self.rows or None
        return None

    def position_index(self, label):
        """The 1-based position (row by row) a label such as "B3" names, or None when this
        type has no such position: free text ("top shelf, left"), kept as it is so old sheets
        import, or a label written like a position that isn't one here ("J1" in a 9 x 9 box),
        which the services refuse (``looks_like_position``)."""
        parsed = self._parse_position(label)
        if parsed is None:
            return None
        if self.position_scheme == self.PositionScheme.GRID:
            row, col = parsed
            if row < self.rows and 0 <= col < self.cols:
                return row * self.cols + col + 1
            return None
        index, capacity = parsed, self.capacity
        if index >= 1 and (capacity is None or index <= capacity):
            return index
        return None

    def looks_like_position(self, label):
        """Is ``label`` written the way this scheme writes positions (a row letter and a
        number in a grid, the prefix and a number in a linear scheme), whether or not this
        type has that position?"""
        return self._parse_position(label) is not None

    def _parse_position(self, label):
        """``(row, col)`` (0-based) of a grid label, or the number of a linear one, as written
        and not checked against the size; None when it isn't written like a position."""
        text = re.sub(r"\s+", "", str(label or "")).upper()
        if self.position_scheme == self.PositionScheme.GRID:
            match = re.fullmatch(r"([A-Z])-?(\d{1,3})", text)
            return (ord(match[1]) - ord("A"), int(match[2]) - 1) if match else None
        if self.position_scheme == self.PositionScheme.LINEAR:
            prefix = self.prefix.upper()
            number = text.removeprefix(prefix) if text.startswith(prefix) else ""
            return int(number) if re.fullmatch(r"\d{1,6}", number) else None
        return None

    def position_label(self, index):
        """The label of 1-based position ``index`` ("A1" for 1 in a grid), or ""."""
        capacity = self.capacity
        if not index or index < 1 or (capacity is not None and index > capacity):
            return ""
        if self.position_scheme == self.PositionScheme.GRID:
            row, col = divmod(index - 1, self.cols)
            return f"{chr(ord('A') + row)}{col + 1}"
        if self.position_scheme == self.PositionScheme.LINEAR:
            return f"{self.prefix}{index}"
        return ""


class LocationQuerySet(MP_NodeQuerySet):
    def for_lab(self, lab):
        return self.filter(lab=lab)

    def active(self):
        return self.filter(active=True)

    def physical(self):
        """Real places: everything but the per-person unsorted containers."""
        return self.filter(is_unsorted=False)


class LocationManager(MP_NodeManager.from_queryset(LocationQuerySet)):
    def get_queryset(self):
        return LocationQuerySet(self.model, using=self._db).order_by("path")


class Location(MP_Node):
    """A room, freezer, shelf, rack, box or bag; or a person's unsorted container.

    Add and move locations through ``services.create_location`` / ``move_location``, never
    with treebeard directly: they check the lab, refuse cycles and record a LocationEvent.
    ``code`` is printed on labels: a hand-made one ("F80A") or 5 generated Crockford
    characters. Codes are unique across all labs and stored in upper case, so they are
    unique case-insensitively.
    """

    lab = models.ForeignKey("core.Lab", on_delete=models.CASCADE, related_name="locations")
    name = models.CharField(max_length=200)
    code = models.CharField(max_length=shortid.LOCATION_CODE_MAX_LENGTH, unique=True, blank=True)
    location_type = models.ForeignKey(
        LocationType, on_delete=models.PROTECT, related_name="locations"
    )
    # Blank: the same as the enclosing location (effective_temperature).
    temperature = models.CharField(max_length=5, choices=Temperature.choices, blank=True)
    # Whose stock goes here by default; used only at put-away. A box's owner.
    default_custodian = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="default_custody_locations",
    )
    is_unsorted = models.BooleanField(default=False, editable=False)
    unsorted_owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        editable=False,
        related_name="unsorted_locations",
    )
    # For boxes: a restricted box hides where everything in it is from people other than
    # its custodian (presenters). Structural locations and unsorted containers are shared.
    visibility = models.CharField(
        max_length=12, choices=Visibility.choices, default=Visibility.SHARED
    )
    # ItemType keys, so a registered box is searchable before it's itemized.
    tags = models.JSONField(default=list, blank=True)
    photo = models.FileField(upload_to="location-photos/%Y/%m/", blank=True)
    notes = models.TextField(blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    objects = LocationManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["lab", "unsorted_owner", "temperature"],
                condition=Q(is_unsorted=True),
                name="inventory_location_one_unsorted_per_owner_temperature",
            ),
            models.CheckConstraint(
                condition=Q(is_unsorted=False, unsorted_owner__isnull=True)
                | Q(is_unsorted=True, unsorted_owner__isnull=False),
                name="inventory_location_unsorted_has_owner",
            ),
            models.CheckConstraint(
                condition=Q(code__regex=shortid.LOCATION_CODE_PATTERN),
                name="inventory_location_code_format",
            ),
        ]

    def __str__(self):
        return self.display_name

    def save(self, *args, **kwargs):
        self.code = (self.code or "").strip().upper()
        if self.code:
            problem = shortid.location_code_problem(self.code)
            if problem:
                raise ValueError(problem)
            super().save(*args, **kwargs)
            return
        _save_with_fresh_code(
            self,
            "code",
            shortid.new_location_code,
            lambda: super(Location, self).save(*args, **kwargs),
        )

    @property
    def display_name(self):
        """The name people see: "Box 4", or "Unsorted: Priya Patel, -20 °C"."""
        if not self.is_unsorted:
            return self.name
        temperature = temperature_label(self.temperature)
        owner = self.unsorted_owner
        return f"Unsorted: {owner}, {temperature}" if temperature else f"Unsorted: {owner}"

    @property
    def custodian_id(self):
        """Whose location this is: an unsorted container's owner, else the default custodian."""
        return self.unsorted_owner_id if self.is_unsorted else self.default_custodian_id

    def ancestors(self):
        """The locations around this one, outermost first."""
        return list(Location.objects.get_ancestors(self))

    def path_text(self):
        """ "Room 210 / -80 A / Shelf 1 / Box 4"; an unsorted container's display name."""
        return PATH_SEPARATOR.join(loc.display_name for loc in [*self.ancestors(), self])

    def effective_temperature(self):
        """This location's temperature, else the nearest enclosing one's; "" if none says."""
        if self.temperature:
            return self.temperature
        for location in reversed(self.ancestors()):
            if location.temperature:
                return location.temperature
        return ""


class LocationEvent(models.Model):
    """A location moved, with its contents, to another parent (None: the top level)."""

    lab = models.ForeignKey("core.Lab", on_delete=models.CASCADE, related_name="location_events")
    location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="events")
    from_parent = models.ForeignKey(
        Location, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    to_parent = models.ForeignKey(
        Location, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="location_events",
    )
    at = models.DateTimeField(default=timezone.now)
    reason = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["at", "pk"]

    def __str__(self):
        return f"{self.location} moved {self.at:%Y-%m-%d}"


# ---------------------------------------------------------------------------------------
# Stock


class Source(models.TextChoices):
    PURCHASED = "purchased", "Purchased"
    PREVIOUS_LAB = "previous_lab", "From a previous lab"
    COLLABORATOR = "collaborator", "From a collaborator"
    LAB_MADE = "lab_made", "Made in the lab"
    GIFT = "gift", "Gift"
    OUTSIDE_PURCHASE = "outside_purchase", "Bought outside Quartzy"
    QUARTZY_INVENTORY = "quartzy_inventory", "Quartzy inventory"
    IMPORT = "import", "Imported"


class Lifecycle(models.TextChoices):
    # Probably there (imported, from Quartzy inventory), but nobody has looked yet.
    UNVERIFIED = "unverified", "Unverified"
    AVAILABLE = "available", "Available"
    OPENED = "opened", "Opened"
    EMPTY = "empty", "Empty"
    DISCARDED = "discarded", "Discarded"
    LOST = "lost", "Lost"


ACTIVE_LIFECYCLES = (Lifecycle.UNVERIFIED, Lifecycle.AVAILABLE, Lifecycle.OPENED)


class Level(models.TextChoices):
    OK = "ok", "OK"
    LOW = "low", "Running low"


class ExpiryPrecision(models.TextChoices):
    DAY = "day", "Day"
    MONTH = "month", "Month"  # labels often print only "2027-03"
    YEAR = "year", "Year"


# Changed only by services.apply_event; a StockEvent records every change.
STATE_FIELDS = (
    "custodian",
    "visibility",
    "location",
    "position_label",
    "position_index",
    "lifecycle",
    "level",
    "quantity",
    "unit",
    "opened_at",
    "container_count",
)
# Written with the state fields, by the ledger only: version == the number of events.
LEDGER_FIELDS = (*STATE_FIELDS, "version")
# Never change once the item exists: MTA stock stays restricted, and only aggregates have a
# container count, so neither flag may be dropped or set behind the ledger's back.
FROZEN_FIELDS = ("lab", "short_id", "mta", "is_aggregate")
_QUANTUM = Decimal("0.0001")  # the quantity columns: max_digits=14, decimal_places=4


def round_quantity(value):
    """``value`` rounded as Postgres rounds it into the quantity columns (half away from
    zero), so the cached quantity and the one its event records are the same number."""
    return value.quantize(_QUANTUM, rounding=ROUND_HALF_UP)


def ledger_value(value):
    """A state value as ``StockEvent.data["state"]`` stores it: ids for people and places,
    strings for decimals, datetimes and dates, so an event compares equal to the item it
    made (the services turn a date given for a datetime field into a datetime first)."""
    if isinstance(value, models.Model):
        return value.pk
    if isinstance(value, Decimal):
        return str(round_quantity(value))
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


@cache
def _attnames(names):
    return tuple((name, StockItem._meta.get_field(name).attname) for name in names)


@cache
def _guarded_names():
    """Every spelling (name and attname) of a field only the ledger may write."""
    pairs = _attnames((*LEDGER_FIELDS, *FROZEN_FIELDS))
    return frozenset(spelling for pair in pairs for spelling in pair)


def _refuse_ledger_writes(fields, how):
    hit = sorted(set(fields) & _guarded_names())
    if hit:
        raise LedgerError(
            f"{', '.join(hit)} can't be written with {how}: stock state changes only through "
            f"apps.inventory.services.apply_event, and {', '.join(FROZEN_FIELDS)} never change."
        )


class StockItemQuerySet(models.QuerySet):
    def for_lab(self, lab):
        return self.filter(lab=lab)

    def active(self):
        """Unverified, available or opened."""
        return self.filter(lifecycle__in=ACTIVE_LIFECYCLES)

    def update(self, **kwargs):
        """QuerySet.update for anything but state fields (merges repoint products and lots)."""
        _refuse_ledger_writes(kwargs, "QuerySet.update()")
        return super().update(**kwargs)

    update.alters_data = True

    def bulk_update(self, objs, fields, batch_size=None):
        _refuse_ledger_writes(fields, "bulk_update()")
        return super().bulk_update(objs, fields, batch_size=batch_size)

    bulk_update.alters_data = True

    def bulk_create(self, objs, *args, **kwargs):
        raise LedgerError(
            "Create stock items with apps.inventory.services.create_stock_item, so each one "
            "gets its 'created' event."
        )

    bulk_create.alters_data = True


class StockItem(models.Model):
    """One container (tube, vial, bottle, kit), or an aggregate of N not yet split.

    State fields (``STATE_FIELDS``) are a cache of the item's StockEvents. ``save()`` raises
    ``LedgerError`` when one of them, ``version`` or a field that never changes
    (``FROZEN_FIELDS``: ``lab``, ``short_id``, ``mta``, ``is_aggregate``) changed since the
    item was loaded, and ``StockItem.objects`` refuses them in ``update()`` and
    ``bulk_update()``. The one internal path that writes state is ``_save_from_ledger()``,
    called only by ``services.apply_event`` and ``services.create_stock_item``.
    """

    Lifecycle = Lifecycle
    Level = Level
    Source = Source
    ExpiryPrecision = ExpiryPrecision

    short_id = models.CharField(
        max_length=shortid.SHORT_ID_LENGTH, unique=True, editable=False, blank=True
    )
    lab = models.ForeignKey("core.Lab", on_delete=models.PROTECT, related_name="stock_items")
    product = models.ForeignKey(
        "catalog.Product", on_delete=models.PROTECT, related_name="stock_items"
    )
    supplier_product = models.ForeignKey(
        "catalog.SupplierProduct",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="stock_items",
    )
    lot = models.ForeignKey(
        "catalog.Lot", on_delete=models.PROTECT, null=True, blank=True, related_name="stock_items"
    )
    # Aliquots in v1: the item this one was split from.
    parent = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="aliquots"
    )

    # State fields: changed only by services.apply_event.
    custodian = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="stock_held"
    )
    visibility = models.CharField(max_length=12, choices=Visibility.choices)
    location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="stock_items")
    position_label = models.CharField(max_length=20, blank=True)
    position_index = models.PositiveIntegerField(null=True, blank=True)
    lifecycle = models.CharField(max_length=12, choices=Lifecycle.choices)
    level = models.CharField(max_length=5, choices=Level.choices, default=Level.OK)
    quantity = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    unit = models.CharField(max_length=20, blank=True)
    opened_at = models.DateTimeField(null=True, blank=True)
    # Only for aggregates: how many containers it stands for.
    container_count = models.PositiveIntegerField(null=True, blank=True)

    expiry_date = models.DateField(null=True, blank=True)
    expiry_precision = models.CharField(
        max_length=5, choices=ExpiryPrecision.choices, default=ExpiryPrecision.DAY
    )
    attributes = models.JSONField(default=dict, blank=True)
    source = models.CharField(max_length=20, choices=Source.choices)
    source_order_request = models.ForeignKey(
        "quartzy.OrderRequest",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stock_items",
    )
    # Received under a material transfer agreement: always restricted (a check constraint).
    # Fixed at creation, as is_aggregate (FROZEN_FIELDS).
    mta = models.BooleanField("MTA", default=False)
    # "N containers, split later" (container_count says how many).
    is_aggregate = models.BooleanField(default=False)
    notes = models.TextField(blank=True)
    version = models.PositiveIntegerField(default=0, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    objects = StockItemQuerySet.as_manager()

    class Meta:
        # Related-object access, refresh_from_db and deletion use the guarded queryset too.
        base_manager_name = "objects"
        constraints = [
            models.CheckConstraint(
                condition=Q(short_id__regex=shortid.SHORT_ID_PATTERN),
                name="inventory_stockitem_short_id_crockford",
            ),
            models.CheckConstraint(
                condition=Q(mta=False) | Q(visibility=Visibility.RESTRICTED),
                name="inventory_stockitem_mta_is_restricted",
            ),
            models.CheckConstraint(
                condition=Q(is_aggregate=True) | Q(container_count__isnull=True),
                name="inventory_stockitem_count_only_for_aggregates",
            ),
        ]
        indexes = [
            models.Index(fields=["lab", "lifecycle"], name="inventory_item_lab_lifecycle"),
        ]

    _ledger_writing = False
    _ledger_snapshot = None

    def __str__(self):
        return f"{self.short_id} {self.product}" if self.short_id else str(self.product)

    def save(self, *args, **kwargs):
        if self._state.adding and not self._ledger_writing:
            raise LedgerError(
                "Create stock items with apps.inventory.services.create_stock_item, so each "
                "one gets its 'created' event."
            )
        if not self._state.adding:
            names = FROZEN_FIELDS if self._ledger_writing else (*LEDGER_FIELDS, *FROZEN_FIELDS)
            changed = self.changed_ledger_fields(names)
            if changed:
                raise LedgerError(
                    f"{', '.join(changed)} changed outside apps.inventory.services.apply_event "
                    f"(stock item {self.short_id}); {', '.join(FROZEN_FIELDS)} never change."
                )
            if not self._ledger_writing:
                # Never write the guarded fields here, so a copy loaded before the last event
                # can't put its old state back while saving, say, the notes.
                kwargs["update_fields"] = self._unguarded_fields(kwargs.get("update_fields"))
        if self._state.adding and not self.short_id:
            _save_with_fresh_code(
                self,
                "short_id",
                shortid.new_short_id,
                lambda: super(StockItem, self).save(*args, **kwargs),
            )
        else:
            super().save(*args, **kwargs)
        self._take_snapshot()

    @classmethod
    def from_db(cls, db, field_names, values, **kwargs):
        instance = super().from_db(db, field_names, values, **kwargs)
        instance._take_snapshot()
        return instance

    def refresh_from_db(self, using=None, fields=None, from_queryset=None):
        super().refresh_from_db(using=using, fields=fields, from_queryset=from_queryset)
        self._take_snapshot(fields)

    def _take_snapshot(self, fields=None):
        """Remember the loaded values of the guarded fields (``fields``: only these)."""
        snapshot = dict(self._ledger_snapshot or {})
        for name, attname in _attnames((*LEDGER_FIELDS, *FROZEN_FIELDS)):
            if fields is not None and name not in fields and attname not in fields:
                continue
            if attname in self.__dict__:
                snapshot[attname] = self.__dict__[attname]
        self._ledger_snapshot = snapshot

    def changed_ledger_fields(self, names=(*LEDGER_FIELDS, *FROZEN_FIELDS)):
        """Guarded fields whose value differs from the one loaded (or last saved)."""
        snapshot = self._ledger_snapshot or {}
        # Deferred fields that were never set are not compared.
        pairs = [pair for pair in _attnames(tuple(names)) if pair[1] in self.__dict__]
        unseen = [attname for _, attname in pairs if attname not in snapshot]
        if unseen and self.pk is not None:
            # Loaded after the fact without refresh_from_db (Django's FETCH_PEERS mode).
            stored = type(self)._base_manager.filter(pk=self.pk).values(*unseen).first()
            snapshot = {**snapshot, **(stored or {})}
        return [
            name
            for name, attname in pairs
            if attname not in snapshot or snapshot[attname] != self.__dict__[attname]
        ]

    def _unguarded_fields(self, update_fields):
        """``update_fields`` (None: every loaded field) without the ledger's fields."""
        if update_fields is None:
            deferred = self.get_deferred_fields()
            update_fields = [
                field.name
                for field in self._meta.concrete_fields
                if not field.primary_key and field.attname not in deferred
            ]
        return [name for name in update_fields if name not in _guarded_names()]

    def _save_from_ledger(self, **kwargs):
        """The one internal path that writes state fields. ``services.apply_event`` and
        ``services.create_stock_item`` call it inside their transaction; nothing else may."""
        self._ledger_writing = True
        try:
            self.save(**kwargs)
        finally:
            self._ledger_writing = False

    def ledger_state(self):
        """The state fields as events record them (``ledger_value``), keyed by field name."""
        return {
            name: ledger_value(getattr(self, attname)) for name, attname in _attnames(STATE_FIELDS)
        }

    @property
    def is_active(self):
        return self.lifecycle in ACTIVE_LIFECYCLES


# Retention pseudonymizes the person who acted; nothing else about an event ever changes.
_EVENT_UPDATABLE = frozenset({"user", "user_id"})


def _refuse_event_writes(fields, how):
    hit = sorted(set(fields) - _EVENT_UPDATABLE)
    if hit:
        raise LedgerError(
            f"Stock events are append-only: {', '.join(hit)} can't be written with {how} "
            "(only user, which retention pseudonymizes)."
        )


class StockEventQuerySet(models.QuerySet):
    def for_lab(self, lab):
        return self.filter(lab=lab)

    def update(self, **kwargs):
        _refuse_event_writes(kwargs, "QuerySet.update()")
        return super().update(**kwargs)

    update.alters_data = True

    def bulk_update(self, objs, fields, batch_size=None):
        _refuse_event_writes(fields, "bulk_update()")
        return super().bulk_update(objs, fields, batch_size=batch_size)

    bulk_update.alters_data = True

    def bulk_create(self, objs, *args, **kwargs):
        raise LedgerError("Append stock events with apps.inventory.services.apply_event.")

    bulk_create.alters_data = True

    def delete(self):
        raise LedgerError("Stock events are append-only; they are never deleted.")

    delete.alters_data = True
    delete.queryset_only = True


class StockEvent(models.Model):
    """One change to one stock item; ``seq`` 1 is its creation.

    Append-only: ``services.apply_event`` adds rows, nothing updates or deletes them except
    retention pseudonymizing ``user`` (also enforced by a database trigger). ``data["state"]``
    holds the new value of every state field the event changed (``ledger_value``), so the
    events folded in ``seq`` order reproduce the item's cached state.
    """

    class Kind(models.TextChoices):
        CREATED = "created", "Created"
        VERIFIED = "verified", "Verified"
        MOVED = "moved", "Moved"
        OPENED = "opened", "Opened"
        MARKED_LOW = "marked_low", "Marked low"
        MARKED_OK = "marked_ok", "Marked OK"
        EMPTIED = "emptied", "Emptied"
        DISCARDED = "discarded", "Discarded"
        LOST = "lost", "Lost"
        FOUND = "found", "Found"
        CUSTODY_CHANGED = "custody_changed", "Custody changed"
        VISIBILITY_CHANGED = "visibility_changed", "Visibility changed"
        QUANTITY_ADJUSTED = "quantity_adjusted", "Quantity adjusted"
        SPLIT = "split", "Split"
        NOTE = "note", "Note"

    class Source(models.TextChoices):
        WEB = "web", "Web"
        BENCH = "bench", "Bench"
        EMAIL = "email", "Email"
        IMPORT = "import", "Import"
        SYNC = "sync", "Quartzy sync"
        ADMIN = "admin", "Admin"
        CLAIM = "claim", "Claim"
        SYSTEM = "system", "System"

    lab = models.ForeignKey("core.Lab", on_delete=models.PROTECT, related_name="stock_events")
    stock_item = models.ForeignKey(StockItem, on_delete=models.PROTECT, related_name="events")
    seq = models.PositiveIntegerField()
    kind = models.CharField(max_length=20, choices=Kind.choices)
    qty_delta = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    from_location = models.ForeignKey(
        Location, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    to_location = models.ForeignKey(
        Location, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    from_custodian = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    to_custodian = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    reason = models.CharField(max_length=500, blank=True)
    # Who recorded it; None for the sync, imports and the system.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stock_events",
    )
    occurred_at = models.DateTimeField(default=timezone.now)  # may be backdated
    recorded_at = models.DateTimeField(auto_now_add=True)
    client_uuid = models.UUIDField(null=True, blank=True, unique=True)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.WEB)
    data = models.JSONField(default=dict, blank=True)

    objects = StockEventQuerySet.as_manager()

    class Meta:
        base_manager_name = "objects"
        ordering = ["stock_item", "seq"]
        constraints = [
            models.UniqueConstraint(
                fields=["stock_item", "seq"], name="inventory_stockevent_unique_item_seq"
            ),
            models.CheckConstraint(condition=Q(seq__gte=1), name="inventory_stockevent_seq_1_up"),
        ]

    _ledger_writing = False

    def __str__(self):
        return f"{self.stock_item_id}#{self.seq} {self.kind}"

    def save(self, *args, **kwargs):
        if self._state.adding:
            if not self._ledger_writing:
                raise LedgerError("Append stock events with apps.inventory.services.apply_event.")
        else:
            _refuse_event_writes(kwargs.get("update_fields") or ["every field"], "save()")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise LedgerError("Stock events are append-only; they are never deleted.")

    def _append_from_ledger(self):
        """Insert this event; only ``services`` calls it, inside apply_event's transaction."""
        self._ledger_writing = True
        try:
            self.save(force_insert=True)
        finally:
            self._ledger_writing = False

    @property
    def state(self):
        """The state fields this event set, as ``ledger_value``s."""
        return (self.data or {}).get("state", {})


class ContainerCode(models.Model):
    """A barcode or QR code stuck on one container (scanned from MVP-B)."""

    class Kind(models.TextChoices):
        BARCODE = "barcode", "Barcode"
        QR = "qr", "QR code"
        GS1 = "gs1", "GS1"

    code = models.CharField(max_length=200, unique=True)
    stock_item = models.ForeignKey(
        StockItem, on_delete=models.CASCADE, related_name="container_codes"
    )
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.BARCODE)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.code

    def save(self, *args, **kwargs):
        self.code = self.code.strip()
        super().save(*args, **kwargs)


class ExternalRef(models.Model):
    """The record a stock item came from in another system. One Quartzy instance may stand
    for several stock items (a quantity of 4 becomes 4 containers)."""

    class System(models.TextChoices):
        QUARTZY_ITEM = "quartzy_item", "Quartzy inventory item"
        QUARTZY_INSTANCE = "quartzy_instance", "Quartzy inventory instance"

    stock_item = models.ForeignKey(
        StockItem, on_delete=models.CASCADE, related_name="external_refs"
    )
    system = models.CharField(max_length=20, choices=System.choices)
    external_id = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "external reference"
        constraints = [
            models.UniqueConstraint(
                fields=["stock_item", "system", "external_id"],
                name="inventory_externalref_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["system", "external_id"], name="inventory_externalref_lookup")
        ]

    def __str__(self):
        return f"{self.get_system_display()} {self.external_id}"


class HistoryClaimQuerySet(models.QuerySet):
    def for_lab(self, lab):
        return self.filter(lab=lab)


class HistoryClaim(models.Model):
    """ "Claim your history": what a person says became of one past request of theirs
    (docs/design/mvp-a.md §4). The claims flow fills it; one row per request and person."""

    class Outcome(models.TextChoices):
        PENDING = "pending", "Not answered yet"
        STILL_HAVE = "still_have", "Still have it"
        USED_UP = "used_up", "Used up"
        GAVE_AWAY = "gave_away", "Gave it to someone"
        SHARED_STOCK = "shared_stock", "In the shared stock"
        NOT_MINE = "not_mine", "Not mine"
        NOT_ARRIVED = "not_arrived", "Did this arrive?"
        SKIPPED = "skipped", "Skipped"

    lab = models.ForeignKey("core.Lab", on_delete=models.CASCADE, related_name="history_claims")
    order_request = models.ForeignKey(
        "quartzy.OrderRequest", on_delete=models.CASCADE, related_name="history_claims"
    )
    person = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="history_claims"
    )
    round = models.PositiveIntegerField(default=1)
    outcome = models.CharField(max_length=20, choices=Outcome.choices, default=Outcome.PENDING)
    count_kept = models.PositiveIntegerField(null=True, blank=True)
    # "Gave it to X".
    target_person = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    stock_items = models.ManyToManyField(StockItem, blank=True, related_name="history_claims")
    surfaced_at = models.DateTimeField(default=timezone.now)
    decided_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    objects = HistoryClaimQuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["order_request", "person"], name="inventory_historyclaim_unique"
            ),
        ]

    def __str__(self):
        return f"{self.person} · {self.order_request} ({self.get_outcome_display()})"
