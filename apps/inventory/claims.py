"""Claim your history (docs/design/mvp-a.md §4): people say what became of what they bought,
so the lab's stock fills in without typing at the bench.

- ``claimable_requests(lab)``: durable-type Quartzy requests within ``claims.lookback_months``,
  not cancelled, ``claims.after_receipt_days`` after RECEIVED was seen or
  ``claims.after_request_days`` after they were made.
- ``surface_claims(lab)``: a pending HistoryClaim for each, for whoever asked for it
  (``requested_for``, else ``requested_by``); a skipped or "did this arrive?" claim comes back
  the next weekly round, up to ``claims.max_rounds``.
- ``decide(claim, outcome, ...)``: still have it (new stock, or "this is the same bottle"),
  used up, gave it to X, in the shared stock, not mine, did this arrive?, later. Stock is
  only ever written through ``services``.
- ``champions_list(lab)``: what the lab pool has to claim: undecided claims of people who
  left, and "not mine" ones.
- ``queue_claims_emails(lab)``: the weekly email (kind ``claims``) with a link that opens a
  7-day session scoped to that person's claim list (``claim_link`` / ``open_claim_session``).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.db.models import Exists, F, Min, OuterRef, Q
from django.db.models.functions import Coalesce
from django.urls import reverse
from django.utils import timezone

from apps.core.mail import queue_notification, register_context_builder
from apps.core.models import Membership, Notification
from apps.core.permissions import is_member
from apps.core.policies import get_policy
from apps.quartzy.models import OrderRequest, OrderStatusObservation
from apps.quartzy.presenters import RequestView, present_requests, with_related
from apps.quartzy.services import watch_requests

from . import services
from .models import HistoryClaim, Lifecycle, Source, StockItem, Visibility
from .presenters import present_items
from .selectors import items_by_ids, product_family_ids

Outcome = HistoryClaim.Outcome
OPEN_OUTCOMES = (Outcome.PENDING,)
# Outcomes that come back in the next round.
RETURNING = (Outcome.SKIPPED, Outcome.NOT_ARRIVED)
CLAIMS_SALT = "labstock.claims.v1"
LINK_MAX_AGE = timedelta(days=14)
SESSION_DAYS = 7
SESSION_KEY = "claims_scope"
_NAMESPACE = uuid.UUID("a3c2e1f0-4b5d-4c6e-9f8a-1b2c3d4e5f60")


class ClaimError(services.InventoryError):
    """A claim answer that can't be taken (the message can be shown)."""


# ---------------------------------------------------------------------------------------
# Surfacing


def _durable(order_request_qs):
    return order_request_qs.filter(
        Q(quartzy_type__item_type__durable=True)
        | Q(quartzy_type__item_type__isnull=True, product__item_type__durable=True)
        | Q(quartzy_type__isnull=True, product__item_type__durable=True)
    )


def claimable_requests(lab, *, now=None):
    """The lab's requests that should be claimed by now (design §4 "Surfacing")."""
    now = now or timezone.now()
    lookback = now - timedelta(days=round(30.44 * get_policy("claims.lookback_months", lab)))
    after_receipt = now - timedelta(days=get_policy("claims.after_receipt_days", lab))
    after_request = now - timedelta(days=get_policy("claims.after_request_days", lab))
    received = (
        OrderStatusObservation.objects.filter(
            order_request=OuterRef("pk"), to_status=OrderRequest.Status.RECEIVED
        )
        .order_by()
        .values("order_request")
        .annotate(first=Min("observed_at"))
        .values("first")[:1]
    )
    rows = (
        _durable(OrderRequest.objects.for_lab(lab).live())
        .filter(source=OrderRequest.Source.QUARTZY)
        .exclude(status=OrderRequest.Status.CANCELLED)
        .annotate(at=Coalesce("requested_at", "first_seen_at"), received_seen=received)
        .filter(at__gte=lookback)
        .filter(Q(received_seen__lte=after_receipt) | Q(at__lte=after_request))
        .filter(Q(requested_for__isnull=False) | Q(requested_by__isnull=False))
    )
    return rows


