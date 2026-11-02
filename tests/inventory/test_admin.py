"""The inventory admin: no editable stock state, no back doors around the ledger, and
locations added and moved through the services."""

from html.parser import HTMLParser

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.catalog.models import Product
from apps.inventory import services
from apps.inventory.models import (
    STATE_FIELDS,
    HistoryClaim,
    Location,
    LocationEvent,
    LocationType,
    StockEvent,
    StockItem,
)
from apps.quartzy.models import OrderRequest


class FormData(HTMLParser):
    """What a browser would submit for the forms on an admin page."""

    def __init__(self, html):
        super().__init__()
        self.data, self._select, self._textarea = {}, None, None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        name = attrs.get("name")
        if tag == "input" and name:
            kind = attrs.get("type", "text")
            if kind in ("checkbox", "radio"):
                if "checked" in attrs:
                    self.data[name] = attrs.get("value", "on")
            elif kind not in ("submit", "button", "file", "image"):
                self.data[name] = attrs.get("value", "")
        elif tag == "select" and name:
            self._select = name
            self.data.setdefault(name, "")
        elif tag == "option" and self._select and "selected" in attrs:
            self.data[self._select] = attrs.get("value", "")
        elif tag == "textarea" and name:
            self._textarea = name
            self.data[name] = ""

    def handle_endtag(self, tag):
        if tag == "select":
            self._select = None
        elif tag == "textarea":
            self._textarea = None

    def handle_data(self, data):
        if self._textarea:
            self.data[self._textarea] += (
                data.removeprefix("\n") if not self.data[self._textarea] else data
            )


def change_url(obj):
    return reverse(f"admin:inventory_{obj._meta.model_name}_change", args=[obj.pk])


def errors(response):
    """The form's error messages, as plain text."""
    form = response.context["adminform"].form
    return " ".join(message for messages in form.errors.values() for message in messages)


@pytest.mark.django_db
def test_the_stock_item_change_form_has_no_editable_state_fields(superuser_client, item):
    response = superuser_client.get(change_url(item))
    assert response.status_code == 200
    form = response.context["adminform"].form
    assert not set(form.fields) & {*STATE_FIELDS, "version", "lab", "short_id", "mta"}
    assert set(form.fields) == {
        "product",
        "supplier_product",
        "lot",
        "expiry_date",
        "expiry_precision",
        "source",
        "source_order_request",
        "attributes",
        "notes",
    }
    shown_read_only = set(response.context["adminform"].readonly_fields)
    assert set(STATE_FIELDS) <= shown_read_only


@pytest.mark.django_db
def test_posting_state_fields_changes_nothing(superuser_client, item, raj, make_location, freezer):
    other_box = make_location("Box 5", "box", freezer)
    url = change_url(item)
    data = FormData(superuser_client.get(url).content.decode()).data
    data.update(
        notes="checked in the admin",
        custodian=raj.pk,
        location=other_box.pk,
        lifecycle="lost",
        visibility="shared",
        version=7,
    )
    response = superuser_client.post(url, data)
    assert response.status_code == 302, response.context and response.context["adminform"].errors
    fresh = StockItem.objects.get(pk=item.pk)
    assert fresh.notes == "checked in the admin"
    assert (fresh.custodian, fresh.location, fresh.lifecycle, fresh.visibility, fresh.version) == (
        item.custodian,
        item.location,
        "available",
        item.visibility,
        1,
    )
    assert services.ledger_drift(fresh) == {}


@pytest.mark.django_db
def test_no_back_doors_to_stock(superuser_client, item):
    assert superuser_client.get(reverse("admin:inventory_stockitem_add")).status_code == 403
    delete = reverse("admin:inventory_stockitem_delete", args=[item.pk])
    assert superuser_client.post(delete, {"post": "yes"}).status_code == 403
    event = item.events.get()
    assert superuser_client.get(reverse("admin:inventory_stockevent_add")).status_code == 403
    page = superuser_client.get(change_url(event))
    assert page.status_code == 200 and b'name="_save"' not in page.content  # view only
    assert superuser_client.post(change_url(event), {"reason": "x"}).status_code == 403
    delete = reverse("admin:inventory_stockevent_delete", args=[event.pk])
    assert superuser_client.post(delete, {"post": "yes"}).status_code == 403
    assert StockEvent.objects.filter(pk=event.pk, reason="").exists()


