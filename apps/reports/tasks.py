"""Django-Q2 tasks of the reports: the weekly emails and the daily nudges, from one hourly
tick that reads each lab's local time (apps.core.schedules). Each email is once per week
(its period key) or once per request, so a tick that runs again sends nothing twice."""

from datetime import time

from django.utils import timezone

from apps.core import schedules
from apps.core.models import Lab

from . import digests, nudges

SCHEDULE_PREFIX = "reports."
MONDAY = 0
DIGEST_AT = time(8, 0)
SUMMARY_AT = time(8, 30)
NUDGES_AT = time(9, 0)


def tick(now=None):
    now = now or timezone.now()
    done = []
    for lab in Lab.objects.all():
        local = schedules.local_now(lab, now)
        clock = local.time()
        parts = []
        if local.weekday() == MONDAY and clock >= DIGEST_AT:
            parts.append(f"{digests.queue_personal_digests(lab, now=now)} digests")
        if local.weekday() == MONDAY and clock >= SUMMARY_AT:
            parts.append(f"{digests.queue_lab_summary(lab, now=now)} summaries")
        if clock >= NUDGES_AT:
            parts.append(f"{nudges.nudge_stale_requests(lab, now=now)} nudges")
        if parts:
            done.append(f"{lab.slug}: {', '.join(parts)}")
    return "; ".join(done) or "not due"


def wanted_schedules():
    return {"reports.tick": schedules.hourly("apps.reports.tasks.tick", minute_offset=35)}
