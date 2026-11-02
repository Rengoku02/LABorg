"""Changing stock and locations: the only code that writes them (docs/design/mvp-a.md §2).

Stock:
- ``create_stock_item(...)`` -> StockItem, with its "created" event (seq 1)
- ``apply_event(item, kind, ...)`` -> StockEvent; the only writer of state fields
- ``fold_events(events)`` / ``ledger_drift(item)`` replay the ledger

Locations:
- ``create_location(...)``, ``move_location(...)``, ``unsorted_location(lab, owner, temperature)``
- ``clean_location_name`` / ``_code`` / ``_tags`` / ``_visibility`` and ``validate_placement``
  check one field or a placement, for forms that edit a location (the admin)
- ``lock_location_tree(lab)``: what creates and moves take first, so a lab's tree changes one
  change at a time

Every function checks tenancy: an item, its location, its custodian, the request it came from
and the item it was split from belong to one lab (products, supplier products and lots are
global). They don't decide who may act; views do that with ``apps.core.permissions``.
Refusals raise an ``InventoryError`` whose message can be shown to people; a refusal about
another lab's row doesn't name it.
"""

import hashlib
import logging
import uuid
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import IntegrityError, OperationalError, connection, transaction
from django.db.models import Max, Q
from django.utils import timezone

from apps.catalog.models import ItemType
from apps.catalog.services import canonical
from apps.core.models import Lab, Membership
from apps.core.permissions import is_member
from apps.core.policies import get_policy

from . import shortid
from .models import (
    LEDGER_FIELDS,
    STATE_FIELDS,
    ExpiryPrecision,
    Level,
    Lifecycle,
    Location,
    LocationEvent,
    LocationType,
    Source,
    StockEvent,
    StockItem,
    Visibility,
    ledger_value,
    parse_temperature,
    round_quantity,
)

logger = logging.getLogger(__name__)

Kind = StockEvent.Kind
UNSORTED = "unsorted"  # the LocationType key of "place later" containers
_INSERT_ATTEMPTS = 5
_MAX_QUANTITY = Decimal("9999999999.9999")  # DecimalField(max_digits=14, decimal_places=4)


class InventoryError(Exception):
    """A change the inventory refuses; the message can be shown to people."""


class StaleVersion(InventoryError):
    """The item changed after the caller read it (``expected_version``)."""

    def __init__(self, item, expected):
        self.item_id, self.expected, self.actual = item.pk, expected, item.version
        super().__init__(
            f"{item.short_id} changed since you opened it (now version {item.version}, you had "
            f"{expected}); reload and try again."
        )


class InvalidTransition(InventoryError):
    """That event can't happen to the item in its current state."""


class CrossLab(InventoryError):
    """Rows of two different labs."""


class InvalidCustodian(InventoryError):
    """Someone who can't hold this stock (``may_hold_stock`` / ``may_receive_stock``)."""


class IdempotencyConflict(InventoryError):
    """A ``client_uuid`` already used for another item or another kind of event."""


class InvalidLocation(InventoryError):
    """A location that can't be created, moved or used like that."""


# ---------------------------------------------------------------------------------------
# Who may hold stock


def may_hold_stock(user, lab):
    """The lab's pool user, or anyone with a membership in the lab: current members, and
    people who left (leavers, ended memberships, placeholders) who still hold stock until
    it is handed over. Imports may create stock for them; nobody can give them more."""
    if user is None or user.pk is None:
        return False
    return user.pk == lab.pool_user_id or Membership.objects.filter(user=user, lab=lab).exists()


def may_receive_stock(user, lab):
    """New custody (a hand-over) goes only to current members and the lab's pool user."""
    if user is None or user.pk is None:
        return False
    return user.pk == lab.pool_user_id or is_member(user, lab)


def _check_holder(user, lab, *, receiving):
    allowed = may_receive_stock(user, lab) if receiving else may_hold_stock(user, lab)
    if allowed:
        return
    if user is None:
        raise InvalidCustodian("Say who holds the stock (a custodian).")
    if not may_hold_stock(user, lab):
        # Someone of another lab, or of none: who they are isn't this lab's to read.
        raise InvalidCustodian(f"That person isn't in {lab}, so they can't hold its stock.")
    raise InvalidCustodian(
        f"{user} can't take stock of {lab}: the custodian must be a current member or the lab pool."
    )


# ---------------------------------------------------------------------------------------
# Small validators


def _choice(choices, value, what):
    if value not in choices.values:
        raise InventoryError(f"Unknown {what} {value!r}; use one of {', '.join(choices.values)}.")
    return choices(value)


def _temperature(value):
    try:
        return parse_temperature(value)
    except ValueError as exc:
        raise InvalidLocation(str(exc)) from exc


def _quantity(value):
    """A quantity as the database will store it (4 decimal places), or None."""
    if value is None or value == "":
        return None
    try:
        quantity = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise InventoryError(f"{value!r} isn't a quantity.") from exc
    if not quantity.is_finite() or quantity < 0 or quantity > _MAX_QUANTITY:
        raise InventoryError(f"{value!r} isn't a quantity (0 or more).")
    return round_quantity(abs(quantity))  # abs: "-0" is 0


