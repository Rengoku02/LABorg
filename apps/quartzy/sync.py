"""The Quartzy sync (plan §8): read Quartzy through an injected QuartzyClient, mirror it here.

Every function takes the client as an argument: tasks.py passes ``client_from_settings()``,
tests pass one wired to tests/quartzy/fake_quartzy.py. The client can only read, and nothing
here deletes a mirror row.

- ``head_probe`` finds new requests, reading from the end of the list where Quartzy inserts
  them (settings.QUARTZY_NEW_ITEMS_AT, measured by scripts/quartzy_probe.py).
- ``rolling_sweep`` re-reads a few pages of history per cycle, so every request is re-read
  every few hours; the API has no ``updated_since``.
- ``refresh_hot_set`` and ``confirm_unlisted_requests`` re-read requests by id. A 404 there is
  the only way a request becomes "deleted in Quartzy", and only when a canary request still
  answers (``_ById``).
- ``sync_labs``, ``sync_types``, ``sync_inventory``, ``full_backfill`` and ``arm_alerts``.
- ``resolve_again`` resolves mirrored requests against the catalog again, after a Quartzy
  type's mapping changed (tasks.resolve_type) or a catalog fix (manage.py quartzy_resolve).

Records are taken in one by one, each in its own transaction, so a record that can't be parsed
or saved becomes a SyncError and the rest of its page goes on. Every payload is made storable
first (``_storable``): Postgres can't hold some text that Quartzy can send. A request payload
fetched before another run wrote the row is not applied over it (``upsert_order_request``).

Head, sweep, hot and re-resolution runs tell the duplicate guard which requests they created
or resolved differently (``signals.requests_synced``, sent when ``recorded_run`` saves them).

Where a list ends was only inferred by the research, so every page loop takes the same rule
(``_list_page``, ``_repeats``): the page after the last one may come back empty, be refused
(400, 404 or 416), or repeat the last page, as an API that clamps the page number does.
"""

import hashlib
import json
import logging
from collections import Counter, defaultdict
from contextlib import contextmanager, nullcontext
from datetime import date, timedelta
from decimal import Decimal
from functools import partial
from typing import NamedTuple

from django.conf import settings
from django.core.management.base import CommandError
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import F, Q
from django.db.models.functions import Greatest
from django.utils import timezone
from pydantic import ValidationError

from apps.catalog.models import ItemType, Product, SupplierProduct
from apps.catalog.services import resolve_supplier_product, retire_orphans, suggest_item_type
from apps.core.mail import queue_notification
from apps.core.models import Membership, Notification, User
from apps.core.people import resolve_person
from apps.core.policies import get_policy

from . import schemas
from .client import QuartzyAuthError, QuartzyClient, QuartzyError
from .models import (
    OrderRequest,
    OrderStatusObservation,
    QuartzyInventoryInstance,
    QuartzyInventoryItem,
    QuartzyLab,
    QuartzyType,
    SyncError,
    SyncRun,
)
from .money import MONEY_UNITS, money_amount, parse_quantity, to_decimal
from .signals import requests_synced

logger = logging.getLogger(__name__)

FIRST_PAGE, LAST_PAGE, UNKNOWN = "first_page", "last_page", "unknown"
# The hot set stays a few dozen by-id reads per run (plan §8 "Hot set").
HOT_RECENT = timedelta(days=2)
HOT_FRESH = timedelta(minutes=10)  # seen on a list page this recently: no need to re-read
HOT_SET_MAX = 30
UNLISTED_PER_RUN = 15
# Reads by id: one record Quartzy refuses or fails on is a SyncError, but these statuses stop
# the run (a refused token, a rate limit), and so do this many failures in a row that look
# like an outage: no answer, or a 5xx (``_outage``).
BY_ID_STOP_STATUSES = frozenset({401, 429})
BY_ID_FAILURES_IN_A_ROW = 3
# The runs that announce the requests they took in (signals.requests_synced). The backfill
# doesn't: history is the guard's replay's job.
ANNOUNCING_KINDS = frozenset(
    {SyncRun.Kind.HEAD, SyncRun.Kind.SWEEP, SyncRun.Kind.HOT, SyncRun.Kind.RESOLVE}
)
# The share of 404s that makes a run's deletions unbelievable counts from this many 404s up.
MIN_404S_FOR_SHARE = 5
# A failed catalog resolution is tried again when the request is next read after this long.
RESOLVE_RETRY_AFTER = timedelta(days=1)
# The exponential search for the last page gives up at base + 2**12.
LAST_PAGE_SEARCH_STEPS = 12
# "Every page to the end of the list" also stops here, in case Quartzy ever ignores ``page``.
MAX_PAGES = 5000
# How an API may answer for a page after the last one, besides [] or a copy of the last page.
PAST_END_STATUSES = frozenset({400, 404, 416})
# The run-note line that counts payloads skipped as older than what is stored.
STALE_NOTE = "Stale payloads skipped (a later read had already written the row)"
# Least recently looked at first, by a list page or by id (answered or not), so a request
# Quartzy keeps failing on goes to the back of the queue instead of blocking the rest.
_LOOKED_AT = Greatest("last_seen_at", "by_id_checked_at")
_MAX_DECIMAL = Decimal(10) ** 10  # DecimalField(max_digits=14, decimal_places=4)
_FOUR_PLACES = Decimal("0.0001")
_warned = set()


def client_from_settings(**overrides):
    """The QuartzyClient for the configured token (tests swap this function for the fake)."""
    options = {"base_url": settings.QUARTZY_API_BASE, "timeout": settings.QUARTZY_HTTP_TIMEOUT}
    return QuartzyClient(settings.QUARTZY_ACCESS_TOKEN, **{**options, **overrides})


def payload_sha256(payload):
    text = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )
    return hashlib.sha256(text.encode()).hexdigest()


# ---------------------------------------------------------------------------------------
# Runs and errors


@contextmanager
def recorded_run(kind, *, quartzy_lab=None, client=None):
    """A SyncRun around a block: running, then ok, or failed if the block raises (the block
    may also mark it aborted). ``calls`` counts what ``client`` sent in the meantime. Once
    saved, the run announces the requests it took in (``_announce``), failed or not: what it
    saved before it stopped is saved."""
    run = SyncRun.objects.create(kind=kind, quartzy_lab=quartzy_lab)
    calls_before = client.calls if client is not None else 0
    try:
        yield run
    except Exception as exc:
        run.status = SyncRun.Status.FAILED
        run.add_note(f"{type(exc).__name__}: {exc}")
        raise
    finally:
        if run.status == SyncRun.Status.RUNNING:
            run.status = SyncRun.Status.OK
        run.finished_at = timezone.now()
        if client is not None:
            run.calls = client.calls - calls_before
        run.save()
        _announce(run)


def _announce(run):
    """Send ``signals.requests_synced`` for ``run`` once its transaction commits (at once
    outside one, so never for a dry run): the live requests among those it created or
    resolved differently, if any, and only for ANNOUNCING_KINDS."""
    if run.kind not in ANNOUNCING_KINDS or not run.synced_request_ids:
        return
    # Live only: a re-resolution also reaches requests deleted in Quartzy, and the types of a
    # Quartzy lab that is not mirrored, whose rows the rest of LabStock must not see.
    live = OrderRequest.objects.filter(pk__in=run.synced_request_ids).live().order_by("pk")
    request_ids = list(live.values_list("pk", flat=True))
    if request_ids:
        send = partial(_send_requests_synced, run.quartzy_lab_id, request_ids, str(run.kind))
        transaction.on_commit(send)


def _send_requests_synced(qlab_id, request_ids, via):
    # The run is saved and its rows committed: a receiver that raises can't change that, so
    # it is logged (Django logs the traceback) and the others still hear it.
    responses = requests_synced.send_robust(
        SyncRun, qlab_id=qlab_id, request_ids=request_ids, via=via
    )
    for _receiver, response in responses:
        if isinstance(response, Exception):
            logger.error(
                "A requests_synced receiver failed on %d request(s) of Quartzy lab #%s "
                "(%s run): %s",
                len(request_ids),
                qlab_id,
                via,
                response,
            )


def _run_for(run, kind, quartzy_lab, client):
    if run is not None:
        return nullcontext(run)
    return recorded_run(kind, quartzy_lab=quartzy_lab, client=client)


