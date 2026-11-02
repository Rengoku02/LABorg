"""Product identity: turning a Quartzy request line into a product, and cleaning up after.

The Quartzy sync calls:
- ``resolve_vendor(raw_name)`` -> Vendor
- ``suggest_item_type(type_name)`` -> ItemType or None
- ``resolve_supplier_product(...)`` -> Resolution
- ``get_or_create_lot(product, lot_raw)`` -> Lot or None
- ``retire_orphans(supplier_products, replacement=...)`` after it re-resolves a request

The review queue (the admin) calls ``merge_products``, ``merge_vendors``,
``merge_vendor_families``, ``move_vendor`` and ``refresh_catalog_keys``.

Merges never delete a product, vendor or family: the merged row stays as a redirect
(``merged_into``), and every lookup here follows redirects (``canonical``), so ids stored
elsewhere keep working. A redirect is deleted only with the product it leads to, when
``retire_orphans`` deletes that one. Rows created here are provisional until someone reviews
them.
"""

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.db.models import Q, Value
from django.db.models.functions import Replace
from django.utils.text import slugify
from simple_history.utils import bulk_update_with_history

from .models import (
    GenericCatalogKey,
    ItemType,
    LabProduct,
    Lot,
    Product,
    ProductCode,
    ResolutionStatus,
    SupplierProduct,
    Vendor,
    VendorAlias,
    VendorFamily,
    seed_snapshot,
)
from .normalization import (
    UNKNOWN_FAMILY,
    is_placeholder_vendor,
    load_vendor_table,
    normalize_catalog,
    normalize_lot,
    normalize_vendor_name,
    vendor_family_for,
)

UNKNOWN_FAMILY_SLUG = UNKNOWN_FAMILY  # an older name for it
UNKNOWN_VENDOR_NAME = "Unknown vendor"
MAX_REDIRECT_HOPS = 25


class MergeError(Exception):
    """A merge that must not happen (into itself, a loop, a stale redirect...)."""


Status = ResolutionStatus


@dataclass(frozen=True)
class Resolution:
    """``created`` is True when this call created the supplier product (every status but
    "exact"); a new product exists only for "new", "generic" and "no_catalog". ``status`` is
    a ``ResolutionStatus`` (a str: "exact", "base_variant", "new", "generic", "no_catalog")."""

    supplier_product: SupplierProduct
    product: Product
    created: bool
    status: ResolutionStatus


# ---------------------------------------------------------------------------------------
# Redirects


def canonical(obj):
    """Follow ``merged_into`` to the row that is not a redirect (Product, Vendor or
    VendorFamily); ``obj`` itself when it isn't merged. The redirect is read from the
    database, so a copy loaded before a merge still finds its target."""
    if obj is None or obj.pk is None:
        return obj
    manager = type(obj)._base_manager
    next_pk = manager.filter(pk=obj.pk).values_list("merged_into_id", flat=True).first()
    seen = [obj.pk]
    while next_pk is not None:
        if next_pk in seen or len(seen) > MAX_REDIRECT_HOPS:
            raise MergeError(f"Redirect loop at {obj._meta.verbose_name} #{next_pk}")
        seen.append(next_pk)
        obj = manager.get(pk=next_pk)
        next_pk = obj.merged_into_id
    return obj


# ---------------------------------------------------------------------------------------
# Vendors


def family_seed_values(info):
    """VendorFamily field values for a vendors.yaml family (a ``VendorFamilyInfo``)."""
    return {
        "name": info.name,
        "is_distributor": info.distributor,
        "catalog_rule": info.rule or "",
        "provisional": False,
    }


def vendor_seed_values(family):
    return {"family": family, "provisional": False}


