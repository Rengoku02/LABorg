"""OrderRequest rows → ``baseline.RequestRecord``, so the Phase 0 baseline can be computed
from the mirror (``guard_report``) and the replay can be compared with it (tests).

A mirrored request carries the payload Quartzy sent (``raw_payload``), which is exactly what
the baseline reads from an export, so the record is built the same way. A request the lab
logged itself (an outside purchase) has no payload: one is put together from its fields.
"""

from django.conf import settings

from apps.quartzy.models import OrderRequest

from . import baseline


def request_record(order_request):
    """The baseline's view of one request."""
    raw = order_request.raw_payload or _payload(order_request)
    if not raw.get("id"):
        raw = {**raw, "id": order_request.quartzy_id or f"labstock-{order_request.pk}"}
    unit = getattr(settings, "QUARTZY_MONEY_UNIT", "auto") or "auto"
    return baseline._record(raw, unit, {}, {})  # the same reader as load_requests


def lab_records(lab):
    """Records of the lab's live requests, as ``baseline.find_clusters`` takes them."""
    rows = OrderRequest.objects.for_lab(lab).live().order_by("pk")
    return [request_record(row) for row in rows.iterator()]


def _payload(order_request):
    def person(user):
        if user is None:
            return None
        first, _, last = (user.full_name or "").partition(" ")
        return {
            "id": f"user-{user.pk}",
            "first_name": first,
            "last_name": last,
            "email": user.email,
        }

    return {
        "id": order_request.quartzy_id or f"labstock-{order_request.pk}",
        "status": order_request.status,
        "item_name": order_request.item_name,
        "vendor_name": order_request.vendor_name_raw,
        "catalog_number": order_request.catalog_number_raw,
        "quantity": str(order_request.quantity) if order_request.quantity is not None else None,
        "unit_price": (
            str(order_request.unit_price) if order_request.unit_price is not None else None
        ),
        "total_price": None,
        "requested_at": (
            order_request.requested_at.isoformat() if order_request.requested_at else None
        ),
        "requested_by": person(order_request.requested_by),
        "created_by": None,
        "app_url": order_request.app_url or None,
    }