def record_error(run, kind, message, *, quartzy_id="", payload=None):
    """Count a per-record error on ``run`` and keep it as a SyncError, once a day per record
    and message, so a record that fails every cycle doesn't flood the table. A payload the
    database refuses as well is left out rather than lose the error with it."""
    run.errors += 1
    message = _storable(str(message))[:4000]
    quartzy_id = _storable(str(quartzy_id or ""))[:64]
    repeat = SyncError.objects.filter(
        sync_run__quartzy_lab=run.quartzy_lab_id,
        kind=kind,
        quartzy_id=quartzy_id,
        message=message,
        created_at__gte=timezone.now() - timedelta(days=1),
    ).exists()
    if repeat:
        return
    fields = {"sync_run": run, "kind": kind, "quartzy_id": quartzy_id, "message": message}
    if payload is not None:
        payload = _storable(json.loads(json.dumps(payload, default=str)))
    try:
        with transaction.atomic():
            SyncError.objects.create(**fields, payload=payload)
    except DatabaseError:
        if payload is None:
            raise
        SyncError.objects.create(**fields)


def notify_admins(lab, *, title, message, subject_obj, period_key=""):
    """Queue a SYSTEM email to the lab's system owners and to superusers."""
    admins = (
        User.objects.filter(is_active=True, email__isnull=False)
        .filter(
            Q(is_superuser=True) | Q(memberships__lab=lab, memberships__role=Membership.Role.ADMIN)
        )
        .distinct()
    )
    for admin in admins:
        queue_notification(
            lab=lab,
            kind=Notification.Kind.SYSTEM,
            subject_obj=subject_obj,
            recipient_email=admin.email,
            recipient=admin,
            period_key=period_key,
            context={"title": title, "message": message},
        )


def _abort(run, qlab, message):
    run.status = SyncRun.Status.ABORTED
    run.add_note(message)
    logger.error("Quartzy sync of %s ABORTED: %s", qlab, message)
    notify_admins(
        qlab.lab,
        title=f"Quartzy sync aborted for {qlab}",
        message=message,
        subject_obj=qlab,
        period_key=f"abort-{timezone.localdate():%Y-%m-%d}",
    )


def _warn_once(key, message, *args):
    if key not in _warned:
        _warned.add(key)
        logger.warning(message, *args)


# ---------------------------------------------------------------------------------------
# Parsing helpers


def _raw_id(payload):
    return str(payload.get("id") or "") if isinstance(payload, dict) else ""


def _storable(value):
    """``value`` with every string in it, dict keys too, made storable in Postgres: NUL
    removed, since neither text nor jsonb can hold it (notes pasted from a PDF bring it), and
    a lone surrogate, which can't be encoded, replaced."""
    if isinstance(value, str):
        return value.replace("\x00", "").encode("utf-8", "replace").decode()
    if isinstance(value, dict):
        return {_storable(key): _storable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_storable(item) for item in value]
    return value


def _parse(schema, payload, run):
    try:
        model = schema.model_validate(payload)
        problems = [] if str(model.id).strip() else ["id: blank"]
    except ValidationError as exc:
        problems = [
            f"{'.'.join(str(part) for part in error['loc']) or 'record'}: {error['msg']}"
            for error in exc.errors(include_url=False)[:5]
        ]
    if problems:
        record_error(
            run,
            SyncError.Kind.PARSE,
            "; ".join(problems),
            quartzy_id=_raw_id(payload),
            payload=payload,
        )
        return None
    # API drift is worth knowing about: log each undocumented field once per process.
    for name in sorted(schemas.unknown_fields(model)):
        label = f"{schema.__name__}.{name}"
        _warn_once(f"field:{label}", "Quartzy sent an undocumented field: %s", label)
    return model


def _clip(value, length):
    return str(value).strip()[:length] if value is not None else ""


def _fit(value):
    """``value`` as it fits a DecimalField(14, 4), or None."""
    if value is None or not value.is_finite() or abs(value) >= _MAX_DECIMAL:
        return None
    return value.quantize(_FOUR_PLACES)


def _date(value):
    try:
        return date.fromisoformat(str(value).strip()[:10]) if value else None
    except ValueError:
        return None


def _money_unit():
    unit = settings.QUARTZY_MONEY_UNIT
    if unit in MONEY_UNITS:
        return unit
    _warn_once(
        "money-unit", "QUARTZY_MONEY_UNIT=%r is not one of %s; using auto", unit, MONEY_UNITS
    )
    return "auto"


def _new_items_at():
    mode = settings.QUARTZY_NEW_ITEMS_AT
    if mode in (FIRST_PAGE, LAST_PAGE, UNKNOWN):
        return mode
    _warn_once("new-items-at", "QUARTZY_NEW_ITEMS_AT=%r is not recognised; using unknown", mode)
    return UNKNOWN


def _save(qlab, **fields):
    """Update a few QuartzyLab fields without overwriting what other runs saved meanwhile."""
    QuartzyLab.objects.filter(pk=qlab.pk).update(**fields)
    for name, value in fields.items():
        setattr(qlab, name, value)


# ---------------------------------------------------------------------------------------
# Order requests

# What an update writes; requested_for and hot_until belong to the app.
_MIRRORED = [
    "lab", "status", "item_name", "vendor_name_raw", "catalog_number_raw", "unit_size_raw",
    "quantity", "unit_price", "total_price", "currency", "money_unit_used", "quartzy_type",
    "requested_by_alias", "created_by_alias", "requested_by", "requested_at",
    "quartzy_updated_at", "app_url", "spend_tracking_code", "is_urgent", "notes",
    "payload_sha256", "raw_payload", "last_seen_at", "deleted_in_quartzy_at",
    "supplier_product", "product", "resolution_status", "resolution_attempted_at",
]  # fmt: skip
_RESOLUTION = ["supplier_product", "product", "resolution_status", "resolution_attempted_at"]
# Resolutions that made the line a product of its own...
_OWN_PRODUCT = (
    OrderRequest.Resolution.NEW,
    OrderRequest.Resolution.GENERIC,
    OrderRequest.Resolution.NO_CATALOG,
)
# ... and those that make a fresh one every time.
_ONE_PER_LINE = (OrderRequest.Resolution.GENERIC, OrderRequest.Resolution.NO_CATALOG)


def upsert_order_request(qlab, payload, *, via, run, fetched_at=None):
    """Mirror one order-request payload. Returns ``(obj, created, changed)``, or
    ``(None, False, False)`` when the record could not be taken in (a SyncError says why).

    A sha256 of the canonical payload decides whether anything changed; Quartzy's updated_at
    is only a hint and may be null. A new status writes an OrderStatusObservation. The
    catalog is resolved on create, and again when the vendor, the catalog # or the type's item
    type changes (the old rows are then retired), or once a day while it failed even if
    nothing changed. A line without a usable catalog # that is only renamed renames its
    product instead (``_rename_product``).

    ``fetched_at`` is when the payload was read from Quartzy (default: now), and becomes the
    row's ``last_seen_at``. A changed payload fetched before the read that last wrote or
    confirmed the row, or before the 404 that marked it deleted, is older than what is stored:
    it is skipped and counted in the run's note (STALE_NOTE), so a slow backfill or an
    overlapping run can't write back a status, or a status change, that is no longer true.

    The request's lab is its Quartzy lab's as stored when the row is written, not as ``qlab``
    was when the run started (``_follow_remap``). A request created, or resolved differently
    (``_resolution``), is noted on ``run`` for ``signals.requests_synced``.
    """
    run.seen += 1
    fetched_at = fetched_at or timezone.now()
    payload = _storable(payload)
    model = _parse(schemas.OrderRequest, payload, run)
    if model is None:
        return None, False, False
    digest = payload_sha256(payload)
    error = None
    for _attempt in range(2):
        try:
            with transaction.atomic():
                obj, created, changed, resolution_changed = _upsert_request(
                    qlab, model, payload, digest, via, run, fetched_at
                )
        except IntegrityError as exc:
            error = exc  # most likely another run created it just now: retry as an update
            continue
        except Exception as exc:
            logger.exception("Could not mirror Quartzy order request %s", model.id)
            error = exc
            break
        run.new += created
        run.changed += changed
        if created or resolution_changed:
            run.synced_request_ids.add(obj.pk)
        return obj, created, changed
    record_error(
        run,
        SyncError.Kind.UPSERT,
        f"{type(error).__name__}: {error}",
        quartzy_id=model.id,
        payload=payload,
    )
    return None, False, False


