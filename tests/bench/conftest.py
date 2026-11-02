"""Fixtures for the bench tests: the inventory's places and people, item types, and
``bought`` (a past Quartzy purchase, as the mirror holds it) for claims."""

import itertools
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.catalog.models import ItemType
from apps.catalog.services import resolve_supplier_product
from apps.inventory import services
from apps.quartzy.models import OrderRequest, OrderStatusObservation, QuartzyLab, QuartzyType
from tests.catalog.conftest import item_types  # noqa: F401  (fixture)
from tests.inventory.conftest import (  # noqa: F401  (fixtures)
    box,
    freezer,
    leaver,
    location_types,
    make_location,
    other_lab,
    outsider,
    pool,
    raj,
    room,
    shelf,
)

_ids = itertools.count(1)


@pytest.fixture
def qlab(lab):
    return QuartzyLab.objects.create(lab=lab, quartzy_id="ql-1", name="Smith Lab")


@pytest.fixture
def qtypes(qlab, item_types):  # noqa: F811
    return {
        key: QuartzyType.objects.create(
            quartzy_lab=qlab,
            quartzy_id=f"qt-{key}",
            name=name,
            item_type=ItemType.objects.get(key=key),
        )
        for key, name in (("antibody", "Antibodies"), ("consumable", "Lab Supplies"))
    }


@pytest.fixture
def bought(lab, qlab, qtypes, member):
    """bought(days_ago=30, status="RECEIVED", received_days_ago=None, by=member, kind=...)"""

    def _bought(
        *,
        days_ago=30,
        status="RECEIVED",
        received_days_ago=None,
        by=None,
        kind="antibody",
        name="GAPDH (14C10) Rabbit mAb",
        vendor="CST",
        catalog="2118S",
        total="289.00",
        requested_for=None,
    ):
        resolution = resolve_supplier_product(
            vendor_name=vendor, catalog_number=catalog, item_name=name, item_type=kind
        )
        row = OrderRequest.objects.create(
            lab=lab,
            quartzy_lab=qlab,
            quartzy_id=f"q-{next(_ids)}",
            status=status,
            item_name=name,
            vendor_name_raw=vendor,
            catalog_number_raw=catalog,
            total_price=total,
            currency="USD",
            quartzy_type=qtypes[kind],
            requested_by=by or member,
            requested_for=requested_for,
            requested_at=timezone.now() - timedelta(days=days_ago),
            supplier_product=resolution.supplier_product,
            product=resolution.product,
            resolution_status=resolution.status,
        )
        if received_days_ago is not None:
            OrderStatusObservation.objects.create(
                order_request=row,
                from_status="ORDERED",
                to_status="RECEIVED",
                observed_at=timezone.now() - timedelta(days=received_days_ago),
                via=OrderStatusObservation.Via.POLL,
            )
        return row

    return _bought


@pytest.fixture
def make_item(lab, member, box, item_types):  # noqa: F811
    """make_item(custodian=raj, visibility="restricted", catalog="2118S") -> a StockItem."""

    def _make(*, custodian=None, catalog="2118S", name="GAPDH (14C10) Rabbit mAb", **fields):
        resolution = resolve_supplier_product(
            vendor_name="CST", catalog_number=catalog, item_name=name, item_type="antibody"
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

    return _make
