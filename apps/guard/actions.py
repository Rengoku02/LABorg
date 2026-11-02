"""The duplicate alert's one-tap answers (design §8 "Actions"). Each records an Outcome.

"Have you placed the vendor order yet?" — the answer sets ``alert_in_time``:
- not yet: *Borrow from X* (asks the holder), *Share X's order* (asks the earlier requester),
  *Cancel my Quartzy request* (the confirm page links to it and names the Lab Admin when the
  request is past CREATED; the sync then watches it to verify the cancel);
- yes: *I'll cancel with the vendor*, *Keep it, offer the spare*;
- always: *Not the same thing* (a PairSuppression for this requester, and a label suggestion
  for the co-owner, never a label), *I need my own* (with a reason).

A link is single-use per email (core.actions): later follow-ups live on /guard/mine/. Only
the requester's email has these links; the earlier requester's copy is informational.
"""

from datetime import timedelta

from django.utils import timezone

from apps.core.actions import InvalidActionLink, register_action
from apps.core.models import Membership, Notification
from apps.core.policies import get_policy
from apps.inventory.asks import ask_owner, ask_share
from apps.quartzy.services import watch_requests

from . import outcomes
from .models import DuplicateCheck, DuplicateMatch, Outcome, PairSuppression

Kind = Outcome.Kind
NEED_OWN_REASONS = (
    ("lot", "I need a specific lot"),
    ("application", "Different application"),
    ("urgent", "Too urgent to wait"),
    ("other", "Other"),
)


def _context(notification):
    if notification.context.get("role") != "requester":
        raise InvalidActionLink("This link isn't for you.")
    check = DuplicateCheck.objects.select_related("order_request", "lab").get(
        pk=notification.subject_id
    )
    match = DuplicateMatch.objects.select_related("candidate_order_request").get(
        pk=notification.context["match"]
    )
    return check, match


def _in_time(check, value):
    DuplicateCheck.objects.filter(pk=check.pk).update(alert_in_time=value)


def _cancel_context(notification):
    check, _match = _context(notification)
    request = check.order_request
    admins = []
    if request.status != "CREATED":
        admins = [
            str(m.user)
            for m in Membership.objects.filter(
                lab=check.lab, is_quartzy_lab_admin=True
            ).select_related("user")
            if m.is_current()
        ]
    return {"quartzy_url": request.app_url, "lab_admins": admins, "status": request.status}


def _vendor_context(notification):
    check, _match = _context(notification)
    request = check.order_request
    return {"vendor": request.vendor_name_raw, "quartzy_url": request.app_url}


@register_action(Notification.Kind.DUPLICATE_ALERT, "borrow", label="Borrow instead")
def borrow(notification, request, data):
    check, match = _context(notification)
    if match.candidate_stock_item is None:
        raise InvalidActionLink("There is no stock to borrow from in this alert.")
    thread = ask_owner(
        lab=check.lab,
        asker=notification.recipient,
        stock_item=match.candidate_stock_item,
        source="alert",
    )
    outcomes.record(
        check, Kind.BORROW_REQUESTED, by=notification.recipient, notification=notification,
        ask_thread=thread,
    )  # fmt: skip
    _in_time(check, True)
    return {"ask": thread.pk}


@register_action(Notification.Kind.DUPLICATE_ALERT, "share", label="Share their order")
def share(notification, request, data):
    check, match = _context(notification)
    if match.candidate_order_request is None:
        raise InvalidActionLink("There is no open order to share in this alert.")
    thread = ask_share(
        lab=check.lab,
        asker=notification.recipient,
        order_request=match.candidate_order_request,
        source="alert",
    )
    outcomes.record(
        check, Kind.JOIN_REQUESTED, by=notification.recipient, notification=notification,
        ask_thread=thread,
    )  # fmt: skip
    watch_requests([check.order_request])
    _in_time(check, True)
    return {"ask": thread.pk}


@register_action(
    Notification.Kind.DUPLICATE_ALERT,
    "cancel",
    label="Cancel my Quartzy request",
    confirm_template="guard/action_cancel_confirm.html",
    confirm_context=_cancel_context,
    done_template="guard/action_cancel_done.html",
)
def cancel(notification, request, data):
    check, _match = _context(notification)
    outcomes.record(
        check, Kind.CANCEL_INTENDED, by=notification.recipient, notification=notification
    )
    watch_requests([check.order_request])
    _in_time(check, True)
    return {}


@register_action(
    Notification.Kind.DUPLICATE_ALERT,
    "vendor_cancel",
    label="I'll cancel with the vendor",
    confirm_template="guard/action_vendor_confirm.html",
    confirm_context=_vendor_context,
)
def vendor_cancel(notification, request, data):
    check, _match = _context(notification)
    outcomes.record(
        check, Kind.VENDOR_CANCEL_INTENDED, by=notification.recipient, notification=notification
    )
    _in_time(check, False)
    return {}


@register_action(Notification.Kind.DUPLICATE_ALERT, "keep_offer", label="Keep it, offer the spare")
def keep_offer(notification, request, data):
    check, _match = _context(notification)
    outcomes.record(
        check, Kind.KEPT_OFFER_SPARE, by=notification.recipient, notification=notification
    )
    _in_time(check, False)
    return {}


def _suppress(check, match, person, days, reason):
    candidate = match.candidate_stock_item or match.candidate_order_request
    candidate_product = getattr(candidate, "product_id", None)
    if check.order_request.product_id is None or candidate_product is None:
        return
    PairSuppression.objects.create(
        lab=check.lab,
        subject_product_id=check.order_request.product_id,
        candidate_product_id=candidate_product,
        scope=PairSuppression.Scope.REQUESTER,
        requester=person,
        expires_at=timezone.now() + timedelta(days=days),
        created_by=person,
        reason=reason,
    )


@register_action(Notification.Kind.DUPLICATE_ALERT, "not_same", label="Not the same thing")
def not_same(notification, request, data):
    check, match = _context(notification)
    person = notification.recipient
    outcomes.record(check, Kind.NOT_SAME, by=person, notification=notification)
    days = get_policy("guard.not_same_suppress_days", check.lab)
    _suppress(check, match, person, days, "Requester: not the same thing")
    DuplicateMatch.objects.filter(pk=match.pk).update(label_suggestion=DuplicateMatch.Label.FALSE)
    return {}


@register_action(
    Notification.Kind.DUPLICATE_ALERT,
    "need_own",
    label="I need my own",
    reasons=NEED_OWN_REASONS,
)
def need_own(notification, request, data):
    check, match = _context(notification)
    person = notification.recipient
    reason = data.get("reason", "")
    outcomes.record(check, Kind.NEED_OWN, by=person, reason=reason, notification=notification)
    days = get_policy("guard.need_own_suppress_days", check.lab)
    _suppress(check, match, person, days, f"Requester needs their own ({reason or 'no reason'})")
    return {}