def _upsert_request(qlab, model, payload, digest, via, run, fetched_at):
    """``upsert_order_request``'s work, in its transaction. Returns ``(obj, created, changed,
    resolution_changed)``."""
    obj = _locked_request(
        "requested_by_alias", "created_by_alias", "quartzy_type", quartzy_id=model.id
    )
    if obj is not None and obj.payload_sha256 != digest and fetched_at < _last_read(obj):
        run.add_count(STALE_NOTE)
        return obj, False, False, False
    _follow_remap(qlab)
    if obj is not None and obj.payload_sha256 == digest:
        _touch(obj, qlab, fetched_at)
        old_resolution = _resolution(obj)
        if _retry_due(obj, timezone.now()):
            _resolve_catalog(obj, run)
            obj.save(update_fields=_RESOLUTION)
        return obj, False, False, _resolution(obj) != old_resolution
    created = obj is None
    if created:
        obj = OrderRequest(quartzy_lab=qlab, quartzy_id=model.id, first_seen_at=fetched_at)
    old_status = "" if created else obj.status
    old_identity, old_name = _identity(obj), obj.item_name
    old_resolution = _resolution(obj)
    _apply_request(obj, qlab, model, payload)
    obj.payload_sha256 = digest
    obj.last_seen_at = fetched_at
    obj.deleted_in_quartzy_at = None
    left = None
    if created or obj.resolution_status == OrderRequest.Resolution.UNRESOLVED:
        _resolve_catalog(obj, run)
    elif _identity(obj) != old_identity:
        left = _resolve_again(obj, run, line_changed=True)
    elif obj.item_name != old_name and obj.resolution_status in _ONE_PER_LINE:
        _rename_product(obj)
    obj.save(update_fields=None if created else _MIRRORED)
    if left is not None:
        _retire([left], run)
    if obj.status != old_status:
        OrderStatusObservation.objects.create(
            order_request=obj,
            from_status=old_status,
            to_status=obj.status,
            observed_at=fetched_at,
            quartzy_updated_at=obj.quartzy_updated_at,
            via=via,
        )
    return obj, created, not created, _resolution(obj) != old_resolution


def _locked_request(*related, **lookup):
    """The order request ``lookup`` finds, locked until the transaction ends, with the
    relations ``related`` loaded; None when there is none.

    The lock is taken first and the row read after it, in a statement of its own. A
    SELECT ... FOR UPDATE that has to wait for another transaction's lock re-reads the locked
    row once that one commits, but keeps the rows it joins from before: a supplier product,
    product, type or alias that transaction created would come back as None, although the
    request points at it."""
    rows = OrderRequest.objects.select_for_update().filter(**lookup)
    pk = rows.values_list("pk", flat=True).first()
    if pk is None:
        return None
    return OrderRequest.objects.select_related(*related).get(pk=pk)


def _follow_remap(qlab):
    """Take which lab ``qlab`` maps onto from the database. A run holds the QuartzyLab it
    started with, and must not write the lab of before onto requests that an admin's re-map
    moved meanwhile (QuartzyLab.save)."""
    lab_id = QuartzyLab.objects.filter(pk=qlab.pk).values_list("lab", flat=True).first()
    if lab_id is not None and lab_id != qlab.lab_id:
        qlab.lab_id = lab_id


def _resolution(obj):
    """What the duplicate guard checks a request on: how it resolved, and to what."""
    return obj.resolution_status, obj.supplier_product_id, obj.product_id


def _last_read(obj):
    """When the newest read of ``obj`` so far was fetched: the one that last wrote or
    confirmed it, or the 404 that marked it deleted."""
    return max(filter(None, (obj.last_seen_at, obj.deleted_in_quartzy_at)))


def _identity(obj):
    """What a line's catalog resolution depends on (the name only names a new product)."""
    qtype = obj.quartzy_type
    return obj.vendor_name_raw, obj.catalog_number_raw, qtype.item_type_id if qtype else None


def _apply_request(obj, qlab, model, payload):
    obj.lab_id = qlab.lab_id  # a mirrored request belongs to its Quartzy lab's lab
    status = _clip(model.status, 20).upper()
    if status:
        if status not in OrderRequest.Status.values:
            _warn_once(
                f"status:{status}", "Quartzy sent an unknown status %r; kept as sent", status
            )
        obj.status = status
    obj.item_name = _clip(model.item_name, 500)
    obj.vendor_name_raw = _clip(model.vendor_name, 300)
    obj.catalog_number_raw = _clip(model.catalog_number, 200)
    obj.unit_size_raw = _clip(model.unit_size, 200)
    obj.quantity = _fit(parse_quantity(model.quantity))
    obj.unit_price = _fit(to_decimal(model.unit_price))
    total, currency, unit = money_amount(
        model.total_price, _money_unit(), model.unit_price, model.quantity
    )
    obj.total_price = _fit(total)
    obj.currency = (currency or "")[:3]
    obj.money_unit_used = unit
    obj.quartzy_type = _quartzy_type(qlab, model.type)
    obj.requested_by_alias = resolve_person(payload.get("requested_by"))
    obj.created_by_alias = resolve_person(payload.get("created_by"))
    obj.requested_by_id = _requester_user_id(obj)
    obj.requested_at = model.requested_at
    obj.quartzy_updated_at = model.updated_at
    obj.app_url = _clip(model.app_url, 500)
    code = model.spend_tracking_code
    obj.spend_tracking_code = _clip(code.name if code else None, 200)
    obj.is_urgent = model.is_urgent
    obj.notes = model.notes or ""
    obj.raw_payload = payload


def _requester_user_id(obj):
    alias = obj.requested_by_alias or obj.created_by_alias
    return alias.user_id if alias else None


def _touch(obj, qlab, fetched_at):
    """An unchanged payload: record the sighting, keep the request in its Quartzy lab's lab,
    and follow alias fixes made in the admin."""
    updates = {}
    if fetched_at > obj.last_seen_at:
        updates["last_seen_at"] = fetched_at
    if obj.deleted_in_quartzy_at is not None and fetched_at >= obj.deleted_in_quartzy_at:
        updates["deleted_in_quartzy_at"] = None  # listed again since, so it exists after all
    if obj.lab_id != qlab.lab_id:
        updates["lab_id"] = qlab.lab_id
    user_id = _requester_user_id(obj)
    if user_id != obj.requested_by_id:
        updates["requested_by_id"] = user_id
    if updates:
        OrderRequest.objects.filter(pk=obj.pk).update(**updates)
    for name, value in updates.items():
        setattr(obj, name, value)


def _retry_due(obj, now):
    """Whether to try a failed resolution again although the payload is unchanged: once a day.
    Most history is never edited again, so waiting for a change would keep it from the guard
    for good."""
    attempted = obj.resolution_attempted_at
    return obj.resolution_status == OrderRequest.Resolution.UNRESOLVED and (
        attempted is None or now - attempted > RESOLVE_RETRY_AFTER
    )


def _rename_product(obj):
    """A line without a usable catalog # (or with a generic one) was renamed in Quartzy. Its
    product was made from the old name for this line alone, so it takes the new name, unless
    someone has reviewed it or another request uses it too."""
    product, name = obj.product, " ".join(obj.item_name.split())[:500]
    if (
        product is None
        or not name
        or product.name == name
        or not product.provisional
        or product.merged_into_id is not None
        or product.order_requests.exclude(pk=obj.pk).exists()
    ):
        return
    product.name = name
    product.save(update_fields=["name", "updated_at"])


def _quartzy_type(qlab, qtype):
    if qtype is None or not qtype.id:
        return None
    name = _clip(qtype.name, 200)
    obj = QuartzyType.objects.filter(quartzy_lab=qlab, quartzy_id=qtype.id).first()
    if obj is None:
        return _create_type(qlab, qtype.id, name)
    if name and obj.name != name:
        obj.name = name
        obj.save(update_fields=["name"])
    return obj


