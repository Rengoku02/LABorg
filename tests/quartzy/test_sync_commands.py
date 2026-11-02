"""manage.py quartzy_sync, quartzy_backfill, quartzy_resolve and quartzy_purge_lab against
FakeQuartzy."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import StringIO

import pytest
import time_machine
from django.apps import apps
from django.core.management import CommandError, call_command
from django.utils import timezone
from django_q.models import Schedule

from apps.catalog.models import GenericCatalogKey, Product, SupplierProduct
from apps.core.models import PersonAlias
from apps.quartzy import sync
from apps.quartzy.models import (
    OrderRequest,
    OrderStatusObservation,
    QuartzyInventoryItem,
    QuartzyLab,
    QuartzyType,
    SyncRun,
)

pytestmark = pytest.mark.django_db


def row_counts():
    """Rows in every table of our apps (the catalog and people rows the sync creates too)."""
    labels = ("quartzy", "catalog", "core")
    return {
        model._meta.label: model.objects.count()
        for model in apps.get_models()
        if model._meta.app_label in labels
    }


@pytest.fixture
def stocked(fake, fake_lab):
    fake.add_type(fake_lab, "Antibodies")
    requests = [fake.add_request(fake_lab) for _ in range(3)]
    fake.add_inventory_item(fake_lab)
    return requests


def call(*args, **options):
    out = StringIO()
    call_command(*args, stdout=out, **options)
    return out.getvalue()


def confirm_types():
    """The co-owner confirms every suggested item type in the admin."""
    QuartzyType.objects.update(suggested=False, reviewed_at=timezone.now())


def test_a_dry_run_reads_everything_and_saves_nothing(lab, stocked, use_fake):
    before = row_counts()
    out = call("quartzy_sync", lab="test-lab", dry_run=True)
    assert row_counts() == before
    assert "Labs: ok" in out and "Dry run: rolled back, nothing was saved." in out
    assert "Head probe Smith Lab: ok" in out and "3 new" in out
    assert "Inventory Smith Lab: ok" in out


def test_a_dry_run_backfill_counts_every_request(fake, fake_lab, lab, stocked, use_fake):
    fake.page_size = 2  # 3 requests: two pages and the empty third
    out = call("quartzy_sync", lab="test-lab", labs=True, backfill=True, dry_run=True)
    assert "Request history Smith Lab: ok; 3 calls, 3 pages, 3 seen, 3 new" in out
    assert not OrderRequest.objects.exists()


def test_selected_steps_run_and_are_saved(lab, stocked, use_fake):
    out = call("quartzy_sync", lab="test-lab", labs=True, head=True)
    assert OrderRequest.objects.count() == 3
    assert sorted(SyncRun.objects.values_list("kind", flat=True)) == ["head", "labs"]
    assert "Done: 2 run(s)." in out


def test_quartzy_sync_needs_a_mapped_lab_or_labs(lab, use_fake):
    with pytest.raises(CommandError, match="run with --labs"):
        call("quartzy_sync", lab="test-lab", head=True)
    with pytest.raises(CommandError, match="No lab with slug"):
        call("quartzy_sync", lab="nope")


def test_the_lab_option_can_be_left_out_with_one_lab(lab, stocked, use_fake):
    call("quartzy_sync", labs=True)
    assert QuartzyLab.objects.get().lab == lab


def test_an_aborted_step_says_why(fake, fake_lab, lab, use_fake, antibody_type):
    requests = [fake.add_request(fake_lab) for _ in range(6)]
    call("quartzy_backfill", lab="test-lab")
    now = timezone.now()
    OrderRequest.objects.update(
        hot_until=now + timedelta(days=1), last_seen_at=now - timedelta(hours=1)
    )
    for request in requests[:5]:
        fake.delete_request(request["id"])
    out = call("quartzy_sync", lab="test-lab", hot=True)
    assert "Hot set Smith Lab: aborted" in out and "5 of 6 requests read by id" in out


def test_quartzy_errors_end_the_command_cleanly(fake, lab, stocked, use_fake):
    fake.fail_next(401, path="/labs")
    with pytest.raises(CommandError, match="Quartzy: GET /labs returned 401"):
        call("quartzy_sync", lab="test-lab", labs=True)


def test_the_backfill_mirrors_history_without_arming_alerts(
    fake, fake_lab, lab, stocked, use_fake, antibody_type
):
    out = call("quartzy_backfill", lab="test-lab")
    qlab = QuartzyLab.objects.get()
    assert qlab.alerts_armed_at is None and qlab.last_synced_at is not None
    assert OrderRequest.objects.count() == 3
    assert set(OrderStatusObservation.objects.values_list("via", flat=True)) == {"backfill"}
    assert qlab.head_cursor == {"last_page": 1} and qlab.types.count() == 1
    assert qlab.inventory_items.count() == 1
    assert sorted(SyncRun.objects.values_list("kind", flat=True)) == [
        "backfill",
        "full",
        "inventory",
        "labs",
        "types",
    ]
    assert "Alerts are not armed" in out
    assert Schedule.objects.filter(name=f"quartzy.head.{qlab.pk}").exists()

    # Again, now with --arm once the co-owner confirmed the type: nothing new is written,
    # and alerts are armed from now on.
    confirm_types()
    call("quartzy_backfill", lab="test-lab", arm=True)
    qlab.refresh_from_db()
    assert OrderStatusObservation.objects.count() == 3
    assert timezone.now() - qlab.alerts_armed_at < timedelta(minutes=1)


def test_the_backfill_leaves_the_newest_price_on_the_listing(
    fake, fake_lab, lab, use_fake, antibody_type
):
    for price, size in (("10.00", "100 µL"), ("11.00", ""), ("12.50", "")):
        fake.add_request(
            fake_lab, vendor_name="Abcam", catalog_number="ab290", unit_price=price, unit_size=size
        )
    assert fake.order == "newest_first"  # so the backfill reads the newest page first
    call("quartzy_backfill", lab="test-lab")
    listing = SupplierProduct.objects.get()
    assert (listing.last_unit_price, listing.currency) == (Decimal("12.50"), "USD")
    assert listing.unit_size_raw == "100 µL"  # the newest that gave one
    assert OrderRequest.objects.filter(resolution_status="exact").count() == 2


def test_the_backfill_needs_the_item_types_first(fake, fake_lab, lab, api, use_fake):
    fake.add_request(fake_lab)
    with pytest.raises(CommandError, match=r"run manage\.py seed_catalog first"):
        call("quartzy_backfill", lab="test-lab")
    with pytest.raises(CommandError, match="seed_catalog"):
        sync.full_backfill(api, lab)
    assert fake.total_calls == 0 and not SyncRun.objects.exists()


# ---------------------------------------------------------------------------------------
# Arming alerts


T0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


def test_arming_again_keeps_the_first_arming_time(
    fake, fake_lab, lab, stocked, use_fake, antibody_type
):
    with time_machine.travel(T0):
        call("quartzy_backfill", lab="test-lab")
        confirm_types()
        call("quartzy_backfill", lab="test-lab", arm=True)
    qlab = QuartzyLab.objects.get()
    armed = qlab.alerts_armed_at
    # A request made while the worker was down, after the arming, must still alert later.
    fake.add_request(fake_lab, requested_at=T0 + timedelta(days=2))
    with time_machine.travel(T0 + timedelta(days=3)):
        out = call("quartzy_backfill", lab="test-lab", arm=True)  # the owner catches up
    qlab.refresh_from_db()
    assert qlab.alerts_armed_at == armed and "already armed since 2026-10-20" in out
    assert "--rearm" in out
    with time_machine.travel(T0 + timedelta(days=4)):
        out = call("quartzy_backfill", lab="test-lab", rearm=True)
    qlab.refresh_from_db()
    assert qlab.alerts_armed_at > T0 + timedelta(days=4) - timedelta(minutes=1)
    assert "armed from now on" in out


def test_alerts_stay_disarmed_while_a_type_with_requests_is_only_a_suggestion(
    fake, fake_lab, lab, stocked, use_fake, antibody_type
):
    fake.add_type(fake_lab, "Spare antibodies")  # suggested too, but nobody requests it
    with pytest.raises(CommandError, match="Antibodies") as caught:
        call("quartzy_backfill", lab="test-lab", arm=True)
    assert "Spare antibodies" not in str(caught.value)
    assert QuartzyLab.objects.get().alerts_armed_at is None
    assert OrderRequest.objects.count() == 3  # the backfill itself went ahead
    QuartzyType.objects.filter(name="Antibodies").update(
        suggested=False, reviewed_at=timezone.now()
    )
    call("quartzy_backfill", lab="test-lab", arm=True)
    assert QuartzyLab.objects.get().alerts_armed_at is not None


# ---------------------------------------------------------------------------------------
# Quartzy labs the token sees that are not this lab's


def add_collaborator(fake):
    """A collaborator's Quartzy lab that the token owner is also a member of."""
    theirs = fake.add_lab("Jones Lab (collaborator)")
    stranger = fake.add_user("Zed", "Outsider", "zed@elsewhere.edu")
    fake.add_request(theirs, item_name="theirs", requested_by=stranger, notes="private note")
    fake.add_inventory_item(theirs)
    return theirs


