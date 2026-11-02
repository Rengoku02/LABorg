"""Phase 0 baseline: what did duplicate purchases cost before LabStock? (plan §5 Phase 0.3-4)

Pure Python (stdlib, plus PyYAML through apps.catalog.normalization), so
notebooks/baseline_duplicates.ipynb runs without Django. The result is the PI's "before"
figure, so every choice leans towards claiming less, and summarize() lists the assumptions:

- Upper bound: every request for a product that someone else requested in the previous
  ``window_days`` (90) counts once, at its own price, avoidable or not.
- Reviewed figure: only the pairs the wet-lab co-owner labels ``true_dup`` in the review CSV
  (write_review_csv -> spreadsheet -> read_review_csv -> reviewed_summary).

"The same product" uses the guard's tiers (plan §9) on apps.catalog.normalization keys:
T1 is the same catalog key within a vendor family; T2 the same base with another pack size;
T1x the same key in another family, only when ``cross_family_key_ok`` and the Quartzy types
match or the item names are similar (pg_trgm-style similarity of at least 0.2). Requests
that name no vendor (blank, "N/A"...) share UNKNOWN_FAMILY, but their vendors may differ, so
a key they share counts only the way T1x does. Requests with no usable catalog number
(placeholders, Excel damage, a pasted link), a generic key (custom oligos, services: see
generic_catalog_candidates), or no vendor and a key too short for T1x can't be compared and
count as "uncheckable". Only the lab's own Quartzy labs are read (load_requests: the ones the
export lists), never a collaborator's that the token also sees. Cancelled requests are left
out, and a repeat by the same person is reported separately because it is usually a re-order.

Each duplicate pair gets one bucket for why Quartzy's own warning (the same catalog # among
open requests in the same Quartzy Lab) didn't stop it: the first of BUCKETS that applies.

1. same_number_same_lab_open: the same catalog # as typed, the same Lab, and the earlier
   request still open when the later one was made, so Quartzy should have warned.
   unknown_open_state: the same, except it can't be told whether the earlier one was open.
2. different_quartzy_labs
3. format_variant: the same key, typed differently ("11965-092" and "11965092")
4. pack_variant: T2
5. received_earlier: the earlier request was received ``received_days`` (14) or more before
6. other_vendor_equivalent: T1x
7. unknown

Whether the earlier request was open comes from status dates (the Quartzy UI "All Time"
export, see attach_status_dates), else from its current status: one that is still open today
was open then (an Admin reverting Received to New would break this; find_clusters'
``infer_open_from_current_status=False`` gives the strict reading). A RECEIVED one without
dates counts as received long before only when it was requested at least ``received_days +
assumed_delivery_days`` (28) days earlier; otherwise the open state is unknown.
"""

import contextlib
import csv
import io
import json
import re
import statistics
import warnings
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple

from apps.catalog.normalization import (
    UNKNOWN_FAMILY,
    NormalizedCatalog,
    cross_family_key_ok,
    normalize_catalog,
    normalize_vendor_name,
    vendor_family_for,
)
from apps.guard.anonymize import NameScrubber
from apps.quartzy.money import MONEY_UNITS, money_amount, parse_quantity, to_decimal

OPEN_STATUSES = frozenset({"CREATED", "APPROVED", "ORDERED", "BACKORDERED"})
BUCKETS = (
    "same_number_same_lab_open", "unknown_open_state", "different_quartzy_labs",
    "format_variant", "pack_variant", "received_earlier", "other_vendor_equivalent", "unknown",
)  # fmt: skip
TIERS = ("T1", "T2", "T1x")
LABELS = ("true_dup", "related_not_dup", "false")
# The first three are NormalizedCatalog.no_key_reason values.
UNCHECKABLE_REASONS = ("placeholder", "excel_mangled", "too_long", "generic", "no_vendor")
# Logical names for attach_status_dates' column mapping.
STATUS_FIELDS = (
    "id", "requested_at", "approved_at", "ordered_at", "received_at", "cancelled_at", "lab",
    "item_name", "catalog_number", "vendor_name", "requester", "price",
)  # fmt: skip
_DATE_FIELDS = ("approved_at", "ordered_at", "received_at", "cancelled_at")
# The same number as apps.core.policies.DEFAULTS["guard.cross_family_min_name_similarity"].
CROSS_FAMILY_MIN_SIMILARITY = 0.2
_CENT = Decimal("0.01")
_UUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
_NUMERIC_DATE = re.compile(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4}|\d{2})\b")


@dataclass(frozen=True)
class RequestRecord:
    """One Quartzy order request, reduced to what the baseline compares."""

    id: str
    status: str
    item_name: str
    vendor_name: str | None
    # A vendors.yaml family slug; UNKNOWN_FAMILY ("unknown") when no vendor is named (blank,
    # "N/A", "TBD"...); else "?" plus the normalized name of a vendor vendors.yaml doesn't
    # list, so requests from one unlisted vendor still compare with each other.
    vendor_family: str
    catalog: NormalizedCatalog
    requester_key: str | None  # the requested_by id, else the created_by id
    requester: str
    requester_email: str | None
    lab_id: str | None
    lab_name: str | None
    type_name: str | None
    requested_at: datetime | None
    updated_at: datetime | None
    price: Decimal | None  # major units (dollars)
    currency: str | None
    # How ``price`` was found: "minor" or "major" (total_price in the configured unit),
    # "auto:minor" or "auto:major" (unit decided per request), "unit_price*quantity", "none".
    price_basis: str
    unit_price: Decimal | None
    quantity: Decimal | None
    app_url: str | None
    # Status-change dates from the Quartzy UI export (attach_status_dates).
    has_status_dates: bool = False
    approved_at: datetime | None = None
    ordered_at: datetime | None = None
    received_at: datetime | None = None
    cancelled_at: datetime | None = None
    # First, last and full names of requested_by and created_by, for anonymized output to
    # remove from item and vendor names (a full name is how Chinese, Japanese and Korean
    # names are found).
    person_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class DuplicatePair:
    """A later request and the earlier request for the same product it repeats.

    Same-requester repeats are not classified: their ``bucket`` is None.
    """

    earlier: RequestRecord
    later: RequestRecord
    tier: str
    gap_days: float
    bucket: str | None = None
    reasons: tuple[str, ...] = ()  # every bucket that applies, in BUCKETS order
    earlier_state: str = "unknown"  # the earlier request when the later one was made
    state_evidence: str = ""  # "status dates", "still open today", "age heuristic" or ""

    @property
    def pair_id(self):
        return f"{self.earlier.id}:{self.later.id}"

    @property
    def amount(self):
        """What the pair adds to the upper bound: the later request's price."""
        return self.later.price

    @property
    def same_requester(self):
        return self.earlier.requester_key == self.later.requester_key


