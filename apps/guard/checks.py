"""The checks pipeline (docs/design/mvp-a.md §7): one DuplicateCheck per request and matcher
version, live or as a replay of history.

``check_request`` builds the check and its matches, records whether Quartzy's own warning
would have fired, then decides the status:

1. ``uncheckable`` (with the reason) or ``out_of_scope`` (the item type isn't in scope);
2. ``no_match``, or ``suppressed`` when every match is;
3. live checks only: ``unarmed`` when the request predates ``QuartzyLab.alerts_armed_at``,
   ``late`` when it is checked more than ``alerts.late_after_hours`` after it was made;
4. otherwise ``shadow_only``. Alerts (``alerted``, ``held``, ``clear``) come with the alert
   milestone; until then the guard only records.

Each check runs in one transaction under a per-lab advisory lock, so concurrent runs of the
same batch make one check each, and counts per cycle stay consistent. A check is final for
its matcher version, except one that was ``uncheckable`` only because the request wasn't
resolved yet: it is redone in place once the request resolves.
"""

from datetime import timedelta

from django.db import connection, transaction
from django.db.models import Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.core.policies import get_policy
from apps.quartzy.locks import lock_id
from apps.quartzy.models import OrderRequest
from apps.quartzy.presenters import request_value, requester_name

from . import suppression
from .matcher import (
    MATCHER_VERSION,
    UNRESOLVED,
    Subject,
    find_matches,
    item_type_of,
    request_time,
    states_at,
    subject_rows,
    uncheckable_reason,
)
from .models import DuplicateCheck, DuplicateMatch

Status = DuplicateCheck.Status


def lock_lab(lab_id):
    """Serialize the guard's checks of one lab until the transaction ends."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [lock_id("guard.check", lab_id)])


def check_request(order_request_id, *, run=None, replay=False, now=None):
    """Check one request (an OrderRequest pk); returns its DuplicateCheck, or None if the
    request is gone. Safe to call twice: the second call returns the first's check."""
    now = now or timezone.now()
    with transaction.atomic():
        order_request = subject_rows().filter(pk=order_request_id).first()
        if order_request is None:
            return None
        lock_lab(order_request.lab_id)
        check = (
            DuplicateCheck.objects.select_for_update()
            .filter(order_request=order_request, matcher_version=MATCHER_VERSION, replay=replay)
            .first()
        )
        if check is not None and not _waiting_for_resolution(check, order_request):
            return check
        check = _build(order_request, check, run=run, replay=replay, now=now)
        if run is not None:
            run.requests += 1
            run.checks_created += 1
            run.save(update_fields=["requests", "checks_created"])
    return check


def _waiting_for_resolution(check, order_request):
    """A check made before the request was resolved, which can now be done properly."""
    return (
        check.status == Status.UNCHECKABLE
        and check.uncheckable_reason == UNRESOLVED
        and uncheckable_reason(order_request) != UNRESOLVED
    )


def _build(order_request, check, *, run, replay, now):
    lab = order_request.lab
    as_of = request_time(order_request) if replay else now
    reason = uncheckable_reason(order_request)
    item_type = item_type_of(order_request)
    candidates, suppressed = [], {}
    raw_fires = norm_fires = False
    late = False
    if reason is not None:
        status = Status.UNCHECKABLE
    elif item_type is not None and not item_type.in_scope:
        status = Status.OUT_OF_SCOPE
    else:
        subject = Subject.of(order_request)
        candidates = find_matches(order_request, as_of=as_of, replay=replay)
        suppressed = suppression.reasons(subject, candidates, as_of=as_of)
        raw_fires, norm_fires = native_warning(order_request, replay=replay)
        unsuppressed = [c for c in candidates if c.key not in suppressed]
        if not replay:
            late = now - request_time(order_request) > timedelta(
                hours=get_policy("alerts.late_after_hours", lab)
            )
        if not candidates:
            status = Status.NO_MATCH
        elif not unsuppressed:
            status = Status.SUPPRESSED
        elif replay:
            status = Status.SHADOW_ONLY
        elif not is_armed(order_request):
            status = Status.UNARMED
        elif late:
            status = Status.LATE
        else:
            status = Status.SHADOW_ONLY
    if check is None:
        check = DuplicateCheck(
            lab=lab,
            order_request=order_request,
            matcher_version=MATCHER_VERSION,
            replay=replay,
            created_at=now,
        )
    unsuppressed = [c for c in candidates if c.key not in suppressed]
    check.run = run
    check.shadow = True
    check.status = status
    check.uncheckable_reason = reason or ""
    check.late = late
    check.best_tier = unsuppressed[0].tier if unsuppressed else ""
    check.quartzy_native_would_fire_raw = raw_fires
    check.quartzy_native_would_fire_norm = norm_fires
    check.as_of = as_of
    check.decided_at = now
    check.save()
    _save_matches(check, order_request, candidates, suppressed)
    return check


def is_armed(order_request):
    """Was the request made after alerts were armed for its Quartzy lab? (design §7.2)"""
    qlab = order_request.quartzy_lab
    armed_at = qlab.alerts_armed_at if qlab is not None else None
    return armed_at is not None and request_time(order_request) >= armed_at