def test_the_backfill_leaves_a_collaborators_lab_alone_and_says_so(
    fake, fake_lab, lab, stocked, use_fake, settings, antibody_type
):
    theirs = add_collaborator(fake)
    settings.QUARTZY_LAB_IDS = [fake_lab["id"]]
    out = call("quartzy_backfill", lab="test-lab")
    assert set(OrderRequest.objects.values_list("quartzy_lab__quartzy_id", flat=True)) == {
        fake_lab["id"]
    }
    assert not QuartzyInventoryItem.objects.filter(quartzy_lab__quartzy_id=theirs["id"]).exists()
    assert "Jones Lab (collaborator)" in out and theirs["id"] in out
    assert "inactive" in out and "Is active" in out


def test_quartzy_sync_says_which_labs_it_left_inactive(fake, fake_lab, lab, stocked, use_fake):
    add_collaborator(fake)
    # Two labs and no QUARTZY_LAB_IDS: neither is mirrored until someone says which.
    with pytest.raises(CommandError, match=r"Jones Lab \(collaborator\)") as caught:
        call("quartzy_sync", lab="test-lab")
    assert "Smith Lab" in str(caught.value) and not OrderRequest.objects.exists()
    QuartzyLab.objects.filter(quartzy_id=fake_lab["id"]).update(is_active=True)
    out = call("quartzy_sync", lab="test-lab", dry_run=True)
    assert "Head probe Smith Lab: ok" in out and "Jones Lab (collaborator)" in out


