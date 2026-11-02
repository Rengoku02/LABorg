"""Mirroring single records: change detection, status observations, people, money, catalog
resolution, types and labs. All against FakeQuartzy."""

import json
import logging
import threading
from datetime import datetime
from decimal import Decimal

import pytest
from django.db import connection
from django.utils import timezone

from apps.catalog.models import Product, SupplierProduct
from apps.core.models import Lab, PersonAlias
from apps.quartzy import sync
from apps.quartzy.models import (
    OrderRequest,
    OrderStatusObservation,
    QuartzyLab,
    QuartzyType,
    SyncError,
    SyncRun,
)
from tests.quartzy.fake_quartzy import PAST_END

pytestmark = pytest.mark.django_db


def upsert(api, qlab, run, request, via="poll"):
    """Read ``request`` through the API (so the fake's null_* switches apply) and mirror it."""
    return sync.upsert_order_request(qlab, api.order_request(request["id"]), via=via, run=run)


def observations(obj):
    return list(obj.status_observations.values_list("from_status", "to_status", "via"))


def test_a_new_request_is_mirrored_with_its_first_observation(
    fake, fake_lab, quartzy_lab, api, run, member
):
    priya = fake.add_user("Priya", "Patel", "Priya@Lab.test")
    request = fake.add_request(
        fake_lab,
        item_name="GAPDH (14C10) Rabbit mAb",
        vendor_name="Cell Signaling Technology",
        catalog_number="2118S",
        unit_price="350.00",
        quantity="2",
        is_urgent=True,
        spend_tracking_code={"name": "R01-GM12345"},
        requested_by=priya,
    )
    obj, created, changed = upsert(api, quartzy_lab, run, request)
    assert (created, changed) == (True, False)
    obj.refresh_from_db()
    assert (obj.lab, obj.quartzy_lab, obj.source) == (quartzy_lab.lab, quartzy_lab, "quartzy")
    assert (obj.status, obj.item_name, obj.catalog_number_raw) == (
        "CREATED",
        "GAPDH (14C10) Rabbit mAb",
        "2118S",
    )
    assert (obj.quantity, obj.unit_price, obj.total_price) == (2, Decimal(350), Decimal(700))
    assert (obj.currency, obj.money_unit_used) == ("USD", "minor")
    assert obj.requested_by == member and obj.requested_by_alias.user == member
    assert obj.requested_at == datetime.fromisoformat(request["requested_at"])
    assert obj.is_urgent is True and obj.spend_tracking_code == "R01-GM12345"
    assert obj.app_url == request["app_url"] and obj.raw_payload == request
    assert obj.first_seen_at == obj.last_seen_at and obj.deleted_in_quartzy_at is None
    assert obj.resolution_status == "new" and obj.product == obj.supplier_product.product
    assert observations(obj) == [("", "CREATED", "poll")]
    assert (run.seen, run.new, run.changed, run.errors) == (1, 1, 0, 0)


def test_a_rerun_writes_nothing_and_a_status_change_exactly_one_observation(
    fake, fake_lab, quartzy_lab, api, run
):
    request = fake.add_request(fake_lab)
    for _ in range(3):
        upsert(api, quartzy_lab, run, request)
    obj = OrderRequest.objects.get()
    assert observations(obj) == [("", "CREATED", "poll")]
    assert (run.seen, run.new, run.changed) == (3, 1, 0)

    fake.update_request(request["id"], status="ORDERED")
    for _ in range(2):
        upsert(api, quartzy_lab, run, request)
    obj.refresh_from_db()
    assert obj.status == "ORDERED" and run.changed == 1
    assert observations(obj) == [("", "CREATED", "poll"), ("CREATED", "ORDERED", "poll")]
    latest = obj.status_observations.last()
    assert latest.quartzy_updated_at == datetime.fromisoformat(request["updated_at"])


def test_a_payload_change_is_caught_without_a_new_updated_at(fake, fake_lab, quartzy_lab, api, run):
    request = fake.add_request(fake_lab)
    upsert(api, quartzy_lab, run, request)
    stamp = request["updated_at"]
    fake.update_request(request["id"], bump_updated_at=False, notes="Please order lot 2")
    assert request["updated_at"] == stamp
    _, created, changed = upsert(api, quartzy_lab, run, request)
    obj = OrderRequest.objects.get()
    assert (created, changed, obj.notes) == (False, True, "Please order lot 2")
    assert obj.payload_sha256 == sync.payload_sha256(request)
    assert obj.status_observations.count() == 1  # same status, no new observation


