import pytest
from allauth.mfa.models import Authenticator
from django.core.management import call_command

from apps.catalog.management.commands.seed_catalog import (
    ITEM_TYPES_YAML,
    item_type_values,
    load_yaml,
)
from apps.catalog.models import (
    ItemType,
    Product,
    SupplierProduct,
    Vendor,
    VendorFamily,
    seed_snapshot,
)
from apps.core.models import User


@pytest.fixture
def seeded(db):
    """The full catalog seed (vendors.yaml and item_types.yaml)."""
    call_command("seed_catalog", verbosity=0)


@pytest.fixture
def item_types(db):
    """Just the item types from item_types.yaml: much faster than the full seed. Vendors need
    no fixture, because resolve_vendor creates the ones vendors.yaml knows on first use."""
    for key, values in item_type_values(load_yaml(ITEM_TYPES_YAML), ITEM_TYPES_YAML):
        ItemType.objects.create(key=key, **values, seeded=seed_snapshot(values))


@pytest.fixture
def make_vendor(db):
    """make_vendor("Acme", family="acme", catalog_rule="neb"): a vendor in its own family."""

    def _make(name, *, family=None, catalog_rule="", **fields):
        slug = family or name.lower().replace(" ", "-")
        fam, _ = VendorFamily.objects.get_or_create(
            slug=slug, defaults={"name": slug.title(), "catalog_rule": catalog_rule}
        )
        return Vendor.objects.create(name=name, family=fam, **fields)

    return _make


@pytest.fixture
def make_supplier_product(db):
    """make_supplier_product(vendor, "R0101S", name="EcoRI"): a product and its listing."""

    def _make(vendor, catalog_raw, *, name=None, **fields):
        product = Product.objects.create(name=name or f"{vendor} {catalog_raw}")
        return SupplierProduct.objects.create(
            product=product, vendor=vendor, catalog_raw=catalog_raw, **fields
        )

    return _make


@pytest.fixture
def curator(db):
    """A superuser with TOTP set up, so the staff-MFA middleware lets them into the admin."""
    user = User.objects.create_superuser(email="curator@lab.test", full_name="Lab Curator")
    Authenticator.objects.create(user=user, type=Authenticator.Type.TOTP, data={})
    return user


@pytest.fixture
def curator_client(client, curator):
    client.force_login(curator)
    return client
