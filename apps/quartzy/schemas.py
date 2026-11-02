"""Lenient pydantic models of Quartzy's API objects (docs/research/quartzy-public-api.yaml).

Quartzy's API is unversioned, so these models bend rather than break: unknown fields are kept
(``unknown_fields`` names them, so the sync can log API drift), and every field except ``id``
is optional, because one odd record must never stop a sync (its raw payload is stored anyway).
Numbers sent where the spec says string are kept as strings. Datetimes parse to aware
datetimes (naive ones are taken as UTC); dates, uuids and URLs stay strings, and prices and
quantities stay strings for ``money.py`` to turn into Decimals.
"""

from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict

ORDER_STATUSES = ("CREATED", "CANCELLED", "APPROVED", "ORDERED", "BACKORDERED", "RECEIVED")


def _blank_to_none(value):
    return None if isinstance(value, str) and not value.strip() else value


def _assume_utc(value):
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


Timestamp = Annotated[datetime | None, BeforeValidator(_blank_to_none), AfterValidator(_assume_utc)]


class QuartzyModel(BaseModel):
    model_config = ConfigDict(extra="allow", coerce_numbers_to_str=True)


class Organization(QuartzyModel):
    id: str
    name: str | None = None


class User(QuartzyModel):
    id: str
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None


class Lab(QuartzyModel):
    id: str
    name: str | None = None
    # Temporary rollout flag; the research notes say to treat a missing value as migrated.
    uses_instances: bool | None = None
    organization: Organization | None = None


class QType(QuartzyModel):
    """A Quartzy Type. The spec makes ``id`` optional here, and a nested type without one
    must not sink the whole order request, so it stays optional."""

    id: str | None = None
    name: str | None = None


class TypeWithLab(QType):
    """An item of ``GET /types``: a top-level record, so it needs an id."""

    id: str
    lab: Lab | None = None


class Money(QuartzyModel):
    """``amount`` is a string of an integer in an undocumented unit; see money.money_amount."""

    amount: str | None = None
    currency: str | None = None


class SpendTrackingCode(QuartzyModel):
    name: str | None = None


class Location(QuartzyModel):
    name: str | None = None
    pathNames: list[str] | None = None


class InventoryInstance(QuartzyModel):
    """One physical instance of an inventory item (``InventorySubitem`` in the spec)."""

    id: str
    quantity: str | None = None
    expiration_date: str | None = None
    lot_number: str | None = None
    location: Location | None = None


class InventoryItem(QuartzyModel):
    id: str
    name: str | None = None
    vendor: str | None = None
    catalog_number: str | None = None
    price: str | None = None
    unit_size: str | None = None
    quantity: str | None = None
    url: str | None = None
    technical_details: str | None = None
    expiration_date: str | None = None  # deprecated in the spec; instances carry it now
    auto_reminder: str | None = None
    lot_number: str | None = None  # deprecated in the spec; instances carry it now
    cas_number: str | None = None
    vendor_product_id: str | None = None
    lab: Lab | None = None
    type: QType | None = None
    location: Location | None = None  # deprecated
    sublocation: Location | None = None  # deprecated
    added_by: User | None = None
    updated_by: User | None = None
    app_url: str | None = None
    serial_number: str | None = None
    instances: list[InventoryInstance] | None = None


class OrderRequest(QuartzyModel):
    id: str
    app_url: str | None = None
    status: str | None = None  # one of ORDER_STATUSES, but a new status must not fail parsing
    vendor_name: str | None = None
    catalog_number: str | None = None
    quantity: str | None = None
    unit_size: str | None = None
    unit_price: str | None = None
    total_price: Money | None = None
    requested_at: Timestamp = None
    updated_at: Timestamp = None
    requested_by: User | None = None
    created_by: User | None = None
    notes: str | None = None
    item_name: str | None = None
    invoice_number: str | None = None
    requisition_number: str | None = None
    confirmation_number: str | None = None
    tracking_number: str | None = None
    purchase_order_number: str | None = None
    shipping_and_handling: str | None = None
    details: str | None = None
    lab: Lab | None = None
    type: QType | None = None
    spend_tracking_code: SpendTrackingCode | None = None
    backordered_expected_at: str | None = None
    is_urgent: bool | None = None


def unknown_fields(model):
    """Names of fields the spec doesn't define, found anywhere in ``model``.

    Top-level extras come back bare (``"new_field"``); nested ones are dotted
    (``"requested_by.username"``, ``"instances.barcode"``).
    """
    found = set(model.model_extra or {})
    for name in type(model).model_fields:
        value = getattr(model, name)
        children = value if isinstance(value, list) else [value]
        for child in children:
            if isinstance(child, BaseModel):
                found.update(f"{name}.{field}" for field in unknown_fields(child))
    return found