def test_the_sha_ignores_key_order():
    assert sync.payload_sha256({"a": 1, "b": [1, 2]}) == sync.payload_sha256({"b": [1, 2], "a": 1})
    assert sync.payload_sha256({"a": 1}) != sync.payload_sha256({"a": 2})


def test_payloads_are_made_storable_all_the_way_down():
    payload = {"no\x00tes": ["a\x00b", ("c", "\ud800")], "n": 1, "x": None, "ok": True}
    assert sync._storable(payload) == {"notes": ["ab", ["c", "?"]], "n": 1, "x": None, "ok": True}
    plain = {"id": "r-1", "item_name": "GAPDH (14C10) Rabbit mAb", "quantity": "2"}
    assert sync._storable(plain) == plain


def test_a_bad_record_becomes_a_sync_error_and_the_page_goes_on(
    fake, fake_lab, quartzy_lab, api, run, monkeypatch, settings
):
    settings.QUARTZY_NEW_ITEMS_AT = "first_page"
    good = [fake.add_request(fake_lab) for _ in range(3)]
    unreadable = fake.add_request(fake_lab, requested_at="not a date")
    trouble = fake.add_user("Troubled", "Record")
    unsaveable = fake.add_request(fake_lab, requested_by=trouble)
    real = sync.resolve_person

    def flaky(quartzy_user):
        if quartzy_user and quartzy_user.get("id") == trouble["id"]:
            raise RuntimeError("database hiccup")
        return real(quartzy_user)

    monkeypatch.setattr(sync, "resolve_person", flaky)
    sync.head_probe(api, quartzy_lab, run)
    mirrored = set(OrderRequest.objects.values_list("quartzy_id", flat=True))
    assert mirrored == {request["id"] for request in good}
    errors = {error.quartzy_id: error for error in SyncError.objects.all()}
    assert errors.keys() == {unreadable["id"], unsaveable["id"]}
    assert errors[unreadable["id"]].kind == "parse"
    assert "requested_at" in errors[unreadable["id"]].message
    assert errors[unreadable["id"]].payload["requested_at"] == "not a date"
    assert errors[unsaveable["id"]].kind == "upsert"
    assert "database hiccup" in errors[unsaveable["id"]].message
    assert (run.seen, run.new, run.errors) == (5, 3, 2)

    # The same failure on the next cycle is counted but not stored again.
    sync.head_probe(api, quartzy_lab, run)
    assert run.errors == 4 and SyncError.objects.count() == 2


def test_nul_characters_are_removed_before_anything_is_hashed_or_stored(
    fake, fake_lab, quartzy_lab, api, run, settings
):
    settings.QUARTZY_NEW_ITEMS_AT = "first_page"
    plain = [fake.add_request(fake_lab) for _ in range(2)]
    pasted = fake.add_request(
        fake_lab,
        item_name="GAPDH (14C10)\x00 Rabbit mAb",
        notes="Lot 0012, as on the quote\x00 (pasted from a PDF)",
        requested_by=fake.add_user("Pri\x00ya", "Patel"),
        spend_tracking_code={"name": "R01\x00-GM12345"},
        qbot={"sou\x00rce": ["chat\x00"]},  # a field the spec doesn't know, NUL in its key too
    )
    plain.append(fake.add_request(fake_lab))  # newest first: the pasted one mid-page
    sync.head_probe(api, quartzy_lab, run)
    assert OrderRequest.objects.count() == 4
    assert run.errors == 0 and not SyncError.objects.exists()
    obj = OrderRequest.objects.get(quartzy_id=pasted["id"])
    assert obj.item_name == "GAPDH (14C10) Rabbit mAb"
    assert obj.notes == "Lot 0012, as on the quote (pasted from a PDF)"
    assert obj.spend_tracking_code == "R01-GM12345"
    assert obj.requested_by_alias.display_name == "Priya Patel"
    assert obj.raw_payload["qbot"] == {"source": ["chat"]}
    assert "\\u0000" not in json.dumps(obj.raw_payload)
    # The digest is of what was stored, so the next read finds nothing new.
    assert obj.payload_sha256 == sync.payload_sha256(obj.raw_payload)
    assert upsert(api, quartzy_lab, run, pasted)[1:] == (False, False)