@pytest.fixture
def collaborator(fake, fake_lab, lab, use_fake, antibody_type, settings, member):
    """Ours and a collaborator's Quartzy lab, both mirrored by mistake; the collaborator's is
    then switched off in the admin."""
    theirs = add_collaborator(fake)
    settings.QUARTZY_LAB_IDS = [fake_lab["id"], theirs["id"]]
    both = fake.add_user("Sam", "Both", "sam@lab.test")  # orders in both labs
    priya = fake.add_user("Priya", "Patel", "priya@lab.test")  # our member, who has a login
    fake.add_request(fake_lab, item_name="ours", requested_by=both)
    fake.add_request(theirs, item_name="theirs too", requested_by=both, created_by=priya)
    call("quartzy_backfill", lab="test-lab")
    assert OrderRequest.objects.for_lab(lab).count() == 3
    QuartzyLab.objects.filter(quartzy_id=theirs["id"]).update(is_active=False)
    return theirs


def aliases():
    return set(PersonAlias.objects.values_list("value", flat=True))


def test_purging_a_lab_is_a_dry_run_unless_committed(collaborator):
    before = row_counts()
    out = call("quartzy_purge_lab", collaborator["id"])
    assert row_counts() == before
    assert "Jones Lab (collaborator)" in out and "2 order request(s)" in out
    assert "Dry run" in out and "--commit" in out


