"""Django-Q2 tasks for the Quartzy mirror (plan §8 cadence). Arguments are ids only.

``ensure_schedules`` (manage.py setup_schedules) keeps one schedule per task and active
Quartzy lab:

- run_head every 2 minutes. Outside lab hours it skips itself until
  ``sync.head_interval_off_hours_minutes`` have passed since the last head run, so a single
  schedule gives both cadences.
- run_sweep every 5 minutes, run_hot every 15, run_nightly daily at 03:00 lab time.
- spec_diff weekly and heartbeat every 5 minutes, once for all labs.

Every sync run takes a single-flight advisory lock (locks.py): an overlapping run of the same
job records a skipped_locked SyncRun and returns. Each run reads only a few pages, which keeps
it well under a minute at real latency; the inventory re-enqueues itself to continue.

Only a successful head run that reached Quartzy vouches for the sync: it sets
``last_synced_at`` and pings the sync heartbeat, and only while no active lab is behind
(``QuartzyLab.objects.stale()``), so one lab's success can't hide another's outage. A refused
token emails the system owners, once a day.

``resolve_type`` is not scheduled: QuartzyType.save enqueues it when a type's item type
changes, receivers.py when an item type's identity rule does, and it re-enqueues itself in
chunks too. Its runs show on the health panel, and never ping the heartbeat.
"""

import hashlib
import logging
from datetime import timedelta

import httpx
from django.conf import settings
from django.utils import timezone
from django_q.tasks import async_task

from apps.core import schedules
from apps.core.mail import notify_admins
from apps.core.models import Lab
from apps.core.policies import get_policy

from . import sync
from .client import QuartzyAuthError, QuartzyError
from .locks import single_flight
from .models import QuartzyLab, QuartzyType, SpecSnapshot, SyncRun

logger = logging.getLogger(__name__)

SPEC_URL = "https://docs.quartzy.com/redocusaurus/public-api.yaml"
SPEC_BASELINE = settings.BASE_DIR / "docs" / "research" / "quartzy-public-api.yaml"
INVENTORY_PAGES_PER_TASK = 30
# A few seconds of work per task, far from the 600 s timeout.
RESOLVE_PER_TASK = 200
RUN_RETENTION = timedelta(days=60)
# Q2's scheduler may start a 2-minute schedule a little early; a run that is due within this
# margin still goes ahead.
SCHEDULE_SLACK = timedelta(minutes=1)
SCHEDULE_PREFIX = "quartzy."
NIGHTLY_HOUR = 3


def http_get(url, *, timeout):
    """A plain GET outside the Quartzy client (heartbeats, the spec). Tests replace it."""
    return httpx.get(url, timeout=timeout, follow_redirects=True)


# ---------------------------------------------------------------------------------------
# Sync tasks


def run_head(qlab_id):
    qlab = _active(qlab_id)
    if qlab is None:
        return "inactive"
    if not head_due(qlab):
        return "not_due"
    return _sync_job(SyncRun.Kind.HEAD, qlab, sync.head_probe)


def run_sweep(qlab_id):
    qlab = _active(qlab_id)
    if qlab is None:
        return "inactive"
    return _sync_job(SyncRun.Kind.SWEEP, qlab, sync.rolling_sweep)


def run_hot(qlab_id):
    qlab = _active(qlab_id)
    if qlab is None:
        return "inactive"

    def work(client, qlab, run):
        sync.refresh_hot_set(client, qlab, run, sync.hot_set_ids(qlab))
        if run.status != SyncRun.Status.ABORTED:
            sync.confirm_unlisted_requests(client, qlab, run)

    return _sync_job(SyncRun.Kind.HOT, qlab, work)


def run_nightly(qlab_id):
    """Types and inventory (plan §8 "Nightly"), and pruning of old run records."""
    qlab = _active(qlab_id)
    if qlab is None:
        return "inactive"
    types = _sync_job(SyncRun.Kind.TYPES, qlab, sync.sync_types)
    SyncRun.objects.filter(started_at__lt=timezone.now() - RUN_RETENTION).delete()
    return f"types {types}, inventory {run_inventory(qlab.pk)}"


