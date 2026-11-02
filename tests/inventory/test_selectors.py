"""Selectors: active stock (with product redirects), holdings, contents, paths and codes,
always inside one lab."""

import pytest

from apps.catalog.models import Product, SupplierProduct
from apps.catalog.services import merge_products
from apps.inventory import selectors, services
from apps.inventory.models import ContainerCode
from apps.inventory.selectors import active_stock, location_by_path, resolve_code
from apps.inventory.services import apply_event


@pytest.fixture
def theirs(other_lab, product, location_types):
    """Another lab's room, box and item, with codes ours could be confused with."""
    room = services.create_location(
        lab=other_lab, parent=None, name="Room 210", location_type="room", user=None, code="R9"
    )
    box = services.create_location(
        lab=other_lab, parent=room, name="Box 4", location_type="box", user=None
    )
    item = services.create_stock_item(
        lab=other_lab, product=product, custodian=other_lab.pool_user, user=None,
        source="gift", location=box,
    )  # fmt: skip
    return room, box, item


@pytest.mark.django_db
def test_active_stock_is_the_labs_unverified_available_and_opened(make_item, lab, theirs):
    available, unverified, opened = make_item(), make_item(lifecycle="unverified"), make_item()
    apply_event(opened, "opened", user=None)
    gone = [make_item() for _ in range(3)]
    for item, kind in zip(gone, ("emptied", "discarded", "lost"), strict=True):
        apply_event(item, kind, user=None)
    assert list(active_stock(lab)) == [available, unverified, opened]


@pytest.mark.django_db
def test_active_stock_follows_product_merges_both_ways(make_item, lab, product, supplier_product):
    mine = make_item()
    other = Product.objects.create(name="Beta-actin (13E5)")
    actin = make_item(product=other)
    listed = make_item(supplier_product=supplier_product)
    assert set(active_stock(lab, products=[product])) == {mine, listed}
    assert list(active_stock(lab, products=[other.pk])) == [actin]
    # Beta-actin turns out to be the same product: its stock now counts as GAPDH's...
    merge_products(other, product)
    assert set(active_stock(lab, products=[product])) == {mine, listed, actin}
    # ...whichever of the two the caller has.
    assert set(active_stock(lab, products=[Product.objects.get(pk=other.pk)])) == {
        mine,
        listed,
        actin,
    }
    assert list(active_stock(lab, products=[])) == []


@pytest.mark.django_db
def test_items_still_pointing_at_a_merged_product_count_as_its_targets(make_item, lab, product):
    old = Product.objects.create(name="GAPDH (old record)")
    stale = make_item(product=old)
    # As if the item kept its old product (merge_products repoints it; this checks the
    # selector doesn't rely on that).
    old.merged_into = product
    old.save(update_fields=["merged_into"])
    assert list(active_stock(lab, products=[product])) == [stale]
    assert selectors.product_family_ids([product]) == {product.pk, old.pk}


@pytest.mark.django_db
def test_active_stock_by_listing(make_item, lab, product, supplier_product):
    listed, unlisted = make_item(supplier_product=supplier_product), make_item()
    other_listing = SupplierProduct.objects.create(
        product=product, vendor=supplier_product.vendor, catalog_raw="2118L"
    )
    assert list(active_stock(lab, supplier_products=[supplier_product])) == [listed]
    assert list(active_stock(lab, supplier_products=[other_listing])) == []
    either = active_stock(lab, products=[], supplier_products=[supplier_product])
    assert list(either) == [listed]
    assert unlisted in active_stock(lab, products=[product], supplier_products=[other_listing])


@pytest.mark.django_db
def test_stock_held_by_a_person(make_item, member, raj, lab):
    mine, theirs = make_item(), make_item(custodian=raj)
    gone = make_item()
    apply_event(gone, "discarded", user=None)
    assert list(selectors.stock_held_by(member, lab=lab)) == [mine]
    assert list(selectors.stock_held_by(raj, lab=lab)) == [theirs]
    assert set(selectors.stock_held_by(member, lab=lab, active_only=False)) == {mine, gone}


@pytest.mark.django_db
def test_location_contents(make_item, make_location, box, shelf, freezer, theirs):
    later, first = make_item(position_label="B1"), make_item(position_label="A1")
    unplaced = make_item(position_label="")
    other_box = make_location("Box 5", "box", shelf)
    inner = make_item(location=other_box)
    assert list(selectors.location_contents(box)) == [first, later, unplaced]
    assert set(selectors.location_contents(shelf, include_sublocations=True)) == {
        first,
        later,
        unplaced,
        inner,
    }
    assert list(selectors.location_contents(shelf)) == []
    apply_event(inner, "emptied", user=None)
    assert inner not in selectors.location_contents(freezer, include_sublocations=True)
    assert inner in selectors.location_contents(other_box, active_only=False)
    assert list(selectors.child_locations(shelf)) == [box, other_box]
    other_box.active = False
    other_box.save(update_fields=["active"])
    assert list(selectors.child_locations(shelf)) == [box]
    assert list(selectors.child_locations(shelf, include_inactive=True)) == [box, other_box]


