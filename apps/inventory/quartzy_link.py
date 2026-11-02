"""Quartzy inventory → LabStock stock (design §3 "link_quartzy_inventory").

Until a lab's cutover, its Quartzy inventory is mirrored raw (``QuartzyInventoryItem`` and
its instances). Linking turns each live instance (or each item, when the Quartzy lab doesn't
use instances) into unverified stock with an ExternalRef back to it:

- a whole-number quantity up to ``inventory.quartzy_split_max`` (20) gives one stock item
  per container; a larger one gives one aggregate ("N containers, split later"); anything
  else (a volume, no quantity) gives one item, with the quantity in its notes;
- the custodian is whoever added it in Quartzy, if they may hold the lab's stock, else the
  lab's pool user;
- the location comes from Quartzy's location path when LabStock has that path, else the
  custodian's unsorted container (temperature unknown).

Idempotent: an instance with an ExternalRef is skipped, and each item's client_uuid comes
from the instance, so even a concurrent run creates nothing twice.
"""

import uuid
from dataclasses import dataclass, field

from apps.catalog.services import get_or_create_lot, resolve_supplier_product
from apps.core.people import resolve_person
from apps.core.policies import get_policy
from apps.quartzy.models import QuartzyInventoryItem, QuartzyType
from apps.quartzy.money import parse_quantity

from . import services
from .importers import import_run
from .models import ExternalRef, Lifecycle, Source
from .selectors import location_by_path

_NAMESPACE = uuid.UUID("7c1d4b9e-3a2f-4e8d-9b6a-5f4e3d2c1b0a")
INSTANCE = "quartzy_instance"
ITEM = "quartzy_item"


@dataclass
class LinkReport:
    linked: list = field(default_factory=list)  # (external id, name, short ids)
    skipped: int = 0  # already linked
    committed: bool = False

    @property
    def items(self):
        return sum(len(ids) for _external, _name, ids in self.linked)


def link_quartzy_inventory(lab, *, commit=False, user=None):
    """Link the lab's mirrored Quartzy inventory; return a LinkReport. Unless ``commit``,
    everything runs and is rolled back."""
    report = LinkReport()
    split_max = get_policy("inventory.quartzy_split_max", lab)
    pool = lab.pool_user
    with import_run(commit=commit) as errors:
        items = (
            QuartzyInventoryItem.objects.for_lab(lab)
            .live()
            .select_related("quartzy_lab")
            .prefetch_related("instances")
            .order_by("pk")
        )
        for item in items:
            try:
                _link_item(item, lab, pool, split_max, user, report)
            except services.InventoryError as exc:
                errors.append(f"{item} [{item.quartzy_id}]: {exc}")
    report.committed = commit
    return report


def _link_item(item, lab, pool, split_max, user, report):
    if item.quartzy_lab.instances_enabled:
        units = [
            (INSTANCE, instance.quartzy_id, instance.quantity_raw, instance)
            for instance in item.instances.all()
            if instance.deleted_in_quartzy_at is None
        ]
    else:
        units = [(ITEM, item.quartzy_id, item.quantity_raw, None)]
    pending = [
        unit
        for unit in units
        if not ExternalRef.objects.filter(system=unit[0], external_id=unit[1]).exists()
    ]
    report.skipped += len(units) - len(pending)
    if not pending:
        return
    payload = item.raw_payload or {}
    qtype = _item_type(item, payload)
    resolution = resolve_supplier_product(
        vendor_name=item.vendor_name,
        catalog_number=item.catalog_number,
        item_name=item.name,
        item_type=qtype,
        unit_size=item.unit_size_raw,
    )
    custodian = _custodian(payload.get("added_by"), lab, pool)
    for system, external_id, quantity_raw, instance in pending:
        location = location_by_path(lab, instance.location_path) if instance else None
        lot = None
        expiry = instance.expiration_date if instance else None
        if instance is not None and instance.lot_number:
            lot = get_or_create_lot(resolution.product, instance.lot_number, expiry=expiry)
        count = _whole(quantity_raw)
        aggregate = count is not None and count > split_max
        made = 1 if count is None or aggregate else count
        notes = "" if count is not None else f"Quartzy quantity: {quantity_raw or 'not given'}"
        short_ids = []
        for number in range(made):
            stock = services.create_stock_item(
                lab=lab,
                product=resolution.product,
                supplier_product=resolution.supplier_product,
                lot=lot,
                custodian=custodian,
                user=user,
                source=Source.QUARTZY_INVENTORY,
                location=location,
                temperature="" if location is None else None,
                lifecycle=Lifecycle.UNVERIFIED,
                expiry_date=expiry if lot is None else None,
                is_aggregate=aggregate,
                container_count=count if aggregate else None,
                notes=notes,
                event_source="sync",
                reason=f"Quartzy inventory {external_id}",
                client_uuid=uuid.uuid5(_NAMESPACE, f"{lab.pk}:{system}:{external_id}:{number}"),
            )
            ExternalRef.objects.get_or_create(
                stock_item=stock, system=system, external_id=external_id
            )
            short_ids.append(stock.short_id)
        report.linked.append((external_id, item.name or item.quartzy_id, short_ids))


def _item_type(item, payload):
    qtype = payload.get("type") or {}
    if not qtype.get("id"):
        return None
    row = QuartzyType.objects.filter(
        quartzy_lab=item.quartzy_lab, quartzy_id=qtype["id"]
    ).select_related("item_type")
    found = row.first()
    return found.item_type if found else None


def _custodian(added_by, lab, pool):
    alias = resolve_person(added_by) if isinstance(added_by, dict) else None
    person = alias.user if alias is not None else None
    if person is not None and services.may_hold_stock(person, lab):
        return person
    return pool


def _whole(quantity_raw):
    """A count of containers: a whole number of at least 1, else None."""
    quantity = parse_quantity(quantity_raw)
    if quantity is None or quantity < 1 or quantity != quantity.to_integral_value():
        return None
    return int(quantity)