def _count(value, what):
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise InventoryError(f"{what} must be a whole number, 0 or more.")
    return value


def _uuid(value):
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except ValueError as exc:
        raise InventoryError(f"{value!r} isn't a client uuid.") from exc


def _same_lab(lab, obj, what):
    """Raise CrossLab unless ``obj`` (or None) is of ``lab`` (a Lab or its pk). ``what``
    names it without its details ("That location"): it may be another lab's row."""
    if obj is not None and obj.lab_id != getattr(lab, "pk", lab):
        raise CrossLab(f"{what} belongs to another lab.")


def _lab_zone(lab_id):
    name = Lab.objects.filter(pk=lab_id).values_list("time_zone", flat=True).first()
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.get_default_timezone()


def _moment(value, lab_id, what):
    """``value`` as an aware datetime: a date is midnight in the lab's time zone, and a naive
    datetime is read in it (an import's "date" column). None stays None."""
    if value is None or (isinstance(value, datetime) and timezone.is_aware(value)):
        return value
    if isinstance(value, datetime):
        return timezone.make_aware(value, _lab_zone(lab_id))
    if isinstance(value, date):
        return timezone.make_aware(datetime.combine(value, time.min), _lab_zone(lab_id))
    raise InventoryError(f"{what} must be a date or a date and time, not {value!r}.")


def default_visibility(lab):
    """``inventory.default_visibility`` for the lab; "ask first" if the policy is not a
    visibility (the PI's "registered ≠ shared" promise, plan §4)."""
    value = get_policy("inventory.default_visibility", lab)
    if value not in Visibility.values:
        logger.warning(
            "inventory.default_visibility=%r is not a visibility; using ask_first", value
        )
        return Visibility.ASK_FIRST
    return Visibility(value)


# ---------------------------------------------------------------------------------------
# Locations


def get_location_type(key):
    """The LocationType for a key (or a LocationType, returned as is). A key that
    location_types.yaml knows but the database doesn't yet (seed_inventory not run) is
    created from the YAML, as ``resolve_vendor`` does for vendors."""
    if isinstance(key, LocationType):
        return key
    found = LocationType.objects.filter(key=key).first()
    if found is not None:
        return found
    from apps.catalog.models import seed_snapshot

    from .management.commands.seed_inventory import seeded_location_type

    values = seeded_location_type(key)
    if values is None:
        raise InvalidLocation(f"Unknown kind of location {key!r}.")
    found, _ = LocationType.objects.get_or_create(
        key=key, defaults={**values, "seeded": seed_snapshot(values)}
    )
    return found


def clean_location_name(name):
    name = " ".join(str(name or "").split())
    if not name:
        raise InvalidLocation("A location needs a name.")
    if "/" in name:
        raise InvalidLocation(f"{name!r}: a name can't contain '/', which separates path parts.")
    if len(name) > 200:
        raise InvalidLocation(f"{name[:40]}…: names are at most 200 characters.")
    return name


def clean_location_code(code, *, location=None):
    """A hand-made code in upper case ("" for none); raises InvalidLocation if it is malformed,
    reads like a stock item id or is used by another location (in any lab)."""
    code = str(code or "").strip().upper()
    if not code:
        return ""
    problem = shortid.location_code_problem(code)
    if problem:
        raise InvalidLocation(problem)
    clash = Location.objects.filter(code=code)
    if location is not None and location.pk is not None:
        clash = clash.exclude(pk=location.pk)
    if clash.exists():
        raise InvalidLocation(_code_in_use(code))
    return code


def _code_in_use(code):
    return f"The code {code} is already in use; codes are unique in LabStock."


def clean_location_tags(tags):
    """A location's ``tags``: ItemType keys (a list, or text split on commas), each checked
    to exist, without blanks or repeats. Raises InvalidLocation."""
    if isinstance(tags, str):
        tags = tags.split(",")
    keys = list(dict.fromkeys(str(tag).strip() for tag in tags or () if str(tag).strip()))
    known = set(ItemType.objects.filter(key__in=keys).values_list("key", flat=True))
    unknown = [key for key in keys if key not in known]
    if unknown:
        raise InvalidLocation(f"Unknown item type tag(s): {', '.join(unknown)}.")
    return keys


def clean_location_visibility(visibility, *, location_type, lab):
    """A location's visibility. None (or "") means shared for structural types and the lab
    default (``inventory.default_visibility``) for boxes and bags. Rooms, freezers, shelves
    and other structural places are always shared: a restricted freezer would hide
    everyone's stock in it, so restrict the box or the items instead."""
    if visibility is None or visibility == "":
        return Visibility.SHARED if location_type.structural else default_visibility(lab)
    visibility = _choice(Visibility, visibility, "visibility")
    if location_type.structural and visibility != Visibility.SHARED:
        raise InvalidLocation(
            f"A {location_type.label.lower()} is always shared; make the boxes or the items "
            f"in it {visibility.label.lower()} instead."
        )
    return visibility


