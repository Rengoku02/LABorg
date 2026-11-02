"""Presenters: what each viewer sees of stock, on every channel.

The redaction matrix: restricted fixtures (a restricted item, a shared item inside a
restricted box, and the restricted box itself) x every channel x every viewer role. Where the
design's matrix says no, none of the location, position, temperature, lot, expiry, quantity
or notes may appear in any field of the view, not even inside another string. Its listing
cases (a box's page, a freezer's page with what is inside it, a shelf's boxes) may not list
a redacted row at all: the page itself would tell where it is."""

from dataclasses import asdict, fields
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import AnonymousUser
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.catalog.models import LabProduct, Lot, Product
from apps.core.models import Membership
from apps.inventory import services
from apps.inventory.models import Location, StockItem
from apps.inventory.presenters import (
    CHANNELS,
    ItemView,
    LocationView,
    present_item,
    present_items,
    present_location,
    present_locations,
)
from apps.inventory.selectors import child_locations, location_contents, stock_held_by
from apps.inventory.services import apply_event

# Lower case on purpose: short ids and codes are upper-case Crockford, so these can't turn
# up inside them by chance. The numbers carry a "." or "-" for the same reason: no short id
# or database id has one.
ROOM, FREEZER, SHELF = "Room zanzibar", "Freezer kestrel", "Shelf pelican"
SECRET_BOX, OPEN_BOX = "Box quokka", "Box wombat"


@pytest.fixture
def world(lab, make_location, member, product, location_types):
    room = make_location(ROOM, "room")
    freezer = make_location(FREEZER, "freezer", room, temperature="-80C")
    shelf = make_location(SHELF, "shelf", freezer)
    secret_box = make_location(
        SECRET_BOX, "box", shelf, default_custodian=member, visibility="restricted",
        notes="boxnote-marigold",
    )  # fmt: skip
    open_box = make_location(OPEN_BOX, "box", shelf, default_custodian=member, visibility="shared")
    lot = Lot.objects.create(product=product, lot_raw="lot-umbra9", expiry=date(2031, 4, 1))

    def stock(location, visibility, mark, **fields):
        return services.create_stock_item(
            lab=lab, product=product, custodian=member, user=member, source="collaborator",
            location=location, visibility=visibility, position_label=f"pos-{mark}",
            lot=lot, quantity="137.25", unit=f"ml-{mark}", notes=f"note-{mark}",
            expiry_date=date(2031, 5, 1), expiry_precision="month", **fields,
        )  # fmt: skip

    return {
        "restricted item": stock(open_box, "restricted", "uxa"),
        "item in a restricted box": stock(secret_box, "shared", "uxb"),
        "restricted box": secret_box,
        "restricted aggregate": stock(
            open_box, "restricted", "uxc", is_aggregate=True, container_count=13
        ),
        "shared item": stock(open_box, "shared", "uxd"),  # everyone may see it, and where
    }


ITEM_SECRETS = [ROOM, FREEZER, SHELF, "lot-umbra9", "2031-0", "137.25", "-80", "°c"]
SECRETS = {
    "restricted item": [*ITEM_SECRETS, OPEN_BOX, "pos-uxa", "note-uxa", "ml-uxa"],
    "item in a restricted box": [*ITEM_SECRETS, SECRET_BOX, "pos-uxb", "note-uxb", "ml-uxb"],
    # How many containers it stands for is a quantity too.
    "restricted aggregate": [*ITEM_SECRETS, OPEN_BOX, "pos-uxc", "note-uxc", "ml-uxc", "split"],
    # The box itself may be named; where it is and its notes may not.
    "restricted box": [ROOM, FREEZER, SHELF, "boxnote-marigold", "-80", "°c"],
}


@pytest.fixture
def viewers(lab, member, make_member, outsider, superuser):
    def member_with(**membership):
        return make_member(**membership)

    return {
        "custodian": member,
        "co-owner": member_with(role=Membership.Role.CO_OWNER),
        "system admin": superuser,
        "admin member": member_with(role=Membership.Role.ADMIN),
        "PI": member_with(role=Membership.Role.PI),
        "another member": member_with(),
        "rotator": member_with(status=Membership.Status.ROTATOR),
        "leaver": member_with(status=Membership.Status.LEAVER),
        "ended membership": member_with(end_date=timezone.localdate() - timedelta(days=1)),
        "other lab": outsider,
        "anonymous": AnonymousUser(),
        "nobody": None,
    }