def _create_type(qlab, quartzy_id, name):
    item_type = suggest_item_type(name)
    obj, _created = QuartzyType.objects.get_or_create(
        quartzy_lab=qlab,
        quartzy_id=quartzy_id,
        defaults={"name": name, "item_type": item_type, "suggested": item_type is not None},
    )
    return obj


def _resolve_catalog(obj, run):
    """Resolve ``obj``'s line against the catalog and set the result on it (not saved). A
    failure becomes a SyncError and leaves the line unresolved. Returns whether it worked."""
    obj.resolution_attempted_at = timezone.now()
    # An unconfirmed suggestion counts too: it is the best guess until someone maps the type,
    # and correcting the mapping then resolves the type's requests again (QuartzyType.save).
    item_type = obj.quartzy_type.item_type if obj.quartzy_type else None
    try:
        with transaction.atomic():
            resolution = resolve_supplier_product(
                vendor_name=obj.vendor_name_raw,
                catalog_number=obj.catalog_number_raw,
                item_name=obj.item_name,
                item_type=item_type,
                unit_size=obj.unit_size_raw,
                unit_price=obj.unit_price,
                currency=obj.currency,
            )
    except Exception as exc:
        logger.exception("Catalog resolution failed for Quartzy request %s", obj.quartzy_id)
        record_error(
            run, SyncError.Kind.RESOLVE, f"{type(exc).__name__}: {exc}", quartzy_id=obj.quartzy_id
        )
        obj.supplier_product = obj.product = None
        obj.resolution_status = OrderRequest.Resolution.UNRESOLVED
        return False
    obj.supplier_product = resolution.supplier_product
    obj.product = resolution.product
    obj.resolution_status = str(resolution.status)
    return True


class _Left(NamedTuple):
    """A listing a request left when it was resolved again, for ``_retire``."""

    supplier_product: SupplierProduct
    status: str  # what the request had been resolved as
    product: Product | None  # where it went; None when the new resolution failed


def _resolve_again(obj, run, *, line_changed):
    """Resolve ``obj`` again and set the result on it (not saved), inside a transaction.
    Returns the ``_Left`` for the listing it left, or None.

    When only the catalog or the type's mapping changed (not ``line_changed``), a fresh
    generic or no-catalog product just like its own (one per line either way) is rolled back,
    rows and history, and it keeps its own; a failure also keeps what it had. Finding its own
    listing again ("exact" on it), it keeps its resolution status: still "new" or
    "base_variant". A line that has lost its own rows (an overlapping re-resolution used to
    leave it "generic" with none) keeps the fresh ones.
    """
    # Put back by id, as stored, which no cached row of before can get wrong (_locked_request).
    old = (obj.supplier_product_id, obj.product_id, obj.resolution_status)
    listing, status = obj.supplier_product, obj.resolution_status
    savepoint = transaction.savepoint_create()
    resolved = _resolve_catalog(obj, run)
    fresh_copy = resolved and status in _ONE_PER_LINE and obj.resolution_status == status
    if fresh_copy and not line_changed and listing is not None:
        transaction.savepoint_rollback(savepoint)
        obj.supplier_product_id, obj.product_id, obj.resolution_status = old
        return None
    transaction.savepoint_commit(savepoint)
    if listing is None:
        return None
    if obj.supplier_product_id == listing.pk:
        obj.resolution_status = status
        return None
    if not line_changed and not resolved:
        obj.supplier_product_id, obj.product_id, obj.resolution_status = old
        return None
    return _Left(listing, status, obj.product)


def _retire(left, run):
    """Retire the listings requests left, and their products, once nothing uses them
    (catalog.services.retire_orphans: provisional rows only). A product that was the lines'
    own (resolved "new", "generic" or "no_catalog") and that they all left for one product
    becomes a redirect to it; one they merely matched, or left for several, is deleted, and
    the products that redirect to it with it. Returns retire_orphans' counts, summed."""
    counts = Counter()
    by_product = defaultdict(list)
    for entry in left:
        by_product[entry.supplier_product.product_id].append(entry)
    for entries in by_product.values():
        targets = {entry.product.pk if entry.product else None for entry in entries}
        own = any(entry.status in _OWN_PRODUCT for entry in entries)
        replacement = entries[0].product if own and len(targets) == 1 else None
        try:
            retired = retire_orphans(
                [entry.supplier_product for entry in entries], replacement=replacement
            )
        except Exception as exc:
            logger.exception("Could not retire the catalog rows requests left")
            record_error(run, SyncError.Kind.RESOLVE, f"Retiring: {type(exc).__name__}: {exc}")
            continue
        counts.update(retired)
    return counts


def resolve_again(request_ids, run):
    """Resolve these mirrored requests again, in this order and each in its own transaction,
    with their current fields and their Quartzy type's current item type: after the type's
    mapping changed (tasks.resolve_type) or after a catalog fix (manage.py quartzy_resolve).

    A request whose resolution comes out the same keeps it (``_resolve_again``). One that
    moves gets its new supplier product and product, is noted on ``run`` for
    ``signals.requests_synced``, and the rows it left are retired at the end (``_retire``).

    Each request found its listing in turn and recorded its unit size, price and currency on
    it (catalog.services), so the last one's are there now. At the end the listings take those
    of their newest requests again (``_newest_on_listings``), as after the backfill, whose
    newest-first pages give the newest requests the lowest ids.

    Returns a Counter of "changed", "unchanged" and "failed" requests, plus the counts of
    ``retire_orphans``.
    """
    counts, left = Counter(), []
    for pk in request_ids:
        try:
            with transaction.atomic():
                outcome, entry, moved = _resolve_one_again(pk, run)
        except Exception as exc:
            logger.exception("Could not resolve order request #%s again", pk)
            record_error(run, SyncError.Kind.RESOLVE, f"#{pk}: {type(exc).__name__}: {exc}")
            outcome, entry, moved = "failed", None, False
        counts[outcome] += 1
        if moved:
            run.synced_request_ids.add(pk)
        if entry is not None:
            left.append(entry)
    counts.update(_retire(left, run))
    listings = OrderRequest.objects.filter(pk__in=request_ids).exclude(supplier_product=None)
    _newest_on_listings(set(listings.values_list("supplier_product", flat=True)))
    return counts


def _resolve_one_again(pk, run):
    """Resolve request ``pk`` again, in the caller's transaction. Returns ``(outcome, entry,
    moved)``: "changed", "unchanged", "failed" or "gone"; the ``_Left`` for ``_retire``, or
    None; and whether its resolution moved. A failure moves a line that had lost its rows:
    it is "unresolved" now, and tried again daily (``_retry_due``)."""
    obj = _locked_request("quartzy_type__item_type", "supplier_product", "product", pk=pk)
    if obj is None:
        return "gone", None, False
    run.seen += 1
    before = _resolution(obj)
    errors = run.errors
    entry = _resolve_again(obj, run, line_changed=False)
    obj.save(update_fields=_RESOLUTION)
    moved = _resolution(obj) != before
    if run.errors > errors:  # the resolution failed; its SyncError says why
        return "failed", entry, moved
    if not moved:
        return "unchanged", entry, False
    run.changed += 1
    return "changed", entry, True


# ---------------------------------------------------------------------------------------
# Reads by id, and when their 404s are deletions