def validate_placement(*, lab, parent, location_type, name, location=None, active=True):
    """Check that a location of this type and name may sit in ``parent`` (None: the top
    level), as a new one, or as ``location`` moved there or changed to that type and name.
    ``active`` is whether it will be in use. A retired location that stays retired where it
    is isn't checked against its siblings' names (a new "Box 9" may have replaced it) or a
    retired parent, so its notes can still be edited; bringing it back into use is checked.
    Raises CrossLab or InvalidLocation."""
    if location is not None and location.is_unsorted:
        raise InvalidLocation("An unsorted container isn't a real place, so it can't move.")
    if location_type.key == UNSORTED:
        raise InvalidLocation("Unsorted containers are made on demand (unsorted_location).")
    if (
        location is not None
        and location.location_type_id != location_type.pk
        and not location_type.structural
    ):
        # A shelf with a rack on it can't become a box: nothing structural goes in a box.
        inner = Location.objects.get_children(location).filter(location_type__structural=True)
        first = inner.select_related("location_type").first()
        if first is not None:
            raise InvalidLocation(
                f"{location} holds {first} ({first.location_type.label.lower()}), so it can't "
                f"be a {location_type.label.lower()}."
            )
    # Where it already is: its parent's path is its own path without the last step.
    stays = location is not None and location.path[: -Location.steplen] == (
        parent.path if parent is not None else ""
    )
    if parent is not None:
        _same_lab(lab, parent, "That location")
        if parent.is_unsorted:
            raise InvalidLocation("Nothing can go inside an unsorted container but stock.")
        if not parent.active and (active or not stays):
            raise InvalidLocation(f"{parent} is no longer in use.")
        if location is not None and (
            parent.pk == location.pk or parent.path.startswith(location.path)
        ):
            raise InvalidLocation(f"{location} can't go inside itself.")
        if location_type.structural and not parent.location_type.structural:
            raise InvalidLocation(
                f"A {location_type.label.lower()} can't go inside a "
                f"{parent.location_type.label.lower()}."
            )
        siblings = Location.objects.get_children(parent)
    else:
        siblings = Location.objects.filter(depth=1, lab=lab, is_unsorted=False)
    if not active:
        return  # names are unique among the locations in use
    clash = siblings.filter(name__iexact=name, active=True)
    if location is not None:
        clash = clash.exclude(pk=location.pk)
    if clash.exists():
        where = parent or f"the top level of {lab}"
        raise InvalidLocation(f"{where} already has a location called {name!r}.")


def lock_location_tree(lab):
    """Hold the location-tree lock of ``lab`` (a Lab or its pk) until the transaction ends.

    ``create_location`` and ``move_location`` take it before anything else, so a lab's tree
    changes one change at a time: a move rewrites its whole subtree in one UPDATE, which
    can't see a location another transaction is still adding inside it (that one would keep
    its old path). It is a Postgres advisory lock, so it holds up nothing else: not stock
    writes, not the foreign keys to the lab row. Take it before checking a change to the tree
    that you then make (the admin form does), and before the first row of a transaction that
    writes stock and then adds locations (``import_stock --create-locations``): it would
    otherwise hold locks a move waits for while it waits for the move.
    """
    # A stable signed 64-bit key, as apps.quartzy.locks makes them (hash() varies by process).
    key = f"inventory.location_tree:{getattr(lab, 'pk', lab)}"
    number = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big", signed=True)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [number])


def _lock_parent(parent):
    """``parent`` fresh from the database and locked (the row alone, as in apply_event), so
    it can't be retired (the admin locks it to check) while a location is added to it; None
    at the top level."""
    return None if parent is None else Location.objects.select_for_update().get(pk=parent.pk)


def _add_root(location):
    """``add_root``, again when a concurrent insert took the new root's path first."""
    for attempt in range(_INSERT_ATTEMPTS):
        try:
            with transaction.atomic():
                return Location.objects.add_root(instance=location)
        except IntegrityError:
            taken = location.path and Location.objects.filter(path=location.path).exists()
            if not taken or attempt == _INSERT_ATTEMPTS - 1:
                raise
    raise AssertionError("unreachable")


@transaction.atomic
def create_location(
    *,
    lab,
    parent,
    name,
    location_type,
    user,
    code="",
    temperature="",
    default_custodian=None,
    visibility=None,
    tags=(),
    notes="",
    photo=None,
):
    """Add a location under ``parent`` (None: a top-level room) and return it.

    ``location_type`` is a LocationType or its key. A blank ``code`` gets 5 generated
    Crockford characters. ``visibility=None`` means shared for structural types and the
    lab default (``inventory.default_visibility``) for boxes and bags; structural types are
    always shared. ``tags`` are ItemType keys (a list, or text split on commas); ``photo``
    an uploaded file (a box lid). The lab's tree (``lock_location_tree``) and the parent are
    locked until the transaction ends. Raises CrossLab, InvalidLocation or InvalidCustodian.
    """
    kind = get_location_type(location_type)
    name = clean_location_name(name)
    _same_lab(lab, parent, "That location")
    lock_location_tree(lab)
    fresh = _lock_parent(parent)
    validate_placement(lab=lab, parent=fresh, location_type=kind, name=name)
    if default_custodian is not None:
        _check_holder(default_custodian, lab, receiving=False)
    location = Location(
        lab=lab,
        name=name,
        code=clean_location_code(code),
        location_type=kind,
        temperature=_temperature(temperature),
        default_custodian=default_custodian,
        visibility=clean_location_visibility(visibility, location_type=kind, lab=lab),
        tags=clean_location_tags(tags),
        notes=notes or "",
        created_by=user,
    )
    if photo:
        location.photo = photo
    try:
        if parent is None:
            return _add_root(location)
        # treebeard locks the parent row too, and refreshes the caller's copy of it.
        return Location.objects.add_child(parent, instance=location)
    except IntegrityError:
        if location.code and Location.objects.filter(code=location.code).exists():
            # Another lab took the code between clean_location_code and the insert.
            raise InvalidLocation(_code_in_use(location.code)) from None
        raise


