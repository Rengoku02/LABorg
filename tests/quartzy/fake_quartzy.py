"""A stateful, in-memory fake of Quartzy's public API, for client, sync and probe tests.

``FakeQuartzy`` is an ``httpx.MockTransport`` handler. It routes on the URL path only, so any
base URL works (the test settings use ``https://api.quartzy.test``). Records are plain dicts
shaped like docs/research/quartzy-public-api.yaml, and list endpoints answer with bare JSON
arrays, as the real API does::

    fake = FakeQuartzy()                   # token "test-token", as in labstock.settings.test
    lab = fake.add_lab("Smith Lab")
    req = fake.add_request(lab, catalog_number="2118S", vendor_name="Cell Signaling")
    client = fake.client()                 # QuartzyClient wired to the fake; sleeps are
                                           # recorded in fake.slept, never slept
    assert client.order_requests(lab_id=lab["id"], page=1)[0]["id"] == req["id"]

    fake.update_request(req["id"], status="ORDERED")   # also bumps updated_at
    fake.delete_request(req["id"])                     # GET /order-requests/{id} -> 404 now

    # Code that builds its own client only needs the transport:
    QuartzyClient(fake.token, transport=fake.transport(), sleep=lambda seconds: None)

Options are constructor keywords and also plain attributes, so a test can change them
between reads:

- ``page_size`` (25) and ``page_base`` (1).
- ``omitted_page``: the page served when ``page`` is left out; None (default) = page_base.
- ``page_below_base``: what a page under the base returns: "empty" ([], default), "clamp"
  (the base page) or "error" (400).
- ``past_end``: what a page after the last one returns (the research only inferred []):
  "empty" ([], default), "error" (404) or "clamp" (the last page again). The base page of an
  empty list is [] whatever this says.
- ``order``: "newest_first" (default), "oldest_first", or "unstable" (reshuffled on every
  read, seeded). Applies to order requests and inventory items; labs and types come back in
  insertion order.
- ``sort_key``: "requested_at" (default) or "updated_at", so editing a request moves it to the
  newest end. Ties fall back to insertion order.
- ``money_unit``: how a computed total_price.amount is written: "minor" (cents, default) or
  "major" (dollars). It applies when a record is added or its price changes.
- ``null_updated_at``, ``null_requested_by``, ``null_emails``: serve those fields as null on
  every record (stored data is untouched); per record, pass ``updated_at=None`` and so on.
- ``healthy``: False makes /healthz answer 500.
- ``require_token``: when True (default), every route but /healthz answers 401 unless the
  ``Access-Token`` header equals ``token``.
- ``start`` (2025-01-06 09:00 UTC) and ``tick`` (1 hour): the clock behind default
  timestamps. Each new request and each edit moves ``fake.now`` on by one tick, so pass
  ``start=`` (or explicit ``requested_at=``) when a test freezes time with time-machine.
- ``seed`` (0): makes ids and "unstable" shuffles the same on every run.

Hooks and inspection:

- ``fail_next(503, times=2, retry_after=5)``: the next calls fail with that status; pass
  ``"timeout"`` to raise ``httpx.ReadTimeout`` instead, and ``path="/order-requests"`` to aim
  at one route. Queued failures are used in order.
- ``after_page(action, page=1)``: run ``action()`` right after that list page is served, e.g.
  ``fake.after_page(lambda: fake.add_request(lab), page=1)`` inserts a request mid-scan.
  The page already served is unaffected.
- ``calls``: a Counter per route ("/order-requests", "/order-requests/{id}", ...);
  ``total_calls``; ``log``: every ``(method, path, params)`` received, including refused
  writes and injected failures.
- ``check_against_spec(route, body)``: raise unless a body is what the spec allows for that
  GET route (works for fixtures too).

Helpers return the stored dict, so mutating it changes what the fake serves (without bumping
``updated_at``; use ``update_request`` for realistic edits). Datetimes passed in are stored as
ISO 8601 strings. ``AUTO`` means "use the default"; pass None for an explicit null.
"""