def test_a_database_error_on_one_record_mid_page_becomes_its_sync_error(
    fake, fake_lab, quartzy_lab, api, run, settings, monkeypatch
):
    settings.QUARTZY_NEW_ITEMS_AT = "first_page"
    requests = [fake.add_request(fake_lab) for _ in range(5)]
    doomed = requests[2]
    real = sync._apply_request

    def failing(obj, qlab, model, payload):
        real(obj, qlab, model, payload)
        if model.id == doomed["id"]:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 / 0")  # Postgres fails the statement and the transaction

    monkeypatch.setattr(sync, "_apply_request", failing)
    sync.head_probe(api, quartzy_lab, run)
    mirrored = set(OrderRequest.objects.values_list("quartzy_id", flat=True))
    assert mirrored == {request["id"] for request in requests} - {doomed["id"]}
    error = SyncError.objects.get()
    assert (error.kind, error.quartzy_id) == ("upsert", doomed["id"])
    assert "division by zero" in error.message and error.payload["id"] == doomed["id"]
    assert (run.seen, run.new, run.errors) == (5, 4, 1)


def test_a_record_the_database_refuses_even_as_an_error_is_kept_without_its_payload(
    fake, fake_lab, quartzy_lab, api, run, settings, monkeypatch
):
    settings.QUARTZY_NEW_ITEMS_AT = "first_page"
    page = [api.order_request(fake.add_request(fake_lab)["id"]) for _ in range(3)]
    # Python's JSON parser takes NaN; Postgres's jsonb refuses it, in the request and in a
    # SyncError's payload alike.
    page[1]["shipping_and_handling"] = float("nan")
    monkeypatch.setattr(api, "order_requests", lambda **params: page if params["page"] == 1 else [])
    sync.head_probe(api, quartzy_lab, run)
    assert set(OrderRequest.objects.values_list("quartzy_id", flat=True)) == {
        page[0]["id"],
        page[2]["id"],
    }
    error = SyncError.objects.get()
    assert (error.kind, error.quartzy_id, error.payload) == ("upsert", page[1]["id"], None)
    assert "DataError" in error.message and run.errors == 1


def test_a_blank_id_is_refused(fake, fake_lab, quartzy_lab, api, run):
    payload = {**api.order_request(fake.add_request(fake_lab)["id"]), "id": "  "}
    assert sync.upsert_order_request(quartzy_lab, payload, via="poll", run=run) == (
        None,
        False,
        False,
    )
    assert SyncError.objects.get().message == "id: blank" and not OrderRequest.objects.exists()


def test_null_requested_by_and_updated_at_fall_back(fake, fake_lab, quartzy_lab, api, run, member):
    creator = fake.add_user("Priya", "Patel", "priya@lab.test")
    request = fake.add_request(fake_lab, requested_by=None, created_by=creator, updated_at=None)
    obj, created, _ = upsert(api, quartzy_lab, run, request)
    assert created and obj.requested_by_alias is None
    assert obj.created_by_alias.user == member and obj.requester_alias == obj.created_by_alias
    assert obj.requested_by == member  # requested_by falls back to created_by
    assert obj.quartzy_updated_at is None


def test_the_fakes_null_switches_are_handled_on_every_record(
    fake, fake_lab, quartzy_lab, api, run, settings
):
    settings.QUARTZY_NEW_ITEMS_AT = "first_page"
    fake.null_requested_by = fake.null_updated_at = True
    for _ in range(3):
        fake.add_request(fake_lab)
    sync.head_probe(api, quartzy_lab, run)
    assert run.errors == 0 and OrderRequest.objects.count() == 3
    for obj in OrderRequest.objects.all():
        assert obj.requested_by_alias is None and obj.quartzy_updated_at is None
        assert obj.created_by_alias.value == fake.me["id"].lower()