def _move_to_top(node):
    for attempt in range(_INSERT_ATTEMPTS):
        try:
            with transaction.atomic():
                last_root = Location.objects.get_last_root_node()
                Location.objects.move(node, last_root, "last-sibling")
                return
        except IntegrityError:
            if attempt == _INSERT_ATTEMPTS - 1:
                raise
            node.refresh_from_db(fields=["path", "depth", "numchild"])


_DEADLOCK = "40P01"  # SQLSTATE deadlock_detected


@transaction.atomic
def move_location(location, new_parent, *, user, reason=""):
    """Move ``location`` and everything in it under ``new_parent`` (None: the top level).

    The stock inside keeps its location (the box), so nothing else changes. Refuses another
    lab's parent, a move into itself or below itself, unsorted containers, an inactive parent
    and a move to where it already is. Returns the LocationEvent; ``location`` (and
    ``new_parent``) are refreshed.

    It takes the lab's tree lock first (``lock_location_tree``), so no location is added or
    moved in the lab meanwhile, then locks the whole subtree and both parents in id order.
    If Postgres still breaks a deadlock with another transaction by cancelling this move, it
    is an InvalidLocation asking to try again.
    """
    _same_lab(location.lab_id, new_parent, "That location")
    try:
        lock_location_tree(location.lab_id)
        return _move(location, new_parent, user=user, reason=reason)
    except OperationalError as exc:
        if getattr(exc.__cause__, "sqlstate", None) != _DEADLOCK:
            raise
        raise InvalidLocation("Someone else is changing these locations; try again.") from exc


def _move(location, new_parent, *, user, reason):
    node = Location.objects.get(pk=location.pk)
    old_parent = Location.objects.get_parent(node)
    # Everything the move rewrites (the subtree) and the parents whose numchild changes, rows
    # alone (as in apply_event: a join would lock the location type row too) and in id
    # order, the order stock writers lock the locations they use, so neither waits for the
    # other in a circle.
    parents = [parent.pk for parent in (old_parent, new_parent) if parent is not None]
    locked = Location.objects.select_for_update().filter(
        Q(path__startswith=node.path) | Q(pk__in=parents)
    )
    rows = {row.pk: row for row in locked.order_by("pk")}
    node = rows[location.pk]
    target = rows[new_parent.pk] if new_parent is not None else None
    old_parent = rows[old_parent.pk] if old_parent is not None else None
    validate_placement(
        lab=node.lab,
        parent=target,
        location_type=node.location_type,
        name=node.name,
        location=node,
        active=node.active,
    )
    if (old_parent and old_parent.pk) == (target and target.pk):
        raise InvalidLocation(
            f"{node} is already {'in ' + str(target) if target else 'at the top'}."
        )
    if target is None:
        _move_to_top(node)
    else:
        Location.objects.move(node, target, "last-child")
    event = LocationEvent.objects.create(
        lab=node.lab,
        location=node,
        from_parent=old_parent,
        to_parent=target,
        user=user,
        reason=(reason or "")[:300],
    )
    for copy in (location, new_parent):
        if copy is not None:
            copy.refresh_from_db(fields=["path", "depth", "numchild"])
    return event


def unsorted_location(lab, owner, temperature):
    """The owner's "place later" container at ``temperature`` in ``lab``, created on first
    use; ``temperature`` "" or None means not known. Race-safe: a unique constraint on
    (lab, owner, temperature) and a retry after the IntegrityError of a concurrent insert.

    Unsorted containers are roots (no real place), shared, of type "unsorted", and display
    as "Unsorted: Priya Patel, -20 °C". The owner must be able to hold stock in the lab.
    """
    temperature = _temperature(temperature)
    _check_holder(owner, lab, receiving=False)
    lookup = {"lab": lab, "is_unsorted": True, "unsorted_owner": owner, "temperature": temperature}
    for _attempt in range(_INSERT_ATTEMPTS):
        found = Location.objects.filter(**lookup).first()
        if found is not None:
            return found
        location = Location(
            name="Unsorted",
            location_type=get_location_type(UNSORTED),
            visibility=Visibility.SHARED,
            **lookup,
        )
        try:
            with transaction.atomic():
                return _add_root(location)
        except IntegrityError:
            continue  # created concurrently: the next lookup finds it
    raise InvalidLocation("Couldn't make the unsorted container; try again.")


