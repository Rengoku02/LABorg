"""Asking instead of buying (plan §5 "Check before you buy"; design §8 "Actions").

- ``ask_owner``: email the holder of a stock item (or, for lab stock and stock of people who
  left, its steward or the co-owners) that someone would like to borrow some. Reply-To is
  the asker, so they arrange it between them; the holder's one tap (*Yes, borrow some* /
  *None left*) is recorded too.
- ``ask_share``: email the earlier requester of an open order: could it cover both?

Whom to ask comes from the presenter (``ItemView.ask_user_id``), so a restricted item's
holder is asked without the asker ever learning where it is. Replies are AskReplies, and
``signals.ask_answered`` tells the guard (a lent item verifies a borrow).
"""

from django.db import transaction

from apps.core.actions import InvalidActionLink, action_url, register_action
from apps.core.mail import queue_notification, register_context_builder
from apps.core.models import Membership, Notification, User
from apps.core.permissions import is_member
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