@pytest.mark.django_db
def test_every_inventory_admin_page_loads(superuser_client, item, box, member, lab):
    services.unsorted_location(lab, member, "-20C")
    for model in (
        "locationtype",
        "location",
        "locationevent",
        "stockitem",
        "stockevent",
        "externalref",
        "containercode",
        "historyclaim",
    ):
        response = superuser_client.get(reverse(f"admin:inventory_{model}_changelist"))
        assert response.status_code == 200, model
    for url in (change_url(item), change_url(box), reverse("admin:inventory_location_add")):
        assert superuser_client.get(url).status_code == 200, url
    assert (
        b"Room 210 / -80 A / Shelf 1 / Box 4"
        in superuser_client.get(reverse("admin:inventory_location_changelist")).content
    )


@pytest.mark.django_db
def test_adding_a_location_goes_through_the_service(superuser_client, shelf, lab, member):
    url = reverse("admin:inventory_location_add")
    data = FormData(superuser_client.get(url).content.decode()).data
    box_type = shelf.location_type.__class__.objects.get(key="box")
    data.update(
        lab=lab.pk, parent=shelf.pk, name="Box 9", location_type=box_type.pk, code="",
        default_custodian=member.pk, tags="", visibility="",
    )  # fmt: skip
    response = superuser_client.post(url, data)
    assert response.status_code == 302, errors(response)
    box = Location.objects.get(name="Box 9")
    assert box.path_text() == "Room 210 / -80 A / Shelf 1 / Box 9"
    assert len(box.code) == 5 and box.visibility == "ask_first"  # generated, lab default
    assert box.created_by.email == "root@lab.test"
    assert not [problem for problem in Location.objects.find_problems() if problem]

    data.update(name="Shelf X", location_type=shelf.location_type.pk, parent=box.pk)
    response = superuser_client.post(url, data)
    assert response.status_code == 200
    assert "A shelf can't go inside a box" in errors(response)
    data.update(name="Room 999", parent="", code="SHELF1")
    response = superuser_client.post(url, data)
    assert "reads like a stock item's 6-character id" in errors(response)


@pytest.mark.django_db
def test_changing_the_parent_moves_the_location(superuser_client, room, freezer, shelf, box):
    other = services.create_location(
        lab=room.lab, parent=room, name="-20 B", location_type="freezer", user=None
    )
    url = change_url(shelf)
    data = FormData(superuser_client.get(url).content.decode()).data
    assert data["parent"] == str(freezer.pk)
    data.update(parent=other.pk, notes="moved during the defrost")
    response = superuser_client.post(url, data)
    assert response.status_code == 302, errors(response)
    box.refresh_from_db()
    assert box.path_text() == "Room 210 / -20 B / Shelf 1 / Box 4"
    event = LocationEvent.objects.get()
    assert (event.location, event.from_parent, event.to_parent) == (shelf, freezer, other)
    assert Location.objects.get(pk=shelf.pk).notes == "moved during the defrost"
    assert not [problem for problem in Location.objects.find_problems() if problem]

    # Box 4 is inside -20 B now: it isn't offered as -20 B's parent, and is refused if posted.
    data = FormData(superuser_client.get(change_url(other)).content.decode()).data
    data.update(parent=box.pk)
    response = superuser_client.post(change_url(other), data)
    assert "Select a valid choice" in errors(response)
    assert Location.objects.get(pk=other.pk).path == other.path


@pytest.mark.django_db
def test_unsorted_containers_and_the_lab_are_not_editable(superuser_client, box, lab, member):
    unsorted = services.unsorted_location(lab, member, "4C")
    page = superuser_client.get(change_url(unsorted))
    assert page.status_code == 200 and b'name="_save"' not in page.content
    form = superuser_client.get(change_url(box)).context["adminform"].form
    assert "lab" not in form.fields and "path" not in form.fields
    assert reverse("admin:inventory_location_delete", args=[box.pk])
    assert (
        superuser_client.post(
            reverse("admin:inventory_location_delete", args=[box.pk]), {"post": "yes"}
        ).status_code
        == 403
    )