@dataclass(frozen=True)
class Cluster:
    """Requests joined by duplicate pairs: one product bought by several people."""

    cluster_id: str
    pairs: tuple[DuplicatePair, ...]
    records: tuple[RequestRecord, ...]

    @property
    def amount(self):
        return _total(self.pairs)


@dataclass(frozen=True)
class ClusterResult:
    pairs: tuple[DuplicatePair, ...]  # different requesters; at most one per later request
    repeats: tuple[DuplicatePair, ...]  # the same requester
    clusters: tuple[Cluster, ...]  # largest amount first
    uncheckable: dict  # reason -> records that can't be compared by catalog number
    excluded: dict  # "cancelled" / "undated" / "no_requester" -> count
    compared: int
    records: tuple[RequestRecord, ...]
    settings: dict


class GenericCandidate(NamedTuple):
    vendor_family: str
    catalog_key: str
    n_names: int
    examples: tuple[str, ...]


class Review(NamedTuple):
    label: str | None
    notes: str


@dataclass(frozen=True)
class StatusJoin:
    """How the rows of a Quartzy UI export joined to the API's requests."""

    rows: int
    by_id: int
    by_fields: int
    ambiguous: int
    unmatched: int
    bad_dates: int
    date_order: str

    @property
    def attached(self):
        return self.by_id + self.by_fields


# ---------------------------------------------------------------------------------------
# Loading an export


def read_manifest(export_dir):
    """The export's manifest.json, or {} when there is none."""
    return _read_json(Path(export_dir) / "manifest.json", {})


def export_money_unit(manifest):
    """The money unit a manifest pins (top level, else the probe facts), or None."""
    facts = manifest.get("probe_facts") or {}
    return manifest.get("money_unit") or facts.get("money_unit") or None


def load_requests(export_dir, money_unit=None, lab_ids=None):
    """Read order_requests.jsonl from a ``scripts/quartzy_probe.py export`` directory.

    Only requests from the Quartzy labs in ``lab_ids`` (a list, or comma-separated as in
    QUARTZY_LAB_IDS) are kept, by default the ones the manifest lists as exported
    (``lab_ids``): the token also sees any collaborator's lab, and that group's purchases must
    not end up in this lab's figure. A request that names no lab is kept. When neither says
    which labs are the lab's (an export made before the probe recorded them), the requests
    must all be from one lab; otherwise ValueError lists the labs to choose from. An id that
    isn't one of the export's labs is a ValueError too.

    ``money_unit`` ("minor", "major" or "auto") overrides the unit the manifest pins; with
    neither, "auto" decides per request. A total whose unit can't be decided falls back to
    unit_price x quantity, and ``price_basis`` records which was used, never silently.
    """
    folder = Path(export_dir)
    manifest = read_manifest(folder)
    unit = money_unit or export_money_unit(manifest) or "auto"
    if unit not in MONEY_UNITS:
        raise ValueError(f"money_unit must be one of {MONEY_UNITS}, not {unit!r}")
    labs = _names_by_id(_read_json(folder / "labs.json", []))
    types = _names_by_id(_read_json(folder / "types.json", []))
    path = folder / "order_requests.jsonl"
    records, repeated = {}, 0
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{number}: not JSON ({exc.msg})") from exc
            if not isinstance(raw, dict) or not raw.get("id"):
                raise ValueError(f"{path}:{number}: not an order request (no id)")
            record = _record(raw, unit, labs, types)
            repeated += record.id in records
            records[record.id] = record
    if repeated:
        warnings.warn(
            f"{path}: {repeated} request ids appear more than once; using the last copy",
            stacklevel=2,
        )
    wanted = manifest.get("lab_ids") if lab_ids is None else lab_ids
    return _of_labs(list(records.values()), wanted, labs, folder)


def _of_labs(records, lab_ids, lab_names, folder):
    """The records from the Quartzy labs ``lab_ids`` (see load_requests)."""
    if isinstance(lab_ids, str):
        lab_ids = lab_ids.split(",")  # as QUARTZY_LAB_IDS writes them
    wanted = {_id(lab_id) for lab_id in lab_ids or ()} - {None, ""}
    found = Counter(r.lab_id for r in records if r.lab_id)
    if not wanted:
        if len(found) > 1:
            raise ValueError(
                f"{folder} holds requests from {len(found)} Quartzy labs, and its manifest "
                "doesn't say which are this lab's: pass their ids as lab_ids (LAB_IDS in the "
                f"notebook). Its labs: {_lab_listing(records, found, lab_names)}"
            )
        return records
    unknown = sorted(wanted - {*found, *lab_names})
    if unknown:
        raise ValueError(
            f"{folder} has no Quartzy lab {', '.join(unknown)}; its labs: "
            f"{_lab_listing(records, found, lab_names)}"
        )
    return [r for r in records if r.lab_id is None or r.lab_id in wanted]


def _lab_listing(records, found, lab_names):
    """'<id> (<name>, <n> requests), ...' for an error message, most requests first."""
    names = {**lab_names, **{r.lab_id: r.lab_name for r in records if r.lab_id and r.lab_name}}
    return ", ".join(
        f"{lab_id} ({names.get(lab_id) or 'no name'}, {_count(found[lab_id], 'request')})"
        for lab_id in sorted({*found, *lab_names}, key=lambda lab_id: (-found[lab_id], lab_id))
    )


def _read_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _names_by_id(items):
    return {_id(i["id"]): i.get("name") for i in items if isinstance(i, dict) and i.get("id")}


def _id(value):
    return str(value).strip().lower() if value else None


def _text(value):
    return " ".join(str(value).split()) if value is not None else ""


