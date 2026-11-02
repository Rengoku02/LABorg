"""Django-Q2 tasks of the duplicate guard. Arguments are ids only; every task is safe to
run twice (a check is unique per request and matcher version)."""

from datetime import timedelta

from django.db.models import Exists, F, OuterRef
from django.db.models.functions import Coalesce
from django.utils import timezone
from django_q.tasks import async_task

from apps.quartzy.models import OrderRequest

from . import alerts, outcomes
from .checks import check_request
from .matcher import MATCHER_VERSION, UNRESOLVED
from .models import CheckRun, DuplicateCheck

SCHEDULE_PREFIX = "guard."
# Checks per task: a few seconds of work, far from the 600 s timeout.
CHECKS_PER_TASK = 50
SAFETY_NET_WINDOW = timedelta(days=2)


def check_requests(request_ids, via, run_id=None):
    """Check these requests (one lab's), CHECKS_PER_TASK at a time; the rest is re-enqueued
    in the same CheckRun, which is one cycle for the circuit breaker."""
    ids = list(request_ids)
    if not ids:
        return "nothing"
    run = CheckRun.objects.filter(pk=run_id).first() if run_id else None
    if run is None:
        lab_id = OrderRequest.objects.filter(pk__in=ids).values_list("lab_id", flat=True).first()
        if lab_id is None:
            return "gone"
        via = via if via in CheckRun.Via.values else CheckRun.Via.MANUAL
        run = CheckRun.objects.create(lab_id=lab_id, via=via)
    chunk, rest = ids[:CHECKS_PER_TASK], ids[CHECKS_PER_TASK:]
    for pk in chunk:
        check_request(pk, run=run)
    if rest:
        async_task("apps.guard.tasks.check_requests", rest, via, run.pk)
        return f"{len(chunk)} checked, {len(rest)} queued"
    run.finished_at = timezone.now()
    run.save(update_fields=["finished_at"])
    alerts.finish_cycle(run)  # release the run's alerts, or trip the circuit breaker
    return f"{len(chunk)} checked"


def safety_net(now=None):
    """Every 10 minutes: check armed requests first seen in the last 2 days that have no
    check yet (a lost signal, a dead worker), and redo checks made before their request was
    resolved. One batch per lab."""
    now = now or timezone.now()
    since = now - SAFETY_NET_WINDOW
    checked = DuplicateCheck.objects.filter(
        order_request=OuterRef("pk"), matcher_version=MATCHER_VERSION, replay=False
    )
    missing = (
        OrderRequest.objects.live()
        .filter(quartzy_lab__is_active=True, quartzy_lab__alerts_armed_at__isnull=False)
        .filter(first_seen_at__gte=since)
        .annotate(at=Coalesce("requested_at", "first_seen_at"))
        .filter(at__gte=F("quartzy_lab__alerts_armed_at"))
        .exclude(Exists(checked))
        .values_list("lab_id", "pk")
    )
    pending = (
        DuplicateCheck.objects.filter(
            matcher_version=MATCHER_VERSION,
            replay=False,
            status=DuplicateCheck.Status.UNCHECKABLE,
            uncheckable_reason=UNRESOLVED,
            order_request__first_seen_at__gte=since,
        )
        .exclude(order_request__resolution_status=OrderRequest.Resolution.UNRESOLVED)
        .values_list("lab_id", "order_request_id")
    )
    by_lab = {}
    for lab_id, pk in [*missing, *pending]:
        by_lab.setdefault(lab_id, set()).add(pk)
    for ids in by_lab.values():
        check_requests(sorted(ids), CheckRun.Via.SAFETY_NET)
    return f"{sum(len(ids) for ids in by_lab.values())} checked"


def verify_outcomes():
    """Every 15 minutes: cancel and join intentions the sync has seen happen; and the alert
    holds of runs that never finished."""
    verified = outcomes.verify_outcomes()
    released = alerts.release_stale_cycles()
    return f"{verified} verified, {released} stale cycle(s) released"


def wanted_schedules():
    """The ``guard.`` schedules (apps.core.schedules)."""
    from django_q.models import Schedule

    return {
        "guard.safety_net": {
            "func": "apps.guard.tasks.safety_net",
            "schedule_type": Schedule.MINUTES,
            "minutes": 10,
        },
        "guard.verify_outcomes": {
            "func": "apps.guard.tasks.verify_outcomes",
            "schedule_type": Schedule.MINUTES,
            "minutes": 15,
        },
    }