class _ById:
    """Reads by id of one kind of mirrored record (``model``: order requests or inventory
    items) in one run. ``check(ids)`` reads them, then marks deleted in Quartzy the ones it
    answered 404 for, when that is believable; it returns those missing ids.

    One record Quartzy refuses or fails on (a 403 for a request moved to a lab the token can't
    see, a persistent 500) is a SyncError and the others are read all the same; a refused
    token, a rate limit or BY_ID_FAILURES_IN_A_ROW failures in a row that look like an outage
    (``_outage``) stop the run. Refusals never do, however many: the ones Quartzy refuses are
    no longer listed, so they gather in the hot set and the deletion suspects, next to each
    other. Every id read is stamped ``by_id_checked_at``, answered or not, and reads by id go
    least recently looked at first (``_LOOKED_AT``), so a failing one moves to the back.
    """

    def __init__(self, client, qlab, run, *, model, fetch, mirror, what):
        self.client, self.qlab, self.run = client, qlab, run
        # ``fetch(id)`` is the client's GET by id; ``mirror(payload, fetched_at)`` takes in
        # what it served; ``what`` names the record ("request").
        self.model, self.fetch, self.mirror, self.what = model, fetch, mirror, what

    def check(self, ids):
        found, missing = self._read_all(ids)
        answered = len(found) + len(missing)
        if missing and self._believable(ids, missing, answered):
            self._mark_deleted(missing)
        return missing

    def read(self, quartzy_id):
        """GET one record by id and mirror it; None when Quartzy answers 404."""
        fetched_at = timezone.now()
        payload = self.fetch(quartzy_id)
        if payload is not None:
            self.mirror(payload, fetched_at)
        return payload

    def _read_all(self, ids):
        found, missing, failures = [], [], 0
        for quartzy_id in ids:
            checked_at = timezone.now()
            try:
                payload = self.read(quartzy_id)
            except QuartzyError as exc:
                failures = failures + 1 if _outage(exc) else 0  # a refusal is an answer
                if exc.status in BY_ID_STOP_STATUSES or failures >= BY_ID_FAILURES_IN_A_ROW:
                    raise
                record_error(self.run, SyncError.Kind.READ, str(exc), quartzy_id=quartzy_id)
            else:
                failures = 0
                (found if payload is not None else missing).append(quartzy_id)
            self.model.objects.filter(quartzy_id=quartzy_id).update(by_id_checked_at=checked_at)
        return found, missing

    def _believable(self, ids, missing, answered):
        """Whether these 404s mean deletions rather than a token or permission problem (plan
        §8). No, and the run is aborted and the system owners are told, when:

        - at least MIN_404S_FOR_SHARE came back missing, and more than
          ``sync.max_404_fraction`` of the ids Quartzy answered;
        - Quartzy no longer shows the lab itself;
        - the canary, the live record of the lab seen most recently other than these (one
          the latest list page returned, as a rule), is refused or missing by id as well.
        """
        qlab, what = self.qlab, f"{self.what}s"
        limit = float(get_policy("sync.max_404_fraction", qlab.lab))
        if len(missing) >= MIN_404S_FOR_SHARE and len(missing) > limit * answered:
            _abort(
                self.run,
                qlab,
                f"{len(missing)} of {answered} {what} read by id returned 404, more than "
                f"{limit:.0%}. Nothing was marked deleted. Check that the Quartzy token still "
                f"sees {qlab}.",
            )
            return False
        try:
            self.client.lab(qlab.quartzy_id)
        except QuartzyError as exc:
            if exc.status != 404:
                raise
            _abort(
                self.run,
                qlab,
                f"Quartzy no longer shows the lab {qlab} to this token, so its 404s prove "
                "nothing. Nothing was marked deleted.",
            )
            return False
        canary = (
            self.model.objects.filter(quartzy_lab=qlab, deleted_in_quartzy_at=None)
            .exclude(quartzy_id__in=ids)
            .order_by("-last_seen_at")
            .values_list("quartzy_id", flat=True)
            .first()
        )
        if canary is None:
            return True  # nothing else to try: the lab check has to do
        try:
            refused = None if self.read(canary) is not None else "404"
        except QuartzyAuthError as exc:
            refused = str(exc.status)
        if refused:
            _abort(
                self.run,
                qlab,
                f"{len(missing)} {what} read by id returned 404, and {canary}, which Quartzy "
                f"listed lately, was refused too ({refused}): a token or permission problem, "
                f"not deletions. Nothing was marked deleted. Check that the Quartzy token still "
                f"sees {qlab}.",
            )
            return False
        return True

    def _mark_deleted(self, missing):
        """Mark the missing rows deleted, at most ``sync.max_tombstones_per_day`` of them per
        Quartzy lab in any 24 hours: past the cap, the rest wait for a later run and the
        system owners are told, once a day. A last guard against mass false deletions."""
        qlab, now = self.qlab, timezone.now()
        cap = int(get_policy("sync.max_tombstones_per_day", qlab.lab))
        rows = self.model.objects.filter(quartzy_lab=qlab)
        recent = rows.filter(deleted_in_quartzy_at__gte=now - timedelta(days=1)).count()
        pending = rows.filter(quartzy_id__in=missing, deleted_in_quartzy_at=None).order_by("pk")
        pks = list(pending.values_list("pk", flat=True))
        marked = self.model.objects.filter(pk__in=pks[: max(0, cap - recent)]).update(
            deleted_in_quartzy_at=now
        )
        self.run.changed += marked
        self.run.add_note(f"Marked {marked} {self.what}(s) deleted in Quartzy (404 by id).")
        held = len(pks) - marked
        if not held:
            return
        self.run.add_count("Deletions held back by sync.max_tombstones_per_day", held)
        notify_admins(
            qlab.lab,
            title=f"Quartzy sync: the daily deletion cap was reached for {qlab}",
            message=(
                f"{held} more {self.what}(s) of {qlab} returned 404 by id, past the cap of "
                f"{cap} marked deleted in any 24 hours (the policy "
                "sync.max_tombstones_per_day), so they wait for a later run. If that many "
                "deletions are real, raise the cap in Admin → Policy settings; if not, check "
                "the Quartzy token."
            ),
            subject_obj=qlab,
            period_key=f"tombstones-{timezone.localdate():%Y-%m-%d}",
        )


def _outage(exc):
    """Whether a failed read looks like Quartzy being down rather than refusing one record:
    no answer at all (a timeout, a dropped connection), or a 5xx."""
    return exc.status is None or exc.status >= 500


def _requests_by_id(client, qlab, run):
    def mirror(payload, fetched_at):
        upsert_order_request(
            qlab, payload, via=OrderStatusObservation.Via.POLL, run=run, fetched_at=fetched_at
        )

    return _ById(
        client, qlab, run, model=OrderRequest, fetch=client.order_request, mirror=mirror,
        what="request",
    )  # fmt: skip


def _abort_if_unlisted(run, qlab):
    """Abort ``run`` when Quartzy listed no requests at all for ``qlab``, although the mirror
    holds live ones: a lab id that no longer matches, or a token that lost sight of the lab,
    rather than a lab that emptied. Then neither the sync heartbeat nor a complete sweep pass
    may vouch for it. Returns whether it aborted."""
    mirrored = OrderRequest.objects.filter(
        quartzy_lab=qlab, quartzy_id__isnull=False, deleted_in_quartzy_at=None
    ).count()
    if not mirrored:
        return False
    _abort(
        run,
        qlab,
        f"Quartzy listed no requests for {qlab}, but {mirrored} are mirrored as live. Nothing "
        f"was changed. Check that the Quartzy token still sees the lab, and that its Quartzy "
        f"id is still {qlab.quartzy_id}.",
    )
    return True


# ---------------------------------------------------------------------------------------
# Reading list pages


def _list_page(fetch, page, **filters):
    """``fetch(page=page, **filters)``: one list page, or None when Quartzy refuses a page
    above the base the way an API refuses one past the end of the list. Refusing the base
    page is an error, as is anything else."""
    try:
        return fetch(page=page, **filters)
    except QuartzyError as exc:
        if exc.status in PAST_END_STATUSES and page > settings.QUARTZY_PAGE_BASE:
            return None
        raise


def _ids(records):
    return frozenset(filter(None, map(_raw_id, records)))


def _repeats(ids, before):
    """Whether a page lists the very records of the page before it: past the end of a list
    whose API serves the last page again (or ignores ``page``)."""
    return bool(ids) and ids == before


class _Page(NamedTuple):
    size: int
    ids: frozenset
    new: int
    end: bool = False  # past the end of the list (see _Pages)


