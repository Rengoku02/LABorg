"""Small write services on the mirror that other apps use. The mirror itself is written only
by sync.py; these touch app-only fields.

``watch_requests`` keeps requests in the hot set (re-read by id every 15 minutes) for a while:
after a claim's "Did this arrive?", an alert's "cancel" (so the sync sees the CANCELLED that
verifies it), or a nudge. A watch only ever extends, so a short watch never cuts a longer one
short, and requests the lab logged itself (no Quartzy id) are never watched.
"""

from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from .models import OrderRequest

WATCH_DAYS = 7


def watch_requests(requests, *, days=WATCH_DAYS, now=None):
    """Keep ``requests`` (OrderRequests, ids, or a queryset) in the hot set for ``days``.
    Returns how many watches were started or extended (a longer watch is left as it is)."""
    until = (now or timezone.now()) + timedelta(days=days)
    if isinstance(requests, OrderRequest):
        requests = [requests]
    if hasattr(requests, "values_list") and hasattr(requests, "model"):
        rows = requests
    else:
        ids = [getattr(request, "pk", request) for request in requests]
        rows = OrderRequest.objects.filter(pk__in=ids)
    rows = rows.filter(quartzy_id__isnull=False).filter(
        Q(hot_until__isnull=True) | Q(hot_until__lt=until)
    )
    return rows.update(hot_until=until)