def test_purging_a_lab_removes_what_was_mirrored_from_it(fake, collaborator, member):
    stranger = next(u for u in fake.users.values() if u["first_name"] == "Zed")
    assert {"zed@elsewhere.edu", stranger["id"].lower()} <= aliases()
    out = call("quartzy_purge_lab", collaborator["id"], "--commit")
    assert not QuartzyLab.objects.filter(quartzy_id=collaborator["id"]).exists()
    assert list(OrderRequest.objects.values_list("item_name", flat=True)) == ["ours"]
    assert not QuartzyInventoryItem.objects.exists()  # only theirs had any
    assert not SyncRun.objects.filter(quartzy_lab__quartzy_id=collaborator["id"]).exists()
    assert not QuartzyType.objects.filter(quartzy_lab__quartzy_id=collaborator["id"]).exists()
    # The stranger is forgotten; Sam still orders here, and Priya has a login.
    left = aliases()
    assert "zed@elsewhere.edu" not in left and stranger["id"].lower() not in left
    assert {"sam@lab.test", "priya@lab.test"} <= left
    # The provisional products only their requests made are retired with them.
    assert not Product.objects.filter(name__in=["theirs", "theirs too"]).exists()
    assert "Purged" in out


def test_purging_a_lab_takes_the_products_its_requests_had_before_too(fake, collaborator, api):
    # Their request's catalog # was corrected in Quartzy, so the product it had redirects to
    # the one it has now. Both go: either one would keep their line in the catalog.
    obj = OrderRequest.objects.get(item_name="theirs")
    first = obj.product
    fake.update_request(obj.quartzy_id, catalog_number="JL-0001")
    run = SyncRun.objects.create(kind=SyncRun.Kind.HEAD, quartzy_lab=obj.quartzy_lab)
    obj, *_ = sync.upsert_order_request(
        obj.quartzy_lab, api.order_request(obj.quartzy_id), via="poll", run=run
    )
    assert Product.objects.get(pk=first.pk).merged_into == obj.product
    out = call("quartzy_purge_lab", collaborator["id"], "--commit")
    assert not Product.objects.filter(pk__in=[first.pk, obj.product_id]).exists()
    assert not Product.objects.filter(name__in=["theirs", "theirs too"]).exists()
    assert "3 catalog product(s) nothing else uses" in out


def test_an_active_or_unknown_lab_is_not_purged(fake_lab, lab, quartzy_lab):
    with pytest.raises(CommandError, match="is active"):
        call("quartzy_purge_lab", quartzy_lab.quartzy_id, "--commit")
    with pytest.raises(CommandError, match="No Quartzy lab"):
        call("quartzy_purge_lab", "nope")
    assert QuartzyLab.objects.filter(pk=quartzy_lab.pk).exists()


# ---------------------------------------------------------------------------------------
# manage.py quartzy_resolve


def mirror(api, qlab, run, request):
    return sync.upsert_order_request(qlab, api.order_request(request["id"]), via="poll", run=run)


def statuses(requests=None):
    requests = OrderRequest.objects.all() if requests is None else requests
    return sorted(requests.values_list("resolution_status", flat=True))


@pytest.fixture
def grouped_oligos(fake, fake_lab, quartzy_lab, api, run):
    """Five custom oligos on IDT's shared catalog # 51-01-01-01, grouped as one product."""
    for n in range(5):
        request = fake.add_request(
            fake_lab, vendor_name="IDT", catalog_number="51-01-01-01", item_name=f"primer {n}"
        )
        mirror(api, quartzy_lab, run, request)
    assert statuses() == ["exact"] * 4 + ["new"]
    return SupplierProduct.objects.get()


