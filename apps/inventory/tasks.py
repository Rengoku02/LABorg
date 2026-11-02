"""Django-Q2 tasks of the inventory, from one hourly tick: the weekly "claim your history"
round (Monday from 08:00 lab time), the ask-the-lab batches (at ``asks.batch_hours``) and the
daily purge of search logs (from 03:00)."""

from django.utils import timezone

from apps.core import schedules
from apps.core.models import Lab
from apps.core.policies import get_policy

from . import asks, claims, search

SCHEDULE_PREFIX = "inventory."
CLAIMS_WEEKDAY = 0  # Monday
CLAIMS_HOUR = 8
PURGE_HOUR = 3


def tick(now=None):
    """Hourly. On Monday from 08:00 lab time: surface the claims that are due and queue the
    week's claims emails (once a week each: the email's period is the ISO week)."""
    now = now or timezone.now()
    done = []
    for lab in Lab.objects.all():
        local = schedules.local_now(lab, now)
        if local.hour in (get_policy("asks.batch_hours", lab) or []):
            released = asks.release_ask_batches(lab, now=now)
            if released:
                done.append(f"{lab.slug}: {released} asks sent")
        if local.weekday() != CLAIMS_WEEKDAY or local.hour < CLAIMS_HOUR:
            continue
        surfaced = claims.surface_claims(lab, now=now)
        queued = claims.queue_claims_emails(lab, now=now)
        done.append(f"{lab.slug}: {surfaced} surfaced, {queued} emailed")
    if any(schedules.local_now(lab, now).hour == PURGE_HOUR for lab in Lab.objects.all()):
        deleted = search.purge_search_logs(now=now)
        if deleted:
            done.append(f"{deleted} old search logs deleted")
    return "; ".join(done) or "not due"


def wanted_schedules():
    return {"inventory.tick": schedules.hourly("apps.inventory.tasks.tick", minute_offset=5)}
