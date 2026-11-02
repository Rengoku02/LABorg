"""How the guard hears about new requests (apps.quartzy.signals.requests_synced)."""

import logging

from django_q.tasks import async_task

logger = logging.getLogger(__name__)


def requests_synced(sender, *, qlab_id, request_ids, via, **kwargs):
    """Quick and never raising, as the signal's contract asks: enqueue the checks."""
    async_task("apps.guard.tasks.check_requests", list(request_ids), via)