def test_null_emails_leave_people_for_the_manual_queue_until_resolved(
    fake, fake_lab, quartzy_lab, api, run, member
):
    fake.null_emails = True
    request = fake.add_request(fake_lab, requested_by=fake.add_user("Priya", "Patel"))
    obj, *_ = upsert(api, quartzy_lab, run, request)
    assert obj.requested_by is None and obj.requested_by_alias.user is None
    assert obj.requested_by_alias.display_name == "Priya Patel"
    assert not PersonAlias.objects.filter(kind="email").exists()

    # Someone matches the alias in the admin; the next sighting picks it up although the
    # payload itself is unchanged.
    alias = obj.requested_by_alias
    alias.user = member
    alias.save()
    _, created, changed = upsert(api, quartzy_lab, run, request)
    obj.refresh_from_db()
    assert (created, changed, obj.requested_by) == (False, False, member)


@pytest.mark.parametrize(
    ("served", "pinned", "total", "used"),
    [
        ("minor", "minor", Decimal("25"), "minor"),
        ("minor", "auto", Decimal("25"), "minor"),
        ("major", "major", Decimal("25"), "major"),
        ("major", "auto", Decimal("25"), "major"),
        ("minor", "major", Decimal("2500"), "major"),  # pinned wrongly: taken at its word
    ],
)
def test_money_units(fake, fake_lab, quartzy_lab, api, run, settings, served, pinned, total, used):
    fake.money_unit = served
    settings.QUARTZY_MONEY_UNIT = pinned
    request = fake.add_request(fake_lab, unit_price="12.50", quantity="2")
    obj, *_ = upsert(api, quartzy_lab, run, request)
    assert (obj.total_price, obj.money_unit_used) == (total, used)
    assert (obj.unit_price, obj.quantity, obj.currency) == (Decimal("12.5"), 2, "USD")


def test_auto_money_unit_refuses_to_guess(fake, fake_lab, quartzy_lab, api, run, settings):
    settings.QUARTZY_MONEY_UNIT = "auto"
    request = fake.add_request(
        fake_lab, unit_price=None, total_price={"amount": "2500", "currency": "USD"}
    )
    obj, *_ = upsert(api, quartzy_lab, run, request)
    assert (obj.total_price, obj.currency, obj.money_unit_used) == (None, "USD", "unknown")


def test_absurd_numbers_are_dropped_not_fatal(fake, fake_lab, quartzy_lab, api, run):
    request = fake.add_request(fake_lab, unit_price="99999999999", quantity="lots")
    obj, created, _ = upsert(api, quartzy_lab, run, request)
    assert created and (obj.unit_price, obj.quantity) == (None, None)


