"""Resolving mirrored requests against the catalog again: when a Quartzy type's mapping
changes, when a resolution failed, and when a request line is edited in Quartzy."""

import json
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import StringIO

import pytest
import time_machine
from django.core.management import call_command
from django.db import OperationalError, connection
from django.urls import reverse
from django.utils import timezone

from apps.catalog.models import ItemType, Product, SupplierProduct
from apps.quartzy import sync, tasks
from apps.quartzy.models import OrderRequest, QuartzyType, SyncError, SyncRun

pytestmark = pytest.mark.django_db

NOW = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)


def upsert(api, qlab, run, request):
    return sync.upsert_order_request(qlab, api.order_request(request["id"]), via="poll", run=run)


def statuses():
    return sorted(OrderRequest.objects.values_list("resolution_status", flat=True))


def products():
    """How many products the requests point at."""
    return OrderRequest.objects.values("product").distinct().count()


def review_queue():
    """What the catalog review queue lists: live provisional products."""
    return Product.objects.canonical().filter(provisional=True)


@pytest.fixture
def sequencing(fake, fake_lab, lab, api, item_types):
    """Three people request Illumina's NextSeq P2 reagents ($2,950) under the Quartzy type
    "Sequencing Reagents", which the backfill suggests as a service: identity rule none."""
    fake.add_type(fake_lab, "Sequencing Reagents")
    for n in range(3):
        fake.add_request(
            fake_lab,
            qtype="Sequencing Reagents",
            vendor_name="Illumina",
            catalog_number="20024907",
            item_name="NextSeq 1000/2000 P2 Reagents (300 Cycles)",
            unit_price="2950.00",
            requested_by=fake.add_user(f"P{n}", "Person"),
        )
    sync.full_backfill(api, lab)
    qtype = QuartzyType.objects.get()
    assert (qtype.item_type.key, qtype.suggested, qtype.reviewed_at) == ("service_fee", True, None)
    assert statuses() == ["generic"] * 3 and products() == 3
    return qtype


@pytest.fixture
def custom_oligos(fake, fake_lab, quartzy_lab, api, run, item_types):
    """Five people order different custom oligos on IDT's shared catalog # 51-01-01-01, under
    a Quartzy type no item type matches, so the catalog # groups them as one product."""
    fake.add_type(fake_lab, "IDT orders")
    for n in range(5):
        request = fake.add_request(
            fake_lab,
            qtype="IDT orders",
            vendor_name="IDT",
            catalog_number="51-01-01-01",
            item_name=f"primer {n} fwd",
            requested_by=fake.add_user(f"P{n}", "Person"),
        )
        upsert(api, quartzy_lab, run, request)
    qtype = QuartzyType.objects.get()
    assert qtype.item_type is None
    assert statuses() == ["exact"] * 4 + ["new"] and products() == 1
    return qtype


# ---------------------------------------------------------------------------------------
# A type's mapping changes


def test_mapping_a_type_to_an_identifying_item_type_groups_its_requests(
    sequencing, lab, api, django_capture_on_commit_callbacks
):
    sequencing.item_type = ItemType.objects.get(key="kit")
    with django_capture_on_commit_callbacks(execute=True):
        sequencing.save()
    assert statuses() == ["exact", "exact", "new"] and products() == 1
    product = OrderRequest.objects.first().product
    assert product.item_type.key == "kit"
    # The three generic products the suggestion made redirect to the one product now, so the
    # review queue lists that one only; their listings are gone.
    assert list(review_queue()) == [product]
    assert Product.objects.filter(merged_into=product).count() == 3
    assert SupplierProduct.objects.get().catalog_key == "20024907"
    # "Bought before" can see them now, and a later backfill keeps it that way.
    sync.full_backfill(api, lab)
    assert statuses() == ["exact", "exact", "new"] and products() == 1