def _record(raw, unit, labs, types):
    people = [p for p in (raw.get("requested_by"), raw.get("created_by")) if isinstance(p, dict)]
    person = next((p for p in people if p.get("id")), None) or {}
    names = []
    for someone in people:
        first, last = _text(someone.get("first_name")), _text(someone.get("last_name"))
        names += [first, last, f"{first} {last}".strip()]
    vendor_name = _text(raw.get("vendor_name")) or None
    slug = vendor_family_for(vendor_name)
    lab = raw.get("lab") if isinstance(raw.get("lab"), dict) else {}
    qtype = raw.get("type") if isinstance(raw.get("type"), dict) else {}
    price, currency, basis = _price(raw, unit)
    return RequestRecord(
        id=_id(raw["id"]),
        status=_text(raw.get("status")).upper(),
        item_name=_text(raw.get("item_name")),
        vendor_name=vendor_name,
        vendor_family=slug or "?" + (normalize_vendor_name(vendor_name) or ""),
        catalog=normalize_catalog(raw.get("catalog_number"), slug),
        requester_key=_id(person.get("id")),
        requester=_person_name(person),
        requester_email=_text(person.get("email")).lower() or None,
        lab_id=_id(lab.get("id")),
        lab_name=lab.get("name") or labs.get(_id(lab.get("id"))),
        type_name=qtype.get("name") or types.get(_id(qtype.get("id"))),
        requested_at=_datetime(raw.get("requested_at")),
        updated_at=_datetime(raw.get("updated_at")),
        price=price,
        currency=currency,
        price_basis=basis,
        unit_price=to_decimal(raw.get("unit_price")),
        quantity=parse_quantity(raw.get("quantity")),
        app_url=raw.get("app_url"),
        person_names=tuple(dict.fromkeys(filter(None, names))),
    )


def _person_name(person):
    name = " ".join(_text(person.get(part)) for part in ("first_name", "last_name")).strip()
    return name or _text(person.get("email"))


def _price(raw, unit):
    """(price in major units or None, currency, basis) for one raw request."""
    amount, currency, used = money_amount(
        raw.get("total_price"), unit, raw.get("unit_price"), raw.get("quantity")
    )
    if amount is not None:
        return amount, currency, f"auto:{used}" if unit == "auto" else used
    unit_price, quantity = to_decimal(raw.get("unit_price")), parse_quantity(raw.get("quantity"))
    if unit_price is not None and quantity is not None:
        return unit_price * quantity, currency, "unit_price*quantity"
    return None, currency, "none"


def _datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------------------
# Status dates from the Quartzy UI "All Time" requests export


def attach_status_dates(records, csv_path, columns, *, date_format=None, tolerance_days=1):
    """Add status-change dates from a Quartzy UI "All Time" requests export (CSV).

    ``columns`` maps STATUS_FIELDS names to that file's headers (case-insensitive), e.g.
    ``{"requested_at": "Date Requested", "received_at": "Date Received", "item_name": "Item
    Name", "catalog_number": "Catalog #", "requester": "Requested By"}``. A row joins on a
    request id found in its "id" column (a request URL works), else on every mapped field
    among lab, item name, catalog #, vendor, requester and price plus the requested date
    within ``tolerance_days`` (the UI shows local dates, the API UTC). A row that fits several
    requests is left out, not guessed. "03/04/2026" is read month-first unless some value
    only works day-first; ``date_format`` (strptime) overrides that. Returns
    ``(records, StatusJoin)``.
    """
    columns = {field: header for field, header in dict(columns).items() if header}
    unknown = sorted(set(columns) - set(STATUS_FIELDS))
    if unknown:
        raise ValueError(f"Unknown status columns {unknown}; use names from {STATUS_FIELDS}")
    if not set(columns) & set(_DATE_FIELDS):
        raise ValueError(f"Map at least one of {_DATE_FIELDS} to a column")
    headers, rows = _read_csv(csv_path)
    lookup = {header.casefold(): header for header in headers}
    missing = [header for header in columns.values() if header.casefold() not in lookup]
    if missing:
        raise ValueError(f"{csv_path} has no column {missing}; its columns are {headers}")
    cells = [
        {field: row.get(lookup[header.casefold()]) or "" for field, header in columns.items()}
        for row in rows
    ]
    date_fields = [field for field in ("requested_at", *_DATE_FIELDS) if field in columns]
    order = date_format or _date_order(cell[field] for cell in cells for field in date_fields)
    by_id = {record.id: record for record in records}
    join_by_fields = "requested_at" in columns and bool(
        {"item_name", "catalog_number"} & {*columns}
    )
    index = defaultdict(list)
    for record in records:
        if join_by_fields and record.requested_at:
            index[_join_key(record.item_name, record.catalog.raw, columns)].append(record)

    found, counts = {}, Counter()
    for cell in cells:
        dates = {}
        for field in date_fields:
            try:
                dates[field] = _csv_datetime(cell[field], order)
            except ValueError:
                counts["bad_dates"] += 1
                dates[field] = None
        record = _record_by_id(cell.get("id"), by_id)
        how = "by_id"
        if record is None and join_by_fields and dates["requested_at"]:
            key = _join_key(cell.get("item_name"), cell.get("catalog_number"), columns)
            candidates = [
                r
                for r in index.get(key, ())
                if r.id not in found and _fits(r, cell, dates["requested_at"], tolerance_days)
            ]
            if len(candidates) > 1:
                same_day = dates["requested_at"].date()
                exact = [r for r in candidates if r.requested_at.date() == same_day]
                candidates = exact if len(exact) == 1 else candidates
            if len(candidates) > 1:
                counts["ambiguous"] += 1
                continue
            record, how = (candidates[0] if candidates else None), "by_fields"
        if record is None:
            counts["unmatched"] += 1
        elif record.id in found:
            counts["ambiguous"] += 1  # a second row for one request
        else:
            found[record.id] = {field: dates.get(field) for field in _DATE_FIELDS}
            counts[how] += 1

    joined = [
        replace(record, has_status_dates=True, **found[record.id]) if record.id in found else record
        for record in records
    ]
    return joined, StatusJoin(
        rows=len(cells),
        by_id=counts["by_id"],
        by_fields=counts["by_fields"],
        ambiguous=counts["ambiguous"],
        unmatched=counts["unmatched"],
        bad_dates=counts["bad_dates"],
        date_order=order,
    )


def _read_text(path):
    data = Path(path).read_bytes()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")  # Excel's "CSV" on Windows


def _read_csv(path):
    """(headers, rows) of a CSV written by Quartzy or saved from Excel (comma or semicolon)."""
    text = _read_text(path)
    header = text.split("\n", 1)[0]
    reader = csv.DictReader(io.StringIO(text), delimiter=max(",;\t", key=header.count))
    rows = [{(key or "").strip(): value for key, value in row.items()} for row in reader]
    return [name.strip() for name in reader.fieldnames or ()], rows


def _record_by_id(value, by_id):
    if not value:
        return None
    for candidate in [*_UUID.findall(value), value.strip()]:
        if candidate.lower() in by_id:
            return by_id[candidate.lower()]
    return None


def _loose(text):
    return re.sub(r"[^0-9a-z]", "", (text or "").casefold())


def _tokens(text):
    return frozenset(re.findall(r"[^\W_]+", (text or "").casefold()))


def _join_key(item_name, catalog_raw, columns):
    return (
        _loose(item_name) if "item_name" in columns else "",
        _loose(catalog_raw) if "catalog_number" in columns else "",
    )


