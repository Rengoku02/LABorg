"""Reading requests by id: the hot set, 404 tombstones, when 404s are believed (a canary, the
mass-404 abort, a daily cap), and errors on single ids."""

import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import time_machine
from django.utils import timezone

from apps.core.models import Notification, User
from apps.core.policies import set_policy
from apps.quartzy import sync, tasks
from apps.quartzy.client import QuartzyAuthError, QuartzyError
from apps.quartzy.models import OrderRequest, QuartzyLab, SyncError, SyncRun

pytestmark = pytest.mark.django_db

NOW = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)


@pytest.fixture
def mirrored(fake, fake_lab, quartzy_lab, api):
    """``mirrored(n)``: n requests in the fake, all mirrored."""

    def _make(count):
        requests = [fake.add_request(fake_lab) for _ in range(count)]
        sync.backfill_requests(api, quartzy_lab, SyncRun.objects.create(kind="backfill"))
        fake.reset_calls()
        return requests

    return _make


@pytest.fixture
def owner(db):
    return User.objects.create_superuser(email="root@lab.test", full_name="System Owner")


def deleted_ids():
    return set(
        OrderRequest.objects.filter(deleted_in_quartzy_at__isnull=False).values_list(
            "quartzy_id", flat=True
        )
    )


def test_a_404_by_id_marks_the_request_deleted_and_keeps_the_row(
    fake, quartzy_lab, api, run, mirrored
):
    requests = mirrored(3)
    fake.delete_request(requests[0]["id"])
    missing = sync.refresh_hot_set(api, quartzy_lab, run, [r["id"] for r in requests])
    assert missing == [requests[0]["id"]]
    assert deleted_ids() == {requests[0]["id"]} and OrderRequest.objects.count() == 3
    gone = OrderRequest.objects.get(quartzy_id=requests[0]["id"])
    assert not gone.is_open and OrderRequest.objects.open().count() == 2
    assert run.status == SyncRun.Status.RUNNING  # not aborted
    assert fake.calls["/order-requests/{id}"] == 3
    assert fake.calls["/labs/{id}"] == 1  # the lab itself is still visible: a real deletion
    assert run.seen == 2 and run.changed == 1


def test_a_request_listed_again_loses_its_tombstone(
    fake, fake_lab, quartzy_lab, api, run, mirrored
):
    [request] = mirrored(1)
    payload = fake.delete_request(request["id"])
    sync.refresh_hot_set(api, quartzy_lab, run, [request["id"]])
    assert deleted_ids() == {request["id"]}
    fake.order_requests[payload["id"]] = payload  # it was there after all
    sync.upsert_order_request(quartzy_lab, api.order_request(request["id"]), via="poll", run=run)
    assert deleted_ids() == set()


def test_many_404s_abort_the_run_mark_nothing_and_alert_the_owner(
    fake, quartzy_lab, api, run, mirrored, owner, caplog
):
    requests = mirrored(10)
    for request in requests[:5]:
        fake.delete_request(request["id"])
    with caplog.at_level(logging.ERROR, logger="apps.quartzy.sync"):
        missing = sync.refresh_hot_set(api, quartzy_lab, run, [r["id"] for r in requests])
    assert len(missing) == 5 and deleted_ids() == set()
    assert run.status == SyncRun.Status.ABORTED and "5 of 10 requests" in run.note
    assert any("ABORTED" in message for message in caplog.messages)
    alert = Notification.objects.get(kind=Notification.Kind.SYSTEM, recipient=owner)
    assert "aborted" in alert.context["title"]


def test_the_404_limit_is_a_policy(fake, quartzy_lab, api, run, mirrored):
    requests = mirrored(10)
    for request in requests[:5]:
        fake.delete_request(request["id"])
    set_policy("sync.max_404_fraction", 0.5, lab=quartzy_lab.lab)
    sync.refresh_hot_set(api, quartzy_lab, run, [r["id"] for r in requests])
    assert run.status == SyncRun.Status.RUNNING and len(deleted_ids()) == 5