@pytest.mark.django_db
def test_the_admin_keeps_stock_on_canonical_products(superuser_client, item, product):
    old = Product.objects.create(name="GAPDH (old record)")
    old.merged_into = product
    old.save(update_fields=["merged_into"])
    url = change_url(item)
    data = FormData(superuser_client.get(url).content.decode()).data
    data.update(product=old.pk, notes="relabelled")
    response = superuser_client.post(url, data)
    assert response.status_code == 302, errors(response)
    fresh = StockItem.objects.get(pk=item.pk)
    assert (fresh.product, fresh.notes) == (product, "relabelled")


@pytest.mark.django_db
def test_stock_is_found_by_its_short_id_as_typed(superuser_client, item, make_item):
    make_item()
    sid = item.short_id
    typed = f"{sid[:3].lower()}-{sid[3:].lower()}".replace("0", "o").replace("1", "l")
    response = superuser_client.get(reverse("admin:inventory_stockitem_changelist"), {"q": typed})
    assert [row.pk for row in response.context["cl"].result_list] == [item.pk]


@pytest.mark.django_db
def test_location_changes_are_checked_like_new_locations(
    superuser_client, shelf, make_location, antibody_type
):
    add = superuser_client.get(reverse("admin:inventory_location_add"))
    kinds = add.context["adminform"].form.fields["location_type"].queryset
    assert "unsorted" not in set(kinds.values_list("key", flat=True))
    make_location("Rack 3", "rack", shelf)
    url = change_url(shelf)
    data = FormData(superuser_client.get(url).content.decode()).data

    def refused(**changes):
        response = superuser_client.post(url, {**data, **changes})
        assert response.status_code == 200
        return errors(response)

    assert "Unknown item type tag(s): unicorn" in refused(tags="antibody, unicorn")
    assert "A shelf is always shared" in refused(visibility="restricted")
    box_kind = LocationType.objects.get(key="box")
    assert "holds Rack 3 (rack), so it can't be a box" in refused(location_type=box_kind.pk)
    unsorted_kind = LocationType.objects.get(key="unsorted")
    assert "Select a valid choice" in refused(location_type=unsorted_kind.pk)
    response = superuser_client.post(url, {**data, "tags": "antibody"})
    assert response.status_code == 302, errors(response)
    assert Location.objects.get(pk=shelf.pk).tags == ["antibody"]


@pytest.mark.django_db
def test_a_location_is_retired_only_once_nothing_active_is_left_in_it(
    superuser_client, shelf, box, make_item
):
    item = make_item()

    def retire(location):
        url = change_url(location)
        data = FormData(superuser_client.get(url).content.decode()).data
        data.pop("active", None)  # untick it
        return superuser_client.post(url, data)

    response = retire(shelf)
    assert "Shelf 1 still holds 1 active stock item(s)" in errors(response)
    assert "Box 4 is still in use inside it" in errors(response)
    assert "Box 4 still holds 1 active stock item(s)" in errors(retire(box))
    services.apply_event(item, "emptied", user=None)
    assert retire(box).status_code == 302
    assert retire(shelf).status_code == 302
    assert not Location.objects.get(pk=shelf.pk).active


@pytest.mark.django_db
def test_location_type_keys_are_fixed_once_made(superuser_client, location_types):
    kind = LocationType.objects.get(key="box")
    page = superuser_client.get(reverse("admin:inventory_locationtype_change", args=[kind.pk]))
    form = page.context["adminform"].form
    assert "key" not in form.fields and "label" in form.fields
    add = superuser_client.get(reverse("admin:inventory_locationtype_add"))
    assert "key" in add.context["adminform"].form.fields


