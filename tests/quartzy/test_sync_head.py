"""The head probe, the rolling sweep and the backfill: new requests are found within one cycle
whichever end Quartzy inserts them at, with page base 0 or 1, even when one is inserted
mid-scan, whatever the page after the last one returns; and a payload read before another
run wrote the row never overwrites it."""

import pytest

from apps.core.policies import set_policy
from apps.quartzy import sync
from apps.quartzy.client import QuartzyError
from apps.quartzy.models import OrderRequest, QuartzyLab, SyncRun
from tests.quartzy.fake_quartzy import PAST_END, FakeQuartzy

pytestmark = pytest.mark.django_db

# (Quartzy's order, what QUARTZY_NEW_ITEMS_AT says): the pinned value, and "unknown" before
# the probe has pinned anything.
CASES = [
    ("newest_first", "first_page"),
    ("newest_first", "unknown"),
    ("oldest_first", "last_page"),
    ("oldest_first", "unknown"),
]


@pytest.fixture
def mirrored_lab(lab, settings):
    """A fake with ``existing`` requests on 5-request pages, all mirrored already."""

    def _make(order="newest_first", mode="first_page", base=1, existing=12, past_end="empty"):
        fake = FakeQuartzy(order=order, page_base=base, page_size=5, past_end=past_end)
        fake_lab = fake.add_lab("Smith Lab")
        qlab = QuartzyLab.objects.create(lab=lab, quartzy_id=fake_lab["id"], name="Smith Lab")
        settings.QUARTZY_PAGE_BASE = base
        settings.QUARTZY_NEW_ITEMS_AT = mode
        for _ in range(existing):
            fake.add_request(fake_lab)
        client = fake.client()
        sync.backfill_requests(client, qlab, SyncRun.objects.create(kind="backfill"))
        fake.reset_calls()
        return fake, fake_lab, qlab, client

    return _make


def probe(client, qlab):
    run = SyncRun.objects.create(kind=SyncRun.Kind.HEAD, quartzy_lab=qlab)
    sync.head_probe(client, qlab, run)
    qlab.refresh_from_db()
    return run


def known(*requests):
    ids = [request["id"] for request in requests]
    return OrderRequest.objects.filter(quartzy_id__in=ids).count() == len(ids)


@pytest.mark.parametrize("past_end", PAST_END)
@pytest.mark.parametrize("base", [0, 1])
@pytest.mark.parametrize(("order", "mode"), CASES)
def test_new_requests_are_found_in_one_cycle_even_one_inserted_mid_scan(
    mirrored_lab, order, mode, base, past_end
):
    fake, fake_lab, qlab, client = mirrored_lab(order, mode, base, past_end=past_end)
    new = [fake.add_request(fake_lab) for _ in range(3)]
    late = []
    # Right after the first page this probe reads, someone submits another request.
    fake.after_page(lambda: late.append(fake.add_request(fake_lab)))
    run = probe(client, qlab)
    assert len(late) == 1
    assert known(*new, *late)
    assert (run.status, run.new, run.errors) == ("running", 4, 0)
    assert OrderRequest.objects.count() == 16
    assert fake.calls["/order-requests"] <= 2 * 5  # head_max_pages per end


@pytest.mark.parametrize("past_end", PAST_END)
@pytest.mark.parametrize("base", [0, 1])
@pytest.mark.parametrize(("order", "mode"), CASES)
def test_a_quiet_cycle_is_cheap(mirrored_lab, order, mode, base, past_end):
    fake, _fake_lab, qlab, client = mirrored_lab(order, mode, base, past_end=past_end)
    run = probe(client, qlab)
    assert run.new == 0 and run.status == "running"
    # first_page: just the first page; last_page: last-1, last and the page after it, which
    # is empty, refused, or a copy of the last one.
    expected = {"first_page": 1, "last_page": 3, "unknown": 4}[mode]
    assert fake.calls["/order-requests"] == expected
    assert qlab.head_cursor == {"last_page": base + 2}


def test_a_request_added_after_the_scan_is_found_next_cycle(mirrored_lab):
    fake, fake_lab, qlab, client = mirrored_lab("newest_first", "first_page")
    late = []
    fake.after_page(lambda: late.append(fake.add_request(fake_lab)))
    probe(client, qlab)  # the first page held nothing new, so the scan stopped there
    assert not known(*late)
    probe(client, qlab)
    assert known(*late)


def test_the_first_page_scan_stops_at_head_max_pages(mirrored_lab):
    fake, fake_lab, qlab, client = mirrored_lab("newest_first", "first_page", existing=0)
    for _ in range(30):
        fake.add_request(fake_lab)
    set_policy("sync.head_max_pages", 2)
    run = probe(client, qlab)
    assert fake.calls["/order-requests"] == 2 and run.new == 10
    # The rest is history for the sweep and the backfill; the next probe finds nothing new.
    run = probe(client, qlab)
    assert run.new == 0