def _fits(record, cell, requested, tolerance_days):
    if abs((record.requested_at.date() - requested.date()).days) > tolerance_days:
        return False
    labs = {_loose(record.lab_name), _loose(record.lab_id)}
    if "lab" in cell and _loose(cell["lab"]) not in labs:
        return False
    if "vendor_name" in cell and _loose(cell["vendor_name"]) != _loose(record.vendor_name):
        return False
    if "requester" in cell:
        wanted = _tokens(cell["requester"])
        if wanted != _tokens(record.requester) and not (
            record.requester_email and wanted == _tokens(record.requester_email)
        ):
            return False
    if "price" in cell and (price := to_decimal(cell["price"])) is not None:
        known = [p for p in (record.unit_price, record.price) if p is not None]
        if not any(abs(p - price) <= _CENT for p in known):
            return False
    return True


def _date_order(values):
    """Month first unless a numeric date only works day first (Quartzy imports MM/DD/YYYY)."""
    parts = [(int(m[1]), int(m[2])) for v in values if (m := _NUMERIC_DATE.match(v.strip()))]
    if any(first > 12 for first, _ in parts) and not any(second > 12 for _, second in parts):
        return "day/month"
    return "month/day"


def _csv_datetime(value, order):
    text = (value or "").strip()
    if not text:
        return None
    if order not in ("month/day", "day/month"):
        return datetime.strptime(text, order).replace(tzinfo=UTC)
    if match := _NUMERIC_DATE.match(text):
        first, second, year = (int(group) for group in match.groups())
        month, day = (first, second) if order == "month/day" else (second, first)
        return datetime(year + 2000 if year < 100 else year, month, day, tzinfo=UTC)
    if parsed := _datetime(text):
        return parsed
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y"):
        with contextlib.suppress(ValueError):
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
    raise ValueError(f"Unreadable date {text!r}")


def status_paths(records, *, as_of=None, stale_days=30):
    """Phase 0 step 3: how far requests get through Quartzy's statuses, and how fast.

    ``as_of`` is the export time (the manifest's "started_at"); by default the latest
    timestamp in the records. The timings need status dates and are None without them.
    """
    records = list(records)
    as_of = _datetime(as_of) if isinstance(as_of, str) else as_of
    if as_of is None:
        moments = (t for r in records for t in (r.requested_at, r.updated_at) if t)
        as_of = max(moments, default=None)

    def older(record, days):
        return bool(as_of and record.requested_at) and (as_of - record.requested_at).days > days

    reached = sum(
        r.status in {"ORDERED", "BACKORDERED", "RECEIVED"} or bool(r.ordered_at or r.received_at)
        for r in records
    )
    stuck = sum(r.status == "CREATED" and older(r, stale_days) for r in records)
    stale = sum(
        (r.status in {"CREATED", "APPROVED"} and older(r, 30))
        or (r.status in {"ORDERED", "BACKORDERED"} and older(r, 60))
        for r in records
    )
    return {
        "as_of": as_of.isoformat() if as_of else None,
        "requests": len(records),
        "by_status": dict(Counter(r.status or "(none)" for r in records).most_common()),
        "reached_ordered_or_received": _share(reached, len(records)),
        "stuck_in_created": {**_share(stuck, len(records)), "older_than_days": stale_days},
        "stale_open": {
            **_share(stale, len(records)),
            "rule": "CREATED/APPROVED older than 30 days, ORDERED/BACKORDERED older than 60",
        },
        "with_status_dates": sum(r.has_status_dates for r in records),
        "median_days_to_ordered": _median_days(records, "ordered_at"),
        "median_days_to_received": _median_days(records, "received_at"),
    }


def _share(count, total):
    return {"count": count, "share": round(count / total, 3) if total else None}


def _median_days(records, field):
    gaps = [
        _days(getattr(r, field) - r.requested_at)
        for r in records
        if getattr(r, field) and r.requested_at
    ]
    return round(statistics.median(gaps), 1) if gaps else None


# ---------------------------------------------------------------------------------------
# Generic keys, pairs and clusters


def generic_catalog_candidates(records, min_distinct_names=4):
    """Catalog keys that carry many different item names: custom oligos, services, fees.

    They seed GenericCatalogKey. Check them by eye: a popular product typed under several
    names shows up too. Pass the keys you keep to find_clusters() as ``generic_keys``.
    """
    names = defaultdict(dict)
    for record in records:
        if record.catalog.key and record.item_name:
            seen = names[record.vendor_family, record.catalog.key]
            seen.setdefault(_loose(record.item_name), record.item_name)
    found = [
        GenericCandidate(family, key, len(seen), tuple(seen.values())[:5])
        for (family, key), seen in names.items()
        if len(seen) >= min_distinct_names
    ]
    return sorted(found, key=lambda c: (-c.n_names, c.vendor_family, c.catalog_key))


def find_clusters(
    records,
    *,
    window_days=90,
    generic_keys=(),
    received_days=14,
    assumed_delivery_days=14,
    infer_open_from_current_status=True,
):
    """Pair each request with an earlier request for the same product (see the module doc).

    ``generic_keys`` holds (vendor_family, catalog_key) pairs, GenericCandidates, or bare
    keys that are generic in every family. A later request with candidates from several
    people pairs with the one whose bucket comes first, then the stronger tier, then the
    most recent. With ``infer_open_from_current_status=False`` an earlier request without
    status dates is never taken to have been open, even if it is still open today.
    """
    generic = _generic_key_set(generic_keys)
    settings = {
        "window_days": window_days,
        "received_days": received_days,
        "assumed_delivery_days": assumed_delivery_days,
        "infer_open_from_current_status": infer_open_from_current_status,
        "generic_keys": sorted(f"{f or '*'} {k}" for f, k in generic),
    }
    uncheckable = {reason: [] for reason in UNCHECKABLE_REASONS}
    excluded = Counter()
    usable = []
    for record in records:
        if record.status == "CANCELLED":
            excluded["cancelled"] += 1
        elif reason := _uncheckable_reason(record, generic):
            uncheckable[reason].append(record)
        elif record.requested_at is None:
            excluded["undated"] += 1
        elif record.requester_key is None:
            excluded["no_requester"] += 1
        else:
            usable.append(record)
    usable.sort(key=_when)
    by_key, by_base, by_bare = defaultdict(list), defaultdict(list), defaultdict(list)
    for position, record in enumerate(usable):
        by_key[record.vendor_family, record.catalog.key].append(position)
        by_base[record.vendor_family, record.catalog.base].append(position)
        if cross_family_key_ok(record.catalog.key):
            by_bare[record.catalog.key].append(position)

    window = timedelta(days=window_days)
    pairs, repeats = [], []
    for position, later in enumerate(usable):
        key, family = later.catalog.key, later.vendor_family
        same_key = _recent(usable, by_key[family, key], position, window)
        if family == UNKNOWN_FAMILY:
            # Two requests that name no vendor may still be from different vendors, so their
            # key counts as T1x does (keys too short for that are already uncheckable).
            found = [(e, "T1x") for e in same_key if _related(e, later)]
        else:
            found = [(e, "T1") for e in same_key]
        found += [
            (e, "T2")
            for e in _recent(usable, by_base[family, later.catalog.base], position, window)
            if e.catalog.key != key
        ]
        found += [
            (e, "T1x")
            for e in _recent(usable, by_bare.get(key, ()), position, window)
            if e.vendor_family != family and _related(e, later)
        ]
        others = [
            _duplicate(e, later, tier, settings)
            for e, tier in found
            if e.requester_key != later.requester_key
        ]
        if others:
            pairs.append(min(others, key=_rank))
        elif found:
            repeats.append(
                min(
                    (DuplicatePair(e, later, tier, _gap(e, later)) for e, tier in found),
                    key=lambda p: (TIERS.index(p.tier), p.gap_days),
                )
            )
    return ClusterResult(
        pairs=tuple(pairs),
        repeats=tuple(repeats),
        clusters=_clusters(pairs),
        uncheckable={reason: tuple(members) for reason, members in uncheckable.items()},
        excluded={name: excluded[name] for name in ("cancelled", "undated", "no_requester")},
        compared=len(usable),
        records=tuple(records),
        settings=settings,
    )


