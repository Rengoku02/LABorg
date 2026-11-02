"""Single-flight jobs on a Postgres advisory lock (plan §8 "Cycle").

The lock is session-level, so it outlives the per-record transactions a sync run commits, and
it is released in ``finally``; a worker that dies loses its session and with it the lock.
"""

import hashlib
from contextlib import contextmanager

from django.db import connection
from django.utils import timezone

from .models import SyncRun


def lock_id(job, key):
    """A stable signed 64-bit id for "job:key" (Python's hash() changes between processes)."""
    digest = hashlib.sha256(f"{job}:{key}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


@contextmanager
def single_flight(job, key, *, quartzy_lab=None):
    """Hold the advisory lock for ``job:key`` (e.g. "head", qlab.pk) around a block.

    Yields True when the lock was taken. When another run holds it, records a SyncRun of kind
    ``job`` with status skipped_locked and yields False, so the caller returns at once::

        with single_flight("head", qlab.pk, quartzy_lab=qlab) as acquired:
            if not acquired:
                return "skipped_locked"
            ...
    """
    number = lock_id(job, key)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [number])
        acquired = cursor.fetchone()[0]
    if not acquired:
        now = timezone.now()
        SyncRun.objects.create(
            kind=job,
            quartzy_lab=quartzy_lab,
            status=SyncRun.Status.SKIPPED_LOCKED,
            started_at=now,
            finished_at=now,
            note=f"{job}:{key} was already running",
        )
        yield False
        return
    try:
        yield True
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [number])
