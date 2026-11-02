"""Getting stock in from a desk (docs/design/mvp-a.md §4): "Add what I have" and "Register a
box". Stock is only ever written through ``services``.

- ``add_what_i_have(...)``: stock someone already holds (from a previous lab, a collaborator,
  made in the lab, a gift, or bought outside Quartzy). An outside purchase is also recorded
  as ``OrderRequest(source="outside", status=RECEIVED)``, so the guard sees it as bought.
- ``product_typeahead(lab, q)``: products for the add form, the lab's Quartzy history first.
- ``register_box(...)``: a box (or bag) LabStock should know about before it's itemized, with
  an optional photo of its lid. Photos are served only through a login-checked view.
"""

import uuid
from dataclasses import dataclass

from django.conf import settings
from django.contrib.postgres.search import TrigramSimilarity
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.catalog.models import Product
from apps.catalog.normalization import normalize_catalog
from apps.catalog.services import canonical, get_or_create_lot, resolve_supplier_product
from apps.quartzy.models import OrderRequest

from . import services
from .models import Lifecycle, Source, Visibility

INTAKE_SOURCES = (
    Source.PREVIOUS_LAB,
    Source.COLLABORATOR,
    Source.LAB_MADE,
    Source.GIFT,
    Source.OUTSIDE_PURCHASE,
)
MAX_COUNT = 50
_PHOTO_MAGIC = (
    (b"\xff\xd8\xff", "JPEG"),
    (b"\x89PNG\r\n\x1a\n", "PNG"),
)
_HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1", b"heim", b"heis"}


@dataclass(frozen=True)
class IntakeResult:
    items: list
    order_request: OrderRequest | None


def add_what_i_have(
    *,
    lab,
    person,
    user,
    source,
    product=None,
    name="",
    vendor="",
    catalog_number="",
    item_type=None,
    count=1,
    location=None,
    temperature="",
    visibility=None,
    lot="",
    expiry=None,
    expiry_precision="day",
    notes="",
    mta=False,
    client_uuid=None,
):
    """Record stock ``person`` holds; returns an IntakeResult. ``product`` (from the
    typeahead) or ``name`` with vendor and catalog # says what it is. A repeated
    ``client_uuid`` (a form sent twice) records nothing new."""
    if source not in INTAKE_SOURCES:
        raise services.InventoryError("Say where it came from.")
    if not 1 <= count <= MAX_COUNT:
        raise services.InventoryError(f"Add between 1 and {MAX_COUNT} at a time.")
    with transaction.atomic():
        supplier_product = None
        if product is None:
            if not (name or catalog_number):
                raise services.InventoryError("Say what it is: a name or a catalog #.")
            resolution = resolve_supplier_product(
                vendor_name=vendor,
                catalog_number=catalog_number,
                item_name=name,
                item_type=item_type,
            )
            product, supplier_product = resolution.product, resolution.supplier_product
            status = resolution.status
        else:
            product, status = canonical(product), OrderRequest.Resolution.EXACT
        lot_row = get_or_create_lot(product, lot, expiry=expiry, expiry_precision=expiry_precision)
        purchase = None
        if source == Source.OUTSIDE_PURCHASE:
            purchase = OrderRequest.objects.create(
                lab=lab,
                source=OrderRequest.Source.OUTSIDE,
                status=OrderRequest.Status.RECEIVED,
                item_name=(name or product.name)[:500],
                vendor_name_raw=vendor[:300],
                catalog_number_raw=catalog_number[:200],
                requested_by=person,
                requested_at=timezone.now(),
                supplier_product=supplier_product,
                product=product,
                resolution_status=status,
                raw_payload={"intake": str(client_uuid or "")},
            )
        if source == Source.COLLABORATOR and mta:
            visibility = Visibility.RESTRICTED
        base = uuid.UUID(str(client_uuid)) if client_uuid else uuid.uuid4()
        items = [
            services.create_stock_item(
                lab=lab,
                product=product,
                supplier_product=supplier_product,
                lot=lot_row,
                custodian=person,
                user=user,
                source=source,
                location=location,
                temperature=temperature if location is None else None,
                visibility=visibility,
                lifecycle=Lifecycle.AVAILABLE,
                expiry_date=expiry if lot_row is None else None,
                expiry_precision=expiry_precision,
                notes=notes,
                mta=mta,
                source_order_request=purchase,
                event_source="web",
                reason="Added from the desk",
                client_uuid=uuid.uuid5(base, str(number)),
            )
            for number in range(count)
        ]
    return IntakeResult(items, purchase)


def product_typeahead(lab, q, *, limit=10):
    """[(product id, label)] for what someone types: what the lab ordered before first (by
    name or catalog #), then the catalog (similar names)."""
    q = " ".join((q or "").split())[:100]
    if len(q) < 2:
        return []
    key = normalize_catalog(q).key
    by_number = Q(catalog_number_raw__iexact=q)
    if key:
        by_number |= Q(supplier_product__catalog_key=key)
    history = (
        OrderRequest.objects.for_lab(lab)
        .live()
        .filter(product__isnull=False)
        .filter(Q(item_name__icontains=q) | by_number)
        .select_related("product", "supplier_product__vendor")
        .order_by("-requested_at")
    )
    seen, found = set(), []
    for row in history[: limit * 3]:
        product = canonical(row.product)
        if product.pk in seen:
            continue
        seen.add(product.pk)
        listing = row.supplier_product
        label = product.name
        if listing is not None and listing.catalog_raw:
            label = f"{product.name} — {listing.vendor} {listing.catalog_raw}"
        found.append((product.pk, label))
        if len(found) >= limit:
            return found
    catalog = (
        Product.objects.canonical()
        .exclude(pk__in=seen)
        .annotate(similarity=TrigramSimilarity("name", q))
        .filter(Q(similarity__gte=0.3) | Q(name__icontains=q))
        .order_by("-similarity", "name")
    )
    for product in catalog[: limit - len(found)]:
        found.append((product.pk, product.name))
    return found


def check_photo(upload):
    """None if ``upload`` is a photo LabStock accepts (JPEG, PNG, WebP or HEIC, at most
    LABSTOCK_MAX_PHOTO_BYTES), else what's wrong with it."""
    if upload is None:
        return None
    limit = settings.LABSTOCK_MAX_PHOTO_BYTES
    if upload.size > limit:
        return f"The photo is too large (at most {limit // (1024 * 1024)} MB)."
    head = upload.read(16)
    upload.seek(0)
    if any(head.startswith(magic) for magic, _name in _PHOTO_MAGIC):
        return None
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return None
    if head[4:8] == b"ftyp" and head[8:12] in _HEIF_BRANDS:
        return None
    return "That isn't a photo LabStock can keep (use JPEG, PNG, WebP or HEIC)."


def register_box(
    *, lab, owner, parent, name, user, kind="box", tags=(), photo=None, visibility=None, notes=""
):
    """A box (or bag) on ``parent`` that belongs to ``owner``: searchable before anything
    in it is itemized. Returns the Location."""
    problem = check_photo(photo)
    if problem:
        raise services.InventoryError(problem)
    return services.create_location(
        lab=lab,
        parent=parent,
        name=name,
        location_type=kind,
        user=user,
        default_custodian=owner,
        visibility=visibility,
        tags=tags,
        notes=notes,
        photo=photo,
    )
