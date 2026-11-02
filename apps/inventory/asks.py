"""Asking instead of buying (plan §5 "Check before you buy"; design §8 "Actions").

- ``ask_owner``: email the holder of a stock item (or, for lab stock and stock of people who
  left, its steward or the co-owners) that someone would like to borrow some. Reply-To is
  the asker, so they arrange it between them; the holder's one tap (*Yes, borrow some* /
  *None left*) is recorded too.
- ``ask_share``: email the earlier requester of an open order: could it cover both?
- ``ask_lab``: ask every current member (*I have it* / *Maybe* / *No*, Reply-To the asker).
  At most ``asks.max_per_person_per_day`` both ways: lab-wide asks started per person per
  day (``AskLimit``), and ask emails received per person per day. Asks wait (held
  ``ask_batch``) for ``asks.batch_hours`` unless urgent; ``release_ask_batches`` sends them.

Whom to ask comes from the presenter (``ItemView.ask_user_id``), so a restricted item's
holder is asked without the asker ever learning where it is. Replies are AskReplies, and
``signals.ask_answered`` tells the guard (a lent item verifies a borrow).
"""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.core import schedules
from apps.core.actions import InvalidActionLink, action_url, register_action
from apps.core.mail import (
    Suppressed,
    queue_notification,
    register_context_builder,
    register_send_check,
    release_notification,
)
from apps.core.models import Membership, Notification, User
from apps.core.permissions import is_member
from apps.core.policies import get_policy
from apps.quartzy.models import OrderRequest
from apps.quartzy.presenters import present_request, with_related

from .models import AskReply, AskThread
from .presenters import present_item
from .selectors import items_by_ids
from .signals import ask_answered

Kind = Notification.Kind


def ask_owner(*, lab, asker, stock_item, source):
    """Ask whoever should be asked about ``stock_item``; returns the AskThread."""
    item_view = present_item(stock_item, asker, channel="email")
    if item_view is None:
        raise PermissionError("The asker can't see this item.")
    thread = AskThread.objects.create(
        lab=lab,
        kind=AskThread.Kind.OWNER,
        source=source,
        asker=asker,
        product_id=stock_item.product_id,
        stock_item=stock_item,
    )
    recipients = _people_to_ask(lab, item_view.ask_user_id or item_view.custodian_id)
    for person in recipients:
        _queue(thread, person, role="owner")
    return thread


def ask_share(*, lab, asker, order_request, source):
    """Ask the earlier requester of ``order_request`` whether it can cover both."""
    earlier = order_request.requested_by
    thread = AskThread.objects.create(
        lab=lab,
        kind=AskThread.Kind.SHARE,
        source=source,
        asker=asker,
        product_id=order_request.product_id,
        order_request=order_request,
    )
    if earlier is not None and is_member(earlier, lab) and earlier.email:
        _queue(thread, earlier, role="share")
    return thread


def _people_to_ask(lab, user_id):
    person = User.objects.filter(pk=user_id, kind=User.Kind.MEMBER).first() if user_id else None
    if person is not None and is_member(person, lab) and person.email:
        return [person]
    # Lab stock or a leaver's with no steward: the co-owners.
    co_owners = Membership.objects.filter(lab=lab, role=Membership.Role.CO_OWNER)
    return [m.user for m in co_owners.select_related("user") if m.is_current() and m.user.email]


def _queue(thread, person, *, role):
    queue_notification(
        lab=thread.lab,
        kind=Kind.ASK_OWNER,
        subject_obj=thread,
        recipient_email=person.email,
        recipient=person,
        context={"reply_to": thread.asker.email, "role": role},
    )


@register_context_builder(Kind.ASK_OWNER)
def _ask_owner_context(notification):
    thread = AskThread.objects.select_related("asker", "lab", "product").get(
        pk=notification.subject_id
    )
    person = notification.recipient
    context = {"asker_name": str(thread.asker), "thread": thread.pk, "product_name": ""}
    if thread.product_id:
        context["product_name"] = thread.product.name
    if thread.stock_item_id:
        rows = items_by_ids(thread.lab, [thread.stock_item_id])
        views = [present_item(row, person, channel="email") for row in rows]
        context["item_view"] = views[0] if views else None
    if thread.order_request_id:
        rows = with_related(OrderRequest.objects.filter(pk=thread.order_request_id))
        context["request_view"] = present_request(rows.get(), person, channel="email")
    names = ("share_ok",) if notification.context.get("role") == "share" else ("lend", "none_left")
    labels = dict(AskReply.Answer.choices)
    context["answer_links"] = [(labels[name], action_url(notification, name)) for name in names]
    return context


