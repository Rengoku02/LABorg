"""Duplicate alerts (docs/design/mvp-a.md §7.4 to §8): when a check emails someone, and what.

A check that would be ``shadow_only`` (armed, not late, with matches) alerts when
``alerts.enabled`` and its best match is live: ``alerts.tiers_live`` lists tiers ("T1") or a
tier and candidate kind ("T1/open"), so "T1 against open requests" can go live first. Then:

- **Recipients:** the requester (a current member with an email), and, when the best match is
  an open request, its requester too (informational, Reply-To the new requester).
- **Burst hold:** more than ``alerts.burst_max_requests`` requests from one creator within
  ``alerts.burst_window_minutes`` (a bulk import, a scripted order): held, and the system
  owners hear once.
- **Circuit breaker:** alerts planned in one CheckRun are held until the run ends; more than
  ``alerts.circuit_breaker_max`` of them stay held, and the system owners get a one-tap
  *Release* link (``release_alerts``). A run's holds are released otherwise.
- **No match:** while ``alerts.clear_to_order_until`` hasn't passed, a "No match — clear to
  order" receipt for in-scope requests with nothing found.

Kill switches are re-read at send time (``register_send_check``), so turning alerts off
holds mail that is already queued. Nothing here runs while ``alerts.enabled`` is false.
"""

from datetime import date, timedelta

from django.conf import settings
from django.db.models import F, Q
from django.urls import reverse
from django.utils import timezone

from apps.core.actions import action_url, register_action
from apps.core.mail import (
    Suppressed,
    notify_admins,
    queue_notification,
    register_context_builder,
    register_send_check,
    release_notification,
)
from apps.core.models import Notification
from apps.core.permissions import is_member
from apps.core.policies import get_policy
from apps.inventory.presenters import present_item
from apps.inventory.selectors import items_by_ids
from apps.quartzy.models import OrderRequest
from apps.quartzy.presenters import present_request, with_related

from .models import CheckRun, DuplicateCheck, DuplicateMatch

Kind = Notification.Kind
Status = DuplicateCheck.Status
CYCLE = "cycle:"
CIRCUIT = "circuit:"
BURST = "burst"
REQUESTER, EARLIER = "requester", "earlier"


def tier_is_live(tier, kind, lab):
    live = get_policy("alerts.tiers_live", lab) or []
    return tier in live or f"{tier}/{kind}" in live


def alerts_on(lab):
    return bool(get_policy("alerts.enabled", lab))


def live_match(check):
    """The best unsuppressed match whose tier (and kind) is live, or None."""
    for match in check.matches.filter(suppressed=False).order_by("-score", "pk"):
        if tier_is_live(match.tier, match.candidate_kind, check.lab):
            return match
    return None


def recipient_of(order_request):
    """The requester as a current member with an email, else None (nobody to alert)."""
    person = order_request.requested_for or order_request.requested_by
    if person is None or not person.email or not is_member(person, order_request.lab):
        return None
    return person


def plan_alert(check, order_request, *, run=None, now=None):
    """Turn a would-be ``shadow_only`` check into an alert if a live match allows it.
    Returns the check's status. Called inside the check's transaction (lab lock held)."""
    now = now or timezone.now()
    lab = check.lab
    if not alerts_on(lab):
        return Status.SHADOW_ONLY
    match = live_match(check)
    if match is None:
        return Status.SHADOW_ONLY
    person = recipient_of(order_request)
    if person is None:
        check.hold_reason = "the requester isn't a current member LabStock can email"
        return Status.SHADOW_ONLY
    hold = ""
    if _burst(order_request, now):
        hold = BURST
        _tell_owners_about_burst(order_request, now)
    elif run is not None:
        hold = f"{CYCLE}{run.pk}"
        CheckRun.objects.filter(pk=run.pk).update(alerts_planned=F("alerts_planned") + 1)
    context = {"match": match.pk, "role": REQUESTER}
    queue_notification(
        lab=lab,
        kind=Kind.DUPLICATE_ALERT,
        subject_obj=check,
        recipient_email=person.email,
        recipient=person,
        context=context,
        hold_reason=hold,
    )
    if match.candidate_kind == DuplicateMatch.Kind.OPEN and match.candidate_order_request_id:
        _alert_earlier_requester(check, match, person, hold)
    check.shadow = False
    check.hold_reason = hold
    return Status.HELD if hold == BURST else Status.ALERTED


def _alert_earlier_requester(check, match, requester, hold):
    earlier = match.candidate_order_request
    person = recipient_of(earlier)
    if person is None or person == requester:
        return
    queue_notification(
        lab=check.lab,
        kind=Kind.DUPLICATE_ALERT,
        subject_obj=check,
        recipient_email=person.email,
        recipient=person,
        context={"match": match.pk, "role": EARLIER, "reply_to": requester.email},
        hold_reason=hold,
    )