def _share_lock(pks):
    """FOR KEY SHARE on these locations, in id order. Retiring a location (the admin) and
    moving one (``move_location``) lock it FOR UPDATE, so they wait for this transaction and
    it waits for them; stock writers don't wait for each other: it is the lock their foreign
    keys to the location take anyway, only taken sooner, in the order a move locks rows."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT id FROM inventory_location WHERE id = ANY(%s) ORDER BY id FOR KEY SHARE",
            [sorted({pk for pk in pks if pk is not None})],
        )


def _usable_location(location, lab, custodian, *, current=None):
    """``location`` fresh from the database, if stock of ``custodian`` in ``lab`` can go
    there. It (and ``current``, the pk of the location the stock leaves) is locked first, so
    it can't be retired while the stock goes in (``_share_lock``)."""
    _same_lab(lab, location, "That location")
    _share_lock([location.pk, current])
    location = Location.objects.select_related("location_type").get(pk=location.pk)
    if not location.active:
        raise InvalidLocation(f"{location} is no longer in use.")
    if location.is_unsorted and location.unsorted_owner_id != custodian.pk:
        raise InvalidLocation(f"{location} holds only {location.unsorted_owner}'s stock.")
    return location


def _position(location, label, index):
    """``(position_label, position_index)`` in ``location``: give either one, and the other
    is derived from its type's scheme. A label the scheme knows is written its way ("b-3" is
    "B3"), and one written like a position it doesn't have ("J1" or "A10" in a 9 x 9 box) is
    refused, as an index it doesn't have is. Anything else ("top shelf, left") is kept as
    free text, with no index."""
    label = " ".join(str(label or "").split())
    if len(label) > 20:
        raise InventoryError(f"{label[:20]}…: positions are at most 20 characters.")
    if index is not None and (type(index) is not int or index < 1):
        raise InventoryError("A position number is 1 or more.")
    kind = location.location_type
    if index is None:
        index = kind.position_index(label) if label else None
        if index is None and kind.looks_like_position(label):
            raise InventoryError(f"{location} has no position {label}.")
        return (kind.position_label(index), index) if index is not None else (label, None)
    named = kind.position_label(index)
    if not named:
        raise InventoryError(f"{location} has no position {index}.")
    if label and kind.position_index(label) != index:
        raise InventoryError(f"{label} isn't position {index} in {location}.")
    return named, index


# ---------------------------------------------------------------------------------------
# Stock


def _lifecycle_before_loss(item):
    """What the item was before it was last lost (the events are replayed)."""
    lifecycle = before = None
    for event in item.events.order_by("seq"):
        if event.kind == Kind.LOST:
            before = lifecycle
        lifecycle = event.state.get("lifecycle", lifecycle)
    return before


_ACTIVE = frozenset({Lifecycle.UNVERIFIED, Lifecycle.AVAILABLE, Lifecycle.OPENED})
_PHYSICAL = _ACTIVE | {Lifecycle.EMPTY}  # the container is still there
_NOT_DISCARDED = _PHYSICAL | {Lifecycle.LOST}
_PLACE = frozenset({"location", "position_label", "position_index"})
_AMOUNT = frozenset({"quantity", "unit", "container_count"})
# kind -> (lifecycles it can happen in, state fields the caller may pass as **changes).
# Discarded items take nothing but notes; only lost items are found; only unverified ones
# are verified. Every other effect is implied by the kind (_implied).
TRANSITIONS = {
    Kind.VERIFIED: (frozenset({Lifecycle.UNVERIFIED}), _PLACE),
    Kind.MOVED: (_PHYSICAL, _PLACE),
    Kind.OPENED: (frozenset({Lifecycle.UNVERIFIED, Lifecycle.AVAILABLE}), frozenset()),
    Kind.MARKED_LOW: (_ACTIVE, frozenset()),
    Kind.MARKED_OK: (_ACTIVE, frozenset()),
    Kind.EMPTIED: (_ACTIVE, frozenset()),
    Kind.DISCARDED: (_NOT_DISCARDED, frozenset()),
    Kind.LOST: (_PHYSICAL, frozenset()),
    Kind.FOUND: (frozenset({Lifecycle.LOST}), _PLACE),
    Kind.CUSTODY_CHANGED: (_NOT_DISCARDED, _PLACE | {"custodian"}),
    Kind.VISIBILITY_CHANGED: (_NOT_DISCARDED, frozenset({"visibility"})),
    Kind.QUANTITY_ADJUSTED: (_ACTIVE, _AMOUNT),
    Kind.SPLIT: (_ACTIVE, _AMOUNT),
    Kind.NOTE: (frozenset(Lifecycle.values), frozenset()),
}


def _implied(kind, item, occurred_at):
    """The state a kind of event sets by itself."""
    seen = Lifecycle.OPENED if item.opened_at else Lifecycle.AVAILABLE
    if kind == Kind.VERIFIED:
        return {"lifecycle": seen}
    if kind == Kind.FOUND:
        before = _lifecycle_before_loss(item)
        return {"lifecycle": before if before in _PHYSICAL - {Lifecycle.UNVERIFIED} else seen}
    if kind == Kind.OPENED:
        return {"lifecycle": Lifecycle.OPENED, "opened_at": item.opened_at or occurred_at}
    if kind == Kind.MARKED_LOW:
        if item.level == Level.LOW:
            raise InvalidTransition(f"{item.short_id} is already marked low.")
        return {"level": Level.LOW}
    if kind == Kind.MARKED_OK:
        if item.level == Level.OK:
            raise InvalidTransition(f"{item.short_id} isn't marked low.")
        return {"level": Level.OK}
    if kind == Kind.EMPTIED:
        emptied = {"lifecycle": Lifecycle.EMPTY}
        return emptied if item.quantity is None else {**emptied, "quantity": Decimal(0)}
    if kind == Kind.DISCARDED:
        return {"lifecycle": Lifecycle.DISCARDED}
    if kind == Kind.LOST:
        return {"lifecycle": Lifecycle.LOST}
    return {}