@pytest.mark.parametrize("past_end", PAST_END)
def test_last_page_mode_finds_where_the_list_ends_and_remembers_it(lab, settings, past_end):
    fake = FakeQuartzy(order="oldest_first", page_size=5, past_end=past_end)
    fake_lab = fake.add_lab()
    qlab = QuartzyLab.objects.create(lab=lab, quartzy_id=fake_lab["id"])
    settings.QUARTZY_NEW_ITEMS_AT = "last_page"
    for _ in range(23):  # pages 1-5, the last one holding 3
        fake.add_request(fake_lab)
    client = fake.client()
    probe(client, qlab)
    assert qlab.head_cursor == {"last_page": 5}
    assert OrderRequest.objects.count() == 23  # the search read pages 1, 2, 3, 5 and the tail 4
    newest = fake.add_request(fake_lab)
    fake.reset_calls()
    probe(client, qlab)
    assert known(newest) and fake.calls["/order-requests"] == 3  # pages 4, 5 and the end, 6


@pytest.mark.parametrize("past_end", PAST_END)
def test_last_page_mode_recovers_when_the_list_got_shorter(mirrored_lab, past_end):
    fake, fake_lab, qlab, client = mirrored_lab(
        "oldest_first", "last_page", existing=12, past_end=past_end
    )
    assert qlab.head_cursor == {"last_page": 3}
    QuartzyLab.objects.filter(pk=qlab.pk).update(head_cursor={"last_page": 9})
    qlab.refresh_from_db()
    newest = fake.add_request(fake_lab)
    probe(client, qlab)  # page 8 is past the end: search for the end again
    assert qlab.head_cursor == {"last_page": 3}
    probe(client, qlab)
    assert known(newest)


@pytest.mark.parametrize("past_end", PAST_END)
def test_the_backfill_reads_every_page_whatever_follows_the_last(lab, past_end):
    fake = FakeQuartzy(page_size=5, past_end=past_end)
    fake_lab = fake.add_lab()
    qlab = QuartzyLab.objects.create(lab=lab, quartzy_id=fake_lab["id"])
    requests = [fake.add_request(fake_lab) for _ in range(12)]
    run = SyncRun.objects.create(kind=SyncRun.Kind.BACKFILL, quartzy_lab=qlab)
    sync.backfill_requests(fake.client(), qlab, run)
    qlab.refresh_from_db()
    assert known(*requests) and (run.new, run.errors) == (12, 0)
    assert qlab.head_cursor == {"last_page": 3}
    assert fake.calls["/order-requests"] == 4  # pages 1-3 and the end


@pytest.mark.parametrize("base", [0, 1])
def test_the_sweep_reads_a_few_pages_per_cycle_and_wraps(lab, settings, base):
    fake = FakeQuartzy(page_size=5, page_base=base)
    fake_lab = fake.add_lab()
    qlab = QuartzyLab.objects.create(lab=lab, quartzy_id=fake_lab["id"])
    settings.QUARTZY_PAGE_BASE = base
    set_policy("sync.sweep_pages_per_cycle", 2)
    requests = [fake.add_request(fake_lab) for _ in range(8)]  # pages base and base+1
    client = fake.client()

    def sweep():
        fake.reset_calls()
        run = SyncRun.objects.create(kind=SyncRun.Kind.SWEEP, quartzy_lab=qlab)
        sync.rolling_sweep(client, qlab, run)
        qlab.refresh_from_db()
        return [params.get("page") for _method, _path, params in fake.log]

    assert qlab.sweep_cursor == 0  # below a base of 1: start at the base
    assert sweep() == [str(base), str(base + 1)]
    assert qlab.sweep_cursor == base + 2 and known(*requests)
    assert qlab.full_sweep_started_at is None  # the pass hasn't reached the end yet
    first_pass = qlab.sweep_pass_started_at

    assert sweep() == [str(base + 2), str(base)]  # the empty page ends the pass: wrap
    assert qlab.sweep_cursor == base + 1
    assert qlab.full_sweep_started_at == first_pass
    assert qlab.sweep_pass_started_at > first_pass


@pytest.mark.parametrize("past_end", PAST_END)
def test_the_sweep_wraps_whatever_follows_the_last_page(mirrored_lab, past_end):
    _fake, _fake_lab, qlab, client = mirrored_lab(existing=12, past_end=past_end)  # pages 1-3
    set_policy("sync.sweep_pages_per_cycle", 2)
    for _cycle in range(2):  # pages 1 and 2; then 3 and the end of the list, so wrap
        run = SyncRun.objects.create(kind=SyncRun.Kind.SWEEP, quartzy_lab=qlab)
        sync.rolling_sweep(client, qlab, run)
        qlab.refresh_from_db()
    assert run.status == "running" and qlab.sweep_cursor == 1
    assert qlab.full_sweep_started_at is not None  # a complete pass