def test_404s_prove_nothing_once_the_lab_itself_is_gone(
    fake, fake_lab, quartzy_lab, api, run, mirrored
):
    requests = mirrored(30)
    fake.delete_request(requests[0]["id"])
    del fake.labs[fake_lab["id"]]  # the token lost access to the lab
    sync.refresh_hot_set(api, quartzy_lab, run, [requests[0]["id"]])
    assert run.status == SyncRun.Status.ABORTED and deleted_ids() == set()
    assert "no longer shows the lab" in run.note


def test_hot_set_is_new_open_requests_and_flagged_ones_not_freshly_listed(
    fake, fake_lab, quartzy_lab, api, run
):
    recent = fake.add_request(fake_lab, requested_at=NOW - timedelta(hours=5))
    received = fake.add_request(fake_lab, requested_at=NOW - timedelta(hours=4), status="RECEIVED")
    backfilled = fake.add_request(fake_lab, requested_at=NOW - timedelta(days=30))
    flagged = fake.add_request(fake_lab, requested_at=NOW - timedelta(days=40), status="RECEIVED")
    with time_machine.travel(NOW - timedelta(hours=1), tick=False):
        for request in (recent, received, backfilled, flagged):
            sync.upsert_order_request(
                quartzy_lab, api.order_request(request["id"]), via="poll", run=run
            )
    OrderRequest.objects.filter(quartzy_id=flagged["id"]).update(hot_until=NOW + timedelta(days=1))
    assert set(sync.hot_set_ids(quartzy_lab, now=NOW)) == {recent["id"], flagged["id"]}

    # A list page showed it five minutes ago: fresh enough to leave out.
    OrderRequest.objects.filter(quartzy_id=recent["id"]).update(
        last_seen_at=NOW - timedelta(minutes=5)
    )
    assert sync.hot_set_ids(quartzy_lab, now=NOW) == [flagged["id"]]
    # Requests stop being hot after two days, flags when they expire.
    assert sync.hot_set_ids(quartzy_lab, now=NOW + timedelta(days=3)) == []


def test_requests_a_full_sweep_did_not_list_are_checked_by_id(
    fake, fake_lab, quartzy_lab, api, settings
):
    requests = [fake.add_request(fake_lab) for _ in range(6)]
    with time_machine.travel(NOW, tick=False):
        sync.backfill_requests(api, quartzy_lab, SyncRun.objects.create(kind="backfill"))
    fake.delete_request(requests[2]["id"])
    set_policy("sync.sweep_pages_per_cycle", 5)
    with time_machine.travel(NOW + timedelta(hours=1), tick=False):
        sync.rolling_sweep(api, quartzy_lab, SyncRun.objects.create(kind="sweep"))
    quartzy_lab.refresh_from_db()
    assert quartzy_lab.full_sweep_started_at == NOW + timedelta(hours=1)  # a complete pass

    hot = SyncRun.objects.create(kind=SyncRun.Kind.HOT, quartzy_lab=quartzy_lab)
    fake.reset_calls()
    assert sync.confirm_unlisted_requests(api, quartzy_lab, hot) == [requests[2]["id"]]
    assert deleted_ids() == {requests[2]["id"]}
    assert fake.calls["/order-requests/{id}"] == 2  # the suspect, then a canary
    # Now nothing is unlisted any more.
    assert sync.confirm_unlisted_requests(api, quartzy_lab, hot) == []


def test_an_unlisted_request_that_still_exists_is_simply_refreshed(
    fake, fake_lab, quartzy_lab, api, run, mirrored
):
    [skipped] = mirrored(1)  # e.g. skipped by the sweep while pages shifted
    OrderRequest.objects.update(last_seen_at=NOW - timedelta(days=1))
    quartzy_lab.full_sweep_started_at = NOW
    assert sync.confirm_unlisted_requests(api, quartzy_lab, run) == []
    obj = OrderRequest.objects.get()
    assert obj.deleted_in_quartzy_at is None and obj.last_seen_at > NOW - timedelta(days=1)
    assert skipped["id"] == obj.quartzy_id


