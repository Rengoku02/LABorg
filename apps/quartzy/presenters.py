"""How people see order requests: ``present_requests`` → RequestViews.

Pages, emails, digests and CSVs show requests only through these views, as they show stock
only through ``apps.inventory.presenters`` (the template scan forbids reading ``quantity``
or ``notes`` from anything but a ``_view`` name).

Who may see a request: the current members of its lab, and its co-owners and system admins.
Quartzy shows a lab's requests to all its members too, so nothing is redacted for them.
PI-facing text passes ``scrub`` (an ``apps.guard.anonymize.NameScrubber``): requester names
are dropped and item text is scrubbed of names, emails and ids (plan §11).
"""

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal

from django.utils import timezone

from apps.core.permissions import is_co_owner, is_member

from .models import OrderRequest

CHANNELS = ("page", "email", "digest", "csv", "partial")
_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥"}
RELATED = (
    "requested_by",
    "requested_for",
    "requested_by_alias__user",
    "created_by_alias__user",
    "supplier_product__vendor",
    "product",
    "quartzy_type",
)


@dataclass(frozen=True)
class RequestView:
    channel: str
    pk: int
    lab_id: int
    quartzy_id: str
    source: str
    source_label: str
    status: str
    status_label: str
    is_open: bool
    deleted: bool
    item_name: str
    vendor: str
    catalog_number: str
    unit_size: str
    quantity: Decimal | None
    quantity_text: str
    unit_price: Decimal | None
    total_price: Decimal | None
    currency: str
    # What the request is worth: its total, else unit price times quantity (design §8).
    value: Decimal | None
    value_text: str
    requested_at: datetime | None
    age_days: int | None
    requester_id: int | None
    requester_name: str
    viewer_is_requester: bool
    requested_for_name: str
    product_id: int | None
    product_name: str
    type_name: str
    app_url: str
    notes: str

    def as_json(self):
        """A JSON-able snapshot (e.g. ``DuplicateMatch.candidate_snapshot``)."""
        data = asdict(self)
        for key, value in data.items():
            if isinstance(value, Decimal):
                data[key] = str(value)
            elif isinstance(value, datetime):
                data[key] = value.isoformat()
        return data


def with_related(queryset):
    """``queryset`` with the related rows a RequestView reads."""
    return queryset.select_related(*RELATED)


def money_text(amount, currency):
    """``Decimal("1234.5"), "USD"`` → "$1,234.50"; other currencies get their code."""
    if amount is None:
        return ""
    number = f"{amount:,.2f}"
    symbol = _SYMBOLS.get((currency or "").upper())
    if symbol:
        return f"-{symbol}{number[1:]}" if number.startswith("-") else f"{symbol}{number}"
    return f"{number} {currency}".strip()


def request_value(order_request):
    """The request's total, else unit price times quantity, else None."""
    if order_request.total_price is not None:
        return order_request.total_price
    if order_request.unit_price is not None and order_request.quantity is not None:
        return (order_request.unit_price * order_request.quantity).quantize(Decimal("0.0001"))
    return None


def requester_name(order_request):
    """Who asked for it, as people know them: the user's name, else what Quartzy sent."""
    if order_request.requested_by_id is not None:
        return str(order_request.requested_by)
    alias = order_request.requester_alias
    if alias is None:
        return ""
    if alias.user_id is not None:
        return str(alias.user)
    return alias.display_name or alias.value


def present_request(order_request, viewer, *, channel, scrub=None, now=None):
    views = present_requests([order_request], viewer, channel=channel, scrub=scrub, now=now)
    return views[0] if views else None


def present_requests(rows, viewer, *, channel, scrub=None, now=None):
    """RequestViews of the requests ``viewer`` may see, in order (pass ``with_related``
    rows). ``viewer=None`` with ``scrub`` builds lab-level views for system mail (the PI
    summary), which never name a requester."""
    if channel not in CHANNELS:
        raise ValueError(f"Unknown channel {channel!r}; use one of {', '.join(CHANNELS)}.")
    now = now or timezone.now()
    access = {}
    views = []
    for row in rows:
        if row.lab_id not in access:
            access[row.lab_id] = _may_see(viewer, row.lab, scrub)
        if access[row.lab_id]:
            views.append(_view(row, viewer, channel, scrub, now))
    return views


def _may_see(viewer, lab, scrub):
    if viewer is None:
        return scrub is not None  # lab totals for the PI: names scrubbed
    return bool(getattr(viewer, "is_authenticated", False)) and (
        is_member(viewer, lab) or is_co_owner(viewer, lab)
    )


def _view(row, viewer, channel, scrub, now):
    value = request_value(row)
    supplier_product = row.supplier_product
    vendor = str(supplier_product.vendor) if supplier_product else row.vendor_name_raw
    item_name = row.item_name or (supplier_product.product.name if supplier_product else "")
    notes = row.notes
    who, for_whom = requester_name(row), str(row.requested_for or "")
    if scrub is not None:
        item_name, notes, who, for_whom = scrub(item_name), "", "", ""
    age = (now - row.requested_at).days if row.requested_at else None
    viewer_id = getattr(viewer, "pk", None)
    return RequestView(
        channel=channel,
        pk=row.pk,
        lab_id=row.lab_id,
        quartzy_id=row.quartzy_id or "",
        source=row.source,
        source_label=row.get_source_display(),
        status=row.status,
        status_label=_status_label(row.status),
        is_open=row.is_open,
        deleted=row.deleted_in_quartzy_at is not None,
        item_name=item_name,
        vendor=vendor,
        catalog_number=row.catalog_number_raw,
        unit_size=row.unit_size_raw,
        quantity=row.quantity,
        quantity_text=_number(row.quantity),
        unit_price=row.unit_price,
        total_price=row.total_price,
        currency=row.currency,
        value=value,
        value_text=money_text(value, row.currency),
        requested_at=row.requested_at,
        age_days=age,
        requester_id=None if scrub is not None else row.requested_by_id,
        requester_name=who,
        viewer_is_requester=viewer_id is not None and viewer_id == row.requested_by_id,
        requested_for_name=for_whom,
        product_id=row.product_id,
        product_name=scrub(row.product.name) if scrub and row.product else _name(row.product),
        type_name=row.quartzy_type.name if row.quartzy_type_id else "",
        app_url=row.app_url,
        notes=notes,
    )


def _status_label(status):
    try:
        return OrderRequest.Status(status).label
    except ValueError:
        return status.title()  # a status Quartzy added since


def _number(value):
    return "" if value is None else format(value.normalize(), "f")


def _name(product):
    return product.name if product is not None else ""