def test_the_catalog_is_resolved_once_per_request_line(
    fake, fake_lab, quartzy_lab, api, run, monkeypatch
):
    calls = []
    real = sync.resolve_supplier_product

    def counting(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(sync, "resolve_supplier_product", counting)
    request = fake.add_request(
        fake_lab, vendor_name="New England Biolabs", catalog_number="R0101S", item_name="EcoRI"
    )
    upsert(api, quartzy_lab, run, request)
    upsert(api, quartzy_lab, run, request)
    fake.update_request(request["id"], status="ORDERED", notes="arrives Tuesday")
    upsert(api, quartzy_lab, run, request)
    assert len(calls) == 1
    assert calls[0]["vendor_name"] == "New England Biolabs" and calls[0]["unit_price"] == 10

    fake.update_request(request["id"], catalog_number="R0101L")  # the large pack instead
    upsert(api, quartzy_lab, run, request)
    assert len(calls) == 2
    obj = OrderRequest.objects.get()
    assert obj.resolution_status == "base_variant"
    assert obj.supplier_product.catalog_raw == "R0101L"


def test_a_failed_resolution_keeps_the_request_and_is_retried_on_change(
    fake, fake_lab, quartzy_lab, api, run, monkeypatch
):
    real = sync.resolve_supplier_product

    def broken(**kwargs):
        raise RuntimeError("catalog is down")

    monkeypatch.setattr(sync, "resolve_supplier_product", broken)
    request = fake.add_request(fake_lab)
    obj, created, _ = upsert(api, quartzy_lab, run, request)
    assert created and obj.resolution_status == "unresolved" and obj.product is None
    [error] = SyncError.objects.all()
    assert error.kind == "resolve" and "catalog is down" in error.message

    monkeypatch.setattr(sync, "resolve_supplier_product", real)
    fake.update_request(request["id"], notes="any change")
    upsert(api, quartzy_lab, run, request)
    obj.refresh_from_db()
    assert obj.resolution_status == "new" and obj.product is not None


def test_a_new_type_gets_a_suggested_item_type_that_shapes_resolution(
    fake, fake_lab, quartzy_lab, api, run, oligo_type
):
    request = fake.add_request(
        fake_lab, qtype="Oligos & Primers", vendor_name="IDT", catalog_number="51-01-03-01"
    )
    obj, *_ = upsert(api, quartzy_lab, run, request)
    qtype = obj.quartzy_type
    assert (qtype.name, qtype.item_type, qtype.suggested) == ("Oligos & Primers", oligo_type, True)
    assert qtype.quartzy_lab == quartzy_lab
    # Custom oligos identify nothing, so the catalog # never groups them.
    assert obj.resolution_status == "generic"
    assert obj.supplier_product.identity == SupplierProduct.Identity.GENERIC


def test_undocumented_fields_are_kept_and_logged_once(
    fake, fake_lab, quartzy_lab, api, run, caplog, monkeypatch
):
    monkeypatch.setattr(sync, "_warned", set())
    requests = [fake.add_request(fake_lab, qbot_source="chat") for _ in range(2)]
    with caplog.at_level(logging.WARNING, logger="apps.quartzy.sync"):
        for request in requests:
            upsert(api, quartzy_lab, run, request)
    drift = [message for message in caplog.messages if "qbot_source" in message]
    assert drift == ["Quartzy sent an undocumented field: OrderRequest.qbot_source"]
    assert OrderRequest.objects.first().raw_payload["qbot_source"] == "chat"


def test_an_unknown_status_is_kept_as_sent_and_counts_as_open(
    fake, fake_lab, quartzy_lab, api, run
):
    request = fake.add_request(fake_lab, status="PARTIALLY_RECEIVED")
    obj, *_ = upsert(api, quartzy_lab, run, request)
    assert obj.status == "PARTIALLY_RECEIVED" and obj.is_open
    assert OrderRequest.objects.open().count() == 1


def test_sync_labs_maps_new_labs_inactive_and_keeps_existing_mappings(fake, lab, api):
    fake.page_size = 1
    first = fake.add_lab("Smith Lab", uses_instances=None)
    second = fake.add_lab("Smith Lab teaching")
    other = Lab.objects.create(name="Jones Lab", slug="jones")
    QuartzyLab.objects.create(lab=other, quartzy_id=second["id"], name="old name")

    found = sync.sync_labs(api, lab)
    assert {qlab.quartzy_id for qlab in found} == {first["id"], second["id"]}
    one = QuartzyLab.objects.get(quartzy_id=first["id"])
    # The token sees more than one lab, so nobody has said this one is ours to mirror.
    assert (one.lab, one.name, one.is_active) == (lab, "Smith Lab", False)
    assert one.uses_instances is None and one.instances_enabled
    assert one.organization_name == "Example University"
    two = QuartzyLab.objects.get(quartzy_id=second["id"])
    assert (two.lab, two.name, two.is_active) == (other, "Smith Lab teaching", True)
    run = SyncRun.objects.get(kind="labs")
    assert (run.status, run.pages, run.calls, run.new, run.changed) == ("ok", 3, 3, 1, 1)


def test_the_only_lab_a_new_install_sees_is_mirrored(fake, fake_lab, lab, api):
    [qlab] = sync.sync_labs(api, lab)
    assert (qlab.quartzy_id, qlab.lab, qlab.is_active) == (fake_lab["id"], lab, True)
    # Later the token also sees a collaborator's lab: it is found, but not mirrored.
    collaborator = fake.add_lab("Jones Lab (collaborator)")
    sync.sync_labs(api, lab)
    assert QuartzyLab.objects.get(quartzy_id=collaborator["id"]).is_active is False
    assert QuartzyLab.objects.get(quartzy_id=fake_lab["id"]).is_active is True


def test_a_lab_found_after_the_first_is_not_mirrored_even_alone(fake, lab, api, quartzy_lab):
    del fake.labs[quartzy_lab.quartzy_id]  # the token now sees only another lab
    newcomer = fake.add_lab("Somebody else's lab")
    sync.sync_labs(api, lab)
    assert QuartzyLab.objects.get(quartzy_id=newcomer["id"]).is_active is False


def test_quartzy_lab_ids_says_which_new_labs_to_mirror(fake, lab, api, settings):
    ours, teaching, theirs = (fake.add_lab(name) for name in ("Smith", "Teaching", "Jones"))
    settings.QUARTZY_LAB_IDS = [ours["id"], teaching["id"]]
    sync.sync_labs(api, lab)
    active = dict(QuartzyLab.objects.values_list("quartzy_id", "is_active"))
    assert active == {ours["id"]: True, teaching["id"]: True, theirs["id"]: False}


def test_quartzy_lab_ids_also_rules_a_lone_lab(fake, fake_lab, lab, api, settings):
    settings.QUARTZY_LAB_IDS = ["some-other-lab"]
    [qlab] = sync.sync_labs(api, lab)
    assert qlab.is_active is False


@pytest.mark.parametrize("past_end", PAST_END)
def test_labs_and_types_are_read_to_the_end_of_their_lists(fake, fake_lab, lab, api, past_end):
    fake.page_size, fake.past_end = 1, past_end
    fake.add_type(fake_lab, "Antibodies")
    fake.add_type(fake_lab, "Primers")
    [qlab] = sync.sync_labs(api, lab)
    run = sync.sync_types(api, qlab)
    assert run.status == "ok" and run.errors == 0
    assert set(qlab.types.values_list("name", flat=True)) == {"Antibodies", "Primers"}


def test_sync_types_suggests_item_types_but_leaves_curated_ones(
    fake, fake_lab, quartzy_lab, api, antibody_type, oligo_type
):
    fake.add_type(fake_lab, "Antibodies")
    curated = fake.add_type(fake_lab, "Primers")
    fake.add_type(fake.add_lab("Elsewhere"), "Antibodies")  # another lab's type
    QuartzyType.objects.create(
        quartzy_lab=quartzy_lab,
        quartzy_id=curated["id"],
        name="Primers",
        item_type=None,
        reviewed_at=timezone.now(),
    )
    run = sync.sync_types(api, quartzy_lab)
    types = {qtype.name: qtype for qtype in QuartzyType.objects.all()}
    assert types.keys() == {"Antibodies", "Primers"}
    assert (types["Antibodies"].item_type, types["Antibodies"].suggested) == (antibody_type, True)
    assert types["Primers"].item_type is None  # the co-owner's choice stands
    assert (run.status, run.new, run.seen) == ("ok", 1, 2)


@pytest.mark.django_db(transaction=True)
def test_concurrent_runs_mirror_a_request_once(fake, fake_lab, quartzy_lab, api):
    warm_up = fake.add_request(fake_lab)  # vendor and people rows exist; the race is the request
    sync.upsert_order_request(
        quartzy_lab,
        api.order_request(warm_up["id"]),
        via="poll",
        run=SyncRun.objects.create(kind="head"),
    )
    payload = api.order_request(fake.add_request(fake_lab)["id"])
    barrier = threading.Barrier(3)
    created, errors = [], []

    def worker():
        try:
            run = SyncRun.objects.create(kind="sweep", quartzy_lab=quartzy_lab)
            barrier.wait(timeout=10)
            created.append(sync.upsert_order_request(quartzy_lab, payload, via="poll", run=run)[1])
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors and sorted(created) == [False, False, True]
    obj = OrderRequest.objects.get(quartzy_id=payload["id"])
    assert OrderStatusObservation.objects.filter(order_request=obj).count() == 1
    assert not SyncError.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_a_run_that_waited_for_another_sees_the_rows_that_one_made(
    fake, fake_lab, quartzy_lab, api, oligo_type, monkeypatch, lock_waited
):
    request = fake.add_request(
        fake_lab, qtype="Oligos & Primers", vendor_name="IDT", catalog_number="51-01-01-01"
    )
    head = SyncRun.objects.create(kind="head", quartzy_lab=quartzy_lab)
    obj, *_ = sync.upsert_order_request(
        quartzy_lab, api.order_request(request["id"]), via="poll", run=head
    )
    assert obj.resolution_status == "generic"
    # In Quartzy the line moves to a new type, which maps onto oligo too, and is then ordered.
    # Two runs read it, one after the other, and apply what they read at once.
    custom = fake.add_type(fake_lab, "Oligos (custom)")
    fake.update_request(request["id"], type={"id": custom["id"], "name": custom["name"]})
    moved = api.order_request(request["id"])
    fake.update_request(request["id"], status="ORDERED")
    ordered = api.order_request(request["id"])
    saved, release, errors = threading.Event(), threading.Event(), []
    real = sync._upsert_request

    def first_holds_its_lock(*args):
        result = real(*args)
        if threading.current_thread().name == "first":
            saved.set()  # on the new type, which this transaction created
            release.wait(timeout=10)
        return result

    monkeypatch.setattr(sync, "_upsert_request", first_holds_its_lock)

    def mirror(payload):
        try:
            run = SyncRun.objects.create(kind="sweep", quartzy_lab=quartzy_lab)
            sync.upsert_order_request(quartzy_lab, payload, via="poll", run=run)
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)
        finally:
            connection.close()

    first = threading.Thread(target=mirror, args=(moved,), name="first")
    second = threading.Thread(target=mirror, args=(ordered,), name="second")
    first.start()
    try:
        assert saved.wait(timeout=10)
        second.start()
        assert lock_waited()  # the second waits for the request's row lock
    finally:
        release.set()
        for thread in (first, second):
            if thread.ident is not None:
                thread.join()
    assert not errors and not SyncError.objects.exists()
    after = OrderRequest.objects.select_related("quartzy_type").get()
    assert (after.status, after.quartzy_type.name) == ("ORDERED", "Oligos (custom)")
    # Its vendor, catalog # and item type stayed the same, so it kept its product.
    assert after.product_id == obj.product_id and Product.objects.count() == 1