def test_mapping_a_type_to_identity_none_splits_its_grouped_requests(
    custom_oligos, monkeypatch, django_capture_on_commit_callbacks
):
    monkeypatch.setattr(tasks, "RESOLVE_PER_TASK", 2)
    oligo = ItemType.objects.get(key="oligo")
    with django_capture_on_commit_callbacks(execute=True):
        QuartzyType.objects.filter(pk=custom_oligos.pk).update(item_type=oligo)
    assert statuses() == ["generic"] * 5 and products() == 5
    # The product that grouped them is deleted with its listing: it was provisional and
    # nothing uses it any more.
    assert review_queue().count() == Product.objects.count() == 5
    assert SupplierProduct.objects.count() == 5
    assert {sp.identity for sp in SupplierProduct.objects.all()} == {"generic"}
    # Two requests per task, each task enqueuing the next: three runs.
    runs = SyncRun.objects.filter(kind=SyncRun.Kind.RESOLVE)
    assert [(run.status, run.seen) for run in runs.order_by("pk")] == [
        ("ok", 2),
        ("ok", 2),
        ("ok", 1),
    ]
    assert {run.quartzy_lab for run in runs} == {custom_oligos.quartzy_lab}


def test_mapping_a_type_back_leaves_the_review_queue_only_its_requests_products(
    sequencing, django_capture_on_commit_callbacks
):
    # Mapped to kit, the three requests group on one product, and their generic ones redirect
    # to it. Mapped back, they split onto generic products of their own again: the product
    # they were grouped on goes, and the redirects to it with it. Twice, so nothing piles up.
    for key in ("kit", "service_fee", "kit", "service_fee"):
        sequencing.item_type = ItemType.objects.get(key=key)
        with django_capture_on_commit_callbacks(execute=True):
            sequencing.save()
        if key == "kit":
            continue
        assert statuses() == ["generic"] * 3 and products() == 3
        used = set(OrderRequest.objects.values_list("product", flat=True))
        assert set(review_queue().values_list("pk", flat=True)) == used
        assert set(Product.objects.values_list("pk", flat=True)) == used
        assert SupplierProduct.objects.filter(order_requests__isnull=True).count() == 0


def test_mapping_an_unmapped_type_keeps_its_groups_and_types_their_products(
    custom_oligos, django_capture_on_commit_callbacks
):
    product = OrderRequest.objects.first().product
    custom_oligos.item_type = ItemType.objects.get(key="kit")
    with django_capture_on_commit_callbacks(execute=True):
        custom_oligos.save()
    # Still one group, and the request that made the product is still the "new" one.
    assert statuses() == ["exact"] * 4 + ["new"]
    assert set(OrderRequest.objects.values_list("product", flat=True)) == {product.pk}
    product.refresh_from_db()
    assert product.item_type.key == "kit"