# The design's matrix for restricted stock, row by row.
MATRIX = {
    "custodian": "everything",
    "co-owner": "everything",
    "system admin": "everything",
    "admin member": "everything",
    "PI": "redacted",
    "another member": "redacted",
    "rotator": "redacted",
    "leaver": None,
    "ended membership": None,
    "other lab": None,
    "anonymous": None,
    "nobody": None,
}


def strings_in(view):
    """Every value of a view, as text, including inside tuples and lists."""

    def walk(value):
        if isinstance(value, list | tuple):
            for part in value:
                yield from walk(part)
        elif value is not None:
            yield str(value)

    for value in asdict(view).values():
        yield from walk(value)


def present(subject, viewer, channel):
    if isinstance(subject, Location):
        return present_location(subject, viewer, channel=channel)
    return present_item(subject, viewer, channel=channel)


@pytest.mark.django_db
@pytest.mark.parametrize("role", sorted(MATRIX))
@pytest.mark.parametrize("fixture", sorted(SECRETS))
def test_the_redaction_matrix(fixture, role, world, viewers):
    subject, expected = world[fixture], MATRIX[role]
    for channel in CHANNELS:
        view = present(subject, viewers[role], channel)
        if expected is None:
            assert view is None, channel
            continue
        text = " | ".join(strings_in(view)).casefold()
        leaked = [secret for secret in SECRETS[fixture] if secret.casefold() in text]
        if expected == "redacted":
            assert view.redacted and not leaked, (channel, leaked)
            assert view.ask_text == "Ask Priya Patel", channel
            if isinstance(view, ItemView):
                assert view.product_name == "GAPDH (14C10) Rabbit mAb", channel
                assert view.short_id in view.url, channel
                assert (view.quantity, view.container_count, view.expiry_precision) == (
                    None,
                    None,
                    "",
                )
                assert view.lifecycle == "available" and view.custodian_name == "Priya Patel"
            else:
                assert view.name == SECRET_BOX, channel
        else:
            assert not view.redacted, channel
            assert set(leaked) == set(SECRETS[fixture]), channel  # all there to be hidden


@pytest.mark.django_db
@pytest.mark.parametrize("role", sorted(MATRIX))
def test_a_list_of_what_is_in_a_place_shows_only_what_the_viewer_may_place_there(
    role, world, viewers
):
    """The listing cases of the matrix: a page listing what is in a place is that place, so
    it leaves out the rows that would be redacted (Box wombat's page would otherwise tell
    where the restricted item is, and Shelf pelican's where Box quokka is)."""
    viewer, expected = viewers[role], MATRIX[role]
    secret_box, open_box = world["restricted box"], world["shared item"].location
    shelf = Location.objects.get_parent(secret_box)
    freezer = Location.objects.get_parent(shelf)
    ids = {name: world[name].short_id for name in world if name != "restricted box"}
    for channel in CHANNELS:
        pages = {
            name: present_items(
                location_contents(place, include_sublocations=place == freezer),
                viewer,
                channel=channel,
                listed_in=place,
            )
            for name, place in ((OPEN_BOX, open_box), (FREEZER, freezer), (SECRET_BOX, secret_box))
        }
        listed = {name: {view.short_id for view in views} for name, views in pages.items()}
        boxes = present_locations(child_locations(shelf), viewer, channel=channel, listed_in=shelf)
        if expected is None:
            assert listed == {OPEN_BOX: set(), FREEZER: set(), SECRET_BOX: set()}, channel
            assert boxes == [], channel
            continue
        if expected == "redacted":
            only_shared = {ids["shared item"]}
            assert listed == {OPEN_BOX: only_shared, FREEZER: only_shared, SECRET_BOX: set()}
            assert [view.name for view in boxes] == [OPEN_BOX], channel
        else:
            in_open_box = {ids["restricted item"], ids["restricted aggregate"], ids["shared item"]}
            assert listed == {
                OPEN_BOX: in_open_box,
                FREEZER: {*in_open_box, ids["item in a restricted box"]},
                SECRET_BOX: {ids["item in a restricted box"]},
            }, channel
            assert [view.name for view in boxes] == [SECRET_BOX, OPEN_BOX], channel
        views = [*boxes, *(view for page in pages.values() for view in page)]
        assert not any(view.redacted for view in views), channel
    # Not a list of one place (a search, someone's holdings): redacted rows stay listed.
    if expected == "redacted":
        views = present_items(location_contents(open_box), viewer, channel="page")
        assert {view.short_id for view in views if view.redacted} == {
            ids["restricted item"],
            ids["restricted aggregate"],
        }