def test_a_run_that_started_before_a_re_map_follows_it(fake, fake_lab, lab, quartzy_lab):
    fake.page_size = 5
    requests = [fake.add_request(fake_lab) for _ in range(15)]
    client = fake.client()
    sync.backfill_requests(client, quartzy_lab, SyncRun.objects.create(kind="backfill"))
    fake.update_request(requests[3]["id"], status="ORDERED")  # on the last page
    jones = Lab.objects.create(name="Jones Lab", slug="jones")

    def re_map_in_the_admin():
        qlab = QuartzyLab.objects.get(pk=quartzy_lab.pk)
        qlab.lab = jones
        qlab.save()  # moves the lab's requests to jones

    fake.after_page(re_map_in_the_admin, page=1)
    # A backfill (or a sweep) that started before the re-map, with the QuartzyLab of before.
    sync.backfill_requests(client, quartzy_lab, SyncRun.objects.create(kind="backfill"))
    assert set(OrderRequest.objects.values_list("lab", flat=True)) == {jones.pk}
    assert OrderRequest.objects.for_lab(jones).count() == 15
    assert OrderRequest.objects.get(quartzy_id=requests[3]["id"]).status == "ORDERED"


def test_every_read_puts_a_request_in_its_quartzy_labs_lab(fake, fake_lab, quartzy_lab, api, run):
    request = fake.add_request(fake_lab)
    upsert(api, quartzy_lab, run, request)
    jones = Lab.objects.create(name="Jones Lab", slug="jones")
    # Re-mapped without QuartzyLab.save(), e.g. from a shell, or seen by a run that started
    # before the change: the next read of each request still puts it in the new lab.
    QuartzyLab.objects.filter(pk=quartzy_lab.pk).update(lab=jones)
    quartzy_lab.refresh_from_db()
    upsert(api, quartzy_lab, run, request)  # unchanged
    assert OrderRequest.objects.get(quartzy_id=request["id"]).lab == jones
    fake.update_request(request["id"], status="ORDERED")  # changed
    newer = fake.add_request(fake_lab)
    upsert(api, quartzy_lab, run, request)
    upsert(api, quartzy_lab, run, newer)
    assert set(OrderRequest.objects.values_list("lab", flat=True)) == {jones.pk}
