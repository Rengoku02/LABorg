"""Django-Q2 tasks of the inventory: the weekly "claim your history" round."""

from django.utils import timezone

from apps.core import schedules
from apps.core.models import Lab

from . import claims

SCHEDULE_PREFIX = "inventory."
CLAIMS_WEEKDAY = 0  # Monday
CLAIMS_HOUR = 8


def tick(now=None):
    """Hourly. On Monday from 08:00 lab time: surface the claims that are due and queue the
    week's claims emails (once a week each: the email's period is the ISO week)."""
    now = now or timezone.now()
    done = []
    for lab in Lab.objects.all():
        local = schedules.local_now(lab, now)
        if local.weekday() != CLAIMS_WEEKDAY or local.hour < CLAIMS_HOUR:
            continue
        surfaced = claims.surface_claims(lab, now=now)
        queued = claims.queue_claims_emails(lab, now=now)
        done.append(f"{lab.slug}: {surfaced} surfaced, {queued} emailed")
    return "; ".join(done) or "not due"


def wanted_schedules():
    return {"inventory.tick": schedules.hourly("apps.inventory.tasks.tick", minute_offset=5)}