# ---------------------------------------------------------------------------------------
# Whether 404s are deletions: a canary request, a fraction of what was read, a daily cap


def hot(qlab):
    return SyncRun.objects.create(kind=SyncRun.Kind.HOT, quartzy_lab=qlab)


def test_two_of_three_hot_requests_deleted_are_both_tombstoned(fake, quartzy_lab, api, mirrored):
    older, *recent = mirrored(4)  # the older one stays out of the hot set: the canary
    for request in recent[:2]:
        fake.delete_request(request["id"])
    run = hot(quartzy_lab)
    missing = sync.refresh_hot_set(api, quartzy_lab, run, [r["id"] for r in recent])
    assert missing == [r["id"] for r in recent[:2]]
    assert run.status == SyncRun.Status.RUNNING
    assert deleted_ids() == {r["id"] for r in recent[:2]}
    assert fake.calls["/order-requests/{id}"] == 4  # the three, then the canary
    assert older["id"] in [path.rsplit("/", 1)[-1] for _m, path, _p in fake.log]


def test_two_requests_deleted_today_never_stall_the_hot_set(
    fake, fake_lab, quartzy_lab, use_fake, owner
):
    old = [fake.add_request(fake_lab, requested_at=NOW - timedelta(days=30)) for _ in range(3)]
    today = [fake.add_request(fake_lab, requested_at=NOW - timedelta(hours=3)) for _ in range(3)]
    with time_machine.travel(NOW - timedelta(hours=1)):
        sync.backfill_requests(fake.client(), quartzy_lab, SyncRun.objects.create(kind="backfill"))
    fake.delete_request(today[0]["id"])
    fake.delete_request(today[1]["id"])
    with time_machine.travel(NOW):
        assert tasks.run_hot(quartzy_lab.pk) == "ok"
        assert tasks.run_hot(quartzy_lab.pk) == "ok"
    assert deleted_ids() == {today[0]["id"], today[1]["id"]}
    assert sync.hot_set_ids(quartzy_lab, now=NOW + timedelta(minutes=15)) == [today[2]["id"]]
    assert not Notification.objects.filter(kind=Notification.Kind.SYSTEM).exists()
    assert OrderRequest.objects.filter(quartzy_id__in=[r["id"] for r in old]).live().count() == 3


@pytest.mark.parametrize("refusal", [404, 403])
def test_404s_prove_nothing_when_the_canary_is_refused_too(
    fake, quartzy_lab, api, mirrored, owner, monkeypatch, refusal
):
    older, recent = mirrored(2)

    def lost_sight(request_id):
        # The token no longer sees the lab's requests, though GET /labs/{id} still answers:
        # a lone 404, and the canary (the other request) is refused as well.
        if request_id == older["id"] and refusal == 403:
            raise QuartzyAuthError(f"GET /order-requests/{request_id} returned 403", status=403)
        return None

    monkeypatch.setattr(api, "order_request", lost_sight)
    run = hot(quartzy_lab)
    sync.refresh_hot_set(api, quartzy_lab, run, [recent["id"]])
    assert run.status == SyncRun.Status.ABORTED and deleted_ids() == set()
    assert "token or permission" in run.note
    assert Notification.objects.filter(kind=Notification.Kind.SYSTEM, recipient=owner).exists()


def test_fewer_than_five_404s_are_believed_whatever_their_share(fake, quartzy_lab, api, mirrored):
    _canary, *recent = mirrored(5)
    for request in recent:
        fake.delete_request(request["id"])
    run = hot(quartzy_lab)
    sync.refresh_hot_set(api, quartzy_lab, run, [r["id"] for r in recent])  # 4 of 4 missing
    assert run.status == SyncRun.Status.RUNNING and len(deleted_ids()) == 4