class _Pages:
    """Order-request list pages of one Quartzy lab, read and mirrored within one run. Each
    page is fetched once per run unless ``fresh=True``.

    ``read`` reports a page past the end of the list as ``end``: it came back empty, Quartzy
    refused it (``_list_page``), or it repeats the page before it, as far as this run has read
    that one (``_repeats``).
    """

    def __init__(self, client, qlab, run, *, via=OrderStatusObservation.Via.POLL):
        self.client, self.qlab, self.run, self.via = client, qlab, run, via
        self.base = settings.QUARTZY_PAGE_BASE
        self.fetches = 0
        # Whether a page past the end came back empty or refused: then this list doesn't end
        # in a copy of its last page, and a page with records is one of the list.
        self.ends_empty = False
        self._read = {}

    def __contains__(self, page):
        return page in self._read

    @property
    def lists_nothing(self):
        """The base page was read and came back empty."""
        return self.base in self._read and not self._read[self.base].size

    def read(self, page, *, fresh=False):
        if fresh or page not in self._read:
            self._read[page] = self._fetch(page)
        result, before = self._read[page], self._read.get(page - 1)
        return result._replace(end=not result.size or _repeats(result.ids, before and before.ids))

    def real(self, page):
        """Whether ``page`` is a page of the list. One with records may still be a copy of the
        last page, which only the page before it tells: that is read too, unless this list was
        seen to end in an empty or refused page."""
        if self.read(page).end:
            return False
        if page == self.base or self.ends_empty:
            return True
        return not self.read(page - 1).end and not self.read(page).end

    def _fetch(self, page):
        fetched_at = timezone.now()
        records = _list_page(self.client.order_requests, page, lab_id=self.qlab.quartzy_id)
        self.fetches += 1
        self.run.pages += 1
        if not records:
            self.ends_empty = self.ends_empty or page > self.base
            return _Page(0, frozenset(), 0)
        new = 0
        for payload in records:
            _obj, created, _changed = upsert_order_request(
                self.qlab, payload, via=self.via, run=self.run, fetched_at=fetched_at
            )
            new += created
        return _Page(len(records), _ids(records), new)


def _list_pages(fetch, run, **filters):
    """Yield ``(page, records, fetched_at)`` from the first page to the end of the list: a
    page that comes back empty or refused (``_list_page``), or one that repeats the page
    before it (``_repeats``)."""
    page = settings.QUARTZY_PAGE_BASE
    previous = None
    for _ in range(MAX_PAGES):
        fetched_at = timezone.now()
        records = _list_page(fetch, page, **filters)
        run.pages += 1
        ids = _ids(records or [])
        if not records or _repeats(ids, previous):
            return
        previous = ids
        yield page, records, fetched_at
        page += 1


# ---------------------------------------------------------------------------------------
# Head probe, rolling sweep, hot set


def head_probe(client, qlab, run):
    """Mirror new requests from the end of the list where Quartzy inserts them.

    - "first_page": read pages base, base+1, ... while a page holds ids we didn't know, up to
      the policy ``sync.head_max_pages``.
    - "last_page": read from the page before the last known one to the end of the list, and
      remember where the list ends (``head_cursor["last_page"]``).
    - "unknown" (until the probe pins it): both.

    An empty first page while the mirror holds live requests of the lab aborts the run
    (``_abort_if_unlisted``): that is a lab Quartzy no longer lists, not a quiet cycle.
    """
    mode = _new_items_at()
    base = settings.QUARTZY_PAGE_BASE
    limit = int(get_policy("sync.head_max_pages", qlab.lab))
    pages = _Pages(client, qlab, run)
    if mode in (FIRST_PAGE, UNKNOWN):
        _scan_from_first_page(pages, base, limit)
    if mode in (LAST_PAGE, UNKNOWN) and not pages.lists_nothing:
        _scan_from_last_page(pages, qlab, base, limit)
    if pages.lists_nothing:
        _abort_if_unlisted(run, qlab)


def _scan_from_first_page(pages, base, limit):
    # Requests inserted at the top push the others down: if that happens during the scan, the
    # next page starts with ids the previous one ended with, and the first page is read once
    # more, so a request inserted mid-scan is still found in this cycle.
    page, previous, shifted = base, frozenset(), False
    while pages.fetches < limit:
        result = pages.read(page)
        if result.end:
            break
        shifted = shifted or bool(previous & result.ids)
        if not result.new:
            break
        previous = result.ids
        page += 1
    if shifted and pages.fetches < limit:
        pages.read(base, fresh=True)


def _scan_from_last_page(pages, qlab, base, limit):
    last = qlab.head_cursor.get("last_page")
    if not isinstance(last, int) or last < base:
        last = _find_last_page(pages, base)  # once; the backfill normally records it
        if last is None:
            return  # no requests at all yet
        _save(qlab, head_cursor={**qlab.head_cursor, "last_page": last})
    stop = pages.fetches + limit
    first = page = max(base, last - 1)
    last_seen = None
    while pages.fetches < stop:
        # Always fresh: new requests land on these pages while we read them.
        if pages.read(page, fresh=True).end:
            # The list got shorter if the first page read is past its end already, or is a
            # copy of the last page (clamping), which only the page before it tells.
            shorter = page == first and page > base
            if shorter or (last_seen == first and not pages.real(first)):
                last_seen = _find_last_page(pages, base)
            break
        last_seen = page
        page += 1
    if last_seen is not None and last_seen != qlab.head_cursor.get("last_page"):
        _save(qlab, head_cursor={**qlab.head_cursor, "last_page": last_seen})


def _find_last_page(pages, base):
    """The last page of the list by exponential, then binary search (``_Pages.real``); None
    when there is none."""
    if not pages.real(base):
        return None
    known, step = 0, 1  # offsets from base; ``known`` is a page of the list
    for _ in range(LAST_PAGE_SEARCH_STEPS):
        if not pages.real(base + step):
            break
        known, step = step, step * 2
    else:
        return base + known
    past = step
    while past - known > 1:
        middle = (known + past) // 2
        if pages.real(base + middle):
            known = middle
        else:
            past = middle
    return base + known


def rolling_sweep(client, qlab, run):
    """Re-read ``sync.sweep_pages_per_cycle`` pages from the saved cursor, at least two, so
    that a page past the end that repeats the last one is told by the page before it. At the
    end of the list the sweep wraps to the first page (and goes on, within budget).

    An empty first page while the mirror holds live requests of the lab aborts the run
    (``_abort_if_unlisted``) and saves nothing: no complete pass, so no deletion suspects.
    """
    base = settings.QUARTZY_PAGE_BASE
    budget = max(2, int(get_policy("sync.sweep_pages_per_cycle", qlab.lab)))
    pages = _Pages(client, qlab, run)
    page = qlab.sweep_cursor if qlab.sweep_cursor >= base else base
    updates, attempts, failure = {}, 0, None
    while attempts < budget and page not in pages:
        attempts += 1
        if page == base:
            updates["sweep_pass_started_at"] = timezone.now()
        try:
            result = pages.read(page)
        except QuartzyError as exc:
            if exc.status is None or not 500 <= exc.status < 600:
                raise  # unreachable, refused or rate-limited: no point reading on
            # One page Quartzy can't serve must not stall the sweep for good: skip it, and
            # the pass it belongs to no longer counts as complete.
            failure = exc
            run.errors += 1
            run.add_note(f"Skipped page {page}: {exc}")
            updates["sweep_pass_started_at"] = None
            page += 1
            continue
        if not result.end:
            page += 1
            continue
        if page == base and _abort_if_unlisted(run, qlab):
            return
        started = updates.get("sweep_pass_started_at", qlab.sweep_pass_started_at)
        if started is not None:
            # A whole pass since ``started``: a request not seen since then wasn't listed.
            updates["full_sweep_started_at"] = started
        page = base
    if failure is not None and not pages.fetches:
        raise failure  # nothing could be read: Quartzy is down, not one bad page
    _save(qlab, sweep_cursor=page, **updates)


def hot_set_ids(qlab, *, now=None):
    """Quartzy ids worth re-reading by id now, at most HOT_SET_MAX: open requests first seen
    (and requested) in the last 2 days, plus any flagged with ``hot_until``. Requests a list
    page showed in the last 10 minutes are left out; they are fresh already."""
    now = now or timezone.now()
    recent = now - HOT_RECENT
    new_and_open = (
        Q(first_seen_at__gte=recent)
        & (Q(requested_at__gte=recent) | Q(requested_at__isnull=True))
        & ~Q(status__in=OrderRequest.CLOSED_STATUSES)
    )
    wanted = (
        OrderRequest.objects.filter(quartzy_lab=qlab, quartzy_id__isnull=False)
        .live()
        .filter(new_and_open | Q(hot_until__gt=now))
        .exclude(last_seen_at__gte=now - HOT_FRESH)
        .order_by(_LOOKED_AT, "pk")
    )
    return list(wanted.values_list("quartzy_id", flat=True)[:HOT_SET_MAX])