@pytest.mark.django_db
def test_history_claims_stay_inside_their_lab(
    superuser_client, lab, other_lab, member, outsider, make_item, product
):
    ours = OrderRequest.objects.create(lab=lab, source="outside", item_name="GAPDH")
    theirs = OrderRequest.objects.create(lab=other_lab, source="outside", item_name="Actin")
    their_item = services.create_stock_item(
        lab=other_lab, product=product, custodian=other_lab.pool_user, user=None, source="gift"
    )
    url = reverse("admin:inventory_historyclaim_add")
    data = FormData(superuser_client.get(url).content.decode()).data
    data.update(lab=lab.pk, order_request=theirs.pk, person=outsider.pk, outcome="still_have")
    response = superuser_client.post(url, {**data, "stock_items": [their_item.pk]})
    assert response.status_code == 200
    problems = errors(response)
    assert "That request belongs to another lab." in problems
    assert "Olu Other isn't in Test Lab." in problems
    assert f"Not stock of Test Lab: {their_item.short_id}." in problems
    mine = make_item()
    data.update(order_request=ours.pk, person=member.pk)
    response = superuser_client.post(url, {**data, "stock_items": [mine.pk]})
    assert response.status_code == 302, errors(response)
    assert list(HistoryClaim.objects.get().stock_items.all()) == [mine]


@pytest.mark.django_db
def test_change_lists_build_every_path_in_one_query(
    superuser_client, make_item, make_location, shelf
):
    urls = [reverse(f"admin:inventory_{name}_changelist") for name in ("stockitem", "location")]

    def queries():
        counts = []
        for url in urls:
            with CaptureQueriesContext(connection) as captured:
                assert superuser_client.get(url).status_code == 200
            counts.append(len(captured.captured_queries))
        return counts

    make_item()
    queries()  # warm the caches
    few = queries()
    for number in range(5, 12):
        make_item(location=make_location(f"Box {number}", "box", shelf))
    assert queries() == few


@pytest.mark.django_db
def test_a_location_can_change_kind_and_place_in_one_save(
    superuser_client, freezer, box, make_location
):
    # "Shelf 2" was really a bag of tubes, and it sits in Box 4: a shelf can't go in a box,
    # a bag can, so the move must be checked with the new kind.
    shelf = make_location("Shelf 2", "shelf", freezer)
    url = change_url(shelf)
    data = FormData(superuser_client.get(url).content.decode()).data
    bag = LocationType.objects.get(key="bag")
    data.update(parent=box.pk, location_type=bag.pk, name="Bag 2")
    response = superuser_client.post(url, data)
    assert response.status_code == 302, errors(response)
    moved = Location.objects.get(pk=shelf.pk)
    assert moved.path_text() == "Room 210 / -80 A / Shelf 1 / Box 4 / Bag 2"
    assert moved.location_type.key == "bag"
    assert LocationEvent.objects.get().to_parent == box


@pytest.mark.django_db
def test_a_retired_box_can_still_be_edited(superuser_client, shelf, make_location):
    old = make_location("Box 9", "box", shelf)
    old.active = False
    old.save(update_fields=["active"])
    replacement = make_location("Box 9", "box", shelf)
    url = change_url(old)
    data = FormData(superuser_client.get(url).content.decode()).data
    assert "active" not in data  # unticked
    response = superuser_client.post(url, {**data, "notes": "cracked lid, thrown out"})
    assert response.status_code == 302, errors(response)
    assert Location.objects.get(pk=old.pk).notes == "cracked lid, thrown out"
    # Bringing it back into use is checked: Shelf 1 has a Box 9 in use.
    response = superuser_client.post(url, {**data, "active": "on"})
    assert "Shelf 1 already has a location called 'Box 9'." in errors(response)
    assert not Location.objects.get(pk=old.pk).active
    # The shelf is retired after its boxes; their notes can still be edited.
    for location in (replacement, shelf):
        location.active = False
        location.save(update_fields=["active"])
    response = superuser_client.post(url, {**data, "notes": "kept for the lot record"})
    assert response.status_code == 302, errors(response)
    response = superuser_client.post(url, {**data, "active": "on"})
    assert "Shelf 1 is no longer in use." in errors(response)
