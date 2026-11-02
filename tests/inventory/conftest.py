"""Fixtures for the inventory tests: location types, a small location tree, products, people
in every role the visibility rules care about, and ``make_item``."""

import pytest
from allauth.mfa.models import Authenticator
from django.core.management import call_command

from apps.catalog.models import ItemType, Product, SupplierProduct, Vendor, VendorFamily
from apps.core.models import Lab, Membership, User
from apps.inventory import services


@pytest.fixture
def location_types(db):
    call_command("seed_inventory", verbosity=0)


@pytest.fixture
def antibody_type(db):
    return ItemType.objects.create(key="antibody", label="Antibody", tier=1, durable=True)


@pytest.fixture
def product(antibody_type):
    return Product.objects.create(
        name="GAPDH (14C10) Rabbit mAb", item_type=antibody_type, provisional=False
    )


@pytest.fixture
def supplier_product(product):
    family = VendorFamily.objects.create(slug="cst", name="Cell Signaling Technology")
    vendor = Vendor.objects.create(name="Cell Signaling Technology", family=family)
    return SupplierProduct.objects.create(product=product, vendor=vendor, catalog_raw="2118S")


@pytest.fixture
def make_location(lab, location_types):
    def _make(name, kind, parent=None, **fields):
        fields.setdefault("user", None)
        return services.create_location(
            lab=lab, parent=parent, name=name, location_type=kind, **fields
        )

    return _make


@pytest.fixture
def room(make_location):
    return make_location("Room 210", "room")


@pytest.fixture
def freezer(make_location, room):
    return make_location("-80 A", "freezer", room, temperature="-80C")


@pytest.fixture
def shelf(make_location, freezer):
    return make_location("Shelf 1", "shelf", freezer)


@pytest.fixture
def box(make_location, shelf, member):
    return make_location("Box 4", "box", shelf, default_custodian=member)


@pytest.fixture
def make_item(lab, product, member, box):
    """make_item(lifecycle="unverified", location=None, temperature="-20C"): a stock item
    created through the service, by default Priya's, in Box 4."""

    def _make(**fields):
        values = {
            "lab": lab,
            "product": product,
            "custodian": member,
            "user": member,
            "source": "purchased",
            "location": box,
        }
        return services.create_stock_item(**{**values, **fields})

    return _make


@pytest.fixture
def item(make_item):
    return make_item()


@pytest.fixture
def raj(make_member):
    return make_member("raj@lab.test", full_name="Raj Rao")


@pytest.fixture
def pool(lab):
    return lab.pool_user


@pytest.fixture
def other_lab(db):
    call_command("bootstrap_lab", name="Other Lab", slug="other-lab", verbosity=0)
    return Lab.objects.get(slug="other-lab")


@pytest.fixture
def outsider(other_lab):
    """A current member of another lab only."""
    user = User.objects.create_user(email="olu@other.test", full_name="Olu Other")
    Membership.objects.create(user=user, lab=other_lab)
    return user


@pytest.fixture
def leaver(make_member):
    return make_member("lee@lab.test", full_name="Lee Left", status=Membership.Status.LEAVER)


@pytest.fixture
def superuser(db):
    """A superuser with TOTP set up, so the staff-MFA middleware lets them into the admin."""
    user = User.objects.create_superuser(email="root@lab.test", full_name="Sys Owner")
    Authenticator.objects.create(user=user, type=Authenticator.Type.TOTP, data={})
    return user


@pytest.fixture
def superuser_client(client, superuser):
    client.force_login(superuser)
    return client