def _generic_key_set(items):
    keys = set()
    for item in items:
        family, key = (None, item) if isinstance(item, str) else (item[0], item[1])
        # Keyed as the requests are, under the family's rule (as GenericCatalogKey does): ATCC
        # keeps the dot in "CCL2.2", and "CST #2118S" is 2118S.
        normalized = normalize_catalog(str(key), family).key
        keys.add((family, normalized or re.sub(r"[^0-9A-Z]", "", str(key).upper())))
    return frozenset(keys)


def _uncheckable_reason(record, generic):
    catalog, key = record.catalog, record.catalog.key
    if key is None:
        return catalog.no_key_reason or (
            "placeholder" if catalog.is_placeholder else "excel_mangled"
        )
    if (record.vendor_family, key) in generic or (None, key) in generic:
        return "generic"
    if record.vendor_family == UNKNOWN_FAMILY and not cross_family_key_ok(key):
        return "no_vendor"  # with no vendor named, only a T1x-length key can match
    return None


def _when(record):
    return record.requested_at or datetime.min.replace(tzinfo=UTC), record.id


def _days(delta):
    return round(delta.total_seconds() / 86400, 1)


def _gap(earlier, later):
    return _days(later.requested_at - earlier.requested_at)


def _recent(usable, positions, position, window):
    """The records at ``positions`` before ``position`` and at most ``window`` older."""
    at = usable[position].requested_at
    return [usable[p] for p in positions if p < position and at - usable[p].requested_at <= window]


def _related(a, b):
    """T1x needs more than a shared number: the same Quartzy type, or similar item names."""
    if a.type_name and b.type_name and a.type_name.casefold() == b.type_name.casefold():
        return True
    return trigram_similarity(a.item_name, b.item_name) >= CROSS_FAMILY_MIN_SIMILARITY


def trigram_similarity(a, b):
    """PostgreSQL pg_trgm's similarity(): shared over all trigrams of the words, each word
    padded with two spaces in front and one behind."""
    first, second = _trigrams(a), _trigrams(b)
    if not first or not second:
        return 0.0
    return len(first & second) / len(first | second)


def _trigrams(text):
    grams = set()
    for word in _tokens(text):
        padded = f"  {word} "
        grams.update(padded[i : i + 3] for i in range(len(padded) - 2))
    return grams


def _earlier_state(earlier, at, infer_open):
    """Was the earlier request open at ``at``? (state, evidence)."""
    if earlier.received_at is not None:
        return ("received" if earlier.received_at <= at else "open"), "status dates"
    if earlier.status in OPEN_STATUSES and (earlier.has_status_dates or infer_open):
        return "open", "status dates" if earlier.has_status_dates else "still open today"
    return "unknown", ""


def _duplicate(earlier, later, tier, settings):
    gap = later.requested_at - earlier.requested_at
    infer_open = settings["infer_open_from_current_status"]
    state, evidence = _earlier_state(earlier, later.requested_at, infer_open)
    received = timedelta(days=settings["received_days"])
    long_before = False
    if state == "received":
        long_before = later.requested_at - earlier.received_at >= received
    elif state == "unknown" and earlier.status == "RECEIVED":
        if gap >= received + timedelta(days=settings["assumed_delivery_days"]):
            state, evidence, long_before = "received", "age heuristic", True
    same_lab = earlier.lab_id == later.lab_id if earlier.lab_id and later.lab_id else None
    # Quartzy's warning is taken to compare the catalog number as typed (trimmed).
    same_number = (earlier.catalog.raw or "").strip() == (later.catalog.raw or "").strip()
    reasons = [
        bucket
        for bucket, applies in (
            ("same_number_same_lab_open", same_lab and same_number and state == "open"),
            ("unknown_open_state", same_lab and same_number and state == "unknown"),
            ("different_quartzy_labs", same_lab is False),
            ("format_variant", tier == "T1" and not same_number),
            ("pack_variant", tier == "T2"),
            ("received_earlier", long_before),
            ("other_vendor_equivalent", tier == "T1x"),
        )
        if applies
    ] or ["unknown"]
    return DuplicatePair(
        earlier, later, tier, _days(gap), reasons[0], tuple(reasons), state, evidence
    )


def _rank(pair):
    return BUCKETS.index(pair.bucket), TIERS.index(pair.tier), pair.gap_days, pair.earlier.id


def _total(pairs):
    return sum((p.amount for p in pairs if p.amount is not None), Decimal(0))


def _clusters(pairs):
    """Group pairs that share a request, largest amount first."""
    parent = {}

    def root(node):
        while parent.setdefault(node, node) != node:
            node = parent[node]
        return node

    for pair in pairs:
        parent[root(pair.earlier.id)] = root(pair.later.id)
    groups = defaultdict(list)
    for pair in pairs:
        groups[root(pair.later.id)].append(pair)
    ordered = sorted(groups.values(), key=lambda g: (-_total(g), min(_when(p.earlier) for p in g)))
    clusters = []
    for number, group in enumerate(ordered, 1):
        records = {r.id: r for pair in group for r in (pair.earlier, pair.later)}
        members = tuple(sorted(records.values(), key=_when))
        clusters.append(Cluster(f"C{number:03d}", tuple(group), members))
    return tuple(clusters)


