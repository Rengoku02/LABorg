"""Phase 0 Quartzy probe (plan §5 Phase 0 step 2, build step 0b). Read-only; no Django.

    uv run --no-sync python -m scripts.quartzy_probe measure
    uv run --no-sync python -m scripts.quartzy_probe export
    uv run --no-sync python -m scripts.quartzy_probe fixtures data/export-2026-10-27

- measure: a few dozen calls that pin what the spec leaves undocumented (page base, page size,
  sort order and stability, what the page after the last returns, money unit, null rates).
  Writes data/probe/<UTC timestamp>/report.json, prints a summary and the env lines to pin.
- export: a full, politely paced read into data/export-<UTC date>/, with a manifest. It reads
  only the lab's own Quartzy labs: --lab-id (once per lab), else QUARTZY_LAB_IDS, else the
  only lab the token sees. The token also sees any collaborator's lab, so with several labs
  and none named it refuses and lists them.
- fixtures: an anonymized sample of an export, for tests/fixtures/quartzy/.

The token comes from QUARTZY_ACCESS_TOKEN or --token-file. It is never printed or written,
and all output is scrubbed of it as a second line of defence. QuartzyClient refuses anything
but GET and HEAD, so nothing here can change Quartzy.
"""

import argparse
import copy
import hashlib
import hmac
import itertools
import json
import os
import re
import secrets
import sys
import time
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from apps.guard.anonymize import NameScrubber
from apps.quartzy.client import DEFAULT_BASE_URL, QuartzyAuthError, QuartzyClient, QuartzyError
from apps.quartzy.money import infer_money_unit

REPO = Path(__file__).resolve().parent.parent
SPEC = REPO / "docs" / "research" / "quartzy-public-api.yaml"
SORT_KEYS = ("requested_at", "updated_at")


class ProbeError(Exception):
    """A problem worth a one-line message and exit code 2 (no traceback)."""


class BudgetExhausted(Exception):
    pass


# Output ----------------------------------------------------------------------------------------


class Output:
    """All printing and file writing goes through here, scrubbed of the token."""

    def __init__(self, token=None):
        self._token = token

    def scrub(self, text):
        return text.replace(self._token, "[redacted]") if self._token else text

    def print(self, line=""):
        print(self.scrub(line))

    def write_json(self, path, data):
        text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
        path.write_text(self.scrub(text) + "\n", encoding="utf-8")

    def write_jsonl(self, path, records):
        with path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(self.scrub(json.dumps(record, ensure_ascii=False, default=str)) + "\n")


def _now():
    return datetime.now(UTC)