def run_inventory(qlab_id):
    """One chunk of the inventory pass; re-enqueues itself until the pass is complete."""
    qlab = _active(qlab_id)
    if qlab is None:
        return "inactive"
    progress = {}

    def work(client, qlab, run):
        progress["done"] = sync.sync_inventory(
            client, qlab, run, max_pages=INVENTORY_PAGES_PER_TASK
        )

    status = _sync_job(SyncRun.Kind.INVENTORY, qlab, work)
    if progress.get("done") is False:
        # Enqueued after the lock is released, or the next chunk would find it taken.
        async_task("apps.quartzy.tasks.run_inventory", qlab.pk)
    return status


def resolve_type(qtype_id, after=0):
    """Resolve the requests of a Quartzy type again after its item type, or that item type's
    identity rule, changed: the RESOLVE_PER_TASK after request id ``after``, in id order, then
    it re-enqueues itself for the rest (plan §8 "Long work"). No Quartzy calls, so no lock
    either: each request is resolved with the mapping of that moment, and a later change
    starts a chain of its own. Each chunk is a run that announces the requests it resolved
    differently (signals.requests_synced)."""
    qtype = QuartzyType.objects.select_related("quartzy_lab").filter(pk=qtype_id).first()
    if qtype is None:
        return "gone"
    ids = list(
        qtype.order_requests.filter(pk__gt=after)
        .order_by("pk")
        .values_list("pk", flat=True)[:RESOLVE_PER_TASK]
    )
    if not ids:
        return "done"
    with sync.recorded_run(SyncRun.Kind.RESOLVE, quartzy_lab=qtype.quartzy_lab) as run:
        sync.resolve_again(ids, run)
    if len(ids) == RESOLVE_PER_TASK:
        async_task("apps.quartzy.tasks.resolve_type", qtype_id, ids[-1])
    return str(run.status)


def head_due(qlab, now=None):
    """Whether a head run is due: every ``sync.head_interval_lab_hours_minutes`` within the
    lab's hours (``sync.lab_hours``, lab time zone), every
    ``sync.head_interval_off_hours_minutes`` outside them."""
    now = now or timezone.now()
    lab = qlab.lab
    start, end = get_policy("sync.lab_hours", lab)
    hour = schedules.local_now(lab, now).hour
    in_hours = start <= hour < end if start <= end else (hour >= start or hour < end)
    key = (
        "sync.head_interval_lab_hours_minutes"
        if in_hours
        else "sync.head_interval_off_hours_minutes"
    )
    interval = timedelta(minutes=get_policy(key, lab))
    last = (
        SyncRun.objects.filter(quartzy_lab=qlab, kind=SyncRun.Kind.HEAD)
        .exclude(status=SyncRun.Status.SKIPPED_LOCKED)
        .order_by("-started_at")
        .values_list("started_at", flat=True)
        .first()
    )
    return last is None or now - last >= interval - SCHEDULE_SLACK


def _active(qlab_id):
    return QuartzyLab.objects.select_related("lab").filter(pk=qlab_id, is_active=True).first()


def _sync_job(kind, qlab, work):
    """Run ``work(client, qlab, run)`` as one single-flight, recorded run of ``kind``."""
    if qlab.backoff_until and timezone.now() < qlab.backoff_until:
        return "backoff"
    if not settings.QUARTZY_ACCESS_TOKEN:
        logger.warning("QUARTZY_ACCESS_TOKEN is not set; skipping the %s sync of %s", kind, qlab)
        return "no_token"
    with single_flight(kind, qlab.pk, quartzy_lab=qlab) as acquired:
        if not acquired:
            return str(SyncRun.Status.SKIPPED_LOCKED)
        with sync.client_from_settings() as client:
            try:
                with sync.recorded_run(kind, quartzy_lab=qlab, client=client) as run:
                    work(client, qlab, run)
            except QuartzyError as exc:
                # A refused token stops every sync until someone acts: say so loudly.
                level = logging.ERROR if isinstance(exc, QuartzyAuthError) else logging.WARNING
                logger.log(level, "Quartzy %s sync of %s failed: %s", kind, qlab, exc)
                if isinstance(exc, QuartzyAuthError):
                    _report_refused_token(kind, qlab, exc)
                if exc.status == 429 and exc.retry_after:
                    until = timezone.now() + timedelta(seconds=exc.retry_after)
                    QuartzyLab.objects.filter(pk=qlab.pk).update(backoff_until=until)
    if kind == SyncRun.Kind.HEAD and run.status == SyncRun.Status.OK and run.calls:
        QuartzyLab.objects.filter(pk=qlab.pk).update(last_synced_at=run.finished_at)
        if not QuartzyLab.objects.stale().exists():
            _ping("sync")
    return str(run.status)