def test_only_a_changed_mapping_re_resolves(custom_oligos, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks() as callbacks:
        custom_oligos.name = "IDT orders (all)"
        custom_oligos.save()
        QuartzyType.objects.filter(pk=custom_oligos.pk).update(suggested=False)
        QuartzyType.objects.filter(pk=custom_oligos.pk).update(item_type=None)
    assert callbacks == []


def test_a_re_resolution_that_changes_nothing_keeps_what_the_request_has(
    sequencing, django_capture_on_commit_callbacks
):
    before = list(OrderRequest.objects.order_by("pk").values_list("product", "resolution_status"))
    history = (Product.history.count(), SupplierProduct.history.count())
    # Another identity-none type: still one generic product per request, the same ones.
    sequencing.item_type = ItemType.objects.get(key="oligo")
    with django_capture_on_commit_callbacks(execute=True):
        sequencing.save()
    after = list(OrderRequest.objects.order_by("pk").values_list("product", "resolution_status"))
    assert after == before
    assert (Product.history.count(), SupplierProduct.history.count()) == history


def test_a_failure_while_resolving_again_keeps_what_the_request_had(
    custom_oligos, monkeypatch, django_capture_on_commit_callbacks
):
    def down(**kwargs):
        raise OperationalError("deadlock detected")

    monkeypatch.setattr(sync, "resolve_supplier_product", down)
    before = list(OrderRequest.objects.order_by("pk").values_list("product", "resolution_status"))
    oligo = ItemType.objects.get(key="oligo")
    with django_capture_on_commit_callbacks(execute=True):
        QuartzyType.objects.filter(pk=custom_oligos.pk).update(item_type=oligo)
    after = list(OrderRequest.objects.order_by("pk").values_list("product", "resolution_status"))
    assert after == before  # not unresolved: still checkable until someone retries
    run = SyncRun.objects.get(kind=SyncRun.Kind.RESOLVE)
    assert (run.status, run.seen, run.changed, run.errors) == ("ok", 5, 0, 5)
    assert SyncError.objects.filter(kind="resolve", sync_run=run).count() == 5


def test_saving_a_type_in_the_admin_reviews_it_and_re_resolves_its_requests(
    sequencing, admin_user_client, django_capture_on_commit_callbacks
):
    kit = ItemType.objects.get(key="kit")
    url = f"/admin/quartzy/quartzytype/{sequencing.pk}/change/"
    with django_capture_on_commit_callbacks(execute=True):
        response = admin_user_client.post(url, {"item_type": kit.pk}, follow=True)
    assert response.status_code == 200
    assert "resolved again" in response.content.decode()
    sequencing.refresh_from_db()
    assert (sequencing.item_type, sequencing.suggested) == (kit, False)
    assert timezone.now() - sequencing.reviewed_at < timedelta(minutes=1)
    assert products() == 1


def test_editing_a_type_in_the_list_reviews_it_and_re_resolves_its_requests(
    custom_oligos, admin_user_client, django_capture_on_commit_callbacks
):
    oligo = ItemType.objects.get(key="oligo")
    data = {
        "form-TOTAL_FORMS": "1",
        "form-INITIAL_FORMS": "1",
        "form-MIN_NUM_FORMS": "0",
        "form-MAX_NUM_FORMS": "1000",
        "form-0-id": str(custom_oligos.pk),
        "form-0-item_type": str(oligo.pk),
        "_save": "Save",
    }
    with django_capture_on_commit_callbacks(execute=True):
        response = admin_user_client.post("/admin/quartzy/quartzytype/", data)
    assert response.status_code == 302
    custom_oligos.refresh_from_db()
    assert (custom_oligos.item_type, custom_oligos.suggested) == (oligo, False)
    assert custom_oligos.reviewed_at is not None
    assert statuses() == ["generic"] * 5 and products() == 5


def test_confirming_suggestions_reviews_them_and_changes_no_resolution(
    sequencing, admin_user_client, django_capture_on_commit_callbacks
):
    unmapped = QuartzyType.objects.create(
        quartzy_lab=sequencing.quartzy_lab, quartzy_id="t-unmapped", name="General"
    )
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        admin_user_client.post(
            "/admin/quartzy/quartzytype/",
            {"action": "confirm_suggestions", "_selected_action": [sequencing.pk, unmapped.pk]},
        )
    assert callbacks == []
    sequencing.refresh_from_db()
    unmapped.refresh_from_db()
    assert (sequencing.item_type.key, sequencing.suggested) == ("service_fee", False)
    assert sequencing.reviewed_at is not None
    assert unmapped.reviewed_at is None  # it had no suggestion to confirm
    assert statuses() == ["generic"] * 3


def test_after_seeding_unmapped_types_get_a_suggestion_and_their_requests_follow(
    fake, fake_lab, quartzy_lab, api, run, django_capture_on_commit_callbacks
):
    # A deploy where nobody ran seed_catalog yet: the sync met the types without item types.
    for n in range(5):
        request = fake.add_request(
            fake_lab,
            qtype="Oligos & Primers",
            vendor_name="IDT",
            catalog_number="51-01-01-01",
            item_name=f"primer {n} fwd",
        )
        upsert(api, quartzy_lab, run, request)
    fake.add_type(fake_lab, "Primers (lab stock)")
    sync.sync_types(api, quartzy_lab)
    # Someone reviews that one and decides it maps to nothing.
    QuartzyType.objects.filter(name="Primers (lab stock)").update(reviewed_at=timezone.now())
    assert QuartzyType.objects.filter(item_type__isnull=False).count() == 0
    assert products() == 1

    ItemType.objects.create(
        key="oligo",
        label="Oligo",
        tier=3,
        identity_rule=ItemType.IdentityRule.NONE,
        quartzy_type_keywords=["oligo", "primer"],
    )
    with django_capture_on_commit_callbacks(execute=True):
        types_run = sync.sync_types(api, quartzy_lab)
    oligos = QuartzyType.objects.get(name="Oligos & Primers")
    assert (oligos.item_type.key, oligos.suggested, oligos.reviewed_at) == ("oligo", True, None)
    assert QuartzyType.objects.get(name="Primers (lab stock)").item_type is None
    assert types_run.changed == 1
    assert statuses() == ["generic"] * 5 and products() == 5


@pytest.fixture
def three_prices(fake, fake_lab, lab, api, item_types):
    """Three people order CST 2118S at $10, $20 and $30, oldest to newest, under the type
    "Antibodies". The backfill reads the newest first, so the newest gets the lowest pk."""
    fake.add_type(fake_lab, "Antibodies")
    for n, price in enumerate(("10.00", "20.00", "30.00")):
        fake.add_request(
            fake_lab,
            qtype="Antibodies",
            vendor_name="Cell Signaling",
            catalog_number="2118S",
            item_name="GAPDH (14C10) Rabbit mAb",
            unit_price=price,
            requested_by=fake.add_user(f"P{n}", "Person"),
        )
    sync.full_backfill(api, lab)
    listing = SupplierProduct.objects.get()
    assert listing.last_unit_price == Decimal("30")
    return listing


def test_a_mapping_change_leaves_the_newest_price_on_the_listing(
    three_prices, django_capture_on_commit_callbacks
):
    qtype = QuartzyType.objects.get()
    qtype.item_type = ItemType.objects.get(key="kit")
    with django_capture_on_commit_callbacks(execute=True):
        qtype.save()
    assert SyncRun.objects.filter(kind=SyncRun.Kind.RESOLVE).exists()
    three_prices.refresh_from_db()
    assert three_prices.last_unit_price == Decimal("30")


def test_quartzy_resolve_leaves_the_newest_price_on_the_listing(three_prices):
    call_command("quartzy_resolve", "--all", lab="test-lab", stdout=StringIO())
    three_prices.refresh_from_db()
    assert three_prices.last_unit_price == Decimal("30")


def item_type_form(item_type, **changes):
    """What the catalog admin's change form for ``item_type`` posts, with ``changes``."""
    data = {
        "key": item_type.key,
        "label": item_type.label,
        "tier": item_type.tier,
        "lookback_months": item_type.lookback_months,
        "identity_rule": item_type.identity_rule,
        "tracking_mode": item_type.tracking_mode,
        "quartzy_type_keywords": ", ".join(item_type.quartzy_type_keywords),
        "product_schema": json.dumps(item_type.product_schema),
        "instance_schema": json.dumps(item_type.instance_schema),
        "sort_order": item_type.sort_order,
        "_save": "Save",
    }
    data.update({flag: "on" for flag in ("durable", "in_scope") if getattr(item_type, flag)})
    return {**data, **changes}


def test_changing_an_item_types_identity_rule_resolves_its_types_requests_again(
    sequencing, admin_user_client, django_capture_on_commit_callbacks
):
    # The co-owner decides that a sequencing kit's catalog # does identify it after all.
    service = sequencing.item_type
    url = reverse("admin:catalog_itemtype_change", args=[service.pk])
    with django_capture_on_commit_callbacks(execute=True):
        response = admin_user_client.post(url, item_type_form(service, identity_rule="catalog"))
    assert response.status_code == 302, response.content[:2000]
    service.refresh_from_db()
    assert service.identity_rule == "catalog"
    assert statuses() == ["exact", "exact", "new"] and products() == 1
    run = SyncRun.objects.get(kind=SyncRun.Kind.RESOLVE)
    assert (run.quartzy_lab, run.seen, run.changed) == (sequencing.quartzy_lab, 3, 3)


def test_a_seed_that_changes_an_identity_rule_resolves_its_types_requests_again(
    sequencing, django_capture_on_commit_callbacks
):
    # Say the last deploy's item_types.yaml had sequencing kits identified by catalog #, and
    # the requests were grouped that way. Neither change sends a signal.
    service = sequencing.item_type
    ItemType.objects.filter(pk=service.pk).update(
        identity_rule="catalog", seeded={**service.seeded, "identity_rule": "catalog"}
    )
    call_command("quartzy_resolve", "--all", lab="test-lab", stdout=StringIO())
    assert statuses() == ["exact", "exact", "new"] and products() == 1
    # This deploy's file says "none" again; the web container re-seeds when it starts.
    with django_capture_on_commit_callbacks(execute=True):
        call_command("seed_catalog", stdout=StringIO())
    service.refresh_from_db()
    assert service.identity_rule == "none"
    assert statuses() == ["generic"] * 3 and products() == 3


def test_other_changes_to_an_item_type_resolve_nothing_again(
    sequencing, admin_user_client, django_capture_on_commit_callbacks
):
    service = sequencing.item_type
    url = reverse("admin:catalog_itemtype_change", args=[service.pk])
    with django_capture_on_commit_callbacks() as callbacks:
        admin_user_client.post(url, item_type_form(service, label="Services", lookback_months=6))
        service.refresh_from_db()
        assert (service.label, service.lookback_months) == ("Services", 6)
        service.save(update_fields=["identity_rule"])  # saved, but not changed
        ItemType.objects.create(key="new_kind", label="New kind", tier=5, identity_rule="none")
    assert callbacks == []


def test_a_request_moved_to_another_type_is_resolved_again(
    fake, fake_lab, quartzy_lab, api, run, item_types
):
    request = fake.add_request(
        fake_lab, qtype="Kits", vendor_name="IDT", catalog_number="51-01-01-01", item_name="x"
    )
    obj, *_ = upsert(api, quartzy_lab, run, request)
    assert obj.resolution_status == "new"
    old = obj.supplier_product
    oligos = fake.add_type(fake_lab, "Oligos")
    fake.update_request(request["id"], type={"id": oligos["id"], "name": oligos["name"]})
    obj, *_ = upsert(api, quartzy_lab, run, request)
    assert (obj.quartzy_type.name, obj.resolution_status) == ("Oligos", "generic")
    assert not SupplierProduct.objects.filter(pk=old.pk).exists()  # retired


# ---------------------------------------------------------------------------------------
# A failed resolution


def test_a_failed_resolution_is_tried_again_a_day_later_without_a_change(
    fake, fake_lab, quartzy_lab, api, run, monkeypatch
):
    real = sync.resolve_supplier_product
    calls = []

    def flaky(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise OperationalError("deadlock detected")
        return real(**kwargs)

    monkeypatch.setattr(sync, "resolve_supplier_product", flaky)
    request = fake.add_request(fake_lab, status="RECEIVED")  # history: never edited again
    with time_machine.travel(NOW, tick=False):
        obj, created, _ = upsert(api, quartzy_lab, run, request)
        assert created and obj.resolution_status == "unresolved" and obj.product is None
        assert obj.resolution_attempted_at == NOW
    with time_machine.travel(NOW + timedelta(hours=12), tick=False):
        upsert(api, quartzy_lab, run, request)  # unchanged, and tried less than a day ago
    assert len(calls) == 1

    later = NOW + timedelta(days=1, minutes=2)
    with time_machine.travel(later, tick=False):
        _, created, changed = upsert(api, quartzy_lab, run, request)
    assert (created, changed) == (False, False)
    obj.refresh_from_db()
    assert obj.resolution_status == "new" and obj.product is not None
    assert obj.resolution_attempted_at == later and len(calls) == 2
    with time_machine.travel(later + timedelta(days=2), tick=False):
        upsert(api, quartzy_lab, run, request)
    assert len(calls) == 2  # resolved: nothing to try again
    assert SyncError.objects.get().kind == "resolve"


# ---------------------------------------------------------------------------------------
# Edits in Quartzy


def test_renaming_a_line_without_a_catalog_number_renames_its_product(
    fake, fake_lab, quartzy_lab, api, run
):
    request = fake.add_request(
        fake_lab, vendor_name="Amazon", catalog_number="N/A", item_name="Sharpie"
    )
    upsert(api, quartzy_lab, run, request)
    for name in ("Sharpies", "Sharpie markers, black", "Sharpie fine point x12"):
        fake.update_request(request["id"], item_name=name)
        upsert(api, quartzy_lab, run, request)
    product = Product.objects.get()
    assert (product.name, product.provisional) == ("Sharpie fine point x12", True)
    obj = OrderRequest.objects.get()
    assert (obj.product, obj.resolution_status) == (product, "no_catalog")
    assert SupplierProduct.objects.count() == 1


def test_a_rename_leaves_a_reviewed_or_shared_product_alone(
    fake, fake_lab, quartzy_lab, api, run, oligo_type
):
    reviewed, shared, other = (
        fake.add_request(fake_lab, qtype="Oligos", catalog_number="51-01-01-01", item_name=name)
        for name in ("fwd primer", "rev primer", "probe")
    )
    for request in (reviewed, shared, other):
        upsert(api, quartzy_lab, run, request)
    rows = {obj.quartzy_id: obj for obj in OrderRequest.objects.select_related("product")}
    assert {obj.resolution_status for obj in rows.values()} == {"generic"}
    Product.objects.filter(pk=rows[reviewed["id"]].product_id).update(provisional=False)
    # Someone merged two of them by hand; both requests point at the survivor now.
    OrderRequest.objects.filter(pk=rows[other["id"]].pk).update(product=rows[shared["id"]].product)
    for request in (reviewed, shared):
        fake.update_request(request["id"], item_name="renamed")
        upsert(api, quartzy_lab, run, request)
    names = set(Product.objects.values_list("name", flat=True))
    assert {"fwd primer", "rev primer"} <= names and "renamed" not in names


def test_correcting_a_catalog_number_retires_what_the_request_left(
    fake, fake_lab, quartzy_lab, api, run
):
    gfp = {"vendor_name": "Abcam", "item_name": "Anti-GFP antibody"}
    right = fake.add_request(fake_lab, catalog_number="ab290", **gfp)
    typo = fake.add_request(fake_lab, catalog_number="ab209", **gfp)
    upsert(api, quartzy_lab, run, right)
    obj, *_ = upsert(api, quartzy_lab, run, typo)
    left = obj.product
    assert obj.resolution_status == "new" and review_queue().count() == 2

    fake.update_request(typo["id"], catalog_number="ab290")
    obj, *_ = upsert(api, quartzy_lab, run, typo)
    assert obj.resolution_status == "exact" and products() == 1
    # The typo's own product redirects to the right one; its listing is gone.
    assert Product.objects.get(pk=left.pk).merged_into == obj.product
    assert list(review_queue()) == [obj.product] and SupplierProduct.objects.count() == 1


def test_an_edit_that_finds_the_same_listing_keeps_its_resolution(
    fake, fake_lab, quartzy_lab, api, run
):
    request = fake.add_request(
        fake_lab, vendor_name="New England Biolabs", catalog_number="R0101S", item_name="EcoRI"
    )
    obj, *_ = upsert(api, quartzy_lab, run, request)
    listing = obj.supplier_product
    fake.update_request(request["id"], catalog_number="R0101 S")  # the same catalog #
    obj, *_ = upsert(api, quartzy_lab, run, request)
    assert (obj.supplier_product, obj.resolution_status) == (listing, "new")


# ---------------------------------------------------------------------------------------
# Re-resolutions that overlap: a second mapping change, or quartzy_resolve during a
# resolve_type chain (the worker runs two tasks at once)


def map_past_the_hook(qtype, key):
    """Change ``qtype``'s mapping without QuartzyType.save, so the test drives the runs."""
    QuartzyType._base_manager.filter(pk=qtype.pk).update(item_type=ItemType.objects.get(key=key))


def resolve_in_thread(qlab, ids, errors):
    def target():
        try:
            run = SyncRun.objects.create(kind=SyncRun.Kind.RESOLVE, quartzy_lab=qlab)
            sync.resolve_again(ids, run)
        except Exception as exc:  # pragma: no cover - reported by the test
            errors.append(exc)
        finally:
            connection.close()

    return target


@pytest.mark.django_db(transaction=True)
def test_a_re_resolution_that_waited_for_another_keeps_what_that_one_made(
    custom_oligos, monkeypatch, lock_waited
):
    map_past_the_hook(custom_oligos, "oligo")
    pk = OrderRequest.objects.filter(resolution_status="exact").order_by("pk").first().pk
    saved, release, errors = threading.Event(), threading.Event(), []
    real = sync._resolve_one_again

    def first_holds_its_lock(pk, run):
        outcome = real(pk, run)
        if threading.current_thread().name == "first":
            saved.set()  # moved to a generic listing of its own, not committed yet
            release.wait(timeout=10)
        return outcome

    monkeypatch.setattr(sync, "_resolve_one_again", first_holds_its_lock)
    qlab = custom_oligos.quartzy_lab
    first = threading.Thread(target=resolve_in_thread(qlab, [pk], errors), name="first")
    second = threading.Thread(target=resolve_in_thread(qlab, [pk], errors), name="second")
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
    assert not errors
    obj = OrderRequest.objects.select_related("supplier_product").get(pk=pk)
    assert obj.resolution_status == "generic" and obj.supplier_product is not None
    assert obj.product_id == obj.supplier_product.product_id
    # The second kept the rows the first made: none is left for the review queue alone.
    assert not review_queue().filter(order_requests__isnull=True).exists()
    assert SupplierProduct.objects.filter(order_requests__isnull=True).count() == 0


def test_a_mapping_changed_back_before_the_first_chain_retired_its_rows(sequencing, monkeypatch):
    # The co-owner maps the suggested service to kit and straight back. The second chain is
    # done, and has retired the kit product, before the first retires the generic products
    # its requests left for that kit product: they go all the same, with no error.
    ids = list(OrderRequest.objects.order_by("pk").values_list("pk", flat=True))
    qlab = sequencing.quartzy_lab
    real = sync._retire

    def second_chain_first(left, run):
        monkeypatch.setattr(sync, "_retire", real)
        map_past_the_hook(sequencing, "service_fee")
        sync.resolve_again(ids, SyncRun.objects.create(kind=SyncRun.Kind.RESOLVE, quartzy_lab=qlab))
        return real(left, run)

    map_past_the_hook(sequencing, "kit")
    monkeypatch.setattr(sync, "_retire", second_chain_first)
    sync.resolve_again(ids, SyncRun.objects.create(kind=SyncRun.Kind.RESOLVE, quartzy_lab=qlab))
    assert not SyncError.objects.exists()
    assert statuses() == ["generic"] * 3 and products() == 3
    used = set(OrderRequest.objects.values_list("product", flat=True))
    assert set(Product.objects.values_list("pk", flat=True)) == used
    assert SupplierProduct.objects.filter(order_requests__isnull=True).count() == 0


def test_a_request_left_without_its_rows_gets_them_back_when_resolved_again(sequencing):
    # What an overlapping re-resolution used to leave behind: "generic", and no rows at all.
    broken = OrderRequest.objects.order_by("pk").first()
    OrderRequest.objects.filter(pk=broken.pk).update(supplier_product=None, product=None)
    call_command("quartzy_resolve", "--all", lab="test-lab", stdout=StringIO())
    broken.refresh_from_db()
    assert broken.resolution_status == "generic" and broken.supplier_product is not None
    assert broken.product_id == broken.supplier_product.product_id
    assert products() == 3 and not OrderRequest.objects.filter(product=None).exists()


@pytest.mark.django_db(transaction=True)
def test_two_re_resolutions_of_a_type_at_once_leave_every_request_its_rows(
    fake, fake_lab, quartzy_lab, api, item_types
):
    fake.add_type(fake_lab, "IDT orders")  # no item type matches: grouped on the shared #
    for n in range(20):
        fake.add_request(
            fake_lab,
            qtype="IDT orders",
            vendor_name="IDT",
            catalog_number="51-01-01-01",
            item_name=f"primer {n} fwd",
        )
    sync.backfill_requests(api, quartzy_lab, SyncRun.objects.create(kind="backfill"))
    assert products() == 1
    map_past_the_hook(QuartzyType.objects.get(), "oligo")
    ids = list(OrderRequest.objects.order_by("pk").values_list("pk", flat=True))
    barrier, errors = threading.Barrier(2), []
    resolve = resolve_in_thread(quartzy_lab, ids, errors)

    def chain():
        barrier.wait(timeout=10)
        resolve()

    threads = [threading.Thread(target=chain) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert statuses() == ["generic"] * 20 and products() == 20
    assert not OrderRequest.objects.filter(supplier_product=None).exists()
    assert not review_queue().filter(order_requests__isnull=True).exists()
