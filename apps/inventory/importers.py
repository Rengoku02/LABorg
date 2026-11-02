"""CSV imports into the inventory (docs/design/mvp-a.md §3).

Shared by the import commands:
- ``read_rows(path, required=...)``: the rows with their line numbers, header checked
- ``ImportFailed``: what was wrong, one message per line; nothing was saved
- ``import_run(commit=...)``: the dry run. An import does everything inside one transaction
  and rolls it back unless ``commit``, so a dry run reports exactly what a commit would do
- ``PathIndex(lab)``: the lab's locations by path, and the parts of a path made on demand

``import_locations(path, *, lab, commit=False)`` loads docs/templates/locations.csv
(``path, kind, temperature, owner_email, code, notes``) through ``services.create_location``.
"""

import csv
from contextlib import contextmanager
from dataclasses import dataclass, field

from django.db import transaction

from apps.core.models import User

from . import services
from .management.commands.seed_inventory import (
    LOCATION_TYPES_YAML,
    load_yaml,
    location_type_values,
)
from .models import PATH_SEPARATOR, Location, LocationType, parse_temperature, temperature_label
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