def test_the_sweep_never_reads_a_page_twice_in_one_cycle(fake, fake_lab, quartzy_lab, api):
    for _ in range(3):
        fake.add_request(fake_lab)
    set_policy("sync.sweep_pages_per_cycle", 4)
    run = SyncRun.objects.create(kind=SyncRun.Kind.SWEEP, quartzy_lab=quartzy_lab)
    sync.rolling_sweep(api, quartzy_lab, run)
    assert fake.calls["/order-requests"] == 2 and run.pages == 2  # page 1, then empty page 2
    assert quartzy_lab.sweep_cursor == 1


def test_the_sweep_sees_edits_to_old_requests(mirrored_lab):
    fake, _fake_lab, qlab, client = mirrored_lab("newest_first", "first_page", existing=12)
    oldest = next(iter(fake.order_requests.values()))  # on the last page
    fake.update_request(oldest["id"], status="RECEIVED")
    set_policy("sync.sweep_pages_per_cycle", 3)
    run = SyncRun.objects.create(kind=SyncRun.Kind.SWEEP, quartzy_lab=qlab)
    sync.rolling_sweep(client, qlab, run)
    obj = OrderRequest.objects.get(quartzy_id=oldest["id"])
    assert obj.status == "RECEIVED" and run.changed == 1
    assert obj.status_observations.last().from_status == "CREATED"


def test_the_sweep_skips_a_page_quartzy_cannot_serve(mirrored_lab):
    fake, _fake_lab, qlab, client = mirrored_lab("newest_first", "first_page", existing=12)
    set_policy("sync.sweep_pages_per_cycle", 3)
    fake.fail_next(500, times=4, path="/order-requests")  # every retry of page 1
    run = SyncRun.objects.create(kind=SyncRun.Kind.SWEEP, quartzy_lab=qlab)
    sync.rolling_sweep(client, qlab, run)
    qlab.refresh_from_db()
    assert qlab.sweep_cursor == 4 and run.errors == 1 and "Skipped page 1" in run.note
    assert qlab.sweep_pass_started_at is None  # this pass missed a page: not a complete one

    run = SyncRun.objects.create(kind=SyncRun.Kind.SWEEP, quartzy_lab=qlab)
    sync.rolling_sweep(client, qlab, run)  # page 4 is empty: wrap, but no complete pass
    qlab.refresh_from_db()
    assert qlab.full_sweep_started_at is None and qlab.sweep_cursor == 3


def test_the_sweep_fails_when_quartzy_serves_nothing(mirrored_lab):
    fake, _fake_lab, qlab, client = mirrored_lab("newest_first", "first_page", existing=12)
    fake.fail_next(503, times=100, path="/order-requests")
    run = SyncRun.objects.create(kind=SyncRun.Kind.SWEEP, quartzy_lab=qlab)
    with pytest.raises(QuartzyError):
        sync.rolling_sweep(client, qlab, run)
    qlab.refresh_from_db()
    assert qlab.sweep_cursor == 0  # not moved on past pages nobody read


# ---------------------------------------------------------------------------------------
# An empty listing of a lab that has requests is not a healthy sync


def lose_the_listing(fake):
    """Quartzy lists nothing for the lab any more (its id no longer matches after a migration,
    or the token's user lost sight of it), while reads by id still answer."""
    for request in fake.order_requests.values():
        request["lab"] = {"id": "somewhere-else", "name": "Elsewhere"}


@pytest.mark.parametrize("mode", ["first_page", "last_page", "unknown"])
def test_an_empty_listing_of_a_mirrored_lab_aborts_the_head_run(mirrored_lab, mode):
    order = "oldest_first" if mode == "last_page" else "newest_first"
    fake, _fake_lab, qlab, client = mirrored_lab(order, mode, existing=12)
    lose_the_listing(fake)
    run = probe(client, qlab)
    assert run.status == SyncRun.Status.ABORTED
    assert "listed no requests" in run.note and "12" in run.note
    assert not OrderRequest.objects.filter(deleted_in_quartzy_at__isnull=False).exists()


def test_an_empty_listing_of_a_mirrored_lab_aborts_the_sweep_without_a_complete_pass(
    mirrored_lab,
):
    fake, _fake_lab, qlab, client = mirrored_lab(existing=12)
    set_policy("sync.sweep_pages_per_cycle", 3)
    lose_the_listing(fake)
    for _cycle in range(2):
        run = SyncRun.objects.create(kind=SyncRun.Kind.SWEEP, quartzy_lab=qlab)
        sync.rolling_sweep(client, qlab, run)
        qlab.refresh_from_db()
        assert run.status == SyncRun.Status.ABORTED and "listed no requests" in run.note
    # No complete pass, so no request is a deletion suspect for the hot set.
    assert qlab.full_sweep_started_at is None and qlab.sweep_cursor == 0
    hot = SyncRun.objects.create(kind=SyncRun.Kind.HOT, quartzy_lab=qlab)
    assert sync.confirm_unlisted_requests(client, qlab, hot) == []
    assert fake.calls["/order-requests/{id}"] == 0