def _report_refused_token(kind, qlab, exc):
    notify_admins(
        qlab.lab,
        title=f"Quartzy refused the token ({qlab})",
        message=(
            f"The {kind} sync of {qlab} failed: {exc}. Every sync stops until the token "
            "works again. If it was revoked or rotated, follow the runbook's "
            '"Quartzy token rotation".'
        ),
        subject_obj=qlab,
        period_key=f"auth-{timezone.localdate():%Y-%m-%d}",
    )


# ---------------------------------------------------------------------------------------
# Monitoring


def heartbeat():
    """The worker's ping to the external dead-man's switch (plan §6 "Monitoring")."""
    return _ping("worker")


def _ping(which):
    url = settings.LABSTOCK_HEARTBEAT_URLS.get(which)
    if not url:
        return "skipped"
    try:
        http_get(url, timeout=10).raise_for_status()
    except httpx.HTTPError as exc:
        # The URL holds the check's secret id, so it stays out of the log.
        logger.warning("The %s heartbeat ping failed: %s", which, type(exc).__name__)
        return "failed"
    return "pinged"


def spec_diff():
    """Weekly: has Quartzy's published OpenAPI spec changed? (plan §8 "Weekly")

    Compares the sha256 of the live spec with the last stored one, starting from
    docs/research/quartzy-public-api.yaml. A change is stored, logged and emailed to the
    system owners, who decide whether the client or schemas need work.
    """
    try:
        response = http_get(SPEC_URL, timeout=30)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("Could not fetch Quartzy's API spec: %s", type(exc).__name__)
        return "unavailable"
    digest = hashlib.sha256(response.content).hexdigest()
    last = SpecSnapshot.objects.order_by("-fetched_at", "-pk").first()
    known = last.sha256 if last else hashlib.sha256(SPEC_BASELINE.read_bytes()).hexdigest()
    if digest == known:
        return "unchanged"
    snapshot = SpecSnapshot.objects.create(sha256=digest, content=response.text)
    message = (
        f"The public API spec at {SPEC_URL} changed (sha256 {known[:12]}… → {digest[:12]}…). "
        "Compare it with docs/research/quartzy-public-api.yaml (the new copy is in the admin "
        "under Quartzy mirror → Spec snapshots) before trusting the sync's assumptions."
    )
    logger.warning("Quartzy's API spec changed: %s → %s", known[:12], digest[:12])
    labs = Lab.objects.filter(quartzy_labs__is_active=True).distinct() or Lab.objects.all()
    for lab in labs:
        notify_admins(
            lab, title="Quartzy's API spec changed", message=message, subject_obj=snapshot
        )
    return "changed"


# ---------------------------------------------------------------------------------------
# Schedules


def wanted_schedules():
    """The ``quartzy.`` schedules (see apps.core.schedules): heartbeat and spec diff once,
    then head, sweep, hot and nightly per active Quartzy lab."""
    from django_q.models import Schedule

    wanted = {
        "quartzy.heartbeat": {
            "func": "apps.quartzy.tasks.heartbeat",
            "schedule_type": Schedule.MINUTES,
            "minutes": 5,
        },
        "quartzy.spec_diff": {
            "func": "apps.quartzy.tasks.spec_diff",
            "schedule_type": Schedule.WEEKLY,
            "minutes": None,
        },
    }
    for qlab in QuartzyLab.objects.active().select_related("lab"):
        for job, minutes in (("head", 2), ("sweep", 5), ("hot", 15)):
            wanted[f"quartzy.{job}.{qlab.pk}"] = {
                "func": f"apps.quartzy.tasks.run_{job}",
                "args": str(qlab.pk),
                "schedule_type": Schedule.MINUTES,
                "minutes": minutes,
            }
        wanted[f"quartzy.nightly.{qlab.pk}"] = {
            "func": "apps.quartzy.tasks.run_nightly",
            "args": str(qlab.pk),
            "schedule_type": Schedule.DAILY,
            "minutes": None,
            "first_run": schedules.next_local(qlab.lab, NIGHTLY_HOUR),
        }
    return wanted


def ensure_schedules():
    """Create or update the Quartzy schedules, and remove ours that no longer apply (labs
    made inactive). Idempotent. Returns (created, updated, removed). ``setup_schedules`` runs
    every app's schedules (apps.core.schedules.ensure_all); the backfill runs just these."""
    return schedules.sync_prefix(SCHEDULE_PREFIX, wanted_schedules())