def resolve_vendor(raw_name):
    """The Vendor for a vendor name as typed into Quartzy; never None.

    Looks the name up as an alias (``normalize_vendor_name``, then the same key without
    spaces). A name vendors.yaml knows but the database doesn't (seed_catalog not run yet,
    or the YAML is newer) is created from the YAML. Any other name gets a provisional family,
    vendor and alias for the review queue, unless it joins aliases of one known family
    ("Thermo Fisher (Gibco)"), which puts the provisional vendor in that family. A blank or
    placeholder name ("N/A", "unknown") returns the shared provisional "Unknown vendor", in
    the family ``vendor_family_for`` gives such names (``UNKNOWN_FAMILY``).
    """
    name = _clip(raw_name, 200)
    if is_placeholder_vendor(name):
        return unknown_vendor()
    key = normalize_vendor_name(name)[:200]
    alias = _find_alias(key)
    if alias is None:
        vendor = _vendor_for_new_alias(name, key)
        alias, _ = VendorAlias.objects.get_or_create(
            alias_key=key, defaults={"vendor": vendor, "raw_example": name}
        )
    return canonical(alias.vendor)


def unknown_vendor():
    """The one provisional vendor (in its own family) for requests that name no vendor."""
    family, _ = VendorFamily.objects.get_or_create(
        slug=UNKNOWN_FAMILY_SLUG, defaults={"name": UNKNOWN_VENDOR_NAME, "provisional": True}
    )
    vendor, _ = Vendor.objects.get_or_create(
        name=UNKNOWN_VENDOR_NAME, defaults={"family": canonical(family), "provisional": True}
    )
    return canonical(vendor)


def _find_alias(key):
    compact = key.replace(" ", "")
    aliases = (
        VendorAlias.objects.annotate(compact=Replace("alias_key", Value(" "), Value("")))
        .filter(Q(alias_key=key) | Q(compact=compact))
        .select_related("vendor")
        .order_by("pk")
    )
    found = list(aliases)
    exact = [alias for alias in found if alias.alias_key == key]
    return (exact or found or [None])[0]


def _vendor_for_new_alias(name, key):
    table = load_vendor_table()
    slug = table.lookup(key)
    if slug is not None:
        vendor_name = table.alias_to_vendor_name.get(key) or table.alias_to_vendor_name.get(
            key.replace(" ", "")
        )
        return _curated_vendor(vendor_name, _curated_family(slug))
    existing = Vendor.objects.filter(name__iexact=name).order_by("pk").first()
    if existing is not None:
        return canonical(existing)
    slug = vendor_family_for(name)
    family = _curated_family(slug) if slug else _provisional_family(name)
    vendor, _ = Vendor.objects.get_or_create(
        name=name, defaults={"family": family, "provisional": True}
    )
    return canonical(vendor)


def _curated_family(slug):
    family = VendorFamily.objects.filter(slug=slug).first()
    if family is None:
        values = family_seed_values(load_vendor_table().families[slug])
        family, _ = VendorFamily.objects.get_or_create(
            slug=slug, defaults={**values, "seeded": seed_snapshot(values)}
        )
    return canonical(family)


def _curated_vendor(name, family):
    vendor = Vendor.objects.filter(name__iexact=name).order_by("pk").first()
    if vendor is None:
        values = vendor_seed_values(family)
        vendor, _ = Vendor.objects.get_or_create(
            name=name, defaults={**values, "seeded": seed_snapshot(values)}
        )
    return canonical(vendor)


def _provisional_family(name):
    base = slugify(name)[:50].strip("-") or "vendor"
    curated = load_vendor_table().families
    for n in range(1, 100):
        slug = base if n == 1 else f"{base}-{n}"
        if slug in curated or slug == UNKNOWN_FAMILY_SLUG:
            continue  # keep curated slugs free for seed_catalog
        family, created = VendorFamily.objects.get_or_create(
            slug=slug, defaults={"name": name, "provisional": True}
        )
        if created or (family.provisional and family.name == name):
            return canonical(family)
    raise RuntimeError(f"No free vendor family slug for {name!r}")


# ---------------------------------------------------------------------------------------
# Item types


def _words(text):
    text = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return " ".join(re.sub(r"[^0-9a-z]+", " ", text).split())


def suggest_item_type(type_name):
    """The ItemType whose ``quartzy_type_keywords`` best match a Quartzy Type name, or None.

    A keyword matches at the start of a word, ignoring case and punctuation ("tip" matches
    "Pipette Tips" but not "Multiple"); the longest matching keyword wins, then sort order.
    """
    text = _words(type_name)
    if not text:
        return None
    best, best_length = None, 0
    for item_type in ItemType.objects.all():
        for keyword in item_type.quartzy_type_keywords or []:
            word = _words(keyword)
            if len(word) > best_length and re.search(rf"(?<![0-9a-z]){re.escape(word)}", text):
                best, best_length = item_type, len(word)
    return best