@pytest.mark.django_db
def test_a_full_item_view(make_item, supplier_product, product, member, raj, box):
    lot = Lot.objects.create(product=product, lot_raw="0012", expiry=date(2027, 3, 1))
    item = make_item(
        supplier_product=supplier_product, lot=lot, visibility="ask_first",
        position_label="A1", quantity="100.0000", unit="uL", notes="1:1000 for WB",
    )  # fmt: skip
    view = present_item(item, raj, channel="page")
    assert view == ItemView(
        channel="page",
        short_id=item.short_id,
        url=f"/i/{item.short_id}",
        version=1,
        product_id=product.pk,
        product_name="GAPDH (14C10) Rabbit mAb",
        item_type="Antibody",
        vendor="Cell Signaling Technology",
        catalog_number="2118S",
        custodian_id=member.pk,
        custodian_name="Priya Patel",
        custodian_is_pool=False,
        custodian_has_left=False,
        viewer_is_custodian=False,
        ask_user_id=member.pk,
        ask_name="Priya Patel",
        ask_text="Ask Priya Patel",
        visibility="ask_first",
        visibility_label="Ask first",
        redacted=False,
        lifecycle="available",
        lifecycle_label="Available",
        is_active=True,
        unverified=False,
        unverified_text="",
        level="ok",
        level_label="OK",
        location_code=box.code,
        location_name="Box 4",
        location_path="Room 210 / -80 A / Shelf 1 / Box 4",
        location_url=f"/l/{box.code}",
        unplaced=False,
        temperature="-80C",
        temperature_label="-80 °C",
        position="A1",
        position_index=1,
        lot="0012",
        expiry_date=date(2027, 3, 1),
        expiry_precision="day",
        expiry_text="2027-03-01",
        quantity=Decimal("100"),
        unit="uL",
        quantity_text="100 uL",
        opened_at=None,
        is_aggregate=False,
        container_count=None,
        containers_text="",
        source="purchased",
        source_label="Purchased",
        mta=False,
        notes="1:1000 for WB",
        created_at=item.created_at,
    )
    mine = present_item(item, member, channel="page")
    assert mine.viewer_is_custodian and mine.ask_text == ""
    with pytest.raises(AttributeError):
        view.notes = "changed"  # frozen


@pytest.mark.django_db
def test_channels_change_only_the_links(item, raj, settings):
    views = {channel: present_item(item, raj, channel=channel) for channel in CHANNELS}
    for channel in ("page", "partial"):
        assert views[channel].url == f"/i/{item.short_id}"
    for channel in ("email", "digest", "csv"):
        assert views[channel].url == f"{settings.SITE_URL}/i/{item.short_id}"
        assert views[channel].location_url.startswith(settings.SITE_URL)
    plain = [
        {**asdict(view), "channel": "", "url": "", "location_url": ""} for view in views.values()
    ]
    assert all(view == plain[0] for view in plain)
    with pytest.raises(ValueError, match="Unknown channel"):
        present_item(item, raj, channel="sms")


@pytest.mark.django_db
def test_unverified_stock_says_probably(make_item, member, raj):
    item = make_item(lifecycle="unverified", visibility="shared")
    assert present_item(item, raj, channel="page").unverified_text == "Probably: ask Priya Patel"
    assert present_item(item, raj, channel="page").ask_text == "Ask Priya Patel"
    assert present_item(item, member, channel="page").unverified_text == "Not checked yet"
    apply_event(item, "verified", user=member)
    view = present_item(item, raj, channel="page")
    assert (view.unverified, view.unverified_text, view.ask_text) == (False, "", "")