@pytest.mark.django_db
def test_location_by_path(box, shelf, lab, theirs, member):
    assert location_by_path(lab, "Room 210 / -80 A / Shelf 1 / Box 4") == box
    assert location_by_path(lab, "  room 210/-80 a /  shelf 1 ") == shelf
    assert location_by_path(lab, "Room 210 / -80 A / Box 4") is None
    assert location_by_path(lab, "Box 4") is None
    assert location_by_path(lab, "") is None
    assert location_by_path(theirs[0].lab, "Room 210 / Box 4") == theirs[1]
    services.unsorted_location(lab, member, "-20C")
    assert location_by_path(lab, "Unsorted: Priya Patel, -20 °C") is None
    box.active = False
    box.save(update_fields=["active"])
    assert location_by_path(lab, "Room 210 / -80 A / Shelf 1 / Box 4") is None
    assert location_by_path(lab, "Room 210/-80 A/Shelf 1/Box 4", include_inactive=True) == box


@pytest.mark.django_db
def test_resolve_code_finds_items_and_locations_of_the_lab_only(
    make_item, make_location, room, box, lab, theirs
):
    item = make_item()
    sid = item.short_id
    typed = f"{sid[:3].lower()}-{sid[3:].lower()}"
    assert resolve_code(sid, lab=lab) == item
    assert resolve_code(f" {typed} ", lab=lab) == item
    assert resolve_code(sid.replace("0", "O").replace("1", "l"), lab=lab) == item
    assert resolve_code(f"https://labstock.test/i/{sid}", lab=lab) == item
    assert resolve_code(f"/i/{sid.lower()}/", lab=lab) == item
    assert resolve_code(box.code.lower(), lab=lab) == box
    assert resolve_code(f"https://labstock.test/l/{box.code}?from=qr", lab=lab) == box

    dewar = make_location("LN2 dewar", "ln2_dewar", room, code="LN2-1")
    assert resolve_code("ln2-1", lab=lab) == dewar
    assert resolve_code("LN21", lab=lab) == dewar  # the hyphen left out
    assert resolve_code("LN2 - 1", lab=lab) == dewar
    lookalike = make_location("Fridge", "fridge", room, code="LN21")
    assert resolve_code("LN21", lab=lab) == lookalike  # exact beats compact
    make_location("Fridge 2", "fridge", room, code="L-N21")
    assert resolve_code("LN-21", lab=lab) is None  # LN2-1 or L-N21: ambiguous

    ContainerCode.objects.create(code="0123456789012", stock_item=item)
    assert resolve_code("0123456789012", lab=lab) == item

    their_room, their_box, their_item = theirs
    assert resolve_code(their_item.short_id, lab=lab) is None
    assert resolve_code(their_box.code, lab=lab) is None
    assert resolve_code("R9", lab=lab) is None
    assert resolve_code(f"/i/{their_item.short_id}", lab=lab) is None
    ContainerCode.objects.create(code="999", stock_item=their_item)
    assert resolve_code("999", lab=lab) is None
    assert resolve_code(their_item.short_id, lab=their_room.lab) == their_item
    assert resolve_code("", lab=lab) is None
    assert resolve_code("nothing like it", lab=lab) is None
    assert resolve_code(sid, lab=None) is None


@pytest.mark.django_db
def test_generated_location_codes_forgive_crockford_lookalikes(
    make_location, room, lab, monkeypatch
):
    from apps.inventory import shortid

    monkeypatch.setattr(shortid, "new_location_code", lambda: "1Q0Z5")
    box = make_location("Box Z", "box", room)
    assert box.code == "1Q0Z5"
    assert resolve_code("lqoz5", lab=lab) == box


@pytest.mark.django_db
def test_ancestor_chains_in_one_query(box, shelf, room, freezer, django_assert_num_queries, member):
    unsorted = services.unsorted_location(room.lab, member, "4C")
    with django_assert_num_queries(1):
        chains = selectors.ancestor_chains([box, shelf, room, unsorted])
    assert [loc.name for loc in chains[box.pk]] == ["Room 210", "-80 A", "Shelf 1", "Box 4"]
    assert chains[room.pk] == [room] and chains[unsorted.pk] == [unsorted]
    assert selectors.chain_text(chains[shelf.pk]) == "Room 210 / -80 A / Shelf 1"
