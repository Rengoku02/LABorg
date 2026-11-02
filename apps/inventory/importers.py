"""CSV imports into the inventory (docs/design/mvp-a.md §3).

Shared by the import commands:
- ``read_rows(path, required=...)``: the rows with their line numbers, header checked
- ``ImportFailed``: what was wrong, one message per line; nothing was saved
- ``import_run(commit=...)``: the dry run. An import does everything inside one transaction
  and rolls it back unless ``commit``, so a dry run reports exactly what a commit would do
- ``PathIndex(lab)``: the lab's locations by path, and the parts of a path made on demand

``import_locations(path, *, lab, commit=False)`` loads docs/templates/locations.csv
(``path, kind, temperature, owner_email, code, notes``) through ``services.create_location``.

``import_stock(path, *, lab, commit=False, ...)`` loads docs/templates/stock-sheet.csv through
``services.create_stock_item``; ``write_review_csv`` writes what each row did.
"""

import csv
import hashlib
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from apps.catalog.models import ItemType
from apps.catalog.services import get_or_create_lot, resolve_supplier_product
from apps.core.models import Membership, User
from apps.core.policies import get_policy

from . import services
from .management.commands.seed_inventory import (
    LOCATION_TYPES_YAML,
    load_yaml,
    location_type_values,
)
from .models import (
    PATH_SEPARATOR,
    ExpiryPrecision,
    Lifecycle,
    Location,
    LocationType,
    Source,
    StockEvent,
    Visibility,
    parse_temperature,
    temperature_label,
)
from .selectors import ancestor_chains, split_path
from .shortid import location_code_problem

LOCATION_COLUMNS = ("path", "kind", "temperature", "owner_email", "code", "notes")


class ImportFailed(Exception):
    """The file has errors (``errors``, one message per problem); nothing was saved."""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__(f"{len(self.errors)} error(s); nothing saved")


class _Rollback(Exception):
    pass


@contextmanager
def import_run(*, commit):
    """``with import_run(commit=commit) as errors:`` runs an import in one transaction and
    rolls it back unless ``commit`` is true and nothing was added to ``errors``; then raises
    ImportFailed(errors) if anything was. So a dry run does all a commit would, reports it,
    and saves nothing (not even the location types a commit would create on first use)."""
    errors = []
    try:
        with transaction.atomic():
            yield errors
            if errors or not commit:
                raise _Rollback
    except _Rollback:
        pass
    if errors:
        raise ImportFailed(errors)


@dataclass(frozen=True)
class Row:
    line: int
    values: dict