def mark_generic(listing):
    """The catalog fix: IDT's custom-oligo number is listed as one that identifies nothing."""
    GenericCatalogKey.objects.create(vendor_family=listing.vendor_family, catalog_key="51-01-01-01")


def test_quartzy_resolve_fixes_the_unresolved_requests_in_batches(
    fake, fake_lab, quartzy_lab, api, run, monkeypatch
):
    real = sync.resolve_supplier_product

    def down(**kwargs):
        raise RuntimeError("catalog is down")

    monkeypatch.setattr(sync, "resolve_supplier_product", down)
    for _ in range(3):
        mirror(api, quartzy_lab, run, fake.add_request(fake_lab, status="RECEIVED"))
    monkeypatch.setattr(sync, "resolve_supplier_product", real)
    resolved = mirror(api, quartzy_lab, run, fake.add_request(fake_lab))[0]
    stamp = resolved.resolution_attempted_at
    assert statuses() == ["new"] + ["unresolved"] * 3
    monkeypatch.setattr("apps.quartzy.management.commands.quartzy_resolve.BATCH", 2)

    out = call("quartzy_resolve", "--unresolved", lab="test-lab")
    assert statuses() == ["new"] * 4
    assert "Smith Lab: 2 of 3 requests, 2 changed" in out
    assert "Smith Lab: 3 of 3 requests, 3 changed" in out
    resolve_run = SyncRun.objects.get(kind=SyncRun.Kind.RESOLVE)
    assert (resolve_run.status, resolve_run.seen, resolve_run.changed) == ("ok", 3, 3)
    assert OrderRequest.objects.get(pk=resolved.pk).resolution_attempted_at == stamp  # left alone


def test_quartzy_resolve_all_applies_a_catalog_fix_and_retires_what_is_left(grouped_oligos, lab):
    mark_generic(grouped_oligos)
    out = call("quartzy_resolve", "--all", lab="test-lab")
    assert statuses() == ["generic"] * 5
    assert OrderRequest.objects.values("product").distinct().count() == 5
    # The listing and product that grouped them are gone: provisional, and unused now.
    assert not SupplierProduct.objects.filter(pk=grouped_oligos.pk).exists()
    assert not Product.objects.filter(pk=grouped_oligos.product_id).exists()
    assert "5 of 5 requests, 5 changed" in out
    assert "Retired 1 listing(s) nothing uses; products: 0 merged, 1 deleted." in out


def test_a_quartzy_resolve_dry_run_changes_nothing(grouped_oligos, lab):
    mark_generic(grouped_oligos)
    before = row_counts()
    out = call("quartzy_resolve", "--all", "--dry-run", lab="test-lab")
    assert row_counts() == before and statuses() == ["exact"] * 4 + ["new"]
    assert "5 of 5 requests, 5 changed" in out
    assert "Dry run: rolled back, nothing was saved." in out


def test_quartzy_resolve_can_take_one_type(fake, fake_lab, quartzy_lab, api, run, grouped_oligos):
    probe = fake.add_request(
        fake_lab, qtype="Custom DNA", vendor_name="IDT", catalog_number="51-01-01-01"
    )
    mirror(api, quartzy_lab, run, probe)
    mark_generic(grouped_oligos)
    custom = QuartzyType.objects.get(name="Custom DNA")
    call("quartzy_resolve", "--type", custom.quartzy_id, lab="test-lab")
    assert statuses(OrderRequest.objects.filter(quartzy_type=custom)) == ["generic"]
    assert statuses(OrderRequest.objects.exclude(quartzy_type=custom)) == ["exact"] * 4 + ["new"]
    assert SupplierProduct.objects.filter(pk=grouped_oligos.pk).exists()  # still in use

    with pytest.raises(CommandError, match="No Quartzy type 'nope'"):
        call("quartzy_resolve", "--type", "nope", lab="test-lab")
    with pytest.raises(CommandError, match="one of the arguments"):
        call("quartzy_resolve", lab="test-lab")
