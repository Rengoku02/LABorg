"""Single-flight runs on a Postgres advisory lock."""

import hashlib
import threading

import pytest
from django.db import connection

from apps.quartzy import tasks
from apps.quartzy.locks import lock_id, single_flight
from apps.quartzy.models import SyncRun


def test_lock_ids_are_stable_signed_64_bit_numbers():
    expected = int.from_bytes(hashlib.sha256(b"head:7").digest()[:8], "big", signed=True)
    assert lock_id("head", 7) == expected == lock_id("head", "7")
    assert lock_id("head", 7) != lock_id("sweep", 7) != lock_id("head", 8)
    assert -(2**63) <= expected < 2**63


@pytest.mark.django_db(transaction=True)
def test_a_concurrent_run_of_the_same_job_is_skipped(fake, fake_lab, quartzy_lab, use_fake):
    fake.add_request(fake_lab)
    holding, release = threading.Event(), threading.Event()
    errors = []

    def other_worker():  # its own thread, so its own database connection and session
        try:
            with single_flight("head", quartzy_lab.pk, quartzy_lab=quartzy_lab) as acquired:
                assert acquired
                holding.set()
                release.wait(timeout=10)
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)
        finally:
            connection.close()

    thread = threading.Thread(target=other_worker)
    thread.start()
    try:
        assert holding.wait(timeout=10)
        assert tasks.run_head(quartzy_lab.pk) == "skipped_locked"
        assert tasks.run_sweep(quartzy_lab.pk) == "ok"  # another job's lock is free
    finally:
        release.set()
        thread.join()
    assert not errors
    skipped = SyncRun.objects.get(kind="head")
    assert skipped.status == SyncRun.Status.SKIPPED_LOCKED and skipped.quartzy_lab == quartzy_lab
    assert skipped.calls == 0
    assert fake.calls["/order-requests"] == 2  # only the sweep read: page 1 and empty page 2

    # Released in ``finally``: the next head run goes ahead.
    assert tasks.run_head(quartzy_lab.pk) == "ok"


def held_advisory_locks():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
        )
        return cursor.fetchone()[0]


@pytest.mark.django_db
def test_the_lock_is_released_even_when_the_block_fails(quartzy_lab):
    # Advisory locks are re-entrant within a session, so look at pg_locks rather than
    # trying to take the lock again.
    with single_flight("sweep", quartzy_lab.pk) as acquired:
        assert acquired and held_advisory_locks() == 1
    assert held_advisory_locks() == 0
    with pytest.raises(RuntimeError), single_flight("sweep", quartzy_lab.pk):
        raise RuntimeError("boom")
    assert held_advisory_locks() == 0