import copy
import functools
import itertools
import json
import random
import re
import threading
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import unquote

import httpx

from apps.quartzy.client import QuartzyClient

TOKEN = "test-token"  # the same as QUARTZY_ACCESS_TOKEN in labstock.settings.test
AUTO = object()
START = datetime(2025, 1, 6, 9, 0, tzinfo=UTC)
TICK = timedelta(hours=1)
ORDERS = ("newest_first", "oldest_first", "unstable")
PAST_END = ("empty", "error", "clamp")
LIST_ROUTES = ("/labs", "/types", "/order-requests", "/inventory-items")
_BY_ID = re.compile(r"/(labs|order-requests|inventory-items)/([^/]+)")
_USER_FIELDS = ("requested_by", "created_by", "added_by", "updated_by")
_EPOCH = datetime.min.replace(tzinfo=UTC)
SPEC_PATH = Path(__file__).resolve().parents[2] / "docs" / "research" / "quartzy-public-api.yaml"


class FakeQuartzy:
    def __init__(
        self,
        token=TOKEN,
        *,
        page_size=25,
        page_base=1,
        omitted_page=None,
        page_below_base="empty",
        past_end="empty",
        order="newest_first",
        sort_key="requested_at",
        money_unit="minor",
        null_updated_at=False,
        null_requested_by=False,
        null_emails=False,
        healthy=True,
        require_token=True,
        seed=0,
        start=START,
        tick=TICK,
    ):
        if order not in ORDERS:
            raise ValueError(f"order must be one of {ORDERS}")
        if past_end not in PAST_END:
            raise ValueError(f"past_end must be one of {PAST_END}")
        self.token = token
        self.page_size = page_size
        self.page_base = page_base
        self.omitted_page = omitted_page
        self.page_below_base = page_below_base
        self.past_end = past_end
        self.order = order
        self.sort_key = sort_key
        self.money_unit = money_unit
        self.null_updated_at = null_updated_at
        self.null_requested_by = null_requested_by
        self.null_emails = null_emails
        self.healthy = healthy
        self.require_token = require_token
        self.now = start
        self.tick = tick

        self.labs = {}
        self.types = {}
        self.order_requests = {}
        self.inventory_items = {}
        self.users = {}

        self.calls = Counter()
        self.log = []
        self.slept = []
        self._ids = random.Random(seed)
        self._shuffle = random.Random(seed + 1)
        self._position = {}  # record id -> insertion number, the sort tie-breaker
        self._made = Counter()
        self._sequence = itertools.count(1)
        self._failures = []
        self._hooks = []
        self._lock = threading.RLock()  # concurrent sync runs may share one fake
        self.organization = {"id": self._uuid(), "name": "Example University"}
        self.me = self.add_user("Pat", "Owner")  # the token owner, and the default requester

    # Wiring ----------------------------------------------------------------------------------

    def transport(self):
        return httpx.MockTransport(self)

    def client(self, **kwargs):
        """A QuartzyClient on this fake. Retry waits go to ``fake.slept`` instead of sleeping."""
        kwargs.setdefault("sleep", self.slept.append)
        kwargs.setdefault("base_url", "https://api.quartzy.test")
        return QuartzyClient(kwargs.pop("token", self.token), transport=self.transport(), **kwargs)

    @property
    def total_calls(self):
        return sum(self.calls.values())

    def reset_calls(self):
        self.calls.clear()
        self.log.clear()

    # Data helpers ----------------------------------------------------------------------------

    def add_user(self, first_name="Sam", last_name="Member", email=AUTO, *, id=None):
        """A person, as embedded in requested_by / created_by / added_by / updated_by."""
        self._made["user"] += 1
        if email is AUTO:
            email = f"{first_name}.{last_name}.{self._made['user']}@lab.test".lower()
        user = {
            "id": id or self._uuid(),
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
        }
        self.users[user["id"]] = user
        return user

    def add_lab(self, name=None, *, uses_instances=True, organization=AUTO, id=None):
        """A Quartzy Lab. ``uses_instances=None`` leaves the field out, as for labs that predate
        the instances rollout (the research says to treat that as migrated)."""
        self._made["lab"] += 1
        lab = {"id": id or self._uuid(), "name": name or f"Lab {self._made['lab']}"}
        if uses_instances is not None:
            lab["uses_instances"] = uses_instances
        organization = self.organization if organization is AUTO else organization
        if organization is not None:
            lab["organization"] = dict(organization)
        self._store(self.labs, lab)
        return lab

    def add_type(self, lab=None, name="General", *, id=None):
        """A Quartzy Type in a lab (served by GET /types with the lab embedded)."""
        lab = self._lab(lab)
        qtype = {"id": id or self._uuid(), "name": name, "lab": copy.deepcopy(lab)}
        self._store(self.types, qtype)
        return qtype

    def add_request(self, lab=None, *, qtype=None, requested_by=AUTO, created_by=AUTO, **fields):
        """An order request. Defaults: a uuid id, status CREATED, requested_at one ``tick``
        after the previous record, updated_at equal to requested_at, requested_by and
        created_by the token owner, and total_price from unit_price x quantity in
        ``money_unit``. Any field (also an unknown one, to test API drift) can be passed.

        ``lab`` and ``qtype`` take a dict, an id, or nothing (the first lab / type, created on
        demand); ``qtype`` may also be a type name.
        """
        lab = self._lab(lab)
        qtype = self._type(lab, qtype)
        self._made["request"] += 1
        n = self._made["request"]
        request_id = fields.pop("id", None) or self._uuid()
        requested_at = fields.pop("requested_at", AUTO)
        if requested_at is AUTO:
            requested_at = self._advance()
        requester = self.me if requested_by is AUTO else requested_by
        record = {
            "id": request_id,
            "app_url": f"https://app.quartzy.com/groups/{lab['id']}/requests?id={request_id}",
            "status": "CREATED",
            "vendor_name": "Test Vendor",
            "catalog_number": f"TV-{n:04d}",
            "quantity": "1",
            "unit_size": "1 ea",
            "unit_price": "10.00",
            "total_price": None,
            "requested_at": requested_at,
            "updated_at": requested_at,
            "requested_by": requester,
            "created_by": requester if created_by is AUTO else created_by,
            "notes": None,
            "item_name": f"Test item {n}",
            "invoice_number": None,
            "requisition_number": None,
            "confirmation_number": None,
            "tracking_number": None,
            "purchase_order_number": None,
            "shipping_and_handling": None,
            "details": None,
            "lab": copy.deepcopy(lab),
            "type": {"id": qtype["id"], "name": qtype["name"]},
            "spend_tracking_code": None,
            "backordered_expected_at": None,
            "is_urgent": False,
            **fields,
        }
        if "total_price" not in fields:
            record["total_price"] = self._money(record["unit_price"], record["quantity"])
        return self._store(self.order_requests, _jsonable(record))

    def update_request(self, request_id, bump_updated_at=True, **changes):
        """Edit a stored request the way someone in Quartzy's UI would.

        updated_at moves to the next tick unless ``bump_updated_at=False`` or you pass it.
        Changing unit_price or quantity recomputes total_price unless you pass that too.
        """
        record = self.order_requests[request_id]
        record.update(_jsonable(changes))
        if {"unit_price", "quantity"} & changes.keys() and "total_price" not in changes:
            record["total_price"] = self._money(record["unit_price"], record["quantity"])
        if bump_updated_at and "updated_at" not in changes:
            record["updated_at"] = _iso(self._advance())
        return record

    def delete_request(self, request_id):
        """Delete a request (in Quartzy's UI; the API has no DELETE). Later GETs return 404."""
        return self.order_requests.pop(request_id)

    def add_inventory_item(self, lab=None, *, qtype=None, instances=AUTO, added_by=AUTO, **fields):
        """An inventory item with one instance by default. ``instances`` takes a list of
        partial instance dicts (each gets an id, quantity "1" and a freezer location)."""
        lab = self._lab(lab)
        qtype = self._type(lab, qtype)
        self._made["item"] += 1
        n = self._made["item"]
        item_id = fields.pop("id", None) or self._uuid()
        owner = self.me if added_by is AUTO else added_by
        record = {
            "id": item_id,
            "name": f"Stock item {n}",
            "vendor": "Test Vendor",
            "catalog_number": f"TV-{n:04d}",
            "price": "10.00",
            "unit_size": "1 ea",
            "quantity": "1",
            "url": None,
            "technical_details": None,
            "expiration_date": None,
            "auto_reminder": None,
            "lot_number": None,
            "cas_number": None,
            "vendor_product_id": None,
            "lab": copy.deepcopy(lab),
            "type": {"id": qtype["id"], "name": qtype["name"]},
            "location": None,
            "sublocation": None,
            "added_by": owner,
            "updated_by": owner,
            "app_url": f"https://app.quartzy.com/groups/{lab['id']}/inventory/{item_id}",
            "serial_number": None,
            "instances": [
                self._instance(n, part) for part in ([{}] if instances is AUTO else instances)
            ],
            **fields,
        }
        return self._store(self.inventory_items, _jsonable(record))

    # Failures and hooks ----------------------------------------------------------------------

    def fail_next(self, status, times=1, retry_after=None, *, path=None, body=None):
        """Fail the next ``times`` calls (to route ``path``, or to any route) with ``status``.

        ``status="timeout"`` raises httpx.ReadTimeout. ``retry_after`` adds a Retry-After
        header; ``body`` replaces the default ``{"message": "Injected failure"}``.
        """
        self._failures.append(
            {
                "status": status,
                "times": times,
                "retry_after": retry_after,
                "path": path,
                "body": body,
            }
        )

    def after_page(self, action, *, page=None, path="/order-requests", times=1):
        """Call ``action()`` right after a page of the list route ``path`` is served.

        ``page`` is the page number as the fake resolved it (an omitted page counts as the
        page it served); None matches any page. Runs at most ``times`` times.
        """
        self._hooks.append({"action": action, "page": page, "path": path, "times": times})

    # The handler -----------------------------------------------------------------------------

    def __call__(self, request):
        with self._lock:
            return self._handle(request)

    def _handle(self, request):
        path = request.url.raw_path.decode("ascii").split("?", 1)[0]
        params = dict(request.url.params)
        route, item_id = _route(path)
        self.calls[route] += 1
        self.log.append((request.method, path, params))
        if request.method not in ("GET", "HEAD"):
            return _error(405, "This fake only serves reads")
        failure = self._take_failure(route)
        if failure is not None:
            if failure["status"] == "timeout":
                raise httpx.ReadTimeout("Injected timeout", request=request)
            headers = {}
            if failure["retry_after"] is not None:
                headers["Retry-After"] = str(failure["retry_after"])
            body = (
                failure["body"] if failure["body"] is not None else {"message": "Injected failure"}
            )
            return httpx.Response(failure["status"], headers=headers, json=body)
        if route == "/healthz":  # the spec declares no security for it
            return httpx.Response(200 if self.healthy else 500, json={})
        if self.require_token and request.headers.get("Access-Token") != self.token:
            return _error(401, "Request unauthorized")
        if route == "/user":
            return httpx.Response(200, json=self._render(self.me))
        if route in LIST_ROUTES:
            return self._list(route, params)
        found = {
            "/labs/{id}": self.labs,
            "/order-requests/{id}": self.order_requests,
            "/inventory-items/{id}": self.inventory_items,
        }.get(route, {}).get(item_id)
        if found is None:
            return _error(404, "No matching record" if item_id else "Not Found")
        return httpx.Response(200, json=self._render(found))

    def _list(self, route, params):
        lab_id = params.get("lab_id")
        if route == "/labs":
            org = params.get("organization_id")
            records = [lab for lab in self.labs.values() if not org or _org_id(lab) == org]
        elif route == "/types":
            name = (params.get("name") or "").lower()
            records = [
                qtype
                for qtype in self.types.values()
                if (not lab_id or qtype["lab"]["id"] == lab_id)
                and (not name or qtype["name"].lower() == name)
            ]
        else:
            store = self.order_requests if route == "/order-requests" else self.inventory_items
            records = self._ordered(
                [r for r in store.values() if not lab_id or r["lab"]["id"] == lab_id],
                by_time=route == "/order-requests",
            )
        try:
            page = self._resolve_page(params.get("page"))
        except ValueError as exc:
            return _error(400, str(exc))
        chunk = []
        if page is not None:
            last = self.page_base + max(0, -(-len(records) // self.page_size) - 1)
            if page > last and self.past_end == "error":
                response = _error(404, "Page not found")
                self._run_hooks(route, page)
                return response
            served = min(page, last) if self.past_end == "clamp" else page
            start = (served - self.page_base) * self.page_size
            chunk = records[start : start + self.page_size]
        response = httpx.Response(200, json=[self._render(record) for record in chunk])
        self._run_hooks(route, page)
        return response

    def _resolve_page(self, raw):
        if raw is None:
            page = self.page_base if self.omitted_page is None else self.omitted_page
        else:
            try:
                page = int(raw)
            except ValueError:
                raise ValueError(f"page must be an integer, not {raw!r}") from None
        if page >= self.page_base:
            return page
        if self.page_below_base == "clamp":
            return self.page_base
        if self.page_below_base == "error":
            raise ValueError(f"page must be at least {self.page_base}")
        return None

    def _ordered(self, records, *, by_time):
        if self.order not in ORDERS:
            raise ValueError(f"order must be one of {ORDERS}")
        if self.order == "unstable":
            self._shuffle.shuffle(records)
            return records

        def key(record):
            position = self._position.get(record["id"], 0)
            if not by_time:
                return _EPOCH, position
            stamp = record.get(self.sort_key) or record.get("requested_at")
            return _parse_time(stamp), position

        return sorted(records, key=key, reverse=self.order == "newest_first")

    def _render(self, record):
        out = copy.deepcopy(record)
        if self.null_updated_at and "updated_at" in out:
            out["updated_at"] = None
        if self.null_requested_by and "requested_by" in out:
            out["requested_by"] = None
        if self.null_emails:
            for person in [out, *(out.get(field) for field in _USER_FIELDS)]:
                if isinstance(person, dict) and "email" in person:
                    person["email"] = None
        return out

    def _take_failure(self, route):
        for failure in self._failures:
            if failure["path"] in (None, route):
                failure["times"] -= 1
                if failure["times"] <= 0:
                    self._failures.remove(failure)
                return failure
        return None

    def _run_hooks(self, route, page):
        for hook in list(self._hooks):
            if hook["path"] == route and hook["page"] in (None, page):
                hook["times"] -= 1
                if hook["times"] <= 0:
                    self._hooks.remove(hook)
                hook["action"]()

    # Internals -------------------------------------------------------------------------------

    def _uuid(self):
        return str(uuid.UUID(int=self._ids.getrandbits(128), version=4))

    def _advance(self):
        self.now += self.tick
        return self.now

    def _store(self, store, record):
        store[record["id"]] = record
        self._position[record["id"]] = next(self._sequence)
        return record

    def _lab(self, lab):
        if lab is None:
            return next(iter(self.labs.values()), None) or self.add_lab()
        return self.labs[lab["id"] if isinstance(lab, dict) else lab]

    def _type(self, lab, qtype):
        if isinstance(qtype, dict):
            return qtype
        in_lab = [t for t in self.types.values() if t["lab"]["id"] == lab["id"]]
        if qtype is None:
            return in_lab[0] if in_lab else self.add_type(lab)
        if qtype in self.types:
            return self.types[qtype]
        named = [t for t in in_lab if t["name"] == qtype]
        return named[0] if named else self.add_type(lab, qtype)

    def _instance(self, n, part):
        instance = {
            "id": self._uuid(),
            "quantity": "1",
            "expiration_date": None,
            "lot_number": None,
            "location": {"name": f"Box {n}", "pathNames": ["Room 101", "Freezer -80 A"]},
        }
        instance.update(part)
        return instance

    def _money(self, unit_price, quantity):
        try:
            total = Decimal(str(unit_price)) * Decimal(str(quantity))
        except InvalidOperation:
            return None
        if not total.is_finite():
            return None
        if self.money_unit == "minor":
            return {"amount": str((total * 100).quantize(Decimal(1))), "currency": "USD"}
        return {"amount": str(total.quantize(Decimal("0.01"))), "currency": "USD"}


def _route(path):
    path = path.rstrip("/") or "/"
    if path in ("/healthz", "/user", *LIST_ROUTES):
        return path, None
    match = _BY_ID.fullmatch(path)
    if match:
        return f"/{match[1]}/{{id}}", unquote(match[2])
    return path, None


def _org_id(lab):
    return (lab.get("organization") or {}).get("id")


def _error(status, message):
    return httpx.Response(status, json={"message": message})


def _iso(value):
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


def _parse_time(value):
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return _EPOCH
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _jsonable(value):
    """Deep-copy ``value`` with datetimes as ISO strings and Decimals as strings."""
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    return value


def check_against_spec(route, body, *, allow_unknown_fields=False):
    """Raise unless ``body`` is what the spec allows for ``GET route`` ("/order-requests",
    "/labs/{id}", ...). ``allow_unknown_fields=True`` lifts additionalProperties: false, for
    real exports and fixtures, whose new fields schemas.unknown_fields reports instead."""
    from openapi_core.testing import MockRequest, MockResponse

    path = route.replace("{id}", "00000000-0000-4000-8000-000000000000")
    request = MockRequest("https://api.quartzy.com", "get", path, path_pattern=route)
    _openapi(allow_unknown_fields).validate_response(
        request, MockResponse(json.dumps(body).encode())
    )


@functools.cache
def _openapi(allow_unknown_fields):
    import yaml
    from openapi_core import OpenAPI

    spec = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    spec["info"]["version"] = "unversioned"  # published as null, which fails spec validation
    # Verified fact: requested_by may be null, though OrderRequest itself declares it non-null.
    properties = spec["components"]["schemas"]["OrderRequest"]["properties"]
    properties["requested_by"] = {"nullable": True, "oneOf": [properties["requested_by"]]}
    _relax(spec, allow_unknown_fields)
    return OpenAPI.from_dict(spec)


def _relax(node, allow_unknown_fields):
    # OAS 3.0.3 only honours ``nullable`` next to ``type``, so the spec's ``nullable: true``
    # beside ``oneOf`` (location, added_by, requested_by, ...) would reject null; spell it out.
    if isinstance(node, dict):
        if node.get("nullable") and "type" not in node and "oneOf" in node:
            node.pop("nullable")
            node["anyOf"] = [
                *node.pop("oneOf"),
                {"type": "object", "nullable": True, "enum": [None]},
            ]
        if allow_unknown_fields and node.get("additionalProperties") is False:
            node.pop("additionalProperties")
        for value in list(node.values()):
            _relax(value, allow_unknown_fields)
    elif isinstance(node, list):
        for value in node:
            _relax(value, allow_unknown_fields)