def native_warning(order_request, *, replay=False):
    """Would Quartzy's own duplicate warning have fired? (raw, norm)

    Quartzy warns when the same catalog # is already an open request in the same Quartzy
    lab. ``raw`` compares the catalog # as typed (trimmed), as Quartzy does; ``norm``
    compares the normalized key, i.e. what a formatting-proof warning would have caught.
    """
    if order_request.quartzy_lab_id is None:
        return False, False
    raw = (order_request.catalog_number_raw or "").strip()
    listing = order_request.supplier_product
    key = listing.catalog_key if listing is not None else None
    if not raw and not key:
        return False, False
    same = Q()
    if raw:
        same |= Q(catalog_number_raw=raw)
    if key:
        same |= Q(supplier_product__catalog_key=key)
    at = request_time(order_request)
    rows = list(
        OrderRequest.objects.filter(quartzy_lab_id=order_request.quartzy_lab_id)
        .live()
        .exclude(pk=order_request.pk)
        .filter(same)
        .annotate(at=Coalesce("requested_at", "first_seen_at"))
        .filter(Q(at__lt=at) | Q(at=at, pk__lt=order_request.pk))
        .select_related("supplier_product")
    )
    states = states_at(rows, at) if replay else {row.pk: row.status for row in rows}
    open_rows = [
        row
        for row in rows
        if states.get(row.pk) is not None and states[row.pk] not in OrderRequest.CLOSED_STATUSES
    ]
    raw_fires = bool(raw) and any(row.catalog_number_raw.strip() == raw for row in open_rows)
    norm_fires = bool(key) and any(
        row.supplier_product is not None and row.supplier_product.catalog_key == key
        for row in open_rows
    )
    return raw_fires, norm_fires


def _save_matches(check, order_request, candidates, suppressed):
    check.matches.all().delete()  # only for a check redone after resolution: it had none
    stock = _stock_rows(candidates)
    requests = _request_rows(candidates)
    for candidate in candidates:
        lookup = (
            {"candidate_stock_item_id": candidate.stock_item_id}
            if candidate.stock_item_id is not None
            else {"candidate_order_request_id": candidate.order_request_id}
        )
        DuplicateMatch.objects.create(
            duplicate_check=check,
            tier=candidate.tier,
            candidate_kind=candidate.kind,
            score=candidate.score,
            probably=candidate.probably,
            why=candidate.why[:300],
            candidate_snapshot=_snapshot(candidate, stock, requests),
            suppressed=candidate.key in suppressed,
            suppression_reason=suppressed.get(candidate.key, ""),
            **lookup,
            **_carried_label(order_request, lookup),
        )


def _carried_label(order_request, lookup):
    """The label this pair got before (another matcher version, or the replay/live twin), so
    nobody labels the same pair twice (design §7 "Replay")."""
    earlier = (
        DuplicateMatch.objects.filter(duplicate_check__order_request=order_request, **lookup)
        .exclude(label="")
        .order_by("-labelled_at", "-pk")
        .first()
    )
    if earlier is None:
        return {}
    return {
        "label": earlier.label,
        "labelled_by_id": earlier.labelled_by_id,
        "labelled_at": earlier.labelled_at,
    }


def _stock_rows(candidates):
    from apps.inventory.models import StockItem

    ids = [c.stock_item_id for c in candidates if c.stock_item_id is not None]
    rows = StockItem.objects.filter(pk__in=ids).select_related(
        "product", "supplier_product__vendor", "custodian"
    )
    return {row.pk: row for row in rows}


def _request_rows(candidates):
    ids = [c.order_request_id for c in candidates if c.order_request_id is not None]
    rows = OrderRequest.objects.filter(pk__in=ids).select_related(
        "requested_by", "requested_by_alias__user", "created_by_alias__user", "product"
    )
    return {row.pk: row for row in rows}


def _snapshot(candidate, stock, requests):
    """What the match was when found: never where stock is (that goes through the
    presenter for each viewer), only what it is and who holds or asked for it."""
    base = {"kind": candidate.kind, "tier": candidate.tier, "why": candidate.why}
    if candidate.stock_item_id is not None:
        item = stock[candidate.stock_item_id]
        listing = item.supplier_product
        return {
            **base,
            "short_id": item.short_id,
            "product": item.product.name,
            "vendor": str(listing.vendor) if listing else "",
            "catalog_number": listing.catalog_raw if listing else "",
            "holder": str(item.custodian),
            "lifecycle": item.lifecycle,
            "since": item.created_at.isoformat(),
        }
    row = requests[candidate.order_request_id]
    value = request_value(row)
    return {
        **base,
        "item_name": row.item_name or (row.product.name if row.product_id else ""),
        "vendor": row.vendor_name_raw,
        "catalog_number": row.catalog_number_raw,
        "requester": requester_name(row),
        "status": row.status,
        "requested_at": request_time(row).isoformat() if request_time(row) else None,
        "value": str(value) if value is not None else None,
        "currency": row.currency,
    }