def surface_claims(lab, *, now=None):
    """Add the pending claims that are due; bring back skipped ones a week on. Returns how
    many claims are newly pending."""
    now = now or timezone.now()
    max_rounds = get_policy("claims.max_rounds", lab)
    claimed = HistoryClaim.objects.filter(order_request=OuterRef("pk"))
    new = 0
    rows = claimable_requests(lab, now=now).exclude(Exists(claimed))
    for row in rows.only("pk", "requested_for_id", "requested_by_id", "lab_id"):
        person_id = row.requested_for_id or row.requested_by_id
        _claim, created = HistoryClaim.objects.get_or_create(
            order_request=row,
            person_id=person_id,
            defaults={"lab": lab, "surfaced_at": now},
        )
        new += created
    returning = HistoryClaim.objects.for_lab(lab).filter(
        outcome__in=RETURNING, round__lt=max_rounds, decided_at__lte=now - timedelta(days=6)
    )
    new += returning.update(
        outcome=Outcome.PENDING, round=F("round") + 1, surfaced_at=now, decided_at=None
    )
    return new


def pending_claims(person, lab):
    """The person's claims waiting for an answer, most valuable first."""
    value = Coalesce(
        F("order_request__total_price"),
        F("order_request__unit_price") * F("order_request__quantity"),
    )
    return (
        HistoryClaim.objects.for_lab(lab)
        .filter(person=person, outcome__in=OPEN_OUTCOMES)
        .annotate(value=value)
        .order_by(F("value").desc(nulls_last=True), "pk")
    )


def champions_list(lab):
    """What the lab pool should claim: undecided claims of people who are no longer current
    members (design §4 "Champions' list"), and the ones somebody said aren't theirs."""
    left = Q(outcome__in=OPEN_OUTCOMES) & ~Q(person__in=_current_member_ids(lab))
    return (
        HistoryClaim.objects.for_lab(lab)
        .filter(left | Q(outcome=Outcome.NOT_MINE))
        .select_related("order_request", "lab", "person")
        .order_by("pk")
    )


def _current_member_ids(lab):
    return [
        membership.user_id
        for membership in Membership.objects.filter(lab=lab)
        if membership.is_current()
    ]


# ---------------------------------------------------------------------------------------
# Views of claims, for pages and emails


@dataclass(frozen=True)
class ClaimView:
    pk: int
    outcome: str
    outcome_label: str
    round: int
    request: RequestView | None
    person_name: str
    same_items: tuple  # ItemViews of unverified stock that may be this purchase
    kept_items: tuple  # ItemViews of the stock this claim made or verified


def claim_views(claims, viewer, *, channel="page"):
    claims = list(claims)
    if not claims:
        return []
    lab = claims[0].lab
    rows = with_related(OrderRequest.objects.filter(pk__in=[c.order_request_id for c in claims]))
    requests = {view.pk: view for view in present_requests(rows, viewer, channel=channel)}
    same = same_item_candidates(claims)
    kept_ids = {claim.pk: list(claim.stock_items.values_list("pk", flat=True)) for claim in claims}
    all_ids = {pk for ids in same.values() for pk in ids} | {
        pk for ids in kept_ids.values() for pk in ids
    }
    stock_rows = list(items_by_ids(lab, all_ids))
    short = {row.pk: row.short_id for row in stock_rows}
    items = {view.short_id: view for view in present_items(stock_rows, viewer, channel=channel)}

    def views_of(ids):
        return tuple(items[short[pk]] for pk in ids if short.get(pk) in items)

    return [
        ClaimView(
            pk=claim.pk,
            outcome=claim.outcome,
            outcome_label=claim.get_outcome_display(),
            round=claim.round,
            request=requests.get(claim.order_request_id),
            person_name=str(claim.person),
            same_items=views_of(same.get(claim.pk, [])),
            kept_items=views_of(kept_ids.get(claim.pk, [])),
        )
        for claim in claims
    ]