# ---------------------------------------------------------------------------------------
# Summaries


def summarize(result, *, manifest=None, anonymize=True, top_n=15):
    """The baseline in numbers: counts, the upper bound and its breakdowns, the products
    behind most of it, and the assumptions it rests on.

    With ``anonymize`` (the default) the summary can be shown to the PI and at a lab meeting:
    it names nobody and never counts duplicates per person (plan §11). A product's
    ``people`` is how many different people requested it, with no label that could follow
    anyone from one product to the next. The names of everyone in ``result.records``
    (requested_by and created_by), email addresses and ids are taken out of item, vendor,
    catalog, type, lab and generic-key names (apps.guard.anonymize), and dates are months.
    Without it, ``people`` lists the requesters' names, and dates are days.
    """
    pairs, records = result.pairs, result.records
    scrub = _scrubber(records) if anonymize else _as_is
    date = "%Y-%m" if anonymize else "%Y-%m-%d"
    considered = [r for r in records if r.status != "CANCELLED"]
    unchecked = [r for found in result.uncheckable.values() for r in found]
    window = result.settings["window_days"]
    currencies = {r.currency for r in records if r.price is not None and r.currency}
    repeat_groups = defaultdict(list)
    for pair in result.repeats:
        repeat_groups[pair.later.vendor_family, pair.later.catalog.base].append(pair)
    top_repeats = sorted(repeat_groups.values(), key=lambda g: (-_total(g), _when(g[0].later)))
    return {
        "counts": {
            "requests": len(records),
            **result.excluded,
            "uncheckable": len(unchecked),
            "compared": result.compared,
            "duplicate_pairs": len(pairs),
            "clusters": len(result.clusters),
            "same_requester_repeats": len(result.repeats),
        },
        "upper_bound": {
            "amount": _money(_total(pairs)),
            "pairs": len(pairs),
            "unpriced_pairs": sum(p.amount is None for p in pairs),
            "currency": currencies.pop() if len(currencies) == 1 else None,
        },
        "by_bucket": _breakdown(pairs, lambda p: p.bucket, BUCKETS),
        "by_tier": _breakdown(pairs, lambda p: p.tier, TIERS),
        "by_type": _breakdown(pairs, lambda p: scrub(p.later.type_name) or "(no type)"),
        "by_month": dict(
            sorted(_breakdown(pairs, lambda p: f"{p.later.requested_at:%Y-%m}").items())
        ),
        "by_gap": _breakdown(pairs, lambda p: _gap_band(p.gap_days, window), _gap_bands(window)),
        "median_gap_days": statistics.median(p.gap_days for p in pairs) if pairs else None,
        "top_products": [
            {
                "cluster": cluster.cluster_id,
                **_product(cluster.records, scrub),
                "requests": len(cluster.records),
                "pairs": len(cluster.pairs),
                "amount": _money(cluster.amount),
                "buckets": dict(Counter(p.bucket for p in cluster.pairs).most_common()),
                "people": _people(cluster.records, anonymize),
                "first": f"{cluster.records[0].requested_at:{date}}",
                "last": f"{cluster.records[-1].requested_at:{date}}",
            }
            for cluster in result.clusters[:top_n]
        ],
        "uncheckable": {
            "requests": len(unchecked),
            "of_requests": len(considered),
            "share": _ratio(len(unchecked), len(considered)),
            "amount": _money(_spend(unchecked)),
            "amount_share": _ratio(_spend(unchecked), _spend(considered)),
            "by_reason": {reason: len(found) for reason, found in result.uncheckable.items()},
        },
        "same_requester_repeats": {
            "pairs": len(result.repeats),
            "amount": _money(_total(result.repeats)),
            "top_products": [
                {
                    **_product([r for p in group for r in (p.earlier, p.later)], scrub),
                    "repeats": len(group),
                    "amount": _money(_total(group)),
                }
                for group in top_repeats[:5]
            ],
        },
        "assumptions": _assumptions(result, manifest, scrub),
    }


def _money(amount):
    return Decimal(amount).quantize(_CENT)


def _spend(records):
    return sum((r.price for r in records if r.price is not None), Decimal(0))


def _ratio(part, whole):
    return round(float(part / whole), 3) if whole else None


def _breakdown(pairs, key, order=()):
    """{name: {"pairs", "amount"}} in ``order``, else largest amount first."""
    rows = {name: [0, Decimal(0)] for name in order}
    for pair in pairs:
        row = rows.setdefault(key(pair), [0, Decimal(0)])
        row[0] += 1
        row[1] += pair.amount or 0
    names = list(rows) if order else sorted(rows, key=lambda name: (-rows[name][1], name))
    return {name: {"pairs": rows[name][0], "amount": _money(rows[name][1])} for name in names}


def _gap_edges(window_days):
    return [*(edge for edge in (7, 30, 60, 90, 180, 365) if edge < window_days), window_days]


def _gap_bands(window_days):
    edges = _gap_edges(window_days)
    return [f"{low}-{high} days" for low, high in zip([0, *edges], edges, strict=False)]


def _gap_band(gap_days, window_days):
    bands = _gap_bands(window_days)
    edges = zip(bands, _gap_edges(window_days), strict=True)
    return next((band for band, edge in edges if gap_days <= edge), bands[-1])


def _product(records, scrub):
    def common(values):
        counts = Counter(value for value in values if value)
        return scrub(counts.most_common(1)[0][0]) if counts else ""

    return {
        "product": common(r.item_name for r in records),
        "vendor": common(r.vendor_name for r in records),
        "catalog": common(r.catalog.cleaned for r in records),
        "type": common(r.type_name for r in records),
    }


def _scrubber(records):
    """Removes everyone named in ``records`` (requested_by and created_by) from free text."""
    return NameScrubber(name for r in records for name in (*r.person_names, r.requester))


def _as_is(text):
    return text


def _people(records, anonymize):
    """How many different people requested; without ``anonymize``, their names."""
    if anonymize:
        return len({r.requester_key for r in records})
    return sorted({r.requester or "(unknown)" for r in records})


def _cluster_labels(clusters):
    """(cluster id, requester key) -> "C001 person A"..., lettered by first request and
    afresh in each cluster, so that no label follows anyone from one cluster to another."""
    labels = {}
    for cluster in clusters:
        keys = dict.fromkeys(r.requester_key for r in cluster.records)
        for number, key in enumerate(keys):
            labels[cluster.cluster_id, key] = f"{cluster.cluster_id} person {_letters(number)}"
    return labels


