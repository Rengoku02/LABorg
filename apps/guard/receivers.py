"""How the guard hears about new requests (apps.quartzy.signals.requests_synced)."""

import logging

from django_q.tasks import async_task

logger = logging.getLogger(__name__)


def requests_synced(sender, *, qlab_id, request_ids, via, **kwargs):
    """Quick and never raising, as the signal's contract asks: enqueue the checks."""
    async_task("apps.guard.tasks.check_requests", list(request_ids), via)


def ask_answered(sender, *, thread, reply, **kwargs):
    """A holder said "Yes, borrow some" to an alert's borrow ask: the borrow happened."""
    from apps.inventory.models import AskReply

    from . import outcomes
    from .models import Outcome

    if reply.answer != AskReply.Answer.LEND:
        return
    for intent in Outcome.objects.filter(
        ask_thread=thread, kind=Outcome.Kind.BORROW_REQUESTED
    ).select_related("duplicate_check__order_request"):
        outcomes.record(
            intent.duplicate_check,
            Outcome.Kind.BORROWED,
            by=intent.decided_by,
            ask_thread=thread,
            verified=True,
        )