@pytest.mark.django_db
def test_lab_stock_names_its_steward(make_item, pool, raj, member, lab, product):
    item = make_item(custodian=pool, location=None, temperature="4C", visibility="ask_first")
    view = present_item(item, raj, channel="page")
    assert view.custodian_is_pool and view.custodian_name == "Test Lab — lab stock"
    assert view.ask_text == "Ask the lab's co-owner"
    assert view.unplaced and view.location_name == "Unsorted: Test Lab — lab stock, 4 °C"
    LabProduct.objects.create(lab=lab, product=product, steward=member)
    assert present_item(item, raj, channel="page").ask_text == "Ask Priya Patel"
    assert present_item(item, member, channel="page").ask_text == ""


@pytest.mark.django_db
def test_stock_of_someone_who_left_is_asked_about_through_the_lab(
    make_item, make_location, shelf, leaver, raj, member, lab, product
):
    item = make_item(custodian=leaver, location=None, temperature="-20C", visibility="ask_first")
    view = present_item(item, raj, channel="page")
    assert (view.custodian_name, view.custodian_has_left) == ("Lee Left", True)
    assert (view.ask_user_id, view.ask_text) == (None, "Ask the lab's co-owner")
    LabProduct.objects.create(lab=lab, product=product, steward=member)
    view = present_item(item, raj, channel="page")
    assert (view.ask_user_id, view.ask_text) == (member.pk, "Ask Priya Patel")
    assert present_item(item, member, channel="page").ask_text == ""  # the steward
    assert present_item(item, leaver, channel="page") is None  # they see nothing now
    unsorted = present_location(item.location, raj, channel="page")
    assert unsorted.custodian_has_left and unsorted.ask_text == ""  # shared: nothing to ask
    box = make_location("Box L", "box", shelf, default_custodian=leaver, visibility="ask_first")
    assert present_location(box, raj, channel="page").ask_text == "Ask the lab's co-owner"
    pools = make_location("Box P", "box", shelf, default_custodian=lab.pool_user)
    assert present_location(pools, raj, channel="page").ask_text == "Ask the lab's co-owner"
    mine = present_item(make_item(), raj, channel="page")
    assert (mine.custodian_has_left, mine.ask_user_id, mine.version) == (False, member.pk, 1)


@pytest.mark.django_db
def test_expiry_precision_and_aggregates(make_item, raj):
    by_year = make_item(expiry_date=date(2028, 1, 1), expiry_precision="year")
    assert present_item(by_year, raj, channel="csv").expiry_text == "2028"
    by_month = make_item(expiry_date=date(2027, 11, 1), expiry_precision="month")
    assert present_item(by_month, raj, channel="csv").expiry_text == "2027-11"
    boxes = make_item(is_aggregate=True, container_count=4)
    assert present_item(boxes, raj, channel="page").containers_text == "4 containers, not split yet"
    one = make_item(is_aggregate=True, container_count=1)
    assert present_item(one, raj, channel="page").containers_text == "1 container, not split yet"


@pytest.mark.django_db
def test_present_items_leaves_out_what_the_viewer_may_not_see(
    make_item, raj, other_lab, product, django_assert_max_num_queries, location_types
):
    ours = [make_item() for _ in range(5)]
    theirs = services.create_stock_item(
        lab=other_lab, product=product, custodian=other_lab.pool_user, user=None, source="gift"
    )
    items = list(
        StockItem.objects.filter(pk__in=[i.pk for i in [*ours, theirs]])
        .select_related("lab", "product__item_type", "custodian", "location__location_type")
        .order_by("pk")
    )
    with django_assert_max_num_queries(12):
        views = present_items(items, raj, channel="digest")
    assert [view.short_id for view in views] == [item.short_id for item in ours]


def queries_for(call):
    """How many queries ``call()`` makes, and what it returned."""
    with CaptureQueriesContext(connection) as captured:
        result = call()
    return len(captured.captured_queries), result


@pytest.mark.django_db
def test_selector_rows_are_presented_without_a_query_per_item(make_item, raj, lab, member):
    def present():
        return present_items(stock_held_by(member, lab=lab), raj, channel="page")

    for _ in range(2):
        make_item()
    few, views = queries_for(present)
    assert len(views) == 2
    for _ in range(8):
        make_item(location=None, temperature="-20C")
    many, views = queries_for(present)
    assert len(views) == 10 and many == few


