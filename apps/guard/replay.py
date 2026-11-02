"""Replay: the matcher run over the lab's history (design §7 "Replay").

Each request of the last ``months`` is checked as of the moment it was made, with the other
requests' statuses at that moment (``matcher.states_at``), and stored as a ``replay=True``
check. Replays are never alerted; their matches go to the co-owner's labelling page after
the live shadow matches, so a tier can reach its labelled-match gate before weeks of live
shadow have passed. Re-running skips requests already replayed with this matcher version.
"""

from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.core.dates import months_before
from apps.quartzy.models import OrderRequest

from .checks import check_request
from .matcher import MATCHER_VERSION
from .models import CheckRun, DuplicateCheck


def replay(lab, *, months=12, now=None, progress=None):
    """Replay the lab's Quartzy requests of the last ``months``. Returns the CheckRun."""
    now = now or timezone.now()
    start = months_before(now, months)
    done = DuplicateCheck.objects.filter(
        lab=lab, replay=True, matcher_version=MATCHER_VERSION
    ).values("order_request_id")
    ids = list(
        OrderRequest.objects.for_lab(lab)
        .live()
        .filter(quartzy_lab__isnull=False)
        .exclude(pk__in=done)
        .annotate(at=Coalesce("requested_at", "first_seen_at"))
        .filter(at__gte=start, at__lte=now)
        .order_by("at", "pk")
        .values_list("pk", flat=True)
    )
    run = CheckRun.objects.create(lab=lab, via=CheckRun.Via.REPLAY)
    for number, pk in enumerate(ids, 1):
        check_request(pk, run=run, replay=True, now=now)
        if progress is not None and number % 200 == 0:
            progress(number, len(ids))
    run.finished_at = timezone.now()
    run.save(update_fields=["finished_at"])
    return run