def _burst(order_request, now):
    lab = order_request.lab
    alias = order_request.created_by_alias_id or order_request.requested_by_alias_id
    if alias is None:
        return False
    window = timedelta(minutes=get_policy("alerts.burst_window_minutes", lab))
    at = order_request.requested_at or order_request.first_seen_at
    count = (
        OrderRequest.objects.for_lab(lab)
        .filter(
            Q(created_by_alias_id=alias)
            | Q(created_by_alias__isnull=True, requested_by_alias_id=alias)
        )
        .filter(requested_at__gte=at - window, requested_at__lte=at)
        .count()
    )
    return count > get_policy("alerts.burst_max_requests", lab)


def _tell_owners_about_burst(order_request, now):
    alias = order_request.created_by_alias or order_request.requested_by_alias
    window = get_policy("alerts.burst_window_minutes", order_request.lab)
    notify_admins(
        order_request.lab,
        title="Duplicate alerts held: a burst of requests",
        message=(
            f"More than {get_policy('alerts.burst_max_requests', order_request.lab)} requests "
            f"from {alias.display_name or alias.value if alias else 'one person'} within "
            f"{window} minutes. Their duplicate alerts are held: release them in the admin "
            "(Notifications, 'release held') if they should go out."
        ),
        subject_obj=order_request.lab,
        period_key=f"burst-{alias.pk if alias else 0}-{now:%Y%m%d%H}",
    )


def finish_cycle(run):
    """End of a CheckRun: release its alerts, or trip the circuit breaker (design §7.6)."""
    run.refresh_from_db()
    held = Notification.objects.filter(
        lab=run.lab, status=Notification.Status.HELD, hold_reason=f"{CYCLE}{run.pk}"
    )
    if not held.exists():
        return "nothing held"
    limit = get_policy("alerts.circuit_breaker_max", run.lab)
    if run.alerts_planned > limit:
        held.update(hold_reason=f"{CIRCUIT}{run.pk}")
        DuplicateCheck.objects.filter(run=run, status=Status.ALERTED).update(
            status=Status.HELD, hold_reason=f"{CIRCUIT}{run.pk}"
        )
        CheckRun.objects.filter(pk=run.pk).update(
            held=True, hold_reason=f"{run.alerts_planned} alerts in one cycle (limit {limit})"
        )
        _trip(run, limit)
        return "circuit breaker"
    for notification in held:
        release_notification(notification)
    return "released"


def _trip(run, limit):
    notify_admins(
        run.lab,
        title="Duplicate alerts held: circuit breaker",
        message=(
            f"One sync cycle planned {run.alerts_planned} duplicate alerts (the limit is "
            f"{limit}), so none were sent. That usually means something upstream changed "
            "(a bulk import, a catalog fix). Check /guard/label/ and the admin, then release "
            "them with the link below if they are right."
        ),
        subject_obj=run,
        period_key="circuit",
    )
    for notification in Notification.objects.filter(
        kind=Kind.SYSTEM, subject_id=str(run.pk), period_key="circuit"
    ):
        notification.context = {
            **notification.context,
            "run": run.pk,
            "actions": [["release_alerts", "Release the held alerts"]],
        }
        notification.save(update_fields=["context"])


@register_action(Kind.SYSTEM, "release_alerts", label="Release the held alerts")
def _release(notification, request, data):
    run_id = notification.context.get("run")
    released = 0
    for held in Notification.objects.filter(
        status=Notification.Status.HELD, hold_reason=f"{CIRCUIT}{run_id}"
    ):
        released += release_notification(held)
    DuplicateCheck.objects.filter(run_id=run_id, status=Status.HELD).update(
        status=Status.ALERTED, hold_reason=""
    )
    return {"released": released}


def release_stale_cycles(now=None):
    """Release cycle holds of runs that never finished (a worker died mid-run), once they
    are 30 minutes old and under the limit."""
    now = now or timezone.now()
    released = 0
    for run in CheckRun.objects.filter(
        finished_at__isnull=True, started_at__lt=now - timedelta(minutes=30), held=False
    ):
        run.finished_at = now
        run.save(update_fields=["finished_at"])
        released += finish_cycle(run) == "released"
    return released


# ---------------------------------------------------------------------------------------
# No match: clear to order


def plan_clear_receipt(check, order_request):
    """A "No match — clear to order" receipt (design §7.7), or the plain no_match status."""
    lab = check.lab
    until = get_policy("alerts.clear_to_order_until", lab)
    if not alerts_on(lab) or not until or timezone.localdate() > date.fromisoformat(str(until)):
        return Status.NO_MATCH
    person = recipient_of(order_request)
    if person is None:
        return Status.NO_MATCH
    queue_notification(
        lab=lab,
        kind=Kind.CLEAR_TO_ORDER,
        subject_obj=check,
        recipient_email=person.email,
        recipient=person,
    )
    check.shadow = False
    return Status.CLEAR


# ---------------------------------------------------------------------------------------
# Send checks (kill switches at send time)