def _transition(item, kind, changes, occurred_at, reason):
    """Validate one event against the item; return the state fields it changes."""
    if kind not in TRANSITIONS:
        raise InvalidTransition("Stock is created with create_stock_item, not apply_event.")
    lifecycles, changeable = TRANSITIONS[kind]
    if item.lifecycle not in lifecycles:
        raise InvalidTransition(
            f"{item.short_id} is {item.get_lifecycle_display().lower()}: it can't be "
            f"{Kind(kind).label.lower()}."
        )
    extra = sorted(set(changes) - changeable)
    if extra:
        raise InvalidTransition(f"A {kind} event can't change {', '.join(extra)}.")
    if kind == Kind.NOTE and not str(reason or "").strip():
        raise InvalidTransition("A note needs some text (reason).")
    new = _implied(kind, item, occurred_at)
    if "custodian" in changes or kind == Kind.CUSTODY_CHANGED:
        new.update(_new_custodian(item, changes.get("custodian"), changes))
    if "visibility" in changes or kind == Kind.VISIBILITY_CHANGED:
        new.update(_new_visibility(item, changes.get("visibility")))
    if _PLACE & set(changes):
        new.update(_new_place(item, changes, new.get("custodian", item.custodian)))
    if _AMOUNT & set(changes):
        new.update(_new_amount(item, changes))
    new = {name: value for name, value in new.items() if getattr(item, name) != value}
    if not new and kind not in (Kind.NOTE, Kind.SPLIT):
        raise InvalidTransition(f"Nothing to change: {item.short_id} is already like that.")
    return new


def _new_custodian(item, custodian, changes):
    if custodian is None:
        raise InvalidTransition("Say who takes custody (custodian=...).")
    if custodian.pk == item.custodian_id:
        raise InvalidTransition(f"{custodian} already has {item.short_id}.")
    _check_holder(custodian, item.lab, receiving=True)
    new = {"custodian": custodian}
    here = item.location
    if (
        "location" not in changes
        and here.is_unsorted
        and here.unsorted_owner_id == item.custodian_id
    ):
        # Still to be placed: it moves to the new custodian's "place later" container.
        new["location"] = unsorted_location(item.lab, custodian, here.temperature)
        new["position_label"], new["position_index"] = "", None
    return new


def _new_visibility(item, visibility):
    if visibility is None:
        raise InvalidTransition("Say what the visibility becomes (visibility=...).")
    visibility = _choice(Visibility, visibility, "visibility")
    if item.mta and visibility != Visibility.RESTRICTED:
        raise InvalidTransition(f"{item.short_id} came under an MTA, so it stays restricted.")
    return {"visibility": visibility}


def _new_place(item, changes, custodian):
    location = changes.get("location", item.location)
    if location is None:
        raise InvalidTransition("A stock item is always somewhere; give a location.")
    location = _usable_location(location, item.lab_id, custodian, current=item.location_id)
    if "position_label" in changes or "position_index" in changes:
        # Either one: the other is derived from it when the location has a scheme.
        label, index = _position(
            location, changes.get("position_label", ""), changes.get("position_index")
        )
    elif location.pk == item.location_id:
        label, index = item.position_label, item.position_index
    else:
        label, index = "", None  # the old position means nothing in the new place
    return {"location": location, "position_label": label, "position_index": index}


def _new_amount(item, changes):
    new = {}
    if "quantity" in changes:
        new["quantity"] = _quantity(changes["quantity"])
    if "unit" in changes:
        new["unit"] = str(changes["unit"] or "").strip()[:20]
    if "container_count" in changes:
        if not item.is_aggregate:
            raise InvalidTransition(f"{item.short_id} is one container, not an aggregate.")
        count = _count(changes["container_count"], "container_count")
        if count is None:
            raise InvalidTransition("An aggregate always has a container count.")
        new["container_count"] = count
        if count == 0:
            # Every container was split off (or counted away): nothing is left of it, so it
            # is empty, as if emptied, in this same event, and leaves the active stock.
            new["lifecycle"] = Lifecycle.EMPTY
            if item.quantity is not None and "quantity" not in changes:
                new["quantity"] = Decimal(0)
    return new


def _qty_delta(old, new):
    if old is None and new is None:
        return None
    return (new or Decimal(0)) - (old or Decimal(0))


def _event_data(state, data):
    data = dict(data or {})
    if "state" in data:
        raise InventoryError('"state" in event data is reserved for the ledger.')
    return {"state": {name: ledger_value(value) for name, value in state.items()}, **data}