def _letters(number):
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters, number = "", number + 1
    while number:
        number, rest = divmod(number - 1, 26)
        letters = chr(ord("A") + rest) + letters
    return letters


def _person(record, cluster_id, labels):
    if labels is not None:
        return labels[cluster_id, record.requester_key]
    return record.requester or "(unknown)"


def _count(number, noun):
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _and(names):
    """The names joined as in a sentence: "A", "A and B", "A, B and C"."""
    return " and ".join(filter(None, [", ".join(names[:-1]), names[-1]])) if names else ""


def _assumptions(result, manifest, scrub):
    settings, records, pairs = result.settings, result.records, result.pairs
    received, delivery = settings["received_days"], settings["assumed_delivery_days"]
    excluded = result.excluded
    lines = []
    if manifest:
        state = {
            True: "complete",
            False: "INCOMPLETE, so it covers only what was read (see manifest.json)",
        }.get(manifest.get("complete"), "completeness not recorded")
        lines.append(
            f"Quartzy export started {manifest.get('started_at') or '(unknown)'}; {state}."
        )
    labs = {r.lab_id: r.lab_name for r in records if r.lab_id}
    if labs:
        names = sorted(scrub(name) if name else "a lab with no name" for name in labs.values())
        lines.append(
            f"Quartzy labs included: {_and(names)}. Requests from any other Quartzy lab the "
            "token can see (a collaborator's, say) are left out."
        )
    lines.append(
        f"{_count(len(records), 'request')}. Left out: {excluded['cancelled']} cancelled, "
        f"{excluded['undated']} without a request date, {excluded['no_requester']} with no "
        "requester (neither requested_by nor created_by)."
    )
    lines.append(
        "A duplicate pair is the same product requested by two different people (requested_by, "
        f"else created_by) within {settings['window_days']} days. Each later request counts "
        "once, at its own price, paired with the earlier request that best explains why "
        "Quartzy missed it."
    )
    lines.append(
        "Same product: T1 = the same normalized catalog # within a vendor family; T2 = the "
        "same base number in another pack size; T1x = the same catalog # (7+ characters, with "
        "a digit) in another vendor family, or on two requests that name no vendor, only when "
        "the Quartzy types match or the item names are similar (trigram similarity "
        f"{CROSS_FAMILY_MIN_SIMILARITY} or more)."
    )
    lines.append(
        f"Same-requester repeats ({_count(len(result.repeats), 'pair')}, "
        f"{_money(_total(result.repeats))}) are reported separately and not counted: they are "
        "usually re-orders."
    )
    unchecked = {reason: len(found) for reason, found in result.uncheckable.items()}
    generic = [scrub(key) for key in settings["generic_keys"]]
    lines.append(
        "Not checkable by catalog #, so duplicates among them are missed: "
        f"{_count(unchecked['placeholder'], 'placeholder')} (N/A, TBD, blank...), "
        f"{_count(unchecked['excel_mangled'], 'Excel-damaged number')}, "
        f"{_count(unchecked['too_long'], 'request')} with a link or list pasted as the "
        f"catalog #, {_count(unchecked['no_vendor'], 'request')} with no vendor and a catalog "
        "# too short to compare without one, "
        f"{_count(unchecked['generic'], 'request')} on {_count(len(generic), 'generic key')}"
        + (f" ({', '.join(generic)})" if generic else "")
        + "."
    )
    lines.extend(_money_assumptions(records, pairs))
    lines.append(
        "Prices are what was entered in Quartzy (its total, or unit price x quantity), not what "
        "was paid. Purchases made outside Quartzy are invisible here."
    )
    lines.append(
        "Quartzy's own warning is taken to fire on the same catalog # as typed (spaces trimmed, "
        "case kept) among CREATED/APPROVED/ORDERED/BACKORDERED requests in the same Quartzy "
        "Lab, whatever the vendor. Whether it ignores case or dashes is untested."
    )
    caught = [
        p
        for p in pairs
        if p.bucket == "format_variant"
        and p.earlier_state == "open"
        and p.earlier.lab_id
        and p.earlier.lab_id == p.later.lab_id
    ]
    if caught:
        lines.append(
            f"{_count(len(caught), 'format_variant pair')} ({_money(_total(caught))}) had the "
            "earlier request open in the same Quartzy Lab: Quartzy would have warned about "
            "them too if its warning ignores case and punctuation."
        )
    dated = sum(r.has_status_dates for r in records)
    if dated:
        lines.append(
            f"Status dates from the Quartzy UI export for {dated} of "
            f"{_count(len(records), 'request')}; the rest use their current status."
        )
    still_open = (
        "an earlier request still open today counts as open when the later one was made, and "
        if settings["infer_open_from_current_status"]
        else ""
    )
    lines.append(
        f"Without status dates, {still_open}a RECEIVED earlier request counts as received long "
        f"before only if it was requested at least {received + delivery} days earlier "
        f"({received} days + {delivery} days assumed delivery). Other pairs with the same "
        "number in the same Lab are unknown_open_state."
    )
    lines.append(
        f"received_earlier means received at least {received} days before the later request."
    )
    lines.append(
        "This is an upper bound: it includes parallel purchases that were needed (separate "
        "projects, stock used up, urgent work). The reviewed figure counts only the pairs the "
        "co-owner labels true_dup."
    )
    return lines


def _money_assumptions(records, pairs):
    basis = Counter(r.price_basis for r in records)
    words = {"minor": "minor units (cents)", "major": "major units (dollars)"}
    lines = [
        f"total_price.amount read as {words[unit]}, as configured: "
        f"{_count(basis[unit], 'request')}."
        for unit in ("minor", "major")
        if basis[unit]
    ]
    if basis["auto:minor"] or basis["auto:major"]:
        lines.append(
            "Money unit decided per request (auto: total_price compared with unit_price x "
            f"quantity): {basis['auto:minor']} read as cents, {basis['auto:major']} as dollars."
        )
    if basis["unit_price*quantity"]:
        lines.append(
            f"{_count(basis['unit_price*quantity'], 'request')} priced as unit_price x quantity "
            "(read as dollars) because total_price was missing or its unit could not be decided."
        )
    if basis["none"]:
        unpriced = sum(p.amount is None for p in pairs)
        lines.append(
            f"{_count(basis['none'], 'request')} without a usable price; "
            f"{_count(unpriced, 'duplicate pair')} without an amount."
        )
    currencies = Counter(r.currency for r in records if r.price is not None and r.currency)
    if len(currencies) > 1:
        lines.append(f"Mixed currencies {dict(currencies)}: amounts are added as if they were one.")
    return lines