def test_an_empty_lab_is_still_a_quiet_cycle(lab, settings):
    fake = FakeQuartzy()
    fake_lab = fake.add_lab()
    qlab = QuartzyLab.objects.create(lab=lab, quartzy_id=fake_lab["id"])
    settings.QUARTZY_NEW_ITEMS_AT = "first_page"
    client = fake.client()
    assert probe(client, qlab).status == "running"
    run = SyncRun.objects.create(kind=SyncRun.Kind.SWEEP, quartzy_lab=qlab)
    sync.rolling_sweep(client, qlab, run)
    assert run.status == "running"


# ---------------------------------------------------------------------------------------
# Stale reads: what a run read before another run wrote the row is not applied over it


def observations(request):
    obj = OrderRequest.objects.get(quartzy_id=request["id"])
    return list(obj.status_observations.values_list("from_status", "to_status"))


def meanwhile(fake, qlab, request, status):
    """An action for fake.after_page: someone changes ``request`` in Quartzy, and a head run
    mirrors that before the run that read the page gets to apply it."""

    def action():
        fake.update_request(request["id"], status=status)
        probe(fake.client(), QuartzyLab.objects.get(pk=qlab.pk))

    return action


def test_the_backfill_never_writes_back_a_status_a_later_run_moved_on(mirrored_lab):
    fake, _fake_lab, qlab, client = mirrored_lab("newest_first", "first_page", existing=12)
    newest = list(fake.order_requests.values())[-1]  # on page 1
    fake.update_request(newest["id"], status="ORDERED")
    probe(client, qlab)
    # The backfill reads page 1; before it applies it, the member marks the request Received
    # and the head run mirrors that.
    fake.after_page(meanwhile(fake, qlab, newest, "RECEIVED"), page=1)
    run = SyncRun.objects.create(kind=SyncRun.Kind.BACKFILL, quartzy_lab=qlab)
    sync.backfill_requests(client, qlab, run)
    assert OrderRequest.objects.get(quartzy_id=newest["id"]).status == "RECEIVED"
    assert observations(newest) == [
        ("", "CREATED"),
        ("CREATED", "ORDERED"),
        ("ORDERED", "RECEIVED"),
    ]
    assert f"{sync.STALE_NOTE}: 1" in run.note and run.changed == 0


def test_an_overlapping_sweep_never_writes_back_what_a_head_run_read_after_it(mirrored_lab):
    fake, _fake_lab, qlab, client = mirrored_lab("newest_first", "first_page", existing=12)
    newest = list(fake.order_requests.values())[-1]  # on page 1, which the sweep reads first
    fake.after_page(meanwhile(fake, qlab, newest, "CANCELLED"), page=1)
    run = SyncRun.objects.create(kind=SyncRun.Kind.SWEEP, quartzy_lab=qlab)
    sync.rolling_sweep(client, qlab, run)
    assert OrderRequest.objects.get(quartzy_id=newest["id"]).status == "CANCELLED"
    assert observations(newest) == [("", "CREATED"), ("CREATED", "CANCELLED")]
    assert f"{sync.STALE_NOTE}: 1" in run.note
    # The next read does apply, as it is newer than the head run's.
    fake.update_request(newest["id"], status="CREATED")
    probe(client, qlab)
    assert observations(newest)[-1] == ("CANCELLED", "CREATED")


def test_a_stale_listing_does_not_undo_a_deletion_found_after_it(mirrored_lab):
    fake, _fake_lab, qlab, client = mirrored_lab("newest_first", "first_page", existing=12)
    newest = list(fake.order_requests.values())[-1]
    payload = client.order_request(newest["id"])  # what a list page showed a moment ago
    stamp = OrderRequest.objects.get(quartzy_id=newest["id"]).last_seen_at
    fake.delete_request(newest["id"])
    hot = SyncRun.objects.create(kind=SyncRun.Kind.HOT, quartzy_lab=qlab)
    sync.refresh_hot_set(client, qlab, hot, [newest["id"]])
    run = SyncRun.objects.create(kind=SyncRun.Kind.SWEEP, quartzy_lab=qlab)
    sync.upsert_order_request(qlab, payload, via="poll", run=run, fetched_at=stamp)
    assert OrderRequest.objects.get(quartzy_id=newest["id"]).deleted_in_quartzy_at is not None