def _copy_state(src, dst):
    """Give the caller's copy of the item the state the ledger just wrote."""
    if not isinstance(dst, StockItem) or dst is src:
        return
    for name in (*LEDGER_FIELDS, "updated_at"):
        field = StockItem._meta.get_field(name)
        setattr(dst, field.attname, getattr(src, field.attname))
        if field.is_relation:
            if field.is_cached(src):
                field.set_cached_value(dst, field.get_cached_value(src))
            elif field.is_cached(dst):
                field.delete_cached_value(dst)
    dst._take_snapshot()


def _check_replay(event, item, kind):
    """A client_uuid seen before: fine for the same item and kind, a conflict otherwise."""
    if event.stock_item_id != item.pk or event.kind != kind:
        raise IdempotencyConflict(
            f"That request id was already used for {event.get_kind_display().lower()} of "
            "another item or another action."
        )


def _append(item, kind, new, *, user, occurred_at, client_uuid, source, reason, data):
    """Append the event with the next seq, update the cached state and the version."""
    last = item.events.aggregate(last=Max("seq"))["last"] or 0
    if last != item.version:
        raise InventoryError(f"The ledger of {item.short_id} is out of step; tell the admin.")
    event = StockEvent(
        lab_id=item.lab_id,
        stock_item=item,
        seq=last + 1,
        kind=kind,
        qty_delta=_qty_delta(item.quantity, new["quantity"]) if "quantity" in new else None,
        from_location=item.location if "location" in new else None,
        to_location=new.get("location"),
        from_custodian=item.custodian if "custodian" in new else None,
        to_custodian=new.get("custodian"),
        reason=str(reason or "")[:500],
        user=user,
        occurred_at=occurred_at,
        client_uuid=client_uuid,
        source=source,
        data=_event_data(new, data),
    )
    try:
        with transaction.atomic():
            event._append_from_ledger()
    except IntegrityError:
        clash = client_uuid and StockEvent.objects.filter(client_uuid=client_uuid).first()
        if clash:
            _check_replay(clash, item, kind)
            return clash
        raise
    for name, value in new.items():
        setattr(item, name, value)
    item.version = event.seq
    item._save_from_ledger(update_fields=[*new, "version", "updated_at"])
    return event


@transaction.atomic
def apply_event(
    item,
    kind,
    *,
    user,
    expected_version=None,
    occurred_at=None,
    client_uuid=None,
    source=StockEvent.Source.WEB,
    reason="",
    data=None,
    **changes,
):
    """Record one change to a stock item (a StockItem or its pk) and return its StockEvent.

    In one transaction: lock the item; return the existing event if ``client_uuid`` was used
    before (IdempotencyConflict if for another item or kind); raise StaleVersion when
    ``expected_version`` isn't the item's version; check the transition (``TRANSITIONS``);
    append the event with the next ``seq``; update the cached state fields; bump ``version``.
    ``changes`` are the state fields the kind allows (``location``, ``position_label``,
    ``position_index``, ``custodian``, ``visibility``, ``quantity``, ``unit``,
    ``container_count``); ``data`` is extra JSON for the event. ``occurred_at`` may be a date
    (midnight in the lab's time zone). A passed StockItem is updated in place.
    """
    kind = _choice(Kind, kind, "event kind")
    source = _choice(StockEvent.Source, source, "event source")
    pk = item.pk if isinstance(item, StockItem) else item
    # The row alone: with joins, Postgres re-checks a row that another writer just changed
    # against the joined rows it read before waiting for the lock, and loses it.
    locked = StockItem.objects.select_for_update().get(pk=pk)
    if client_uuid is not None:
        client_uuid = _uuid(client_uuid)
        seen = StockEvent.objects.filter(client_uuid=client_uuid).first()
        if seen is not None:
            _check_replay(seen, locked, kind)
            _copy_state(locked, item)
            return seen
    if expected_version is not None and locked.version != expected_version:
        raise StaleVersion(locked, expected_version)
    occurred_at = _moment(occurred_at, locked.lab_id, "occurred_at") or timezone.now()
    new = _transition(locked, kind, changes, occurred_at, reason)
    event = _append(
        locked,
        kind,
        new,
        user=user,
        occurred_at=occurred_at,
        client_uuid=client_uuid,
        source=source,
        reason=reason,
        data=data,
    )
    _copy_state(locked, item)
    return event


def _created_by_uuid(client_uuid, lab):
    event = StockEvent.objects.filter(client_uuid=client_uuid).select_related("stock_item").first()
    if event is None:
        return None
    if event.kind != Kind.CREATED or event.lab_id != lab.pk:
        raise IdempotencyConflict("That request id was already used for something else.")
    return event.stock_item


def _visibility_or_default(visibility, lab):
    if visibility is None:
        return default_visibility(lab)
    return _choice(Visibility, visibility, "visibility")