def same_item_candidates(claims):
    """{claim pk: [stock item pks]}: unverified stock of the same product (from Quartzy's
    inventory or an import) that the claimed purchase may be: "This is the same bottle"."""
    found = {}
    for claim in claims:
        product_id = claim.order_request.product_id
        if product_id is None or claim.outcome not in OPEN_OUTCOMES:
            continue
        found[claim.pk] = list(
            StockItem.objects.for_lab(claim.lab)
            .filter(
                lifecycle=Lifecycle.UNVERIFIED,
                product_id__in=product_family_ids([product_id]),
            )
            .exclude(history_claims__isnull=False)
            .order_by("pk")
            .values_list("pk", flat=True)[:5]
        )
    return found


# ---------------------------------------------------------------------------------------
# Answers


def decide(
    claim,
    outcome,
    *,
    by,
    count_kept=None,
    location=None,
    temperature="",
    target_person=None,
    same_item=None,
    visibility=None,
    notes="",
):
    """Record ``outcome`` for ``claim`` (one transaction), writing stock only through
    ``services``. Raises ClaimError for an answer that can't be taken."""
    with transaction.atomic():
        claim = (
            HistoryClaim.objects.select_for_update()
            .select_related("order_request", "lab", "person")
            .get(pk=claim.pk)
        )
        if claim.outcome not in OPEN_OUTCOMES:
            raise ClaimError("This one is answered already.")
        request = claim.order_request
        lab = claim.lab
        made = []
        if outcome == Outcome.STILL_HAVE:
            if same_item is not None:
                made = [_same_bottle(claim, same_item, by, location)]
            else:
                made = _new_stock(
                    claim, by, claim.person, count_kept or 1, location, temperature, visibility,
                    lifecycle=Lifecycle.AVAILABLE,
                )  # fmt: skip
        elif outcome == Outcome.GAVE_AWAY:
            if target_person is None:
                raise ClaimError("Who did you give it to?")
            if not services.may_receive_stock(target_person, lab):
                raise ClaimError(f"{target_person} isn't in {lab}, so they can't hold its stock.")
            made = _new_stock(
                claim, by, target_person, count_kept or 1, None, temperature, None,
                lifecycle=Lifecycle.UNVERIFIED,
            )  # fmt: skip
        elif outcome == Outcome.SHARED_STOCK:
            made = _new_stock(
                claim, by, lab.pool_user, count_kept or 1, location, temperature,
                Visibility.SHARED, lifecycle=Lifecycle.AVAILABLE,
            )  # fmt: skip
        elif outcome == Outcome.NOT_ARRIVED:
            watch_requests([request])
        elif outcome not in (Outcome.USED_UP, Outcome.NOT_MINE, Outcome.SKIPPED):
            raise ClaimError("Unknown answer.")
        claim.outcome = outcome
        claim.decided_at = timezone.now()
        claim.decided_by = by
        claim.count_kept = len(made) if made else count_kept
        claim.target_person = target_person if outcome == Outcome.GAVE_AWAY else None
        claim.notes = notes[:2000]
        claim.save()
        if made:
            claim.stock_items.add(*made)
    return claim


def _new_stock(claim, by, custodian, count, location, temperature, visibility, *, lifecycle):
    request = claim.order_request
    if request.product_id is None:
        raise ClaimError("This request has no product yet; try again later.")
    items = []
    for number in range(count):
        items.append(
            services.create_stock_item(
                lab=claim.lab,
                product=request.product,
                supplier_product=request.supplier_product,
                custodian=custodian,
                user=by,
                source=Source.PURCHASED,
                source_order_request=request,
                location=location,
                temperature=temperature if location is None else None,
                visibility=visibility,
                lifecycle=lifecycle,
                event_source="claim",
                reason=f"Claimed: Quartzy request {request.quartzy_id or request.pk}",
                data={"claim": claim.pk},
                client_uuid=uuid.uuid5(_NAMESPACE, f"{claim.pk}:{claim.round}:{number}"),
            )
        )
    return items