# ---------------------------------------------------------------------------------------
# The co-owner's review


REVIEW_COLUMNS = (
    "rank", "label", "notes", "amount", "bucket", "earlier_item", "later_item",
    "earlier_vendor", "later_vendor", "earlier_catalog", "later_catalog", "earlier_requester",
    "later_requester", "earlier_requested", "later_requested", "gap_days", "earlier_status",
    "later_status", "earlier_lab", "later_lab", "type", "tier", "other_reasons",
    "earlier_state", "state_evidence", "cluster", "currency", "earlier_url", "later_url",
    "pair_id",
)  # fmt: skip


def write_review_csv(pairs, path, *, anonymize=False, overwrite=False):
    """Write the pairs for the co-owner to label, largest amount first.

    ``label`` takes true_dup (one purchase could have been shared or borrowed),
    related_not_dup (the same product, but both were needed) or false (not the same thing);
    ``notes`` says why when it isn't obvious. The file opens in Excel (UTF-8 with a BOM),
    and cells Excel would run as formulas get a leading apostrophe. A file that already
    holds labels is someone's review work: it is only replaced with ``overwrite=True``.

    ``anonymize`` hides who asked: requesters become "C001 person A", "C001 person B"...,
    lettered afresh in each cluster so that no label follows anyone from one cluster to
    another, and the names of the people in these pairs are taken out of the Quartzy text.
    The request links and pair ids stay (labels come back by pair id), so even this file is
    not for sharing with the lab.
    """
    path = Path(path)
    if path.exists() and not overwrite and _has_labels(path):
        raise FileExistsError(f"{path} already has labels; choose another path or overwrite=True")
    clusters = _clusters(pairs)
    cluster_ids = {p.pair_id: c.cluster_id for c in clusters for p in c.pairs}
    labels = _cluster_labels(clusters) if anonymize else None
    scrub = _scrubber(r for p in pairs for r in (p.earlier, p.later)) if anonymize else _as_is
    ordered = sorted(
        pairs, key=lambda p: (p.amount is None, -(p.amount or 0), p.gap_days, p.pair_id)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, REVIEW_COLUMNS)
        writer.writeheader()
        for rank, pair in enumerate(ordered, 1):
            earlier, later = pair.earlier, pair.later
            cluster = cluster_ids[pair.pair_id]
            row = {
                "rank": rank,
                "amount": "" if pair.amount is None else _money(pair.amount),
                "bucket": pair.bucket,
                "gap_days": pair.gap_days,
                "type": scrub(later.type_name),
                "tier": pair.tier,
                "other_reasons": " ".join(pair.reasons[1:]),
                "earlier_state": pair.earlier_state,
                "state_evidence": pair.state_evidence,
                "cluster": cluster,
                "currency": later.currency,
                "pair_id": pair.pair_id,
            }
            for side, record in (("earlier", earlier), ("later", later)):
                row |= {
                    f"{side}_item": scrub(record.item_name),
                    f"{side}_vendor": scrub(record.vendor_name),
                    f"{side}_catalog": scrub(record.catalog.raw),
                    f"{side}_requester": _person(record, cluster, labels),
                    f"{side}_requested": f"{record.requested_at:%Y-%m-%d}",
                    f"{side}_status": record.status,
                    f"{side}_lab": scrub(record.lab_name) or record.lab_id,
                    f"{side}_url": record.app_url,
                }
            writer.writerow({column: _safe_cell(value) for column, value in row.items()})
    return path


def _has_labels(path):
    try:
        return any(review.label for review in read_review_csv(path).values())
    except ValueError:
        return True  # not a clean review file: don't replace it either


def _safe_cell(value):
    if value is None:
        return ""
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def read_review_csv(path):
    """Read a labelled review CSV back: {pair_id: Review(label or None, notes)}.

    Accepts what Excel saves (semicolons, a BOM or Windows encoding, FALSE in capitals) and
    raises ValueError for any label other than true_dup, related_not_dup or false.
    """
    headers, rows = _read_csv(path)
    if not {"pair_id", "label"} <= set(headers):
        raise ValueError(f"{path} has no pair_id and label columns; use write_review_csv's file")
    reviews, problems = {}, []
    for line, row in enumerate(rows, 2):
        pair_id = (row.get("pair_id") or "").strip()
        if not pair_id:
            continue
        raw = row.get("label") or ""
        label = re.sub(r"[\s-]+", "_", raw.strip().casefold()) or None
        if label is not None and label not in LABELS:
            problems.append(f"line {line}: {raw!r}")
            continue
        review = Review(label, (row.get("notes") or "").strip())
        if pair_id in reviews and reviews[pair_id].label != label:
            problems.append(f"line {line}: {pair_id} is labelled twice, differently")
        reviews[pair_id] = review
    if problems:
        raise ValueError(
            f"{path}: use true_dup, related_not_dup or false; " + "; ".join(problems[:10])
        )
    return reviews


def reviewed_summary(pairs, reviews, *, window_days=90):
    """The reviewed figure (pairs labelled true_dup) next to the upper bound.

    Unlabelled pairs are not extrapolated: the reviewed figure is only what someone checked.
    """
    by_id = {pair.pair_id: pair for pair in pairs}
    labels = {pid: review.label for pid, review in reviews.items() if review.label}
    labelled = [by_id[pid] for pid in labels if pid in by_id]
    by_label = {label: [p for p in labelled if labels[p.pair_id] == label] for label in LABELS}
    true_dups = by_label["true_dup"]
    return {
        "upper_bound": _figure(pairs),
        "reviewed": _figure(true_dups),
        "labelled": {
            **_figure(labelled),
            "share_of_upper_bound": _ratio(_total(labelled), _total(pairs)),
        },
        "unlabelled": _figure([p for p in pairs if p.pair_id not in labels]),
        "by_label": {label: _figure(found) for label, found in by_label.items()},
        "precision": _ratio(len(true_dups), len(labelled)),
        "by_bucket": {
            bucket: {
                "labelled": sum(p.bucket == bucket for p in labelled),
                **_figure([p for p in true_dups if p.bucket == bucket]),
            }
            for bucket in BUCKETS
        },
        "by_gap": _breakdown(
            true_dups, lambda p: _gap_band(p.gap_days, window_days), _gap_bands(window_days)
        ),
        "stale_labels": sum(pid not in by_id for pid in labels),
    }


def _figure(pairs):
    return {"pairs": len(pairs), "amount": _money(_total(pairs))}