@pytest.mark.django_db
def test_stewards_are_read_once_for_lab_stock_of_many_products(
    make_item, raj, lab, pool, antibody_type
):
    """Lab stock asks the product's steward: one query for all products, not one each."""

    def present():
        return present_items(stock_held_by(pool, lab=lab), raj, channel="page")

    def stock_of_new_product(number):
        product = Product.objects.create(name=f"Product {number}", item_type=antibody_type)
        LabProduct.objects.create(lab=lab, product=product, steward=raj if number % 2 else None)
        make_item(product=product, custodian=pool, location=None, temperature="4C")

    stock_of_new_product(1)
    few, views = queries_for(present)
    for number in range(2, 21):
        stock_of_new_product(number)
    many, views = queries_for(present)
    assert len(views) == 20 and many == few
    asked = {view.product_name: view.ask_name for view in views}
    assert asked["Product 1"] == "Raj Rao" and asked["Product 2"] == "the lab's co-owner"


@pytest.mark.django_db
def test_present_locations_for_a_list_of_boxes(make_location, shelf, box, member, raj, other_lab):
    make_location("Box 7", "box", shelf, default_custodian=member, visibility="restricted")
    make_location("Box 8", "box", shelf, default_custodian=raj, visibility="shared")
    theirs = services.create_location(
        lab=other_lab, parent=None, name="Room 9", location_type="room", user=None
    )
    few, views = queries_for(
        lambda: present_locations([*child_locations(shelf), theirs], raj, channel="page")
    )
    assert [(view.name, view.redacted, view.path) for view in views] == [
        ("Box 4", False, "Room 210 / -80 A / Shelf 1 / Box 4"),
        ("Box 7", True, ""),  # Priya's restricted box: Raj sees its name and whom to ask
        ("Box 8", False, "Room 210 / -80 A / Shelf 1 / Box 8"),
    ]
    assert views[1].ask_text == "Ask Priya Patel" and views[2].viewer_is_custodian
    for number in range(9, 15):
        make_location(f"Box {number}", "box", shelf, default_custodian=member)
    many, views = queries_for(
        lambda: present_locations([*child_locations(shelf), theirs], raj, channel="page")
    )
    assert len(views) == 9 and many == few
    assert present_locations([box, theirs], AnonymousUser(), channel="page") == []


@pytest.mark.django_db
def test_a_location_view(make_location, shelf, member, raj, antibody_type, settings):
    box = make_location(
        "Box 7", "box", shelf, default_custodian=member, tags=["antibody"],
        visibility="ask_first", notes="lid says P.P.",
    )  # fmt: skip
    view = present_location(box, raj, channel="email")
    assert isinstance(view, LocationView)
    assert (view.name, view.kind, view.kind_label, view.structural) == (
        "Box 7",
        "box",
        "Box (9 x 9)",
        False,
    )
    assert view.path == "Room 210 / -80 A / Shelf 1 / Box 7"
    assert view.parent_path == "Room 210 / -80 A / Shelf 1"
    assert (view.temperature, view.temperature_label) == ("-80C", "-80 °C")
    assert (view.tags, view.tag_labels) == (("antibody",), ("Antibody",))
    assert (view.rows, view.cols, view.position_scheme) == (9, 9, "grid")
    assert view.url == f"{settings.SITE_URL}/l/{box.code}"
    assert (view.custodian_name, view.ask_text, view.notes) == (
        "Priya Patel",
        "Ask Priya Patel",
        "lid says P.P.",
    )
    assert present_location(box, member, channel="page").ask_text == ""
    room_view = present_location(shelf, raj, channel="page")
    assert (room_view.custodian_name, room_view.ask_text) == ("", "")


@pytest.mark.django_db
def test_an_unsorted_container_view(lab, member, raj, location_types):
    unsorted = services.unsorted_location(lab, member, "-20C")
    view = present_location(unsorted, raj, channel="page")
    assert (view.name, view.path, view.is_unsorted) == (
        "Unsorted: Priya Patel, -20 °C",
        "Unsorted: Priya Patel, -20 °C",
        True,
    )
    assert (view.custodian_name, view.redacted) == ("Priya Patel", False)


@pytest.mark.django_db
def test_views_serialize_for_snapshots(item, raj):
    data = present_item(item, raj, channel="email").as_json()
    assert data["short_id"] == item.short_id and isinstance(data["created_at"], str)
    assert set(data) == {field.name for field in fields(ItemView)}