def _same_bottle(claim, item, by, location):
    """The claimed purchase is stock LabStock already has, unverified: verify it (where the
    person says it is) and make it theirs."""
    item = StockItem.objects.select_for_update().get(pk=item.pk)
    if item.lab_id != claim.lab_id or item.lifecycle != Lifecycle.UNVERIFIED:
        raise ClaimError("That stock item can't be this purchase.")
    if item.custodian_id != claim.person_id:
        services.apply_event(
            item, "custody_changed", user=by, source="claim", custodian=claim.person
        )
    changes = {"location": location} if location is not None else {}
    services.apply_event(item, "verified", user=by, source="claim", **changes)
    return item


# ---------------------------------------------------------------------------------------
# The weekly email and its scoped link


def week_key(now=None):
    year, week, _ = timezone.localdate(now).isocalendar()
    return f"{year}-W{week:02d}"


def queue_claims_emails(lab, *, now=None):
    """One ``claims`` email this week per current member with pending claims. Returns how
    many were queued."""
    now = now or timezone.now()
    queued = 0
    person_ids = (
        HistoryClaim.objects.for_lab(lab)
        .filter(outcome__in=OPEN_OUTCOMES)
        .values_list("person_id", flat=True)
        .distinct()
    )
    memberships = Membership.objects.filter(lab=lab, user_id__in=list(person_ids))
    for membership in memberships.select_related("user"):
        person = membership.user
        if not membership.is_current() or not person.email or not person.is_active:
            continue
        _notification, created = queue_notification(
            lab=lab,
            kind=Notification.Kind.CLAIMS,
            subject_obj=person,
            recipient_email=person.email,
            recipient=person,
            period_key=week_key(now),
        )
        queued += created
    return queued


@register_context_builder(Notification.Kind.CLAIMS)
def _claims_email_context(notification):
    person, lab = notification.recipient, notification.lab
    claims = list(pending_claims(person, lab).select_related("order_request", "lab"))
    batch = get_policy("claims.batch_size", lab)
    rows = with_related(
        OrderRequest.objects.filter(pk__in=[c.order_request_id for c in claims[:batch]])
    )
    by_pk = {view.pk: view for view in present_requests(rows, person, channel="email")}
    request_views = [
        by_pk[claim.order_request_id] for claim in claims[:batch] if claim.order_request_id in by_pk
    ]
    return {
        "claims_url": claim_link(notification),
        "count": len(claims),
        "request_views": request_views,
        "more": max(len(claims) - batch, 0),
    }


def claim_link(notification):
    """The signed link in the claims email: opens a 7-day session scoped to the recipient's
    claim list. It grants nothing else and logs nobody in (plan §12)."""
    payload = {"n": notification.pk, "r": notification.recipient_id, "k": notification.nonce}
    token = signing.TimestampSigner(salt=CLAIMS_SALT).sign_object(payload)
    return f"{settings.SITE_URL}{reverse('bench:claims_token', kwargs={'token': token})}"


def open_claim_session(request, token):
    """Check the link and scope the session to its person's claims; returns the person.
    Raises signing.BadSignature (expired, tampered, revoked)."""
    payload = signing.TimestampSigner(salt=CLAIMS_SALT).unsign_object(token, max_age=LINK_MAX_AGE)
    notification = (
        Notification.objects.select_related("recipient", "lab")
        .filter(pk=payload.get("n"), kind=Notification.Kind.CLAIMS)
        .first()
    )
    if (
        notification is None
        or notification.nonce != payload.get("k")
        or notification.recipient_id != payload.get("r")
        or notification.recipient is None
        or not is_member(notification.recipient, notification.lab)
    ):
        raise signing.BadSignature("This link is no longer valid.")
    request.session.cycle_key()
    request.session[SESSION_KEY] = {
        "person": notification.recipient_id,
        "lab": notification.lab_id,
        "until": (timezone.now() + timedelta(days=SESSION_DAYS)).isoformat(),
    }
    return notification.recipient, notification.lab


def claim_scope(request):
    """(person id, lab id) of a valid scoped claims session, else None."""
    scope = request.session.get(SESSION_KEY)
    if not scope:
        return None
    try:
        until = datetime.fromisoformat(scope["until"])
    except (KeyError, TypeError, ValueError):
        return None
    if until < timezone.now():
        request.session.pop(SESSION_KEY, None)
        return None
    return scope["person"], scope["lab"]