def _answer(name):
    def handler(notification, request, data):
        thread = AskThread.objects.get(pk=notification.subject_id)
        responder = notification.recipient
        if responder is None:
            raise InvalidActionLink("This link is no longer valid.")
        reply, _created = AskReply.objects.update_or_create(
            thread=thread, responder=responder, defaults={"answer": name}
        )
        transaction.on_commit(
            lambda: ask_answered.send_robust(sender=AskReply, thread=thread, reply=reply)
        )
        return {"answer": name}

    return handler


for _name in ("lend", "none_left", "share_ok"):
    register_action(Kind.ASK_OWNER, _name, label=dict(AskReply.Answer.choices)[_name])(
        _answer(_name)
    )


# ---------------------------------------------------------------------------------------
# Ask the lab

BATCH = "ask_batch"
STALE_AFTER = timedelta(hours=48)


class AskLimit(Exception):
    """The asker reached the daily cap of lab-wide asks (the message can be shown)."""


def _local_day_start(lab, now):
    local = schedules.local_now(lab, now)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def ask_lab(*, lab, asker, product=None, text="", urgent=False, now=None):
    """Ask every current member; returns the AskThread. Raises AskLimit over the cap."""
    now = now or timezone.now()
    cap = get_policy("asks.max_per_person_per_day", lab)
    day = _local_day_start(lab, now)
    started = AskThread.objects.for_lab(lab).filter(
        asker=asker, kind=AskThread.Kind.LAB, created_at__gte=day
    )
    if started.count() >= cap:
        raise AskLimit(
            "You've asked the whole lab today already. Try again tomorrow, or ask a holder "
            "directly from the search results."
        )
    thread = AskThread.objects.create(
        lab=lab,
        kind=AskThread.Kind.LAB,
        source=AskThread.Source.SEARCH,
        asker=asker,
        product=product,
        text=" ".join(text.split())[:300],
        urgent=urgent,
        created_at=now,
    )
    for membership in Membership.objects.filter(lab=lab).select_related("user"):
        person = membership.user
        if person == asker or not person.email or not membership.is_current():
            continue
        if person.kind != User.Kind.MEMBER:
            continue
        hold = "" if urgent and _asks_today(lab, person, now) < cap else BATCH
        queue_notification(
            lab=lab,
            kind=Kind.ASK_LAB,
            subject_obj=thread,
            recipient_email=person.email,
            recipient=person,
            context={"reply_to": asker.email},
            hold_reason=hold,
        )
    return thread


def _asks_today(lab, person, now):
    """Ask-the-lab emails the person has received (or that are on their way) today."""
    return Notification.objects.filter(
        lab=lab,
        kind=Kind.ASK_LAB,
        recipient=person,
        status__in=(Notification.Status.SENT, Notification.Status.PENDING),
        created_at__gte=_local_day_start(lab, now),
    ).count()


def release_ask_batches(lab, *, now=None):
    """At ``asks.batch_hours``: send each person at most their daily cap of waiting asks,
    oldest first; drop asks older than 48 hours. Returns how many went out."""
    now = now or timezone.now()
    cap = get_policy("asks.max_per_person_per_day", lab)
    waiting = Notification.objects.filter(
        lab=lab, kind=Kind.ASK_LAB, status=Notification.Status.HELD, hold_reason=BATCH
    ).order_by("created_at", "pk")
    stale = waiting.filter(created_at__lt=now - STALE_AFTER)
    stale.update(status=Notification.Status.SUPPRESSED, hold_reason="ask expired")
    released = 0
    sent_today = {}
    for notification in waiting.filter(created_at__gte=now - STALE_AFTER):
        person = notification.recipient
        if person is None:
            continue
        if person.pk not in sent_today:
            sent_today[person.pk] = _asks_today(lab, person, now)
        if sent_today[person.pk] >= cap:
            continue
        if release_notification(notification):
            sent_today[person.pk] += 1
            released += 1
    return released


@register_send_check(Kind.ASK_LAB)
def _ask_still_open(notification):
    thread = AskThread.objects.filter(pk=notification.subject_id).first()
    if thread is None or thread.closed_at is not None:
        return Suppressed("the ask was closed")
    return None


@register_context_builder(Kind.ASK_LAB)
def _ask_lab_context(notification):
    thread = AskThread.objects.select_related("asker", "product").get(pk=notification.subject_id)
    labels = dict(AskReply.Answer.choices)
    return {
        "asker_name": str(thread.asker),
        "what": thread.text or (thread.product.name if thread.product_id else "something"),
        "urgent": thread.urgent,
        "answer_links": [
            (labels[name], action_url(notification, name)) for name in ("have_it", "maybe", "no")
        ],
    }


for _name in ("have_it", "maybe", "no"):
    register_action(Kind.ASK_LAB, _name, label=dict(AskReply.Answer.choices)[_name])(_answer(_name))