# ---------------------------------------------------------------------------------------
# Supplier products


def _clip(value, length):
    """``value`` as stored: one line, at most ``length`` characters, and no NUL characters
    (Postgres text can't hold them)."""
    return " ".join(str(value or "").replace("\x00", "").split())[:length]


def _price(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except InvalidOperation:
        return None
    if not number.is_finite() or abs(number) >= Decimal("1e10"):
        return None
    return number.quantize(Decimal("0.0001"))


def _is_generic(family, key, item_type):
    if item_type is not None and item_type.identity_rule == ItemType.IdentityRule.NONE:
        return True
    return GenericCatalogKey.objects.filter(
        Q(vendor_family=family) | Q(vendor_family__isnull=True), catalog_key=key
    ).exists()


def _identifying(family, **lookup):
    return (
        SupplierProduct.objects.filter(
            vendor_family=family, identity=SupplierProduct.Identity.IDENTIFYING, **lookup
        )
        .select_related("product")
        .order_by("pk")
    )


def resolve_supplier_product(
    *,
    vendor_name,
    catalog_number,
    item_name,
    item_type=None,
    unit_size=None,
    unit_price=None,
    currency=None,
):
    """Find or create the supplier product (and product) for one request line.

    In order:
    - no usable catalog # (blank, placeholder, Excel-mangled, or too long to be one, like a
      pasted URL) -> "no_catalog": a new provisional product and a supplier product with a
      NULL catalog key.
    - ``item_type`` has identity_rule "none", or the key is a GenericCatalogKey of this family
      (or of every family) -> "generic": a new provisional product and supplier product each
      time; they are never grouped.
    - an identifying supplier product with the same (vendor family, catalog key) -> "exact";
      the unit size, price and currency given are recorded on it.
    - one with the same catalog base (another pack size) -> "base_variant": a new supplier
      product for that product.
    - otherwise -> "new": a new provisional product named after ``item_name`` (or the
      catalog #) and its supplier product.

    ``item_type`` is an ItemType or its key. Concurrent calls are safe: the unique (vendor
    family, catalog key) constraint decides, and the loser re-reads the winner's row. Every
    call except "exact" creates rows, so call this once per request line and store the result.
    """
    if isinstance(item_type, str):
        item_type = ItemType.objects.filter(key=item_type).first()
    vendor = resolve_vendor(vendor_name)
    family = canonical(vendor.family)
    catalog_raw = _clip(catalog_number, 200)
    # The stored text is what SupplierProduct.save() derives keys from, so normalize that.
    norm = normalize_catalog(catalog_raw or None, rule=family.pack_rule)
    pack = {
        "unit_size_raw": _clip(unit_size, 100),
        "last_unit_price": _price(unit_price),
        "currency": _clip(currency, 3).upper(),
    }
    fields = {"vendor": vendor, "catalog_raw": catalog_raw, "provisional": True, **pack}
    name = _clip(item_name, 500) or _default_name(vendor, family, norm)

    if norm.key is None:
        return _create(name, item_type, fields, Status.NO_CATALOG)
    if _is_generic(family, norm.key, item_type):
        identity = SupplierProduct.Identity.GENERIC
        return _create(name, item_type, {**fields, "identity": identity}, Status.GENERIC)

    for _attempt in range(3):
        supplier_product = _identifying(family, catalog_key=norm.key).first()
        if supplier_product is not None:
            return _exact(supplier_product, pack, item_type)
        sibling = _identifying(family, catalog_base=norm.base).exclude(catalog_key=norm.key).first()
        try:
            if sibling is not None:
                product = canonical(sibling.product)
                with transaction.atomic():
                    supplier_product = SupplierProduct.objects.create(product=product, **fields)
                return Resolution(supplier_product, product, True, Status.BASE_VARIANT)
            return _create(name, item_type, fields, Status.NEW)
        except IntegrityError:
            continue  # another request created this (family, key) first; it is exact now
    raise RuntimeError(f"Could not resolve {vendor} {catalog_number!r}")


def _default_name(vendor, family, norm):
    label = "" if family.slug == UNKNOWN_FAMILY_SLUG else vendor.name
    if norm.key is not None:
        return _clip(f"{label} {norm.cleaned}", 500)
    return _clip(f"Unnamed {label} product", 500)


def _create(name, item_type, fields, status):
    with transaction.atomic():
        product = Product.objects.create(name=name, item_type=item_type, provisional=True)
        supplier_product = SupplierProduct.objects.create(product=product, **fields)
    return Resolution(supplier_product, product, True, status)


def _exact(supplier_product, pack, item_type):
    changed = [
        field
        for field, value in pack.items()
        if value not in (None, "") and getattr(supplier_product, field) != value
    ]
    for field in changed:
        setattr(supplier_product, field, pack[field])
    product = canonical(supplier_product.product)
    if product.pk != supplier_product.product_id:
        # Created for a product while it was being merged away; point it at the survivor.
        supplier_product.product = product
        changed.append("product")
    if changed:
        supplier_product.save(update_fields=changed)
    if item_type is not None and product.item_type_id is None:
        product.item_type = item_type
        product.save(update_fields=["item_type", "updated_at"])
    return Resolution(supplier_product, product, False, Status.EXACT)


def get_or_create_lot(product, lot_raw, *, expiry=None, expiry_precision=Lot.ExpiryPrecision.DAY):
    """The Lot of ``product`` (after redirects) for a lot number as typed, or None when the
    lot number is blank or a placeholder. A known lot gets the expiry if it had none."""
    key = normalize_lot(lot_raw)
    if key is None:
        return None
    product = canonical(product)
    lot, created = Lot.objects.get_or_create(
        product=product,
        lot_key=key,
        defaults={
            "lot_raw": _clip(lot_raw, 200),
            "expiry": expiry,
            "expiry_precision": expiry_precision,
        },
    )
    if not created and expiry is not None and lot.expiry is None:
        lot.expiry, lot.expiry_precision = expiry, expiry_precision
        lot.save(update_fields=["expiry", "expiry_precision"])
    return lot


# ---------------------------------------------------------------------------------------
# Merging


def _has_history(model):
    return hasattr(model._meta, "simple_history_manager_attribute")


def _save(obj, *, user=None, reason="", **kwargs):
    if user is not None:
        obj._history_user = user
    if reason:
        obj._change_reason = reason[:100]
    obj.save(**kwargs)


def _delete(obj, *, user=None, reason=""):
    if user is not None:
        obj._history_user = user
    if reason:
        obj._change_reason = reason[:100]
    obj.delete()


def _repoint(model, src, dst, *, user=None, reason="", skip=()):
    """Point every relation that references ``src`` at ``dst``.

    Relations come from ``model._meta.related_objects``, so foreign keys that other apps add
    later (stock items, order requests, duplicate matches...) are covered without changes
    here. Rows of history-tracked models get a history record. ``skip`` holds
    ``(related_model, field_name)`` pairs the caller handles itself.
    """
    for rel in model._meta.related_objects:
        related, field = rel.related_model, rel.field
        if (related, field.name) in skip:
            continue
        if rel.many_to_many:
            through = field.remote_field.through
            to_name, from_name = field.m2m_reverse_field_name(), field.m2m_field_name()
            rows = through._base_manager.filter(**{to_name: src})
            taken = through._base_manager.filter(**{to_name: dst}).values(from_name)
            rows.filter(**{f"{from_name}__in": taken}).delete()
            rows.update(**{to_name: dst})
            continue
        rows = related._base_manager.filter(**{field.name: src})
        taken = related._base_manager.filter(**{field.name: dst})
        if rel.one_to_one and rows.exists() and taken.exists():
            raise MergeError(
                f"Both records have a {related._meta.verbose_name}; resolve that first."
            )
        try:
            with transaction.atomic():
                if _has_history(related):
                    objs = list(rows)
                    for obj in objs:
                        setattr(obj, field.name, dst)
                    if objs:
                        bulk_update_with_history(
                            objs,
                            related,
                            [field.name],
                            default_user=user,
                            default_change_reason=reason[:100] or None,
                            manager=related._base_manager,
                        )
                else:
                    rows.update(**{field.name: dst})
        except IntegrityError as exc:
            raise MergeError(
                f"Could not move {related._meta.verbose_name_plural} ({field.name}): {exc}"
            ) from exc


def _in_use(model, obj, *, skip=()):
    """Does any row point at ``obj``, through any of the relations ``_repoint`` moves?
    ``skip`` holds ``(related_model, field_name)`` pairs not to count."""
    for rel in model._meta.related_objects:
        field = rel.field
        if (rel.related_model, field.name) in skip:
            continue
        if rel.many_to_many:
            through = field.remote_field.through
            rows = through._base_manager.filter(**{field.m2m_reverse_field_name(): obj})
        else:
            rows = rel.related_model._base_manager.filter(**{field.name: obj})
        if rows.exists():
            return True
    return False


def _lock(model, *pks):
    rows = model._base_manager.select_for_update().filter(pk__in=pks).order_by("pk")
    return {row.pk: row for row in rows}


def _start_merge(model, src, dst):
    """Lock both rows and check the merge makes sense. Returns ``(src, target)`` fresh from
    the database, or None when ``src`` already redirects to the target."""
    target = canonical(model._base_manager.get(pk=dst.pk))
    label = model._meta.verbose_name
    if src.pk == target.pk:
        if dst.pk == src.pk:
            raise MergeError(f"Can't merge a {label} into itself.")
        raise MergeError(f"{dst} already redirects to {src}; merging would make a loop.")
    rows = _lock(model, src.pk, target.pk)
    src, target = rows[src.pk], rows[target.pk]
    if target.merged_into_id is not None:
        raise MergeError(f"{target} was merged while you were working; try again.")
    if src.merged_into_id is not None:
        if canonical(src).pk == target.pk:
            return None
        raise MergeError(f"{src} already redirects to {src.merged_into}; merge that instead.")
    return src, target


def _refuse_unknown(*families):
    if any(family.slug == UNKNOWN_FAMILY_SLUG for family in families):
        raise MergeError(
            f"'{UNKNOWN_VENDOR_NAME}' can't be merged; change the vendor on its supplier "
            "products instead."
        )


@transaction.atomic
def merge_products(src, dst, *, user=None):
    """Merge product ``src`` into ``dst`` (or the product ``dst`` redirects to).

    Everything that points at ``src`` moves to the target. A lot number both products have
    becomes the target's lot, and a lab with notes on both keeps one merged LabProduct. The
    target gains ``src``'s names as synonyms and fills its empty identifiers from it.
    ``src`` stays as a redirect. Merging again is a no-op.
    """
    started = _start_merge(Product, src, dst)
    if started is None:
        return
    given, (src, target) = src, started
    reason = f"merged product #{src.pk} into #{target.pk}"
    _merge_lots(src, target, user=user, reason=reason)
    _merge_lab_products(src, target, user=user, reason=reason)
    _repoint(
        Product,
        src,
        target,
        user=user,
        reason=reason,
        skip={(Lot, "product"), (LabProduct, "product")},
    )
    _absorb(target, src)
    _save(target, user=user, reason=reason)
    src.merged_into = given.merged_into = target
    _save(src, user=user, reason=reason)


def _absorb(target, src):
    known = {name.casefold() for name in [target.name, *target.synonyms]}
    synonyms = list(target.synonyms)
    for name in [src.name, *src.synonyms]:
        if name and name.casefold() not in known:
            synonyms.append(name)
            known.add(name.casefold())
    target.synonyms = synonyms
    target.attributes = {**src.attributes, **target.attributes}
    for field in ("cas_number", "rrid", "cellosaurus_ac", "addgene_id"):
        if not getattr(target, field):
            setattr(target, field, getattr(src, field))
    if target.item_type_id is None:
        target.item_type_id = src.item_type_id


def _merge_lots(src, target, *, user, reason):
    kept = {lot.lot_key: lot for lot in Lot.objects.select_for_update().filter(product=target)}
    for lot in Lot.objects.select_for_update().filter(product=src):
        twin = kept.get(lot.lot_key)
        if twin is None:
            lot.product = target
            _save(lot, user=user, reason=reason, update_fields=["product"])
            continue
        _repoint(Lot, lot, twin, user=user, reason=reason)
        if twin.expiry is None and lot.expiry is not None:
            twin.expiry, twin.expiry_precision = lot.expiry, lot.expiry_precision
            _save(twin, user=user, reason=reason, update_fields=["expiry", "expiry_precision"])
        _delete(lot, user=user, reason=reason)


def _merge_lab_products(src, target, *, user, reason):
    kept = {
        row.lab_id: row for row in LabProduct.objects.select_for_update().filter(product=target)
    }
    for row in LabProduct.objects.select_for_update().filter(product=src):
        twin = kept.get(row.lab_id)
        if twin is None:
            row.product = target
            row.save(update_fields=["product"])
            continue
        if twin.min_stock_containers is None:
            twin.min_stock_containers = row.min_stock_containers
        if twin.steward_id is None:
            twin.steward_id = row.steward_id
        known = {name.casefold() for name in twin.synonyms}
        twin.synonyms = [*twin.synonyms, *(s for s in row.synonyms if s.casefold() not in known)]
        if row.notes and row.notes not in twin.notes:
            twin.notes = "\n\n".join(filter(None, [twin.notes, row.notes]))
        twin.save()
        _repoint(LabProduct, row, twin, user=user, reason=reason)
        row.delete()


def _resync_supplier_product(supplier_product, *, vendor=None, user=None, reason=""):
    """Re-derive a supplier product's family and catalog keys after its vendor, the vendor's
    family or the family's pack rule changed. If another identifying supplier product already
    has the new (family, key), this one is folded into it; returns the survivor."""
    moved = vendor is not None and supplier_product.vendor_id != vendor.pk
    if vendor is not None:
        supplier_product.vendor = vendor  # the caller's copy carries the new family
    changed = ["vendor"] * moved + supplier_product.derive()
    if not changed:
        return supplier_product
    if supplier_product.identity == SupplierProduct.Identity.IDENTIFYING and (
        supplier_product.catalog_key is not None
    ):
        twin = (
            _identifying(
                supplier_product.vendor_family_id, catalog_key=supplier_product.catalog_key
            )
            .exclude(pk=supplier_product.pk)
            .select_for_update(of=("self",))
            .first()
        )
        if twin is not None:
            _fold_supplier_product(supplier_product, twin, user=user, reason=reason)
            return twin
    _save(supplier_product, user=user, reason=reason, update_fields=changed)
    return supplier_product


_PACK_DETAILS = (
    "containers_per_pack",
    "amount_per_container",
    "amount_unit",
    "unit_size_raw",
    "last_unit_price",
    "currency",
    "url",
    "mfr_family",
    "mfr_catalog_key",
)


def _fold_supplier_product(src, dst, *, user, reason):
    """Two supplier products turned out to be one: keep ``dst``, move everything from ``src``
    (merging their products if they differ), then delete ``src``."""
    src_product = Product.objects.get(pk=src.product_id)
    if canonical(src_product).pk != canonical(dst.product).pk:
        merge_products(src_product, dst.product, user=user)
    filled = []
    for field in _PACK_DETAILS:
        if getattr(dst, field) in (None, "") and getattr(src, field) not in (None, ""):
            setattr(dst, field, getattr(src, field))
            filled.append(field)
    if filled:
        _save(dst, user=user, reason=reason, update_fields=filled)
    for code in ProductCode.objects.filter(supplier_product=src):
        if ProductCode.objects.filter(
            supplier_product=dst, kind=code.kind, code=code.code
        ).exists():
            code.delete()
    _repoint(SupplierProduct, src, dst, user=user, reason=reason)
    _delete(src, user=user, reason=reason)


def _resync_all(supplier_products, *, vendor=None, user=None, reason=""):
    """``_resync_supplier_product`` for each row of a queryset; returns ``(changed, folded)``.

    Rows are re-read and locked one at a time, because folding one into another can change
    rows further down the list (their product, for instance)."""

    def keys(row):
        return row.vendor_id, row.catalog_key, row.catalog_base, row.mfr_catalog_key

    changed = folded = 0
    for pk in list(supplier_products.order_by("pk").values_list("pk", flat=True)):
        supplier_product = SupplierProduct.objects.select_for_update().filter(pk=pk).first()
        if supplier_product is None:
            continue
        before = keys(supplier_product)
        survivor = _resync_supplier_product(
            supplier_product, vendor=vendor, user=user, reason=reason
        )
        if survivor.pk != pk:
            folded += 1
        elif keys(survivor) != before:
            changed += 1
    return changed, folded


@transaction.atomic
def merge_vendors(src, dst, *, user=None):
    """Merge vendor ``src`` into ``dst`` (or the vendor ``dst`` redirects to).

    Its supplier products, aliases and anything else pointing at it move to the target, and
    the supplier products take the target's family (and its pack rule). A catalog # the
    target's family already has is folded into that supplier product, merging the two
    products. ``src`` stays as a redirect. Returns how many supplier products were folded.
    """
    started = _start_merge(Vendor, src, dst)
    if started is None:
        return 0
    given, (src, target) = src, started
    _refuse_unknown(src.family, target.family)
    reason = f"merged vendor #{src.pk} into #{target.pk}"
    listings = SupplierProduct.objects.filter(vendor=src)
    _, folded = _resync_all(listings, vendor=target, user=user, reason=reason)
    _repoint(Vendor, src, target, user=user, reason=reason, skip={(SupplierProduct, "vendor")})
    src.merged_into = given.merged_into = target
    _save(src, user=user, reason=reason)
    return folded


@transaction.atomic
def move_vendor(vendor, family, *, user=None):
    """Put ``vendor`` into ``family`` (after redirects), saving the vendor as given, and bring
    its supplier products along. Returns how many were folded into ones the family had."""
    family = canonical(family)
    _lock(Vendor, vendor.pk)
    reason = f"moved vendor #{vendor.pk} to family {family.slug}"
    vendor.family = family
    _save(vendor, user=user, reason=reason)
    listings = SupplierProduct.objects.filter(vendor=vendor)
    return _resync_all(listings, vendor=vendor, user=user, reason=reason)[1]


@transaction.atomic
def merge_vendor_families(src, dst, *, user=None):
    """Merge vendor family ``src`` into ``dst``: its vendors move (``move_vendor``), generic
    keys and manufacturer links follow, and ``src`` stays as a redirect. Returns how many
    supplier products were folded."""
    started = _start_merge(VendorFamily, src, dst)
    if started is None:
        return 0
    given, (src, target) = src, started
    _refuse_unknown(src, target)
    reason = f"merged family #{src.pk} into #{target.pk}"
    folded = sum(
        move_vendor(vendor, target, user=user) for vendor in Vendor.objects.filter(family=src)
    )
    # Supplier products whose vendor_family drifted from their vendor's, if any.
    folded += _resync_all(SupplierProduct.objects.filter(vendor_family=src), reason=reason)[1]
    taken = GenericCatalogKey.objects.filter(vendor_family=target).values("catalog_key")
    GenericCatalogKey.objects.filter(vendor_family=src, catalog_key__in=taken).delete()
    _repoint(
        VendorFamily,
        src,
        target,
        user=user,
        reason=reason,
        skip={(Vendor, "family"), (SupplierProduct, "vendor_family")},
    )
    src.merged_into = given.merged_into = target
    _save(src, user=user, reason=reason)
    return folded


@transaction.atomic
def refresh_catalog_keys(*, family=None, user=None):
    """Re-derive the catalog keys of every supplier product (or one family's, including
    manufacturer keys in that family) after a pack rule or the normalization code changed.
    Returns ``(changed, folded)``."""
    rows = SupplierProduct.objects.all()
    if family is not None:
        family = canonical(family)
        rows = rows.filter(Q(vendor_family=family) | Q(mfr_family=family))
    return _resync_all(rows, user=user, reason="catalog keys refreshed")


# ---------------------------------------------------------------------------------------
# Orphans


@transaction.atomic
def retire_orphans(supplier_products, *, replacement=None, user=None):
    """Clean up the supplier products a re-resolved request no longer uses, and their products.

    A supplier product that nothing references any more (``_meta.related_objects``, so other
    apps' foreign keys and barcodes count) is deleted while provisional and kept once
    reviewed. Then each product that lost a supplier product here, is referenced by nothing
    else and is still provisional is merged into ``replacement`` (a Product) when one is
    given, so its id keeps working as a redirect, or else deleted. A replacement deleted
    meanwhile (an overlapping re-resolution moved its requests on) counts as none.

    Products that redirect to it (an earlier replacement's, say) don't keep it: merged, it
    takes them along, so a request resolved again and again leaves one canonical product;
    deleted, it takes them with it (``_delete_with_redirects``), since they would redirect
    nowhere. Only a redirect that is reviewed or in use keeps it, as
    ``products_kept_for_redirects``. Reviewed products are never deleted or merged, and
    neither is ``replacement``. Rows still in use are skipped, so this is safe to call with
    any rows, and calling it again changes nothing.

    Returns counts: supplier products ``deleted``, ``kept_curated`` (reviewed) and ``in_use``,
    and products ``products_merged``, ``products_deleted`` (the redirects deleted with them
    included), ``products_kept_curated``, ``products_kept_for_redirects`` and
    ``products_in_use``.
    """
    counts = dict.fromkeys(
        ("deleted", "kept_curated", "in_use", "products_merged", "products_deleted",
         "products_kept_curated", "products_kept_for_redirects", "products_in_use"),
        0,
    )  # fmt: skip
    reason = "retired: nothing uses it any more"
    replacement = canonical(replacement)
    pks = {row.pk for row in supplier_products if row is not None and row.pk is not None}
    freed = []
    for supplier_product in _lock(SupplierProduct, *pks).values():
        if _in_use(SupplierProduct, supplier_product):
            counts["in_use"] += 1
        elif not supplier_product.provisional:
            counts["kept_curated"] += 1
        else:
            _delete(supplier_product, user=user, reason=reason)
            freed.append(supplier_product.product_id)
            counts["deleted"] += 1
    redirects = {(Product, "merged_into")}  # they go where the product goes
    for product in _lock(Product, *freed).values():
        if product.merged_into_id is not None:
            continue  # already a redirect
        if (replacement is not None and product.pk == replacement.pk) or _in_use(
            Product, product, skip=redirects
        ):
            counts["products_in_use"] += 1
        elif not product.provisional:
            counts["products_kept_curated"] += 1
        elif replacement is not None and _lock(Product, replacement.pk):  # not retired meanwhile
            merge_products(product, replacement, user=user)
            counts["products_merged"] += 1
        elif deleted := _delete_with_redirects(product, user=user, reason=reason):
            counts["products_deleted"] += deleted
        else:
            counts["products_kept_for_redirects"] += 1  # deleting it would break them
    return counts


def _delete_with_redirects(product, *, user, reason):
    """Delete ``product`` (locked, and used by nothing but redirects) and the products that
    redirect to it, directly or through each other, deepest first. Returns how many went: 0,
    and nothing deleted, when one of the redirects is reviewed or in use.

    Only this deletes products, and only once it holds the lock on the one they lead to, so
    the redirects found here stay as they are until it is done."""
    merged, frontier = [], [product.pk]
    for _hop in range(MAX_REDIRECT_HOPS):
        frontier = list(
            Product._base_manager.filter(merged_into__in=frontier).values_list("pk", flat=True)
        )
        if not frontier:
            break
        merged += frontier
    if frontier:
        return 0  # deeper than any merge makes
    rows = _lock(Product, *merged)
    skip = {(Product, "merged_into")}  # every redirect to them is among ``merged``
    if any(not row.provisional or _in_use(Product, row, skip=skip) for row in rows.values()):
        return 0
    for row in [*(rows[pk] for pk in reversed(merged)), product]:
        _delete(row, user=user, reason=reason)
    return len(merged) + 1