def read_rows(path, *, required):
    """The CSV's non-blank rows, keyed by lower-case column name, with the line each ends
    on. Raises ImportFailed if a ``required`` column is missing or the file isn't UTF-8."""
    try:
        with open(path, newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            header = [(name or "").strip().lower() for name in reader.fieldnames or []]
            missing = [column for column in required if column not in header]
            if missing:
                raise ImportFailed([f"line 1: missing column(s): {', '.join(missing)}"])
            rows = []
            for raw in reader:
                values = {
                    key.strip().lower(): (value or "").strip()
                    for key, value in raw.items()
                    if isinstance(key, str) and isinstance(value, str | None)
                }
                if any(values.values()):
                    rows.append(Row(reader.line_num, values))
    except UnicodeDecodeError as exc:
        # Excel's plain "CSV" is not UTF-8; its "CSV UTF-8" is.
        raise ImportFailed(
            [
                f"line {_undecodable_line(path)}: the file isn't UTF-8 text; save it as "
                "'CSV UTF-8' and try again"
            ]
        ) from exc
    return rows


def _undecodable_line(path):
    """The first line of the file that isn't valid UTF-8."""
    with open(path, "rb") as handle:
        for number, raw in enumerate(handle, start=1):
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError:
                return number
    return 1


def _key(parts):
    return tuple(part.casefold() for part in parts)


@dataclass
class PlannedLocation:
    """One location the file asks for (``line``), or a parent it implies (``implied_by``)."""

    parts: tuple
    kind: LocationType
    line: int | None = None
    implied_by: int | None = None
    temperature: str = ""
    owner: User | None = None
    code: str = ""  # as the file gives it; "" for a generated one
    notes: str = ""
    saved_code: str = ""  # the code it got (a generated one is rolled back in a dry run)

    @property
    def path(self):
        return PATH_SEPARATOR.join(self.parts)

    def describe(self):
        details = [self.kind.key, temperature_label(self.temperature)]
        if self.code:
            details.append(f"code {self.code}")
        if self.owner is not None:
            details.append(f"owner {self.owner.email}")
        return f"{self.path} ({', '.join(detail for detail in details if detail)})"


@dataclass
class LocationReport:
    created: list = field(default_factory=list)  # PlannedLocation, with the code it got
    existing: list = field(default_factory=list)  # (PlannedLocation, [differences])
    committed: bool = False


def _kinds():
    """``(key, label)`` of every kind a file may name: the database's, then the YAML's."""
    kinds = dict(LocationType.objects.values_list("key", "label"))
    for key, values in location_type_values(load_yaml(LOCATION_TYPES_YAML), LOCATION_TYPES_YAML):
        kinds.setdefault(key, values["label"])
    kinds.pop("unsorted", None)
    return list(kinds.items())


def _known_kinds():
    return [key for key, _ in _kinds()]


def _plan_row(row, lab, errors):
    values, problems = row.values, []
    parts = split_path(values.get("path"))
    if not parts:
        problems.append("the path is empty")
    for part in parts:
        try:
            services.clean_location_name(part)
        except services.InventoryError as exc:
            problems.append(str(exc))
    kind = None
    kind_key = values.get("kind", "").lower()
    if not kind_key or kind_key == "unsorted":
        problems.append(f"give a kind: one of {', '.join(_known_kinds())}")
    else:
        try:
            kind = services.get_location_type(kind_key)
        except services.InventoryError:
            problems.append(f"unknown kind {kind_key!r}; use one of {', '.join(_known_kinds())}")
    try:
        temperature = parse_temperature(values.get("temperature"))
    except ValueError as exc:
        problems.append(str(exc))
    owner = None
    email = values.get("owner_email", "").lower()
    if email:
        owner = User.objects.filter(email=email).first()
        if owner is None or not services.may_hold_stock(owner, lab):
            problems.append(f"no one in {lab} has the email {email}")
    code = values.get("code", "").upper()
    if code and location_code_problem(code):
        problems.append(location_code_problem(code))
    errors.extend(f"line {row.line}: {problem}" for problem in problems)
    if problems:
        return None
    return PlannedLocation(
        parts=parts,
        kind=kind,
        line=row.line,
        temperature=temperature,
        owner=owner,
        code=code,
        notes=values.get("notes", ""),
    )


def _plan(rows, lab, errors):
    plans, codes = {}, {}
    for row in rows:
        plan = _plan_row(row, lab, errors)
        if plan is None:
            continue
        key = _key(plan.parts)
        if key in plans:
            errors.append(f"line {row.line}: {plan.path} is already on line {plans[key].line}")
            continue
        if plan.code and plan.code in codes:
            errors.append(
                f"line {row.line}: the code {plan.code} is already on line {codes[plan.code]}"
            )
            continue
        codes[plan.code] = row.line
        plans[key] = plan
    return plans


# Freezers and fridges are often named by their temperature: "-80 A", "-20 B", "4C door".
_KIND_BY_TEMPERATURE = {"-20C": "freezer", "-80C": "freezer", "4C": "fridge", "LN2": "ln2_dewar"}


def _guess_kind(name, depth):
    """``(kind, temperature)`` of a parent the file only implies. A top-level one is a room,
    whatever its name ("4 West", "Freezer room B", "Box room"). Below that it is told from
    its first word: "Shelf 2" is a shelf, "-80 A" a freezer at -80 °C. The kind is None when
    there's no telling."""
    if depth == 1:
        return services.get_location_type("room"), ""
    first = name.split()[0]
    word = first.casefold()
    try:
        temperature = parse_temperature(first)
    except ValueError:
        temperature = ""
    kinds = _kinds()
    for key, _label in kinds:
        if word == key.casefold():
            return services.get_location_type(key), temperature
    for key, label in kinds:
        if word == label.split()[0].casefold():
            return services.get_location_type(key), temperature
    if temperature in _KIND_BY_TEMPERATURE:
        return services.get_location_type(_KIND_BY_TEMPERATURE[temperature]), temperature
    return None, ""


def _differences(location, plan):
    """What the file says differently about a location LabStock already has (kept)."""
    found = []
    if location.location_type_id != plan.kind.pk:
        found.append(f"kind is {location.location_type.key} in LabStock")
    for name, wanted, have in (
        ("temperature", plan.temperature, location.temperature),
        ("code", plan.code, location.code),
        ("notes", plan.notes, location.notes),
    ):
        if wanted and wanted != have:
            found.append(f"{name} is {have or 'blank'} in LabStock")
    if plan.owner is not None and plan.owner.pk != location.default_custodian_id:
        found.append(f"owner is {location.default_custodian or 'nobody'} in LabStock")
    if not location.active:
        found.append("it is retired (inactive) in LabStock")
    return found


class PathIndex:
    """A lab's real locations by path, inactive ones too, for an import that looks up many
    paths (``import_locations``; ``import_stock --create-locations``). Paths match case- and
    space-insensitively, as ``selectors.location_by_path`` does.

    - ``get(path)``: the location, or None (``path`` is text or a tuple of parts)
    - ``parent_of(parts, *, line=None)``: the location ``parts[:-1]`` names (None at the top
      level), creating each part LabStock doesn't have with its kind told from its name
    - ``ensure(path, *, line=None)``: the location, creating the missing parts, the last too
    - ``add(plan, parent)``: create a PlannedLocation under ``parent``

    What it creates is appended to ``created`` (PlannedLocations; ``implied_by`` is the line
    that implied a part). Refusals raise ``services.InventoryError``.
    """

    def __init__(self, lab, *, user=None, created=None):
        self.lab, self.user = lab, user
        self.created = created if created is not None else []
        # Active ones first: as in location_by_path, a location in use wins over a retired
        # one with the same path (a box retired, replaced, then brought back).
        locations = list(
            Location.objects.for_lab(lab)
            .physical()
            .select_related("location_type")
            .order_by("-active", "path")
        )
        chains = ancestor_chains(locations)
        self._by_key = {}
        for place in locations:
            self._by_key.setdefault(_key(loc.name for loc in chains[place.pk]), place)

    def get(self, path):
        parts = split_path(path) if isinstance(path, str) else path
        return self._by_key.get(_key(parts))

    def parent_of(self, parts, *, line=None):
        parent = None
        for depth in range(1, len(parts)):
            parent = self.get(parts[:depth]) or self._imply(parts[:depth], parent, line)
        return parent

    def ensure(self, path, *, line=None):
        parts = split_path(path)
        if not parts:
            raise services.InventoryError("The location path is empty.")
        for part in parts:
            services.clean_location_name(part)
        found = self.get(parts)
        if found is not None:
            return found
        return self._imply(parts, self.parent_of(parts, line=line), line)

    def add(self, plan, parent):
        location = services.create_location(
            lab=self.lab,
            parent=parent,
            name=plan.parts[-1],
            location_type=plan.kind,
            user=self.user,
            code=plan.code,
            temperature=plan.temperature,
            default_custodian=plan.owner,
            notes=plan.notes,
        )
        plan.saved_code = location.code
        self.created.append(plan)
        self._by_key[_key(plan.parts)] = location
        return location

    def _imply(self, parts, parent, line):
        kind, temperature = _guess_kind(parts[-1], len(parts))
        plan = PlannedLocation(
            parts=tuple(parts), kind=kind, implied_by=line, temperature=temperature
        )
        if kind is None:
            raise services.InventoryError(
                f"{plan.path} isn't in LabStock, and its kind can't be told from its name; "
                "give it a row in locations.csv"
            )
        return self.add(plan, parent)


def _apply(plans, lab, user, report, errors):
    """Create what the plans ask for, parents first; ``errors`` collects what failed."""
    paths, failed = PathIndex(lab, user=user, created=report.created), set()
    for key in sorted(plans, key=lambda key: (len(key), plans[key].line)):
        plan = plans[key]
        found = paths.get(plan.parts)
        if found is not None:
            report.existing.append((plan, _differences(found, plan)))
            continue
        try:
            if any(key[:depth] in failed for depth in range(1, len(key))):
                raise services.InventoryError(f"{plan.path}: a location above it failed")
            paths.add(plan, paths.parent_of(plan.parts, line=plan.line))
        except services.InventoryError as exc:
            failed.add(key)
            errors.append(f"line {plan.line}: {exc}")


def import_locations(path, *, lab, commit=False, user=None):
    """Load a locations CSV into ``lab``; return a LocationReport.

    Rows may come in any order; parents are created first. A parent the file doesn't list
    (and LabStock doesn't have) is created with its kind told from its first word ("Room",
    "Shelf"; a temperature names a freezer or fridge: "-80 A"), and the report says which
    line implied it. A path LabStock already has (case-insensitive) is left as it is, and the
    report lists what the file says differently, so re-running creates nothing. Any error
    (unknown kind, owner or temperature, a code that is malformed or already used) names its
    line and nothing is saved: ImportFailed. Unless ``commit``, everything runs and is
    rolled back.
    """
    rows = read_rows(path, required=("path", "kind"))
    report = LocationReport()
    with import_run(commit=commit) as errors:
        plans = _plan(rows, lab, errors)
        if not errors:
            _apply(plans, lab, user, report, errors)
    report.committed = commit
    return report


# ---------------------------------------------------------------------------------------
# import_stock: lab registers and personal sheets (docs/templates/stock-sheet.csv)

STOCK_COLUMNS = (
    "owner_email",
    "item_name",
    "vendor",
    "catalog_number",
    "lot",
    "type",
    "containers",
    "size",
    "location",
    "temperature",
    "position",
    "share",
    "source",
    "date",
    "expiry",
    "opened",
    "mta",
    "notes",
)
YES = {"yes", "y", "true", "1", "x"}
NO = {"", "no", "n", "false", "0"}
_SHARE = {
    "shared": Visibility.SHARED,
    "ask-first": Visibility.ASK_FIRST,
    "ask_first": Visibility.ASK_FIRST,
    "ask first": Visibility.ASK_FIRST,
    "restricted": Visibility.RESTRICTED,
}
_SIZE = re.compile(r"^\s*(?P<amount>\d+(?:[.,]\d+)?)\s*(?P<unit>\S.*)?$")
_STOCK_NAMESPACE = uuid.UUID("2f5b8a1c-9d3e-4c7a-8b6f-0e1d2c3b4a59")


@dataclass
class StockRow:
    """What one sheet row did (or would do): the review CSV is one line per row."""

    line: int
    owner: str
    item_name: str
    action: str = ""  # create | exists | error
    product: str = ""
    resolution: str = ""
    items: int = 0
    aggregate: bool = False
    location: str = ""
    short_ids: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    errors: list = field(default_factory=list)


@dataclass
class StockReport:
    rows: list = field(default_factory=list)
    created_locations: list = field(default_factory=list)  # PlannedLocation
    placeholders: list = field(default_factory=list)  # emails
    committed: bool = False

    @property
    def items(self):
        return sum(row.items for row in self.rows if row.action == "create")


class StockImportFailed(ImportFailed):
    """ImportFailed with the report so far, for the review CSV."""

    def __init__(self, errors, report):
        super().__init__(errors)
        self.report = report


def import_stock(
    path,
    *,
    lab,
    commit=False,
    verified=False,
    create_locations=False,
    placeholders=False,
    user=None,
):
    """Load a stock sheet into ``lab``; return a StockReport (design §3).

    Each row becomes stock through ``services.create_stock_item`` (the ledger records
    "<file> line N"): one item per container up to ``inventory.quartzy_split_max``, else one
    aggregate. Items are unverified unless ``verified``. A row with a location path goes
    there (missing places are created with ``create_locations``, else it's an error); without
    one, into the owner's unsorted container for its temperature. Products resolve through
    the catalog. An ``outside_purchase`` row also records the purchase, so the guard sees it
    as bought before. ``placeholders`` makes former members (unknown owner emails) into
    placeholder users with a leaver membership.

    Re-importing the same file creates nothing: each item's ``client_uuid`` comes from the
    lab, the file's content and the row. Any error names its line and nothing is saved
    (StockImportFailed, with the report for the review CSV). Unless ``commit``, everything
    runs and is rolled back.
    """
    rows = read_rows(path, required=("owner_email", "item_name"))
    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    report = StockReport()
    source_name = Path(path).name
    try:
        with import_run(commit=commit) as errors:
            if create_locations:
                services.lock_location_tree(lab)
            paths = PathIndex(lab, user=user, created=report.created_locations)
            for row in rows:
                result = _import_stock_row(
                    row,
                    lab=lab,
                    paths=paths,
                    digest=digest,
                    source_name=source_name,
                    verified=verified,
                    create_locations=create_locations,
                    placeholders=placeholders,
                    user=user,
                    report=report,
                )
                report.rows.append(result)
                errors.extend(f"line {row.line}: {message}" for message in result.errors)
    except ImportFailed as exc:
        raise StockImportFailed(exc.errors, report) from exc
    report.committed = commit
    return report


def _import_stock_row(row, *, lab, paths, digest, source_name, verified, create_locations,
                      placeholders, user, report):  # fmt: skip
    values = row.values
    result = StockRow(line=row.line, owner=values.get("owner_email", ""), item_name="")
    result.item_name = values.get("item_name", "")
    owner = _stock_owner(values.get("owner_email", ""), lab, placeholders, report, result)
    if not result.item_name:
        result.errors.append("item_name is required")
    item_type = _item_type(values.get("type", ""), result)
    containers = _containers(values.get("containers", ""), result)
    quantity, unit = _size(values.get("size", ""), result)
    occurred_at = _iso_date(values.get("date", ""), "date", result)
    expiry, precision = _expiry(values.get("expiry", ""), result)
    visibility = _visibility(values.get("share", ""), result)
    source = _source(values.get("source", ""), result)
    opened = _flag(values.get("opened", ""), "opened", result)
    mta = _flag(values.get("mta", ""), "mta", result)
    temperature = ""
    try:
        temperature = parse_temperature(values.get("temperature"))
    except ValueError as exc:
        result.errors.append(str(exc))
    location = None
    path_text = values.get("location", "")
    if path_text:
        try:
            location = (
                paths.ensure(path_text, line=row.line)
                if create_locations
                else (paths.get(path_text))
            )
        except services.InventoryError as exc:
            result.errors.append(str(exc))
        else:
            if location is None:
                result.errors.append(
                    f"no location {path_text!r} in LabStock; add it to locations.csv, or "
                    "import with --create-locations"
                )
    if result.errors or owner is None:
        result.action = "error"
        return result

    resolution = resolve_supplier_product(
        vendor_name=values.get("vendor", ""),
        catalog_number=values.get("catalog_number", ""),
        item_name=result.item_name,
        item_type=item_type,
        unit_size=values.get("size", ""),
    )
    product = resolution.product
    result.product, result.resolution = product.name, resolution.status
    if item_type is None and not values.get("type"):
        result.warnings.append("no type given: review the product's type in the admin")
    lot = get_or_create_lot(
        product, values.get("lot", ""), expiry=expiry, expiry_precision=precision or "day"
    )
    order_request = None
    if source == Source.OUTSIDE_PURCHASE:
        order_request = _outside_purchase(lab, owner, values, resolution, occurred_at, digest, row)
    split_max = get_policy("inventory.quartzy_split_max", lab)
    aggregate = containers > split_max
    count = 1 if aggregate else containers
    lifecycle = Lifecycle.AVAILABLE if verified else Lifecycle.UNVERIFIED
    opened_at = None
    if opened:
        opened_at = occurred_at or timezone.now()
        if verified:
            lifecycle = Lifecycle.OPENED
    result.location = location.path_text() if location else _unsorted_text(owner, temperature)
    if location is None and not temperature:
        result.warnings.append("no location and no temperature: unsorted, temperature unknown")
    common = {
        "lab": lab,
        "product": product,
        "supplier_product": resolution.supplier_product,
        "lot": lot,
        "custodian": owner,
        "user": user,
        "source": source,
        "location": location,
        "temperature": None if location else temperature,
        "visibility": visibility,
        "lifecycle": lifecycle,
        "position_label": values.get("position", ""),
        "quantity": quantity,
        "unit": unit,
        "expiry_date": expiry if lot is None else None,
        "expiry_precision": precision or "day",
        "mta": mta,
        "source_order_request": order_request,
        "notes": values.get("notes", ""),
        "occurred_at": occurred_at,
        "opened_at": opened_at,
        "event_source": "import",
        "reason": f"{source_name} line {row.line}",
        "is_aggregate": aggregate,
        "container_count": containers if aggregate else None,
    }
    existed = 0
    for number in range(count):
        client_uuid = uuid.uuid5(_STOCK_NAMESPACE, f"{lab.pk}:{digest}:{row.line}:{number}")
        if StockEvent.objects.filter(client_uuid=client_uuid).exists():
            existed += 1
        try:
            item = services.create_stock_item(client_uuid=client_uuid, **common)
        except services.InventoryError as exc:
            result.errors.append(str(exc))
            result.action = "error"
            return result
        result.short_ids.append(item.short_id)
    result.items, result.aggregate = count, aggregate
    result.action = "exists" if existed == count else "create"
    return result


def _stock_owner(email, lab, placeholders, report, result):
    email = email.strip().lower()
    if not email:
        result.errors.append("owner_email is required")
        return None
    owner = User.objects.filter(email=email).first()
    if owner is not None and services.may_hold_stock(owner, lab):
        return owner
    if not placeholders:
        result.errors.append(
            f"no one in {lab} has the email {email} (import with --placeholders for people "
            "who have left)"
        )
        return None
    if owner is None:
        name = email.split("@")[0].replace(".", " ").title()
        owner = User.objects.create_user(
            email=email, full_name=name, kind=User.Kind.PLACEHOLDER, is_active=False
        )
    Membership.objects.get_or_create(
        user=owner, lab=lab, defaults={"status": Membership.Status.LEAVER}
    )
    if email not in report.placeholders:
        report.placeholders.append(email)
    result.warnings.append(f"{email} is a placeholder (a former member)")
    return owner


def _item_type(key, result):
    key = key.strip().lower()
    if not key:
        return None
    item_type = ItemType.objects.filter(key=key).first()
    if item_type is None:
        keys = ", ".join(ItemType.objects.order_by("key").values_list("key", flat=True))
        result.errors.append(f"unknown type {key!r}; use one of {keys}")
    return item_type


def _containers(value, result):
    if not value.strip():
        return 1
    try:
        number = int(value)
    except ValueError:
        result.errors.append(f"containers must be a whole number, not {value!r}")
        return 1
    if number < 1:
        result.errors.append("containers must be at least 1")
        return 1
    return number


def _size(value, result):
    """("100 uL") -> (Decimal("100"), "uL"); a size that isn't amount + unit is a warning
    and is kept in the notes' place: the review CSV shows it."""
    if not value.strip():
        return None, ""
    match = _SIZE.match(value)
    if match is None:
        result.warnings.append(f"size {value!r} isn't an amount and a unit; left out")
        return None, ""
    amount = Decimal(match["amount"].replace(",", "."))
    return amount, (match["unit"] or "").strip()[:20]


def _iso_date(value, name, result):
    value = value.strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        result.errors.append(f"{name} must be YYYY-MM-DD, not {value!r}")
        return None


def _expiry(value, result):
    value = value.strip()
    if not value:
        return None, None
    if re.fullmatch(r"\d{4}-\d{2}", value):
        try:
            return date.fromisoformat(f"{value}-01"), ExpiryPrecision.MONTH
        except ValueError:
            pass
    else:
        try:
            return date.fromisoformat(value), ExpiryPrecision.DAY
        except ValueError:
            pass
    result.errors.append(f"expiry must be YYYY-MM-DD or YYYY-MM, not {value!r}")
    return None, None


def _visibility(value, result):
    value = value.strip().lower()
    if not value:
        return None  # the lab's default
    if value not in _SHARE:
        result.errors.append(f"share must be shared, ask-first or restricted, not {value!r}")
        return None
    return _SHARE[value]


def _source(value, result):
    value = value.strip().lower() or Source.PURCHASED
    allowed = [s for s in Source.values if s not in (Source.QUARTZY_INVENTORY, Source.IMPORT)]
    if value not in allowed:
        result.errors.append(f"source must be one of {', '.join(allowed)}, not {value!r}")
        return Source.PURCHASED
    return value


def _flag(value, name, result):
    value = value.strip().lower()
    if value in YES:
        return True
    if value not in NO:
        result.errors.append(f"{name} must be yes or no, not {value!r}")
    return False


def _unsorted_text(owner, temperature):
    label = temperature_label(temperature)
    return f"Unsorted: {owner}" + (f", {label}" if label else "")


def _outside_purchase(lab, owner, values, resolution, occurred_at, digest, row):
    """The purchase behind an ``outside_purchase`` row, so the guard counts it as bought
    before. Found again on a re-import by its import key."""
    from apps.quartzy.models import OrderRequest

    key = f"{digest}:{row.line}"
    existing = OrderRequest.objects.filter(
        lab=lab, source=OrderRequest.Source.OUTSIDE, raw_payload__import_key=key
    ).first()
    if existing is not None:
        return existing
    when = (
        timezone.make_aware(datetime.combine(occurred_at, time.min))
        if occurred_at
        else timezone.now()
    )
    return OrderRequest.objects.create(
        lab=lab,
        source=OrderRequest.Source.OUTSIDE,
        status=OrderRequest.Status.RECEIVED,
        item_name=values.get("item_name", "")[:500],
        vendor_name_raw=values.get("vendor", "")[:300],
        catalog_number_raw=values.get("catalog_number", "")[:200],
        unit_size_raw=values.get("size", "")[:200],
        requested_by=owner,
        requested_at=when,
        supplier_product=resolution.supplier_product,
        product=resolution.product,
        resolution_status=resolution.status,
        raw_payload={"import_key": key, "line": row.line},
    )


REVIEW_COLUMNS = (
    "line",
    "action",
    "owner",
    "item_name",
    "product",
    "resolution",
    "items",
    "aggregate",
    "location",
    "short_ids",
    "warnings",
    "errors",
)


def write_review_csv(report, path):
    """One line per sheet row: what was (or would be) done, and why not."""
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(REVIEW_COLUMNS)
        for row in report.rows:
            writer.writerow(
                [
                    row.line,
                    row.action,
                    row.owner,
                    row.item_name,
                    row.product,
                    row.resolution,
                    row.items,
                    "yes" if row.aggregate else "",
                    row.location,
                    " ".join(row.short_ids),
                    "; ".join(row.warnings),
                    "; ".join(row.errors),
                ]
            )
