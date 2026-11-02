"""What people did about alerts, and what it saved (design §8 "Verified outcomes").

``record(check, kind, ...)`` stores an Outcome (one per kind per check). Intentions become
verified outcomes only with evidence:

- ``cancel_intended`` / ``join_requested`` → ``cancelled`` / ``joined_order`` once the sync
  observes the request CANCELLED after the alert (``verify_outcomes``, every 15 minutes:
  ``requests_synced`` doesn't announce status changes, the observations table is the record);
- ``vendor_cancel_intended`` → ``vendor_cancel_confirmed`` when the requester confirms it on
  /guard/mine/;
- ``borrow_requested`` → ``borrowed`` when the holder answers *Yes, borrow some* (or the
  requester says so on /guard/mine/).

``value_avoided(lab, start, end)`` sums the verified ones, minus those Quartzy's own warning
would have caught (counted apart) and those followed by an equivalent request from the same
person within 30 days (they bought it anyway).
"""

from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.inventory.selectors import product_family_ids
from apps.quartzy.models import OrderRequest, OrderStatusObservation
from apps.quartzy.presenters import request_value

from .models import DuplicateCheck, Outcome

Kind = Outcome.Kind
VERIFIES = {Kind.CANCEL_INTENDED: Kind.CANCELLED, Kind.JOIN_REQUESTED: Kind.JOINED_ORDER}
REORDER_WINDOW = timedelta(days=30)


def record(check, kind, *, by=None, reason="", notification=None, ask_thread=None, verified=False):
    """Store (or find) the check's Outcome of ``kind``."""
    values = {
        "decided_by": by,
        "reason": reason[:30],
        "notification": notification,
        "ask_thread": ask_thread,
    }
    if verified:
        values["verified_at"] = timezone.now()
        values["est_value_avoided"] = _value(check)
    try:
        with transaction.atomic():
            outcome, _created = Outcome.objects.get_or_create(
                duplicate_check=check, kind=kind, defaults=values
            )
    except IntegrityError:
        outcome = Outcome.objects.get(duplicate_check=check, kind=kind)
    return outcome


def _value(check):
    value = request_value(check.order_request)
    return value.quantize(Decimal("0.01")) if value is not None else None


def verify_outcomes(now=None):
    """Turn cancel and join intentions into verified outcomes once the sync saw the request
    CANCELLED after the alert. Returns how many were verified."""
    verified = 0
    pending = Outcome.objects.filter(kind__in=list(VERIFIES)).select_related(
        "duplicate_check__order_request"
    )
    for intent in pending:
        target = VERIFIES[intent.kind]
        check = intent.duplicate_check
        if Outcome.objects.filter(duplicate_check=check, kind=target).exists():
            continue
        seen = (
            OrderStatusObservation.objects.filter(
                order_request=check.order_request,
                to_status=OrderRequest.Status.CANCELLED,
                observed_at__gte=check.created_at,
            )
            .order_by("observed_at")
            .first()
        )
        if seen is None:
            continue
        outcome = record(check, target, by=intent.decided_by, verified=True)
        Outcome.objects.filter(pk=outcome.pk).update(verified_at=seen.observed_at)
        verified += 1
    return verified


def value_avoided(lab, start, end):
    """{"amount", "count", "native", "reordered"}: verified outcomes decided in [start, end)
    and what they saved, apart from what Quartzy's own warning would have caught ("native")
    and what was bought again within 30 days ("reordered")."""
    rows = (
        Outcome.objects.filter(
            duplicate_check__lab=lab,
            duplicate_check__replay=False,
            kind__in=Outcome.AVOIDED,
            verified_at__isnull=False,
            decided_at__gte=start,
            decided_at__lt=end,
        )
        .select_related("duplicate_check__order_request")
        .order_by("duplicate_check_id", "pk")
    )
    total, count, native, reordered, seen = Decimal(0), 0, 0, 0, set()
    for outcome in rows:
        check = outcome.duplicate_check
        if check.pk in seen:
            continue  # one saving per alert, whatever verified it
        seen.add(check.pk)
        if check.quartzy_native_would_fire_raw:
            native += 1
            continue
        if _reordered(check, outcome.decided_at):
            reordered += 1
            continue
        count += 1
        total += outcome.est_value_avoided or Decimal(0)
    return {"amount": total, "count": count, "native": native, "reordered": reordered}


def _reordered(check, since):
    request = check.order_request
    person = request.requested_by_id
    if person is None or request.product_id is None:
        return False
    return (
        OrderRequest.objects.for_lab(check.lab)
        .live()
        .exclude(pk=request.pk)
        .exclude(status=OrderRequest.Status.CANCELLED)
        .filter(
            requested_by_id=person,
            product_id__in=product_family_ids([request.product_id]),
            requested_at__gte=since,
            requested_at__lt=since + REORDER_WINDOW,
        )
        .exists()
    )


def alerted_checks_of(person, lab, *, days=90):
    """The person's own alerted checks (for /guard/mine/), newest first."""
    since = timezone.now() - timedelta(days=days)
    return (
        DuplicateCheck.objects.filter(
            lab=lab,
            replay=False,
            status__in=(DuplicateCheck.Status.ALERTED, DuplicateCheck.Status.HELD),
            created_at__gte=since,
        )
        .filter(order_request__requested_by=person)
        .select_related("order_request")
        .prefetch_related("outcomes")
        .order_by("-created_at")
    )