@transaction.atomic
def create_stock_item(
    *,
    lab,
    product,
    custodian,
    user,
    source,
    supplier_product=None,
    lot=None,
    location=None,
    temperature=None,
    visibility=None,
    lifecycle=Lifecycle.AVAILABLE,
    position_label="",
    position_index=None,
    quantity=None,
    unit="",
    expiry_date=None,
    expiry_precision=ExpiryPrecision.DAY,
    mta=False,
    source_order_request=None,
    attributes=None,
    notes="",
    occurred_at=None,
    client_uuid=None,
    event_source=StockEvent.Source.WEB,
    is_aggregate=False,
    container_count=None,
    opened_at=None,
    parent=None,
    reason="",
    data=None,
):
    """Create a stock item and its "created" event (seq 1, version 1); return the item.

    - ``product`` is followed to its canonical product; ``supplier_product`` and ``lot``
      must be of that product.
    - ``custodian`` must be able to hold stock in the lab (``may_hold_stock``).
    - ``location=None`` puts it in the custodian's unsorted container for ``temperature``
      ("" or None: not known); ``temperature`` is ignored when a location is given.
    - ``visibility=None`` means the lab default (``inventory.default_visibility``);
      ``mta=True`` forces restricted.
    - ``lifecycle`` is unverified, available or opened (opened sets ``opened_at``, default
      ``occurred_at``). ``occurred_at`` and ``opened_at`` may be dates (midnight in the lab's
      time zone). An aggregate (``is_aggregate``) needs ``container_count`` >= 1.
    - ``reason`` and ``data`` go on the "created" event ("stock-sheet.csv line 4",
      ``{"claim": 12}``), as for apply_event.
    - A repeated ``client_uuid`` returns the item it created the first time.
    """
    if client_uuid is not None:
        client_uuid = _uuid(client_uuid)
        existing = _created_by_uuid(client_uuid, lab)
        if existing is not None:
            return existing
    product = canonical(product)
    if supplier_product is not None and canonical(supplier_product.product).pk != product.pk:
        raise InventoryError(f"{supplier_product} is a listing of another product.")
    if lot is not None and canonical(lot.product).pk != product.pk:
        raise InventoryError(f"Lot {lot.lot_raw or lot.lot_key} is of another product.")
    _same_lab(lab, source_order_request, "That request")
    _same_lab(lab, parent, "The item it was split from")
    _check_holder(custodian, lab, receiving=False)
    lifecycle = _choice(Lifecycle, lifecycle, "lifecycle")
    if lifecycle not in _ACTIVE:
        raise InvalidTransition("New stock is unverified, available or opened.")
    occurred_at = _moment(occurred_at, lab.pk, "occurred_at") or timezone.now()
    opened_at = _moment(opened_at, lab.pk, "opened_at")
    if lifecycle == Lifecycle.OPENED:
        opened_at = opened_at or occurred_at
    elif lifecycle == Lifecycle.AVAILABLE and opened_at is not None:
        raise InvalidTransition("An item with an opened date isn't 'available'; say 'opened'.")
    if location is None:
        location = unsorted_location(lab, custodian, temperature)
    else:
        location = _usable_location(location, lab, custodian)
    position_label, position_index = _position(location, position_label, position_index)
    container_count = _count(container_count, "container_count")
    if is_aggregate and not container_count:
        raise InventoryError("An aggregate needs container_count (1 or more).")
    if not is_aggregate and container_count is not None:
        raise InventoryError("container_count is only for aggregates (is_aggregate=True).")
    item = StockItem(
        lab=lab,
        product=product,
        supplier_product=supplier_product,
        lot=lot,
        parent=parent,
        custodian=custodian,
        visibility=Visibility.RESTRICTED if mta else _visibility_or_default(visibility, lab),
        location=location,
        position_label=position_label,
        position_index=position_index,
        lifecycle=lifecycle,
        quantity=_quantity(quantity),
        unit=str(unit or "").strip()[:20],
        opened_at=opened_at,
        container_count=container_count,
        expiry_date=expiry_date,
        expiry_precision=_choice(ExpiryPrecision, expiry_precision, "expiry precision"),
        attributes=attributes or {},
        source=_choice(Source, source, "source"),
        source_order_request=source_order_request,
        mta=bool(mta),
        is_aggregate=bool(is_aggregate),
        notes=notes or "",
        version=1,
        created_by=user,
    )
    event = StockEvent(
        lab=lab,
        seq=1,
        kind=Kind.CREATED,
        qty_delta=item.quantity,
        to_location=location,
        to_custodian=custodian,
        reason=str(reason or "")[:500],
        user=user,
        occurred_at=occurred_at,
        client_uuid=client_uuid,
        source=_choice(StockEvent.Source, event_source, "event source"),
        data=_event_data({name: getattr(item, name) for name in STATE_FIELDS}, data),
    )
    try:
        with transaction.atomic():  # a create racing on the same client_uuid rolls back
            item._save_from_ledger()
            event.stock_item = item
            event._append_from_ledger()
    except IntegrityError:
        existing = client_uuid and _created_by_uuid(client_uuid, lab)
        if existing:
            return existing
        raise
    return item


# ---------------------------------------------------------------------------------------
# Replaying the ledger


def fold_events(events):
    """The state ``events`` (one item's, in seq order) add up to, as ``ledger_value``s."""
    state = {}
    for event in events:
        state.update(event.state)
    return state


def ledger_drift(item):
    """``{field: (cached, folded)}`` where the item's cached state differs from its events
    folded in seq order; empty when the ledger and the cache agree."""
    item = StockItem.objects.get(pk=item.pk)
    folded = fold_events(item.events.order_by("seq"))
    cached = item.ledger_state()
    return {
        name: (cached[name], folded.get(name))
        for name in STATE_FIELDS
        if cached[name] != folded.get(name)
    }