def test_the_404_share_of_unlisted_requests_counts_only_what_was_read(
    fake, fake_lab, quartzy_lab, api, run, mirrored
):
    requests = mirrored(40)
    listed, unlisted = requests[:25], requests[25:]  # 15 the last complete pass didn't list
    OrderRequest.objects.update(last_seen_at=NOW)
    ids = [r["id"] for r in unlisted]
    OrderRequest.objects.filter(quartzy_id__in=ids).update(last_seen_at=NOW - timedelta(days=1))
    quartzy_lab.full_sweep_started_at = NOW - timedelta(hours=1)
    for request in unlisted[:6]:
        fake.delete_request(request["id"])
    set_policy("sync.max_404_fraction", 0.2, lab=quartzy_lab.lab)
    # 6 of the 15 read by id are missing: 40%, though only 15% of the lab's 40 requests.
    with time_machine.travel(NOW):
        sync.confirm_unlisted_requests(api, quartzy_lab, run)
    assert run.status == SyncRun.Status.ABORTED and "6 of 15 requests" in run.note
    assert deleted_ids() == set() and len(listed) == 25


def test_tombstones_are_capped_per_lab_and_day(fake, quartzy_lab, api, mirrored, owner):
    _canary, *recent = mirrored(4)
    for request in recent:
        fake.delete_request(request["id"])
    set_policy("sync.max_tombstones_per_day", 2, lab=quartzy_lab.lab)
    run = hot(quartzy_lab)
    sync.refresh_hot_set(api, quartzy_lab, run, [r["id"] for r in recent])
    assert len(deleted_ids()) == 2 and run.status == SyncRun.Status.RUNNING
    assert "max_tombstones_per_day" in run.note
    [alert] = Notification.objects.filter(kind=Notification.Kind.SYSTEM, recipient=owner)
    assert "cap" in alert.context["title"]
    sync.refresh_hot_set(api, quartzy_lab, hot(quartzy_lab), [r["id"] for r in recent])
    assert len(deleted_ids()) == 2 and Notification.objects.count() == 1  # once a day
    with time_machine.travel(timezone.now() + timedelta(days=1, minutes=1)):
        sync.refresh_hot_set(api, quartzy_lab, hot(quartzy_lab), [r["id"] for r in recent])
    assert len(deleted_ids()) == 3


# ---------------------------------------------------------------------------------------
# One id Quartzy won't serve by id doesn't stop the others


def test_one_request_quartzy_refuses_by_id_does_not_stop_the_hot_set(
    fake, quartzy_lab, api, mirrored
):
    requests = mirrored(4)
    fake.delete_request(requests[3]["id"])
    fake.fail_next(403, path="/order-requests/{id}")  # moved to a lab the token can't see
    run = hot(quartzy_lab)
    missing = sync.refresh_hot_set(api, quartzy_lab, run, [r["id"] for r in requests])
    assert missing == [requests[3]["id"]] and deleted_ids() == {requests[3]["id"]}
    error = SyncError.objects.get()
    assert (error.kind, error.quartzy_id) == ("read", requests[0]["id"])
    assert "403" in error.message and run.errors == 1 and run.status == SyncRun.Status.RUNNING


def test_a_request_quartzy_keeps_failing_on_goes_to_the_back_of_the_queue(
    fake, fake_lab, quartzy_lab, api, mirrored, monkeypatch
):
    requests = mirrored(5)
    OrderRequest.objects.update(last_seen_at=NOW - timedelta(days=1))
    quartzy_lab.full_sweep_started_at = NOW - timedelta(hours=1)
    stubborn = OrderRequest.objects.order_by("last_seen_at", "pk").first()
    OrderRequest.objects.filter(pk=stubborn.pk).update(last_seen_at=NOW - timedelta(days=2))
    read = []
    real = api.order_request

    def flaky(request_id):
        read.append(request_id)
        if request_id == stubborn.quartzy_id:
            raise QuartzyError(f"GET /order-requests/{request_id} returned 500", status=500)
        return real(request_id)

    monkeypatch.setattr(api, "order_request", flaky)
    with time_machine.travel(NOW):
        sync.confirm_unlisted_requests(api, quartzy_lab, hot(quartzy_lab), limit=1)
        sync.confirm_unlisted_requests(api, quartzy_lab, hot(quartzy_lab), limit=1)
    assert read[0] == stubborn.quartzy_id and read[1] != stubborn.quartzy_id
    assert SyncError.objects.get().kind == "read"
    assert {r["id"] for r in requests} >= set(read)