@register_send_check(Kind.DUPLICATE_ALERT)
def _alert_still_wanted(notification):
    check = (
        DuplicateCheck.objects.select_related("order_request", "lab")
        .filter(pk=notification.subject_id)
        .first()
    )
    if check is None:
        return Suppressed("check gone")
    request = check.order_request
    if request.status == OrderRequest.Status.CANCELLED or request.deleted_in_quartzy_at:
        return Suppressed("the request was cancelled")
    if not alerts_on(check.lab):
        return "alerts are switched off"
    match = DuplicateMatch.objects.filter(pk=notification.context.get("match")).first()
    if match is None or not tier_is_live(match.tier, match.candidate_kind, check.lab):
        return "this tier is no longer live"
    return None


@register_send_check(Kind.CLEAR_TO_ORDER)
def _receipt_still_wanted(notification):
    lab = notification.lab
    until = get_policy("alerts.clear_to_order_until", lab)
    if not alerts_on(lab) or not until or timezone.localdate() > date.fromisoformat(str(until)):
        return Suppressed("clear-to-order receipts are over")
    return None


# ---------------------------------------------------------------------------------------
# What the emails say


def subject_line(match_kind, item_view=None, other_view=None, product_text=""):
    """ "Before you order: Priya has CST 2118S (GAPDH 14C10) in -20 B" (plan §9)."""
    if item_view is not None:
        what = f"{item_view.vendor} {item_view.catalog_number}".strip() or item_view.product_name
        if item_view.product_name and item_view.product_name not in what:
            what = f"{what} ({item_view.product_name})"
        holder = item_view.custodian_name if not item_view.custodian_is_pool else "The lab"
        where = ""
        if not item_view.redacted and item_view.location_path:
            parts = item_view.location_path.split(" / ")
            where = f" in {parts[1] if len(parts) > 1 else parts[0]}"
        return f"Before you order: {holder} has {what}{where}"
    if other_view is not None:
        what = f"{other_view.vendor} {other_view.catalog_number}".strip() or other_view.item_name
        who = other_view.requester_name or "Someone"
        when = f"{other_view.requested_at:%b %-d}" if other_view.requested_at else "recently"
        if match_kind == DuplicateMatch.Kind.OPEN:
            return f"Before you order: {who} requested {what} on {when}"
        return f"Before you order: {who} bought {what} ({when})"
    return f"Before you order: the lab may have {product_text}"


@register_context_builder(Kind.DUPLICATE_ALERT)
def _alert_context(notification):
    check = DuplicateCheck.objects.select_related("order_request", "lab").get(
        pk=notification.subject_id
    )
    match = DuplicateMatch.objects.get(pk=notification.context["match"])
    person, lab = notification.recipient, check.lab
    rows = with_related(OrderRequest.objects.filter(pk=check.order_request_id))
    request_view = present_request(rows.get(), person, channel="email")
    item_view = other_view = None
    if match.candidate_stock_item_id:
        stock = list(items_by_ids(lab, [match.candidate_stock_item_id]))
        item_view = present_item(stock[0], person, channel="email") if stock else None
    elif match.candidate_order_request_id:
        rows = with_related(OrderRequest.objects.filter(pk=match.candidate_order_request_id))
        other_view = present_request(rows.get(), person, channel="email")
    role = notification.context.get("role", REQUESTER)
    context = {
        "role": role,
        "match_kind": match.candidate_kind,
        "why": match.why,
        "probably": match.probably,
        "request_view": request_view,
        "item_view": item_view,
        "other_view": other_view,
        "subject_line": subject_line(match.candidate_kind, item_view, other_view),
        "search_url": f"{settings.SITE_URL}/",
        "mine_url": f"{settings.SITE_URL}{reverse('guard:mine')}",
    }
    if role == EARLIER:
        context["subject_line"] = (
            f"Heads up: {request_view.requester_name or 'someone'} just requested what you have "
            "on order"
        )
        return context
    not_yet = [("cancel", "Cancel my Quartzy request")]
    if item_view is not None:
        holder = item_view.ask_name or item_view.custodian_name
        not_yet.insert(0, ("borrow", f"Borrow from {holder}"))
    if other_view is not None and match.candidate_kind == DuplicateMatch.Kind.OPEN:
        not_yet.insert(0, ("share", f"Share {other_view.requester_name or 'their'}'s order"))
    already = [
        ("vendor_cancel", "I'll cancel with the vendor"),
        ("keep_offer", "Keep it, offer the spare"),
    ]
    always = [("not_same", "Not the same thing"), ("need_own", "I need my own")]

    def links(pairs):
        return [(label, action_url(notification, name)) for name, label in pairs]

    context.update(
        {
            "not_yet_links": links(not_yet),
            "already_links": links(already),
            "always_links": links(always),
        }
    )
    return context


@register_context_builder(Kind.CLEAR_TO_ORDER)
def _receipt_context(notification):
    check = DuplicateCheck.objects.select_related("order_request").get(pk=notification.subject_id)
    rows = with_related(OrderRequest.objects.filter(pk=check.order_request_id))
    months = get_policy("search.bought_before_months", check.lab)
    return {
        "request_view": present_request(rows.get(), notification.recipient, channel="email"),
        "months": months,
    }