def refresh_hot_set(client, qlab, run, ids):
    """Re-read requests by id (GET /order-requests/{id}). A 404 marks the request deleted in
    Quartzy, unless the token is the likelier problem (``_ById._believable``); then the run is
    aborted and nothing is marked."""
    ids = [str(quartzy_id) for quartzy_id in dict.fromkeys(ids) if quartzy_id]
    return _requests_by_id(client, qlab, run).check(ids)


def confirm_unlisted_requests(client, qlab, run, *, limit=UNLISTED_PER_RUN):
    """Read by id a few requests the last complete sweep pass didn't list: deleted in Quartzy,
    or skipped while pages shifted. Only a 404 marks one deleted."""
    if qlab.full_sweep_started_at is None:
        return []
    live = OrderRequest.objects.filter(quartzy_lab=qlab, quartzy_id__isnull=False).live()
    unlisted = live.filter(last_seen_at__lt=qlab.full_sweep_started_at).order_by(_LOOKED_AT, "pk")
    ids = list(unlisted.values_list("quartzy_id", flat=True)[:limit])
    return _requests_by_id(client, qlab, run).check(ids) if ids else []


# ---------------------------------------------------------------------------------------
# Labs and types


def sync_labs(client, lab, run=None):
    """Mirror every Quartzy Lab the token sees (every page to the end of the list). LabStock
    serves one lab for now, so new ones map onto ``lab``; mapped ones keep their lab.

    A lab met for the first time is active, so synced, only when its id is in
    settings.QUARTZY_LAB_IDS, or, while that is empty, when it is the only lab the token sees
    and ``lab`` has no Quartzy lab yet. Any other is recorded inactive: a collaborator's lab
    the token's owner belongs to must not become this lab's data (plan §11). Someone activates
    it in the admin when it is the lab's.
    """
    found, models, listed = [], [], 0
    with _run_for(run, SyncRun.Kind.LABS, None, client) as run:
        for _page, records, _fetched_at in _list_pages(client.labs, run):
            for payload in records:
                run.seen += 1
                listed += 1
                model = _parse(schemas.Lab, _storable(payload), run)
                if model is not None:
                    models.append(model)
        first = listed == 1 and not QuartzyLab.objects.filter(lab=lab).exists()
        wanted = {quartzy_id.lower() for quartzy_id in settings.QUARTZY_LAB_IDS}
        for model in models:
            active = model.id.lower() in wanted if wanted else first
            qlab, created, changed = _upsert_lab(lab, model, active=active)
            run.new += created
            run.changed += changed
            if created and not active:
                run.add_note(f"Found {qlab} [{qlab.quartzy_id}]; left inactive, not synced.")
            found.append(qlab)
    return found


def _upsert_lab(lab, model, *, active):
    """The QuartzyLab for ``model``, created mapped onto ``lab`` and ``active`` or not."""
    org = model.organization
    values = {
        "name": _clip(model.name, 200),
        "organization_id": _clip(org.id if org else None, 64),
        "organization_name": _clip(org.name if org else None, 200),
        "uses_instances": model.uses_instances,
    }
    qlab, created = QuartzyLab.objects.get_or_create(
        quartzy_id=model.id, defaults={"lab": lab, "is_active": active, **values}
    )
    changed = [] if created else [name for name, v in values.items() if getattr(qlab, name) != v]
    for name in changed:
        setattr(qlab, name, values[name])
    if changed:
        qlab.save(update_fields=changed)
    return qlab, created, bool(changed)


def sync_types(client, qlab, run=None):
    """Mirror the lab's Quartzy Types; a new one gets a suggested item type, and so does an
    unmapped one nobody has reviewed (``suggest_item_types``)."""
    with _run_for(run, SyncRun.Kind.TYPES, qlab, client) as run:
        for _page, records, _fetched_at in _list_pages(client.types, run, lab_id=qlab.quartzy_id):
            for payload in records:
                run.seen += 1
                model = _parse(schemas.TypeWithLab, _storable(payload), run)
                if model is None or (model.lab and model.lab.id != qlab.quartzy_id):
                    continue
                name = _clip(model.name, 200)
                qtype = QuartzyType.objects.filter(quartzy_lab=qlab, quartzy_id=model.id).first()
                if qtype is None:
                    _create_type(qlab, model.id, name)
                    run.new += 1
                elif name and qtype.name != name:
                    qtype.name = name
                    qtype.save(update_fields=["name"])
                    run.changed += 1
        suggested = suggest_item_types(qlab)
        if suggested:
            run.changed += suggested
            run.add_note(f"Suggested an item type for {suggested} unmapped type(s).")
    return run


def suggest_item_types(qlab):
    """Suggest an item type for each of the lab's Quartzy types that has none and that nobody
    has reviewed: types the sync met before seed_catalog loaded the item types, or before one
    matched their name. A reviewed type is never touched. The type's requests are then
    resolved again (QuartzyType.save). Returns how many types got a suggestion."""
    suggested = 0
    for qtype in QuartzyType.objects.filter(quartzy_lab=qlab, item_type=None, reviewed_at=None):
        item_type = suggest_item_type(qtype.name)
        if item_type is not None:
            qtype.item_type, qtype.suggested = item_type, True
            qtype.save(update_fields=["item_type", "suggested"])
            suggested += 1
    return suggested


# ---------------------------------------------------------------------------------------
# Inventory


def sync_inventory(client, qlab, run, *, max_pages=None):
    """Mirror inventory items and their instances, ``max_pages`` pages per call (None: to the
    end; at least two, so a page that repeats the last one is told by the page before it),
    from the saved cursor. Returns True when the pass reached the end of the list: a page
    that comes back empty or refused, or repeats the page before it.

    Items the pass didn't list are then read by id, and only a 404 marks one deleted.
    """
    base = settings.QUARTZY_PAGE_BASE
    page = qlab.inventory_cursor if qlab.inventory_cursor >= base else base
    updates = {"inventory_pass_started_at": timezone.now()} if page == base else {}
    read, previous, done = 0, None, False
    while read < (MAX_PAGES if max_pages is None else max(2, max_pages)):
        records = _list_page(client.inventory_items, page, lab_id=qlab.quartzy_id)
        read += 1
        run.pages += 1
        ids = _ids(records or [])
        if not records or _repeats(ids, previous):
            done = True
            break
        previous = ids
        for payload in records:
            upsert_inventory_item(qlab, payload, run=run)
        page += 1
    started = updates.get("inventory_pass_started_at", qlab.inventory_pass_started_at)
    _save(qlab, inventory_cursor=base if done else page, **updates)
    if done and started is not None:
        _confirm_unlisted_items(client, qlab, run, since=started)
    return done


def upsert_inventory_item(qlab, payload, *, run):
    """Mirror one inventory item with its instances. Returns ``(item, created, changed)``."""
    run.seen += 1
    payload = _storable(payload)
    model = _parse(schemas.InventoryItem, payload, run)
    if model is None:
        return None, False, False
    try:
        with transaction.atomic():
            item, created, changed = _upsert_item(qlab, model, payload)
    except Exception as exc:
        logger.exception("Could not mirror Quartzy inventory item %s", model.id)
        record_error(
            run,
            SyncError.Kind.UPSERT,
            f"{type(exc).__name__}: {exc}",
            quartzy_id=model.id,
            payload=payload,
        )
        return None, False, False
    run.new += created
    run.changed += changed
    return item, created, changed


def _upsert_item(qlab, model, payload):
    now = timezone.now()
    digest = payload_sha256(payload)
    item = QuartzyInventoryItem.objects.select_for_update().filter(quartzy_id=model.id).first()
    if item is not None and item.payload_sha256 == digest:
        QuartzyInventoryItem.objects.filter(pk=item.pk).update(
            last_seen_at=now, deleted_in_quartzy_at=None
        )
        return item, False, False
    created = item is None
    if created:
        item = QuartzyInventoryItem(quartzy_lab=qlab, quartzy_id=model.id, first_seen_at=now)
    item.name = _clip(model.name, 500)
    item.vendor_name = _clip(model.vendor, 300)
    item.catalog_number = _clip(model.catalog_number, 200)
    item.cas_number = _clip(model.cas_number, 50)
    item.quantity_raw = _clip(model.quantity, 100)
    item.unit_size_raw = _clip(model.unit_size, 200)
    item.raw_payload = payload
    item.payload_sha256 = digest
    item.last_seen_at = now
    item.deleted_in_quartzy_at = None
    item.save()
    if model.instances is not None:
        _sync_instances(item, model.instances, payload.get("instances") or [], now)
    return item, created, not created


