"""Fixtures for the duplicate guard tests: the Quartzy fake and a mirrored lab, the seeded
item types (Quartzy types map onto them by name), lab members who are also Quartzy users,
``requested`` (a request mirrored the way the sync writes it) and ``stock``."""

from datetime import datetime

import pytest
from django.utils import timezone

from apps.catalog.services import resolve_supplier_product
from apps.core.policies import set_policy
from apps.inventory import services
from apps.quartzy import sync
from apps.quartzy.models import OrderStatusObservation, SyncRun
from tests.inventory.conftest import (  # noqa: F401  (fixtures)
    box,
    freezer,
    leaver,
    location_types,
    make_location,
    other_lab,
    pool,
    raj,
    room,
    shelf,
    superuser,
    superuser_client,
)
from tests.quartzy.conftest import (  # noqa: F401  (fixtures)
    clear_cache,
    fake,
    fake_lab,
    item_types,
    lock_waited,
    no_quartzy,
    quartzy_lab,
    use_fake,
)


@pytest.fixture
def people(fake, member, raj):  # noqa: F811
    """The Quartzy users of the lab's members (matched to them by email)."""
    return {
        "priya": fake.add_user("Priya", "Patel", email=member.email),
        "raj": fake.add_user("Raj", "Rao", email=raj.email),
        "sam": fake.add_user("Sam", "Ortiz", email="sam@outside.test"),
    }


@pytest.fixture
def mirror(quartzy_lab):  # noqa: F811
    """mirror(payload, fetched_at=None) -> the OrderRequest, as a head run would write it."""
    run = SyncRun.objects.create(kind=SyncRun.Kind.HEAD, quartzy_lab=quartzy_lab)

    def _mirror(payload, *, fetched_at=None, via=OrderStatusObservation.Via.POLL):
        obj, _created, _changed = sync.upsert_order_request(
            quartzy_lab, payload, via=via, run=run, fetched_at=fetched_at
        )
        assert obj is not None, run.note
        return obj

    return _mirror


@pytest.fixture
def requested(fake, fake_lab, mirror, people, item_types):  # noqa: F811
    """requested(by="raj", catalog="2118L", at=datetime, status="RECEIVED") -> OrderRequest.

    Defaults: Priya asks for CST 2118S (an antibody) now (not on the fake's own clock),
    status CREATED."""

    def _requested(
        *,
        by="priya",
        qtype="Antibodies",
        vendor="CST",
        catalog="2118S",
        name="GAPDH (14C10) Rabbit mAb",
        status="CREATED",
        at=None,
        price="289.00",
        **fields,
    ):
        fields["requested_at"] = (at or timezone.now()).isoformat()
        payload = fake.add_request(
            fake_lab,
            qtype=qtype,
            requested_by=people[by],
            created_by=people[by],
            vendor_name=vendor,
            catalog_number=catalog,
            item_name=name,
            status=status,
            unit_price=price,
            **fields,
        )
        return mirror(payload)

    return _requested


@pytest.fixture
def stock(lab, box, member, item_types):  # noqa: F811
    """stock(catalog="2118S", custodian=raj, visibility="restricted") -> a StockItem, made
    through the inventory service, by default Priya's CST 2118S in Box 4."""

    def _stock(
        *,
        vendor="CST",
        catalog="2118S",
        name="GAPDH (14C10) Rabbit mAb",
        item_type="antibody",
        custodian=None,
        **fields,
    ):
        resolution = resolve_supplier_product(
            vendor_name=vendor, catalog_number=catalog, item_name=name, item_type=item_type
        )
        custodian = custodian or member
        fields.setdefault("location", box)
        return services.create_stock_item(
            lab=lab,
            product=resolution.product,
            supplier_product=resolution.supplier_product,
            custodian=custodian,
            user=custodian,
            source="purchased",
            **fields,
        )

    return _stock


@pytest.fixture
def armed(quartzy_lab):  # noqa: F811
    """Alerts armed for the Quartzy lab since 2020 (so every test request is armed)."""
    quartzy_lab.alerts_armed_at = datetime.fromisoformat("2020-01-01T00:00:00+00:00")
    quartzy_lab.save(update_fields=["alerts_armed_at"])
    return quartzy_lab


@pytest.fixture
def policy(lab):
    """policy("guard.include_same_requester", True): a lab-level PolicySetting."""

    def _set(key, value):
        set_policy(key, value, lab=lab)

    return _set