@pytest.mark.parametrize(("status", "failures"), [(401, 1), (429, 1), (500, 3), ("timeout", 3)])
def test_a_refused_token_a_rate_limit_or_an_outage_still_stops_the_run(
    fake, quartzy_lab, api, mirrored, status, failures
):
    requests = mirrored(4)
    fake.fail_next(status, times=4 * failures, path="/order-requests/{id}")
    with pytest.raises(QuartzyError) as caught:
        sync.refresh_hot_set(api, quartzy_lab, hot(quartzy_lab), [r["id"] for r in requests])
    assert caught.value.status == (None if status == "timeout" else status)
    assert SyncError.objects.count() == failures - 1  # the ones before the outage was clear


def refuse_by_id(fake, monkeypatch, requests, status=403):
    """From now on Quartzy answers ``status`` to GET /order-requests/{id} for these requests
    (moved to a lab the token can't see), and still serves everything else."""
    refused = {request["id"] for request in requests}
    real = fake._handle

    def handle(request):
        path = request.url.raw_path.decode("ascii").split("?", 1)[0]
        if path.startswith("/order-requests/") and path.rsplit("/", 1)[-1] in refused:
            return httpx.Response(status, json={"message": "Forbidden"})
        return real(request)

    monkeypatch.setattr(fake, "_handle", handle)


def test_requests_quartzy_refuses_by_id_never_stop_the_hot_set(
    fake, fake_lab, quartzy_lab, use_fake, monkeypatch, owner
):
    old = [fake.add_request(fake_lab, requested_at=NOW - timedelta(days=30)) for _ in range(3)]
    today = [fake.add_request(fake_lab, requested_at=NOW - timedelta(hours=3)) for _ in range(3)]
    with time_machine.travel(NOW - timedelta(hours=1)):
        sync.backfill_requests(fake.client(), quartzy_lab, SyncRun.objects.create(kind="backfill"))
    # A complete sweep pass has started since, so every request is a deletion suspect too.
    QuartzyLab.objects.filter(pk=quartzy_lab.pk).update(
        full_sweep_started_at=NOW - timedelta(minutes=30)
    )
    refuse_by_id(fake, monkeypatch, today)  # the three hot ones, next to each other
    results = []
    for cycle in range(4):
        with time_machine.travel(NOW + timedelta(minutes=15 * cycle)):
            results.append(tasks.run_hot(quartzy_lab.pk))
    assert results == ["ok"] * 4
    errors = SyncError.objects.all()
    assert {(error.kind, error.quartzy_id) for error in errors} == {
        ("read", request["id"]) for request in today
    }
    # No email about a refused token: the token works.
    assert not Notification.objects.filter(kind=Notification.Kind.SYSTEM).exists()
    # The unlisted requests were read by id after the hot set, in the first run already.
    rows = OrderRequest.objects.filter(quartzy_id__in=[request["id"] for request in old])
    assert all(row.by_id_checked_at < NOW + timedelta(minutes=15) for row in rows)
    assert all(row.last_seen_at >= NOW for row in rows)


def test_a_refusal_is_an_answer_so_it_ends_a_run_of_failures(
    fake, quartzy_lab, api, mirrored, monkeypatch
):
    requests = mirrored(6)
    fake.fail_next(500, times=4 * 2, path="/order-requests/{id}")  # the first two, retries too
    refuse_by_id(fake, monkeypatch, requests[2:3])  # then Quartzy answers, if only with a 403
    run = hot(quartzy_lab)
    sync.refresh_hot_set(api, quartzy_lab, run, [r["id"] for r in requests])
    assert run.status == SyncRun.Status.RUNNING and run.errors == 3
    assert OrderRequest.objects.filter(by_id_checked_at__isnull=False).count() == 6