def _sync_instances(item, instances, raw_instances, now):
    raw = {_raw_id(entry): entry for entry in raw_instances}
    for instance in instances:
        QuartzyInventoryInstance.objects.update_or_create(
            quartzy_id=instance.id,
            defaults={
                "item": item,
                "quantity_raw": _clip(instance.quantity, 100),
                "lot_number": _clip(instance.lot_number, 200),
                "expiration_date": _date(instance.expiration_date),
                "location_path": _location_path(instance.location),
                "raw_payload": raw.get(instance.id, {}),
                "deleted_in_quartzy_at": None,
            },
        )
    # An item's payload lists all of its instances, so one it no longer lists is gone.
    item.instances.filter(deleted_in_quartzy_at=None).exclude(
        quartzy_id__in=[instance.id for instance in instances]
    ).update(deleted_in_quartzy_at=now)


def _location_path(location):
    if location is None:
        return ""
    parts = [str(part).strip() for part in location.pathNames or [] if str(part).strip()]
    name = _clip(location.name, 200)
    if name and (not parts or parts[-1] != name):
        parts.append(name)
    return " / ".join(parts)


def _confirm_unlisted_items(client, qlab, run, *, since):
    unlisted = QuartzyInventoryItem.objects.filter(
        quartzy_lab=qlab, deleted_in_quartzy_at=None, last_seen_at__lt=since
    ).order_by(_LOOKED_AT, "pk")
    ids = list(unlisted.values_list("quartzy_id", flat=True)[:UNLISTED_PER_RUN])
    if ids:
        _ById(
            client, qlab, run, model=QuartzyInventoryItem, fetch=client.inventory_item,
            mirror=lambda payload, fetched_at: upsert_inventory_item(qlab, payload, run=run),
            what="inventory item",
        ).check(ids)  # fmt: skip


# ---------------------------------------------------------------------------------------
# History backfill


def backfill_requests(client, qlab, run):
    """Every request page from the first to the end of the list, each mirrored as soon as it
    is read, so no payload waits to be applied over a fresher read (and one that would is
    skipped: ``upsert_order_request``). Remembers where the list ends. Pages may come newest
    first, so the listings' prices are then those of their newest requests
    (``_newest_on_listings``), as if the requests had been read oldest first."""
    via, last_page, listings = OrderStatusObservation.Via.BACKFILL, None, set()
    pages = _list_pages(client.order_requests, run, lab_id=qlab.quartzy_id)
    for page, records, fetched_at in pages:
        for payload in records:
            obj, _created, _changed = upsert_order_request(
                qlab, payload, via=via, run=run, fetched_at=fetched_at
            )
            if obj is not None and obj.supplier_product_id is not None:
                listings.add(obj.supplier_product_id)
        last_page = page
    _newest_on_listings(listings)
    if last_page is not None:
        _save(qlab, head_cursor={**qlab.head_cursor, "last_page": last_page})


def _newest_on_listings(listing_ids):
    """Give each listing the unit size, price and currency of its newest request that gave
    one: what a request records on the listing it is resolved to (catalog.services), and
    so what resolving the requests oldest first leaves there."""
    newest = {}
    for field, source, empty in (
        ("unit_size_raw", "unit_size_raw", ""),
        ("last_unit_price", "unit_price", None),
        ("currency", "currency", ""),
    ):
        rows = (
            OrderRequest.objects.filter(supplier_product__in=listing_ids)
            .exclude(**{source: empty})
            .order_by("supplier_product", F("requested_at").desc(nulls_last=True), "-pk")
            .distinct("supplier_product")
            .values_list("supplier_product", source)
        )
        newest[field] = dict(rows)
    for listing in SupplierProduct.objects.filter(pk__in=listing_ids).order_by("pk"):
        unit_size = newest["unit_size_raw"].get(listing.pk, "")
        values = {
            "unit_size_raw": " ".join(unit_size.split())[:100],  # as catalog.services keeps it
            "last_unit_price": newest["last_unit_price"].get(listing.pk),
            "currency": newest["currency"].get(listing.pk, "")[:3].upper(),
        }
        changed = [
            field
            for field, value in values.items()
            if value not in (None, "") and getattr(listing, field) != value
        ]
        for field in changed:
            setattr(listing, field, values[field])
        if changed:
            listing.save(update_fields=changed)


def full_backfill(client, lab, *, arm=False, rearm=False):
    """The whole history, for ``manage.py quartzy_backfill``: the labs, then for each active
    Quartzy lab its types, every request page to the end of the list, and its inventory.

    Backfilled requests must never alert, so ``alerts_armed_at`` is left alone unless ``arm``
    or ``rearm``: then ``arm_alerts`` runs once the backfill is done. Returns the runs it
    recorded, the umbrella run (kind "full", with the totals) last.

    Raises CommandError until ``manage.py seed_catalog`` has loaded the item types: without
    them every request would be resolved with none, which groups custom oligos and fees on
    their vendors' shared catalog numbers.
    """
    if not ItemType.objects.exists():
        raise CommandError(
            "There are no item types yet, so requests can't be resolved properly: "
            "run manage.py seed_catalog first."
        )
    runs = []
    with recorded_run(SyncRun.Kind.FULL, client=client) as umbrella:
        with recorded_run(SyncRun.Kind.LABS, client=client) as run:
            runs.append(run)
            sync_labs(client, lab, run=run)
        qlabs = list(QuartzyLab.objects.for_lab(lab).active())
        steps = (
            (SyncRun.Kind.TYPES, sync_types),
            (SyncRun.Kind.BACKFILL, backfill_requests),
            (SyncRun.Kind.INVENTORY, sync_inventory),
        )
        for qlab in qlabs:
            for kind, step in steps:
                with recorded_run(kind, quartzy_lab=qlab, client=client) as run:
                    runs.append(run)
                    step(client, qlab, run)
            _save(qlab, last_synced_at=timezone.now())
        for counter in ("pages", "seen", "new", "changed", "errors"):
            setattr(umbrella, counter, sum(getattr(run, counter) for run in runs))
        umbrella.add_note(f"{len(qlabs)} Quartzy lab(s) backfilled.")
    if arm or rearm:
        arm_alerts(lab, rearm=rearm)
    return [*runs, umbrella]


def arm_alerts(lab, *, rearm=False):
    """Arm the duplicate alerts of ``lab``'s active Quartzy labs from now on: the guard alerts
    only on requests made at or after ``alerts_armed_at`` (plan §9 "Safety"). A lab armed
    already keeps its time, or the requests made since then that haven't alerted yet (say,
    while the worker was down) never could; ``rearm`` moves it to now all the same. Returns
    ``(qlab, armed_now)`` pairs.

    Raises CommandError, arming nothing, while a Quartzy type with requests still carries an
    item type the sync suggested and nobody confirmed: the guard must not alert on product
    identities built from a guess.
    """
    qlabs = list(QuartzyLab.objects.for_lab(lab).active().order_by("pk"))
    guessed = (
        QuartzyType.objects.filter(
            quartzy_lab__in=qlabs, suggested=True, order_requests__isnull=False
        )
        .select_related("quartzy_lab", "item_type")
        .distinct()
        .order_by("quartzy_lab", "name")
    )
    if guessed:
        names = "; ".join(f"{qtype} ({qtype.quartzy_lab}): {qtype.item_type}" for qtype in guessed)
        raise CommandError(
            "Alerts were not armed. These Quartzy types have requests, and an item type the "
            f"sync suggested that nobody has confirmed: {names}. Confirm or correct them in "
            "Admin → Quartzy mirror → Quartzy types, then arm again."
        )
    now, armed = timezone.now(), []
    for qlab in qlabs:
        rows = QuartzyLab.objects.filter(pk=qlab.pk)
        if not rearm:
            rows = rows.filter(alerts_armed_at__isnull=True)
        armed_now = bool(rows.update(alerts_armed_at=now))
        qlab.refresh_from_db(fields=["alerts_armed_at"])
        armed.append((qlab, armed_now))
    return armed