def _iso(moment):
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _display(path):
    try:
        return str(Path(path).resolve().relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def spec_sha256(path=SPEC):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


# Reading pages ---------------------------------------------------------------------------------


class Budget:
    """Stops a command at --max-calls; the client counts every request it sends."""

    def __init__(self, client, max_calls):
        self.client = client
        self.max_calls = max_calls

    @property
    def exhausted(self):
        return self.client.calls >= self.max_calls

    def check(self):
        if self.exhausted:
            raise BudgetExhausted(f"reached the --max-calls cap of {self.max_calls}")


class Pages:
    """Cached page reads of one list endpoint, e.g. ``Pages(client.order_requests, budget,
    lab_id=...)``. A page is a list, or the QuartzyError of a 4xx answer (a refused page=0
    is a fact to record, not a failure)."""

    def __init__(self, fetch, budget, **filters):
        self._fetch = fetch
        self._budget = budget
        self._filters = filters
        self._cache = {}

    def get(self, page, *, fresh=False):
        if fresh or page not in self._cache:
            self._budget.check()
            try:
                result = self._fetch(page=page, **self._filters)
            except QuartzyAuthError:
                raise
            except QuartzyError as exc:
                if exc.status is None or exc.status == 429 or exc.status >= 500:
                    raise
                result = exc
            if fresh:
                return result
            self._cache[page] = result
        return self._cache[page]


def _records(result):
    return result if isinstance(result, list) else []


def _ids(result):
    return [record.get("id") for record in _records(result) if isinstance(record, dict)]


def _describe(result):
    if isinstance(result, QuartzyError):
        return f"error {result.status}"
    return f"{len(result)} records"


def _dedupe(records):
    seen, unique = set(), []
    for record in records:
        if record.get("id") not in seen:
            seen.add(record.get("id"))
            unique.append(record)
    return unique


def collect_all(pages, base, *, seen=None, max_pages=100_000):
    """Read pages from ``base`` until an empty one, skipping ids already in ``seen``.

    Returns (records, stats) and never loses what it read: it also stops when three pages in
    a row bring nothing new (the page parameter is being ignored), at max_pages, when the call
    budget runs out, or on a Quartzy error (``stats["stopped"]`` says which). A refused token
    still raises.
    """
    seen = set() if seen is None else seen
    found = []
    stats = {"pages": 0, "records": 0, "duplicates": 0, "stopped": None}
    page, idle = base, 0
    try:
        while stats["stopped"] is None:
            result = pages.get(page)
            stats["pages"] += 1
            if isinstance(result, QuartzyError):
                stats["stopped"] = f"page {page} returned {result.status}"
                break
            if not result:
                stats["stopped"] = "empty_page"
                break
            new = 0
            for record in result:
                record_id = record.get("id") if isinstance(record, dict) else None
                if record_id is not None and record_id in seen:
                    stats["duplicates"] += 1
                    continue
                seen.add(record_id)
                found.append(record)
                new += 1
            idle = 0 if new else idle + 1
            page += 1
            if idle >= 3:
                stats["stopped"] = "no_new_ids"
            elif page - base >= max_pages:
                stats["stopped"] = "max_pages"
    except BudgetExhausted:
        stats["stopped"] = "call_budget"
    except QuartzyAuthError:
        raise
    except QuartzyError as exc:
        stats["stopped"] = f"error: {exc}"
    stats["records"] = len(found)
    return found, stats


def _lab_entry(lab):
    return {
        "id": lab.get("id"),
        "name": lab.get("name"),
        "uses_instances": lab.get("uses_instances"),
        "organization": lab.get("organization"),
    }


# Which labs are the lab's ----------------------------------------------------------------------


def named_labs(args):
    """(lab ids, where they came from): export's --lab-id (once per lab, or comma-separated),
    else QUARTZY_LAB_IDS (comma-separated, as in .env), else none."""
    given = {
        "--lab-id": getattr(args, "lab_ids", None) or [],
        "QUARTZY_LAB_IDS": [os.environ.get("QUARTZY_LAB_IDS", "")],
    }
    for source, values in given.items():
        named = [lab_id.strip() for value in values for lab_id in value.split(",")]
        named = [lab_id for lab_id in named if lab_id]
        if named:
            return named, source
    return [], None


def choose_labs(labs, named, source="QUARTZY_LAB_IDS"):
    """Of ``labs`` (the ones the token sees), those ``named`` (ids in any case, from
    ``source``), else the only one, in the token's order. Raises ProbeError, listing the labs,
    when a lab named isn't there or when there are several and none is named: the token also
    sees a collaborator's labs, and their requests must never pass for this lab's."""
    if named:
        wanted = {lab_id.lower() for lab_id in named}
        missing = sorted(wanted - {str(lab.get("id")).lower() for lab in labs})
        if missing:
            raise ProbeError(
                f"{source} names {', '.join(missing)}, but the token sees no Quartzy lab with "
                f"that id.{_lab_list(labs)}"
            )
        return [lab for lab in labs if str(lab.get("id")).lower() in wanted]
    if len(labs) == 1:
        return labs
    if not labs:
        raise ProbeError("The token sees no Quartzy lab, so there is nothing to read.")
    raise ProbeError(
        f"The token sees {len(labs)} Quartzy labs, and a collaborator's must not be read as this "
        "lab's. Name the lab's own with --lab-id (once per lab) or QUARTZY_LAB_IDS (comma-"
        f"separated, as in .env).{_lab_list(labs)}"
    )


def _lab_list(labs):
    lines = [f"  {_one_line(lab.get('id'))}  {_one_line(lab.get('name'))}" for lab in labs]
    return "\nThe token sees:\n" + "\n".join(lines) if labs else ""


def _one_line(value):
    """``value`` as text on one line. Lab names come from Quartzy and are printed among the
    lines to copy into .env, where a line break in one could add a line of its own."""
    return " ".join(str(value).split())


def _lab_of(record):
    lab = record.get("lab") if isinstance(record, dict) else None
    return str(lab["id"]).lower() if isinstance(lab, dict) and lab.get("id") else None


# Inference -------------------------------------------------------------------------------------


def infer_page_base(page0, page1):
    """Return (base or None, reason) from what page=0 and page=1 returned."""
    if isinstance(page1, QuartzyError):
        if _records(page0):
            return 0, f"page=1 is refused ({page1.status}) while page=0 has records"
        return None, "page=1 is refused"
    if isinstance(page0, QuartzyError):
        if page1:
            return 1, f"page=0 is refused ({page0.status})"
        return None, "page=0 is refused and page=1 is empty"
    ids0, ids1 = set(_ids(page0)), set(_ids(page1))
    if not ids0 and not ids1:
        return None, "page=0 and page=1 are both empty"
    if not ids0:
        return 1, "page=0 is empty"
    if not ids1:
        return 0, "page=1 is empty while page=0 has records"
    if ids0 == ids1:
        return 1, "page=0 repeats page=1"
    if not ids0 & ids1:
        return 0, "page=0 and page=1 hold different records"
    return None, "page=0 and page=1 overlap but differ (unstable order?)"


def find_last_page(is_nonempty, base, max_offset=4096):
    """Bounded exponential-then-binary search for the last non-empty page.

    Returns (last page or None when the base page is empty, capped). ``capped`` means pages
    were still non-empty at ``base + max_offset``: the page parameter may be ignored.
    """
    if not is_nonempty(base):
        return None, False
    known, step = 0, 1  # offsets from base: ``known`` is non-empty
    while True:
        if step > max_offset:
            return base + known, True
        if not is_nonempty(base + step):
            break
        known, step = step, step * 2
    empty = step
    while empty - known > 1:
        middle = (known + empty) // 2
        if is_nonempty(base + middle):
            known = middle
        else:
            empty = middle
    return base + known, False


def _parse_time(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _direction(values):
    """'asc', 'desc', 'flat' (all equal), 'none' (unsorted) or 'n/a' (under two values)."""
    values = [value for value in values if value is not None]
    if len(values) < 2:
        return "n/a"
    pairs = list(itertools.pairwise(values))
    ascending = all(a <= b for a, b in pairs)
    descending = all(a >= b for a, b in pairs)
    if ascending and descending:
        return "flat"
    return "asc" if ascending else "desc" if descending else "none"


def _across(first, last):
    first = [value for value in first if value is not None]
    last = [value for value in last if value is not None]
    if not first or not last:
        return "n/a"
    descending, ascending = min(first) >= max(last), max(first) <= min(last)
    if ascending and descending:
        return "flat"
    return "desc" if descending else "asc" if ascending else "none"


def sort_facts(first, last, stable):
    """How the list is sorted, from the first and last pages (``last`` None: one page only).

    A key counts when it runs the same way within both pages and across them. requested_at is
    preferred; updated_at means editing a request moves it. New requests land on the first
    page for a descending key, on the last page for an ascending one, and "unknown" otherwise
    or when the first page changed between two reads.
    """
    facts = {}
    for key in SORT_KEYS:
        head = [_parse_time(record.get(key)) for record in first]
        tail = [_parse_time(record.get(key)) for record in last] if last is not None else []
        facts[key] = {
            "first_page": _direction(head),
            "last_page": _direction(tail) if last is not None else "n/a",
            "across_pages": _across(head, tail),
        }
    facts["key"] = facts["direction"] = None
    for key in SORT_KEYS:
        seen = set(facts[key].values()) - {"n/a", "flat"}
        if len(seen) == 1 and seen <= {"asc", "desc"}:
            facts["key"], facts["direction"] = key, seen.pop()
            break
    landing = {"desc": "first_page", "asc": "last_page"}.get(facts["direction"], "unknown")
    facts["new_items_at"] = landing if stable else "unknown"
    return facts


def recommend_money_unit(counts, *, min_decided=10, share=0.95):
    """'minor' or 'major' only when nearly every decidable record agrees; else 'auto'."""
    decided = counts["minor"] + counts["major"]
    for unit in ("minor", "major"):
        if decided >= min_decided and counts[unit] >= share * decided:
            return unit
    return "auto"


def _pct(part, whole):
    return round(100 * part / whole, 1) if whole else None


def sample_stats(records):
    """Statuses, requester/creator agreement, null rates and money unit of sampled requests."""
    total = len(records)
    requesters = [r["requested_by"] for r in records if isinstance(r.get("requested_by"), dict)]
    creators = [r["created_by"] for r in records if isinstance(r.get("created_by"), dict)]
    pairs = [
        (r["requested_by"], r["created_by"])
        for r in records
        if isinstance(r.get("requested_by"), dict) and isinstance(r.get("created_by"), dict)
    ]
    counts = infer_money_unit(records)
    return {
        "records": total,
        "statuses": dict(Counter(str(r.get("status")) for r in records).most_common()),
        "requested_by_vs_created_by": {
            "compared": len(pairs),
            "different": sum(1 for asker, creator in pairs if asker.get("id") != creator.get("id")),
        },
        "null_pct": {
            "updated_at": _pct(sum(1 for r in records if r.get("updated_at") is None), total),
            "requested_by": _pct(total - len(requesters), total),
            "created_by": _pct(total - len(creators), total),
            "requested_by_email": _pct(sum(1 for u in requesters if not u.get("email")), total),
            "created_by_email": _pct(sum(1 for u in creators if not u.get("email")), total),
        },
        "money": {"counts": counts, "recommended": recommend_money_unit(counts)},
    }


def sample_pages(base, last, extra):
    """The first and last page plus ``extra`` evenly spaced pages in between."""
    pages = {base, last}
    for i in range(1, extra + 1):
        pages.add(base + round(i * (last - base) / (extra + 1)))
    return sorted(pages)


def estimate_total(pages, base, max_offset):
    """Count a list's records with the last-page search instead of reading every page."""
    first = _records(pages.get(base))
    last_page, capped = find_last_page(lambda p: bool(_records(pages.get(p))), base, max_offset)
    if last_page is None:
        return {"estimate": 0, "page_size": 0, "last_page": None, "capped": False}
    estimate = (last_page - base) * len(first) + len(_records(pages.get(last_page)))
    return {"estimate": estimate, "page_size": len(first), "last_page": last_page, "capped": capped}


# measure ---------------------------------------------------------------------------------------


def measure(client, args, out):
    started = _now()
    budget = Budget(client, args.max_calls)
    report = {
        "generated_at": _iso(started),
        "base_url": client.base_url,
        "spec_sha256": spec_sha256(),
        "complete": True,
    }
    try:
        _measure(client, budget, args, report)
    except QuartzyAuthError:
        raise
    except (BudgetExhausted, QuartzyError) as exc:
        report["complete"] = False
        report["stopped_because"] = str(exc)
    report["lab_ids"] = [lab["id"] for lab in _chosen(report.get("labs") or [], args)]
    report["pinned"] = pinned_facts(report)
    report["calls"] = client.calls
    report["rate_limit_headers"] = client.rate_limit_headers
    folder = Path(args.data_dir) / "probe" / started.strftime("%Y%m%dT%H%M%SZ")
    folder.mkdir(parents=True, exist_ok=True)
    out.write_json(folder / "report.json", report)
    for line in summary_lines(report):
        out.print(line)
    out.print(f"\nReport: {_display(folder / 'report.json')}")
    out.print("\nPin these in the environment (.env):")
    for line in env_lines(report["pinned"], report.get("labs") or [], named_labs(args)[0]):
        out.print(line)
    return 0 if report["complete"] else 1


def _chosen(labs, args):
    """The labs choose_labs picks, or none where export would refuse (the env lines then list
    them for the person to choose)."""
    try:
        return choose_labs([lab for lab in labs if lab.get("id")], named_labs(args)[0])
    except ProbeError:
        return []


def _measure(client, budget, args, report):
    budget.check()
    try:
        client.health()
        report["healthz"] = {"ok": True}
    except QuartzyError as exc:
        report["healthz"] = {"ok": False, "status": exc.status, "error": str(exc)}

    budget.check()
    me = client.me()
    report["user"] = {"id": me.get("id"), "email_present": bool(me.get("email"))}  # no names

    budget.check()
    labs = [lab for lab in client.labs() if isinstance(lab, dict)]
    report["labs"] = [_lab_entry(lab) for lab in labs]
    # The lab's own lab, where the Lab Admin's test request lands: the first that
    # QUARTZY_LAB_IDS names and the token sees, else the first (the only one, if it sees one).
    named = {lab_id.lower() for lab_id in named_labs(args)[0]}
    ours = [lab for lab in labs if str(lab.get("id")).lower() in named]
    lab_id = args.lab_id or next((lab.get("id") for lab in ours or labs), None)
    report["probed_lab_id"] = lab_id
    requests = Pages(client.order_requests, budget, lab_id=lab_id)
    report["order_requests"] = paging = {}
    probe_paging(requests, args, paging)  # fills ``paging`` as it goes
    base = paging["page_base"]
    if base is None:
        return

    every_lab, stats = collect_all(Pages(client.labs, budget), base)
    if stats["stopped"] == "empty_page":  # the first read may have been one page of several
        labs = [lab for lab in every_lab if isinstance(lab, dict)]
        report["labs"] = [_lab_entry(lab) for lab in labs]
    for lab, entry in zip(labs, report["labs"], strict=True):
        types, stats = collect_all(Pages(client.types, budget, lab_id=lab.get("id")), base)
        entry["types"] = len(types) if stats["stopped"] == "empty_page" else None
        if lab.get("id") == lab_id:
            own = requests
        else:
            own = Pages(client.order_requests, budget, lab_id=lab.get("id"))
        entry["order_requests"] = estimate_total(own, base, args.max_pages)
        items = Pages(client.inventory_items, budget, lab_id=lab.get("id"))
        entry["inventory_items"] = estimate_total(items, base, args.max_pages)
        sampled = [item for item in _records(items.get(base)) if isinstance(item, dict)]
        entry["inventory_items"]["sampled_with_instances"] = sum(
            1 for item in sampled if item.get("instances")
        )


def probe_paging(pages, args, facts):
    """Fill ``facts`` with the order-request paging facts, from one lab's pages. It is filled
    as the reads come in, so a run cut short by the call cap keeps what it learned."""
    facts["page_base"] = None
    omitted, page0, page1 = pages.get(None), pages.get(0), pages.get(1)
    base, reason = infer_page_base(page0, page1)
    facts.update(
        page_0=_describe(page0),
        page_1=_describe(page1),
        no_page=_describe(omitted),
        page_base=base,
        page_base_reason=reason,
    )
    if base is None:
        return
    first = _records(pages.get(base))
    facts["omitted_page_is_base"] = isinstance(omitted, list) and set(_ids(omitted)) == set(
        _ids(first)
    )
    facts["page_size"] = len(first)
    # A page followed by a non-empty one must be full, so then the size is exact.
    facts["page_size_exact"] = bool(_records(pages.get(base + 1)))
    total = estimate_total(pages, base, args.max_pages)
    last_page = total["last_page"]
    last = _records(pages.get(last_page))
    facts["last_page"] = last_page
    facts["last_page_search_capped"] = total["capped"]
    facts["estimated_total"] = total["estimate"]
    facts["after_last_page"] = after_last_page(pages, last_page)
    facts["first_page_stable"] = _ids(pages.get(base, fresh=True)) == _ids(first)
    facts["sort"] = sort_facts(
        first, last if last_page != base else None, facts["first_page_stable"]
    )
    # Run measure again after adding a request in Quartzy to see where it lands.
    facts["first_page_ids"] = _ids(first)[:5]
    sample = []
    for page in sample_pages(base, last_page, args.sample_pages):
        sample.extend(record for record in _records(pages.get(page)) if isinstance(record, dict))
    facts["sample"] = sample_stats(_dedupe(sample))


def after_last_page(pages, last_page):
    """What the page after the last one returns, which the sync's end-of-list rule relies on:
    [] (the research's guess), an error status, or the last page again (an API that clamps
    the page number)."""
    after, last = pages.get(last_page + 1), pages.get(last_page)
    ids = set(_ids(after))
    return {
        "page": last_page + 1,
        "status": after.status if isinstance(after, QuartzyError) else 200,
        "records": len(_records(after)),
        "repeats_last_page": bool(ids) and ids == set(_ids(last)),
    }


def pinned_facts(report):
    paging = report.get("order_requests") or {}
    money = (paging.get("sample") or {}).get("money") or {}
    return {
        "QUARTZY_PAGE_BASE": paging.get("page_base"),
        "QUARTZY_NEW_ITEMS_AT": (paging.get("sort") or {}).get("new_items_at", "unknown"),
        "QUARTZY_MONEY_UNIT": money.get("recommended", "auto"),
        # The token's only lab, or the ones QUARTZY_LAB_IDS names; None when it's not clear.
        "QUARTZY_LAB_IDS": ",".join(report.get("lab_ids") or []) or None,
    }


def env_lines(pinned, labs=(), named=()):
    """The lines to pin in .env. ``labs`` (the report's) are listed when it isn't clear which
    are the lab's: that is the person's choice. Ids in ``named`` (QUARTZY_LAB_IDS) that the
    token doesn't see are pointed out."""
    base = pinned.get("QUARTZY_PAGE_BASE")
    lines = [
        f"QUARTZY_PAGE_BASE={base}"
        if base is not None
        else "# QUARTZY_PAGE_BASE=?  (could not be measured; see the report)",
        f"QUARTZY_NEW_ITEMS_AT={pinned.get('QUARTZY_NEW_ITEMS_AT') or 'unknown'}",
        f"QUARTZY_MONEY_UNIT={pinned.get('QUARTZY_MONEY_UNIT') or 'auto'}",
    ]
    if pinned.get("QUARTZY_LAB_IDS"):
        return [*lines, f"QUARTZY_LAB_IDS={pinned['QUARTZY_LAB_IDS']}"]
    if not labs:
        return [*lines, "# QUARTZY_LAB_IDS=?  (no Quartzy lab was read; see the report)"]
    seen = {str(lab.get("id")).lower() for lab in labs}
    unseen = [_one_line(lab_id) for lab_id in named if lab_id.lower() not in seen]
    if unseen:
        why = f"QUARTZY_LAB_IDS names {', '.join(unseen)}, which the token doesn't see; it sees"
    else:
        why = "the lab's own labs, comma-separated; the token sees"
    return [
        *lines,
        f"# QUARTZY_LAB_IDS=?  ({why} these:)",
        *(f"#   {_one_line(lab.get('id'))}  {_one_line(lab.get('name'))}" for lab in labs),
    ]


def _yes(value):
    return {True: "yes", False: "no"}.get(value, "?")


def summary_lines(report):
    """The human summary. Copes with a report cut short, where facts are missing."""
    lines = [f"Quartzy probe {report['generated_at']} ({report.get('calls')} calls)"]
    if not report.get("complete"):
        lines.append(f"INCOMPLETE: {report.get('stopped_because')}")
    health = report.get("healthz") or {}
    lines.append("healthz: " + ("ok" if health.get("ok") else f"FAILED ({health.get('error')})"))
    if user := report.get("user"):
        lines.append(f"token owner: id {user['id']}, email present: {_yes(user['email_present'])}")
    labs = report.get("labs") or []
    lines.append(f"labs: {len(labs)}")
    for lab in labs:
        org = (lab.get("organization") or {}).get("name") or "-"
        requests = (lab.get("order_requests") or {}).get("estimate", "?")
        items = lab.get("inventory_items") or {}
        lines.append(
            f"  {lab['name']} [{lab['id']}] uses_instances={lab['uses_instances']} "
            f"organization={org} types={lab.get('types', '?')} requests~{requests} "
            f"inventory_items~{items.get('estimate', '?')} "
            f"(with instances: {items.get('sampled_with_instances', '?')} of "
            f"{items.get('page_size', '?')} sampled)"
        )
    paging = report.get("order_requests") or {}
    if "page_base_reason" in paging:
        lines += [
            f"order requests (lab {report.get('probed_lab_id')}):",
            f"  page=0: {paging['page_0']} | page=1: {paging['page_1']} | "
            f"no page: {paging['no_page']}",
            f"  page base: {paging['page_base']} ({paging['page_base_reason']})",
        ]
    if "page_size" in paging:
        exact = "exact" if paging.get("page_size_exact") else "at least; only one page"
        lines += [
            f"  omitted page = base page: {_yes(paging.get('omitted_page_is_base'))}",
            f"  page size: {paging['page_size']} ({exact})",
        ]
    if "estimated_total" in paging:
        capped = " (search capped: is page ignored?)" if paging["last_page_search_capped"] else ""
        lines.append(
            f"  last page: {paging['last_page']}{capped}, ~{paging['estimated_total']} requests"
        )
    if after := paging.get("after_last_page"):
        lines.append(
            f"  page after the last ({after['page']}): status {after['status']}, "
            f"{after['records']} records, repeats the last page: "
            f"{_yes(after['repeats_last_page'])}"
        )
    if sort := paging.get("sort"):
        lines += [
            f"  sorted by: {sort['key'] or 'nothing recognisable'} {sort['direction'] or '-'}",
            f"  first page identical on two reads: {_yes(paging['first_page_stable'])}",
            f"  new requests land on: {sort['new_items_at']}",
        ]
    if sample := paging.get("sample"):
        pairs, money = sample["requested_by_vs_created_by"], sample["money"]
        statuses = ", ".join(f"{status} {n}" for status, n in sample["statuses"].items())
        nulls = ", ".join(f"{k} {pct}" for k, pct in sample["null_pct"].items() if pct is not None)
        lines += [
            f"  sample: {sample['records']} requests; statuses: {statuses or '-'}",
            f"  requested_by != created_by: {pairs['different']} of {pairs['compared']}",
            f"  null %: {nulls or '-'}",
            f"  money unit: minor {money['counts']['minor']}, major {money['counts']['major']}, "
            f"unknown {money['counts']['unknown']} -> {money['recommended']} (this assumes "
            "unit_price is in dollars; check one request against the Quartzy UI)",
        ]
    lines.append(f"rate-limit headers: {report.get('rate_limit_headers') or 'none seen'}")
    return lines


# export ----------------------------------------------------------------------------------------


def latest_report(data_dir):
    reports = sorted((Path(data_dir) / "probe").glob("*/report.json"))
    return reports[-1] if reports else None


def resolve_facts(client, budget, args):
    """The page base and money unit to use: flags, then the latest probe report, then a
    quick measurement of the page base (money stays "auto")."""
    report = Path(args.report) if args.report else latest_report(args.data_dir)
    pinned = {}
    if report is not None:
        if not report.exists():
            raise ProbeError(f"No probe report at {report}")
        pinned = json.loads(report.read_text(encoding="utf-8")).get("pinned") or {}
    facts = {"report": _display(report) if report else None}
    if args.page_base is not None:
        facts.update(page_base=args.page_base, page_base_source="--page-base")
    elif pinned.get("QUARTZY_PAGE_BASE") is not None:
        facts.update(page_base=int(pinned["QUARTZY_PAGE_BASE"]), page_base_source="probe report")
    else:
        pages = Pages(client.order_requests, budget)
        base, reason = infer_page_base(pages.get(0), pages.get(1))
        if base is None:
            raise ProbeError(f"Could not measure the page base ({reason}); pass --page-base")
        facts.update(page_base=base, page_base_source=f"measured: {reason}")
    if args.money_unit:
        facts.update(money_unit=args.money_unit, money_unit_source="--money-unit")
    elif pinned.get("QUARTZY_MONEY_UNIT"):
        facts.update(money_unit=pinned["QUARTZY_MONEY_UNIT"], money_unit_source="probe report")
    else:
        facts.update(money_unit="auto", money_unit_source="default")
    facts["new_items_at"] = pinned.get("QUARTZY_NEW_ITEMS_AT", "unknown")
    return facts


def _halts(stats):
    """Out of calls, or Quartzy is failing: stop reading and write what we have."""
    return stats["stopped"] == "call_budget" or stats["stopped"].startswith("error")


def _fresh_dir(path):
    candidate, n = path, 1
    while candidate.exists():
        n += 1
        candidate = path.with_name(f"{path.name}-{n}")
    candidate.mkdir(parents=True)
    return candidate


def export(client, args, out):
    started, clock = _now(), time.monotonic()
    budget = Budget(client, args.max_calls)
    facts = resolve_facts(client, budget, args)
    base = facts["page_base"]
    every_lab, labs_stats = collect_all(Pages(client.labs, budget), base)
    if _halts(labs_stats):
        stopped = labs_stats["stopped"]
        raise ProbeError(f"Could not read the list of labs ({stopped}), so nothing was exported")
    every_lab = [lab for lab in every_lab if isinstance(lab, dict) and lab.get("id")]
    named, chosen_by = named_labs(args)
    labs = choose_labs(every_lab, named, chosen_by)
    found = {"types": [], "order_requests": [], "inventory_items": []}
    seen = {name: set() for name in found}
    fetchers = {
        "types": client.types,
        "order_requests": client.order_requests,
        "inventory_items": client.inventory_items,
    }
    per_lab = {}
    halted = False
    for lab_id in [lab["id"] for lab in labs]:
        per_lab[lab_id] = lab_stats = {}
        for name, fetch in fetchers.items():
            if halted:
                break
            records, stats = collect_all(Pages(fetch, budget, lab_id=lab_id), base, seen=seen[name])
            found[name].extend(records)
            lab_stats[name] = stats
            halted = _halts(stats)
    # Each list is read with a lab filter; anything Quartzy sends from another lab all the
    # same stays out too.
    ours = {str(lab["id"]).lower() for lab in labs}
    from_other_labs = {}
    for name, records in found.items():
        found[name] = [record for record in records if _lab_of(record) in {None, *ours}]
        from_other_labs[name] = len(records) - len(found[name])

    folder = _fresh_dir(Path(args.data_dir) / f"export-{started:%Y-%m-%d}")
    out.write_json(folder / "labs.json", labs)
    out.write_json(folder / "types.json", found["types"])
    out.write_jsonl(folder / "order_requests.jsonl", found["order_requests"])
    out.write_jsonl(folder / "inventory_items.jsonl", found["inventory_items"])
    stops = [labs_stats["stopped"]]
    stops += [stats["stopped"] for lab in per_lab.values() for stats in lab.values()]
    left_out = [
        {"id": lab["id"], "name": lab.get("name")}
        for lab in every_lab
        if str(lab["id"]).lower() not in ours
    ]
    manifest = {
        "started_at": _iso(started),
        "finished_at": _iso(_now()),
        "duration_seconds": round(time.monotonic() - clock, 1),
        "base_url": client.base_url,
        "complete": not halted and all(stop == "empty_page" for stop in stops),
        "calls": client.calls,
        "max_calls": args.max_calls,
        "min_interval_seconds": args.min_interval,
        # The baseline reads only these labs' requests.
        "lab_ids": [lab["id"] for lab in labs],
        "lab_ids_source": chosen_by or "the only lab the token sees",
        "labs_left_out": left_out,
        "from_other_labs": from_other_labs,
        "counts": {"labs": len(labs), **{name: len(records) for name, records in found.items()}},
        "labs_read": labs_stats,
        "per_lab": per_lab,
        "spec_sha256": spec_sha256(),
        "probe_facts": {**facts, "money_unit_counts": infer_money_unit(found["order_requests"])},
        "rate_limit_headers": client.rate_limit_headers,
    }
    out.write_json(folder / "manifest.json", manifest)
    counts = ", ".join(f"{name} {n}" for name, n in manifest["counts"].items())
    out.print(f"Exported {counts} in {manifest['duration_seconds']} s ({client.calls} calls)")
    for heading, listed in (("Quartzy labs", labs), ("Left out", left_out)):
        if listed:
            names = (f"{_one_line(lab.get('name'))} [{_one_line(lab['id'])}]" for lab in listed)
            out.print(f"{heading}: {', '.join(names)}")
    if sum(from_other_labs.values()):
        out.print(
            f"Left out {sum(from_other_labs.values())} records Quartzy sent from other labs "
            "although asked for one lab; the manifest counts them (from_other_labs)"
        )
    if not manifest["complete"]:
        reasons = sorted(set(stops) - {"empty_page"})
        out.print(f"INCOMPLETE ({', '.join(reasons)}); the manifest has the details")
    out.print(f"Written to {_display(folder)}")
    return 0 if manifest["complete"] else 1


# fixtures --------------------------------------------------------------------------------------

_UUID = re.compile(r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PEOPLE = ("requested_by", "created_by", "added_by", "updated_by")
_FREE_TEXT = ("notes", "details", "technical_details")
_REFERENCES = {
    "invoice_number": "INV",
    "requisition_number": "REQ",
    "confirmation_number": "CONF",
    "tracking_number": "TRK",
    "purchase_order_number": "PO",
    "serial_number": "SN",
}
# Product facts that normalization and matcher tests need verbatim, so the scrub of people's
# names from other text (e.g. "Priya's primers") leaves them alone.
_PRODUCT_FIELDS = {
    "vendor_name",
    "vendor",
    "catalog_number",
    "unit_size",
    "unit_price",
    "price",
    "quantity",
    "cas_number",
    "url",
    "status",
    "amount",
    "currency",
    "lot_number",
    "shipping_and_handling",
}


class Anonymizer:
    """Keyed, stable pseudonyms: with one key, a person, email or id always maps to the same
    value, and without the key nobody can reverse them by hashing guessed emails. People's
    names are found in other text the way the baseline's NameScrubber finds them (any case or
    accents, and inside other words in Chinese, Japanese and Korean)."""

    def __init__(self, key, records=()):
        self._key = key
        names = []
        for record in records:
            for field in _PEOPLE:
                person = record.get(field)
                if isinstance(person, dict):
                    first = str(person.get("first_name") or "")
                    last = str(person.get("last_name") or "")
                    names += [first, last, f"{first} {last}"]  # a full name finds CJK names
        self._names = NameScrubber(names)

    def tag(self, kind, value, length=6):
        digest = hmac.new(self._key, f"{kind}:{value}".encode(), hashlib.sha256)
        return digest.hexdigest()[:length].upper()

    def uuid(self, value):
        digest = hmac.new(self._key, f"id:{value.lower()}".encode(), hashlib.sha256).digest()
        return str(uuid.UUID(bytes=digest[:16], version=4))

    def email(self, value):
        return f"person-{self.tag('email', value.lower(), 10).lower()}@example.test"

    def person(self, user):
        if not isinstance(user, dict):
            return user
        user = dict(user)
        tag = self.tag("person", str(user.get("id") or user.get("email") or ""))
        if user.get("first_name") is not None:
            user["first_name"] = "Person"
        if user.get("last_name") is not None:
            user["last_name"] = tag
        return user  # the id and email are replaced by the string pass

    def place(self, location):
        if not isinstance(location, dict):
            return location
        location = dict(location)
        if location.get("name"):
            location["name"] = f"Place {self.tag('place', location['name'])}"
        if isinstance(location.get("pathNames"), list):
            location["pathNames"] = [f"Place {self.tag('place', p)}" for p in location["pathNames"]]
        return location

    def lab(self, lab):
        """An anonymized copy of a labs.json entry."""
        return self._strings(self._lab_names(lab))

    def _lab_names(self, lab):
        # Names only: ids go through the string pass exactly once, so they keep matching the
        # same ids inside app_url and elsewhere.
        if not isinstance(lab, dict):
            return lab
        lab = dict(lab)
        if lab.get("name"):
            lab["name"] = f"Lab {self.tag('lab', lab['name'])}"
        org = lab.get("organization")
        if isinstance(org, dict) and org.get("name"):
            lab["organization"] = {**org, "name": f"Organization {self.tag('org', org['name'])}"}
        return lab

    def record(self, record):
        """An anonymized deep copy of an order request, inventory item or type."""
        out = copy.deepcopy(record)
        for field in _PEOPLE:
            if field in out:
                out[field] = self.person(out[field])
        for field in _FREE_TEXT:
            if out.get(field):
                out[field] = ""
        for field, prefix in _REFERENCES.items():
            if out.get(field):
                out[field] = f"{prefix}-{self.tag(field, out[field])}"
        if isinstance(out.get("lab"), dict):
            out["lab"] = self._lab_names(out["lab"])
        code = out.get("spend_tracking_code")
        if isinstance(code, dict) and code.get("name"):
            out["spend_tracking_code"] = {
                **code,
                "name": f"Budget {self.tag('budget', code['name'])}",
            }
        for field in ("location", "sublocation"):
            if field in out:
                out[field] = self.place(out[field])
        for instance in out.get("instances") or []:
            if isinstance(instance, dict) and "location" in instance:
                instance["location"] = self.place(instance["location"])
        return self._strings(out)

    def _strings(self, value, field=None):
        """Re-map every uuid and email in every string, and people's names outside product
        fields. Ids that aren't uuids are re-mapped whole."""
        if isinstance(value, dict):
            return {key: self._strings(item, key) for key, item in value.items()}
        if isinstance(value, list):
            return [self._strings(item, field) for item in value]
        if not isinstance(value, str):
            return value
        if field in ("id", "vendor_product_id") and not _UUID.fullmatch(value):
            return self.uuid(value)
        value = _UUID.sub(lambda m: self.uuid(m[0]), value)
        value = _EMAIL.sub(lambda m: self.email(m[0]), value)
        if field not in _PRODUCT_FIELDS:
            value = self._names.replace_names(
                value, lambda name: f"Person-{self.tag('name', name)}"
            )
        return value


def spread(records, count):
    """``count`` records evenly spaced through ``records`` (all of them if there are fewer)."""
    if count <= 0:
        return []
    if len(records) <= count:
        return list(records)
    if count == 1:
        return [records[0]]
    step = (len(records) - 1) / (count - 1)
    return [records[round(i * step)] for i in range(count)]


def _read_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _read_jsonl(path):
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def fixtures(args, out):
    source = Path(args.export_dir)
    if not (source / "order_requests.jsonl").exists():
        raise ProbeError(f"{source} has no order_requests.jsonl; run export first")
    requests = _read_jsonl(source / "order_requests.jsonl")
    items = _read_jsonl(source / "inventory_items.jsonl")
    labs = _read_json(source / "labs.json", [])
    types = _read_json(source / "types.json", [])
    manifest = _read_json(source / "manifest.json", {})
    # The key never goes into the fixtures. Set LABSTOCK_FIXTURE_SALT (kept like a password)
    # to keep pseudonyms stable from one run to the next.
    secret = os.environ.get("LABSTOCK_FIXTURE_SALT", "")
    anonymizer = Anonymizer(
        secret.encode() if secret else secrets.token_bytes(32), requests + items
    )
    by_time = sorted(requests, key=lambda r: (str(r.get("requested_at") or ""), str(r.get("id"))))
    picked = [anonymizer.record(r) for r in spread(by_time, args.requests)]
    picked_items = [anonymizer.record(item) for item in spread(items, args.inventory)]

    target = Path(args.out)
    target.mkdir(parents=True, exist_ok=True)
    out.write_json(target / "order_requests.json", picked)
    out.write_json(target / "inventory_items.json", picked_items)
    out.write_json(target / "labs.json", [anonymizer.lab(lab) for lab in labs])
    out.write_json(target / "types.json", [anonymizer.record(qtype) for qtype in types])
    probe_facts = {k: v for k, v in (manifest.get("probe_facts") or {}).items() if k != "report"}
    out.write_json(
        target / "manifest.json",
        {
            "generated_at": _iso(_now()),
            # The same pseudonyms as the labs' ids, so the baseline reads these labs' requests.
            "lab_ids": [anonymizer.uuid(str(lab_id)) for lab_id in manifest.get("lab_ids") or []],
            "counts": {
                "order_requests": len(picked),
                "inventory_items": len(picked_items),
                "labs": len(labs),
                "types": len(types),
            },
            "export": {
                "started_at": manifest.get("started_at"),
                "spec_sha256": manifest.get("spec_sha256"),
                "order_requests": len(requests),
                "inventory_items": len(items),
            },
            "probe_facts": probe_facts,
            "pseudonyms": "keyed by LABSTOCK_FIXTURE_SALT"
            if secret
            else "random key: consistent within this set of files only",
            "anonymized": "people's names and emails, all ids, lab/organization/location/budget "
            "names and reference numbers replaced; notes, details and technical_details "
            "blanked; products and prices kept",
        },
    )
    out.print(
        f"Wrote {len(picked)} requests and {len(picked_items)} inventory items to "
        f"{_display(target)}. Review them before committing."
    )
    return 0


# CLI -------------------------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m scripts.quartzy_probe", description="Read-only Quartzy probe (Phase 0)."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    def quartzy(name, help_text, *, max_calls):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--token-file", help="file holding the token ($QUARTZY_ACCESS_TOKEN)")
        command.add_argument(
            "--base-url", default=os.environ.get("QUARTZY_API_BASE", DEFAULT_BASE_URL)
        )
        command.add_argument("--data-dir", default=str(REPO / "data"))
        command.add_argument(
            "--min-interval", type=float, default=0.5, help="seconds between calls (0.5)"
        )
        command.add_argument("--max-calls", type=int, default=max_calls)
        return command

    measure_cmd = quartzy("measure", "pin the undocumented API facts", max_calls=400)
    measure_cmd.add_argument(
        "--lab-id",
        help="lab whose requests are probed (default: the first in $QUARTZY_LAB_IDS, else first)",
    )
    measure_cmd.add_argument("--sample-pages", type=int, default=4, help="middle pages sampled")
    measure_cmd.add_argument("--max-pages", type=int, default=4096, help="last-page search cap")

    export_cmd = quartzy("export", "read everything into data/export-<date>/", max_calls=5000)
    export_cmd.add_argument(
        "--lab-id",
        action="append",
        dest="lab_ids",
        metavar="ID",
        help="one of the lab's own Quartzy labs; repeat for each, or separate ids with commas "
        "(default: $QUARTZY_LAB_IDS, else the token's only lab)",
    )
    export_cmd.add_argument("--page-base", type=int, choices=(0, 1))
    export_cmd.add_argument("--money-unit", choices=("minor", "major", "auto"))
    export_cmd.add_argument("--report", help="probe report to take facts from (default: latest)")

    fixtures_cmd = commands.add_parser("fixtures", help="anonymized sample of an export")
    fixtures_cmd.add_argument("export_dir")
    fixtures_cmd.add_argument("--out", default=str(REPO / "tests" / "fixtures" / "quartzy"))
    fixtures_cmd.add_argument("--requests", type=int, default=60)
    fixtures_cmd.add_argument("--inventory", type=int, default=20)
    return parser


def load_token(args):
    if args.token_file:
        token = Path(args.token_file).expanduser().read_text(encoding="utf-8").strip()
    else:
        token = os.environ.get("QUARTZY_ACCESS_TOKEN", "").strip()
    if not token:
        raise ProbeError("No token: set QUARTZY_ACCESS_TOKEN or pass --token-file")
    return token


def main(argv=None, *, transport=None, sleep=time.sleep):
    """CLI entry point. Tests pass a fake ``transport`` (and a no-op ``sleep``)."""
    args = build_parser().parse_args(argv)
    out = Output()
    try:
        if args.command == "fixtures":
            return fixtures(args, out)
        token = load_token(args)
        out = Output(token)
        try:
            client = QuartzyClient(
                token,
                base_url=args.base_url,
                transport=transport,
                sleep=sleep,
                min_interval=args.min_interval,
            )
        except ValueError as exc:
            raise ProbeError(str(exc)) from None
        with client:
            return {"measure": measure, "export": export}[args.command](client, args, out)
    except QuartzyAuthError as exc:
        out.print(f"Quartzy refused the token ({exc.status}); check that it is current.")
        return 2
    except QuartzyError as exc:
        out.print(f"Quartzy error: {exc}")
        return 1
    except ProbeError as exc:
        out.print(f"Error: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
