"""The global product catalog (plan §7 "Catalog").

Nothing here belongs to one lab except ``LabProduct``. The Quartzy sync creates vendors,
products and supplier products provisionally as it meets them (``services``); people review
them in the admin and merge duplicates, which leaves the old row behind as a redirect
(``merged_into``) so nothing that points at it breaks.
"""

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVector, SearchVectorField
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import F, Func, Q, Value
from django.db.models.functions import Replace
from simple_history.models import HistoricalRecords

from .normalization import normalize_catalog, normalize_lot
from .vendor_rules import RULES

_UNSET = object()


class ResolutionStatus(models.TextChoices):
    """How ``services.resolve_supplier_product`` matched a request line."""

    EXACT = "exact", "Same catalog #"
    BASE_VARIANT = "base_variant", "Other pack size"
    NEW = "new", "New product"
    GENERIC = "generic", "Generic catalog #"
    NO_CATALOG = "no_catalog", "No catalog #"


def validate_catalog_rule(value):
    if value and value not in RULES:
        raise ValidationError(
            f"Unknown pack rule {value!r}; choose one of: {', '.join(sorted(RULES))}."
        )


def seed_snapshot(values):
    """The ``seeded`` snapshot for freshly created rows: related objects are stored by pk."""
    return {
        name: value.pk if isinstance(value, models.Model) else value
        for name, value in values.items()
    }


class Seeded(models.Model):
    """A row that ``manage.py seed_catalog`` loads from a YAML file.

    ``seeded`` keeps the values the seed last wrote. A re-seed changes a field only while it
    still holds that value, so anything a person changed afterwards (in the admin or
    anywhere else) is left alone, and fields the seed never wrote are never touched.
    """

    seeded = models.JSONField(default=dict, blank=True, editable=False)

    class Meta:
        abstract = True

    def apply_seed(self, values, *, adopt=False):
        """Set every field the seed still owns to ``values``; return the names that changed.
        ``adopt`` claims every field, for rows the sync created that nobody has reviewed."""
        changed = []
        for name, value in values.items():
            attname = self._meta.get_field(name).attname
            current = getattr(self, attname)
            new = value.pk if isinstance(value, models.Model) else value
            owned = adopt or self._state.adding or self.seeded.get(name, _UNSET) == current
            if not owned:
                continue  # someone changed it after the last seed
            if current != new:
                setattr(self, attname, new)
                changed.append(name)
            self.seeded[name] = new
        return changed


class VendorFamily(Seeded):
    """Brands that share one catalog-number space: Gibco, Invitrogen and Life Technologies are
    all Thermo Fisher. The duplicate guard compares catalog numbers within a family."""

    slug = models.SlugField(max_length=60, unique=True)
    name = models.CharField(max_length=200)
    is_distributor = models.BooleanField(default=False)
    catalog_rule = models.CharField(
        max_length=40,
        blank=True,
        validators=[validate_catalog_rule],
        help_text="A pack rule from apps/catalog/vendor_rules.py; blank for none.",
    )
    # Created by the sync for a vendor nobody has curated yet.
    provisional = models.BooleanField(default=False)
    merged_into = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="merged_from"
    )
    history = HistoricalRecords(excluded_fields=["seeded"])

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "vendor families"

    def __str__(self):
        return self.name

    @property
    def pack_rule(self):
        """The ``vendor_rules.RULES`` key ``normalize_catalog`` should use, or None."""
        return self.catalog_rule if self.catalog_rule in RULES else None


class Vendor(Seeded):
    name = models.CharField(max_length=200, unique=True)
    family = models.ForeignKey(VendorFamily, on_delete=models.PROTECT, related_name="vendors")
    provisional = models.BooleanField(default=False)
    merged_into = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="merged_from"
    )
    history = HistoricalRecords(excluded_fields=["seeded"])

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class VendorAlias(models.Model):
    """A vendor name as people type it, keyed by ``normalize_vendor_name``. Lookups also try
    the key without spaces, so "BioRad" finds the alias "bio rad"."""

    alias_key = models.CharField(max_length=200, unique=True)
    raw_example = models.CharField(max_length=200, blank=True)
    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name="aliases")

    class Meta:
        ordering = ["alias_key"]
        verbose_name_plural = "vendor aliases"
        indexes = [
            models.Index(Replace("alias_key", Value(" "), Value("")), name="catalog_alias_compact")
        ]

    def __str__(self):
        return self.alias_key


class ItemType(Seeded):
    """What kind of thing a product is, and how the lab treats it (plan §2 "Item types")."""

    class IdentityRule(models.TextChoices):
        CATALOG = "catalog", "Vendor catalog #"
        CAS = "cas", "CAS number"
        RRID = "rrid", "RRID"
        CVCL = "cvcl", "Cellosaurus accession"
        ADDGENE = "addgene", "Addgene ID"
        SEQUENCE = "sequence", "Sequence"
        # Catalog numbers of this type identify nothing (custom oligos, services, fees), so
        # each request becomes its own product.
        NONE = "none", "Nothing (never grouped)"

    class TrackingMode(models.TextChoices):
        CONTAINER = "container", "Each container"
        COUNT = "count", "A count"
        STATUS_ONLY = "status_only", "Status only"

    key = models.SlugField(max_length=40, unique=True)
    label = models.CharField(max_length=100)
    # Rollout order: tier 1 is tracked first, tier 5 last.
    tier = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    # Lasts long enough that an old purchase is probably still in someone's box.
    durable = models.BooleanField(default=False)
    # How far back "bought before" looks; 0 means only open requests count.
    lookback_months = models.PositiveSmallIntegerField(default=0)
    # Whether the duplicate guard checks requests of this type.
    in_scope = models.BooleanField(default=True)
    identity_rule = models.CharField(
        max_length=20, choices=IdentityRule.choices, default=IdentityRule.CATALOG
    )
    tracking_mode = models.CharField(
        max_length=20, choices=TrackingMode.choices, default=TrackingMode.CONTAINER
    )
    product_schema = models.JSONField(default=dict, blank=True)
    instance_schema = models.JSONField(default=dict, blank=True)
    # Matched at the start of a word in a Quartzy Type name (services.suggest_item_type).
    quartzy_type_keywords = models.JSONField(default=list, blank=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "key"]
        constraints = [
            models.CheckConstraint(
                condition=Q(tier__gte=1, tier__lte=5), name="catalog_itemtype_tier_1_to_5"
            ),
        ]

    def __str__(self):
        return self.label


class ProductQuerySet(models.QuerySet):
    def canonical(self):
        """Products that are not redirects left behind by a merge."""
        return self.filter(merged_into__isnull=True)


class Product(models.Model):
    """One product, whoever sells it. Created provisionally by the sync, reviewed in the admin."""

    item_type = models.ForeignKey(
        ItemType, on_delete=models.PROTECT, null=True, blank=True, related_name="products"
    )
    name = models.CharField(max_length=500)
    synonyms = ArrayField(models.CharField(max_length=500), default=list, blank=True)
    attributes = models.JSONField(default=dict, blank=True)
    cas_number = models.CharField("CAS number", max_length=20, blank=True, db_index=True)
    rrid = models.CharField("RRID", max_length=50, blank=True, db_index=True)
    cellosaurus_ac = models.CharField(
        "Cellosaurus accession", max_length=20, blank=True, db_index=True
    )
    addgene_id = models.CharField("Addgene ID", max_length=20, blank=True, db_index=True)
    lab_made = models.BooleanField(default=False)
    provisional = models.BooleanField(default=True)
    merged_into = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="merged_from"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # Postgres only accepts immutable functions in a generated column, and array_to_string
    # is merely stable, so migration 0001 wraps it in catalog_join_words().
    search_vector = models.GeneratedField(
        expression=SearchVector("name", weight="A", config="english")
        + SearchVector(
            Func(F("synonyms"), function="catalog_join_words", output_field=models.TextField()),
            weight="B",
            config="english",
        ),
        output_field=SearchVectorField(),
        db_persist=True,
    )
    history = HistoricalRecords(excluded_fields=["search_vector"])

    objects = ProductQuerySet.as_manager()

    class Meta:
        indexes = [
            GinIndex(fields=["name"], opclasses=["gin_trgm_ops"], name="catalog_product_name_trgm"),
            GinIndex(fields=["search_vector"], name="catalog_product_search"),
        ]

    def __str__(self):
        return self.name


class SupplierProduct(models.Model):
    """A product as one vendor sells it, under one catalog number.

    ``catalog_key``, ``catalog_base``, ``pack_code`` and ``vendor_family`` are derived on every
    save from ``catalog_raw`` and the vendor's family (its pack rule), so they can't drift.
    """

    class Identity(models.TextChoices):
        IDENTIFYING = "identifying", "Identifying"
        # The catalog # names no particular product (custom oligos, services, fees); such
        # supplier products are never matched on it.
        GENERIC = "generic", "Generic"

    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="supplier_products")
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="supplier_products")
    vendor_family = models.ForeignKey(
        VendorFamily, on_delete=models.PROTECT, related_name="supplier_products", editable=False
    )
    catalog_raw = models.CharField("catalog # as typed", max_length=200, blank=True)
    # NULL (not "") means "nothing to match on", which also keeps the row out of the unique
    # constraint below.
    catalog_key = models.CharField(max_length=100, null=True, blank=True, editable=False)  # noqa: DJ001
    catalog_base = models.CharField(max_length=100, null=True, blank=True, editable=False)  # noqa: DJ001
    pack_code = models.CharField(max_length=40, blank=True, editable=False)
    # For a distributor SKU whose manufacturer number is known (Fisher's item for a Gibco #).
    mfr_family = models.ForeignKey(
        VendorFamily,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="manufactured_supplier_products",
        verbose_name="manufacturer family",
    )
    mfr_catalog_key = models.CharField(  # noqa: DJ001
        "manufacturer catalog key", max_length=100, null=True, blank=True
    )
    identity = models.CharField(
        max_length=20, choices=Identity.choices, default=Identity.IDENTIFYING
    )
    containers_per_pack = models.PositiveIntegerField(null=True, blank=True)
    amount_per_container = models.DecimalField(
        max_digits=14, decimal_places=4, null=True, blank=True
    )
    amount_unit = models.CharField(max_length=20, blank=True)
    unit_size_raw = models.CharField("unit size as typed", max_length=100, blank=True)
    last_unit_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    url = models.URLField(max_length=500, blank=True)
    provisional = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["vendor_family", "catalog_key"],
                condition=Q(catalog_key__isnull=False, identity="identifying"),
                name="catalog_supplierproduct_unique_identifying_key",
                violation_error_message=(
                    "This vendor family already has a supplier product with this catalog #. "
                    "Merge the two products instead."
                ),
            ),
        ]
        indexes = [
            models.Index(fields=["catalog_key"], name="catalog_sp_key"),
            models.Index(fields=["catalog_base"], name="catalog_sp_base"),
            models.Index(fields=["mfr_catalog_key"], name="catalog_sp_mfr_key"),
        ]

    def __str__(self):
        return f"{self.vendor} {self.catalog_raw or '(no catalog #)'}"

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        if update_fields is None:
            self.derive()
        elif {"vendor", "catalog_raw", "mfr_family", "mfr_catalog_key"} & set(update_fields):
            kwargs["update_fields"] = {*update_fields, *self.derive()}
        super().save(*args, **kwargs)

    def normalized(self):
        """``normalize_catalog`` of ``catalog_raw`` under the vendor family's pack rule."""
        return normalize_catalog(self.catalog_raw or None, rule=self.vendor.family.pack_rule)

    def derive(self):
        """Recompute the derived fields; return the names of those that changed."""
        norm = self.normalized()
        values = {
            "vendor_family_id": self.vendor.family_id,
            "catalog_key": norm.key,
            "catalog_base": norm.base,
            "pack_code": norm.pack_code or "",
        }
        if self.mfr_catalog_key:
            # Keyed like the manufacturer's own listings, under its family's rule.
            mfr_rule = self.mfr_family.pack_rule if self.mfr_family_id else None
            values["mfr_catalog_key"] = normalize_catalog(self.mfr_catalog_key, rule=mfr_rule).key
        changed = []
        for attname, value in values.items():
            if getattr(self, attname) != value:
                setattr(self, attname, value)
                changed.append(attname.removesuffix("_id"))
        return changed

    def clean(self):
        # The unique constraint spans vendor_family, which admin forms don't show, so form
        # validation would skip it; check it here to get a message instead of a crash.
        if self.vendor_id is None:
            return
        self.derive()
        if self.identity != self.Identity.IDENTIFYING or not self.catalog_key:
            return
        clash = (
            SupplierProduct.objects.filter(
                vendor_family_id=self.vendor_family_id,
                catalog_key=self.catalog_key,
                identity=self.Identity.IDENTIFYING,
            )
            .exclude(pk=self.pk)
            .select_related("vendor", "product")
            .first()
        )
        if clash is not None:
            raise ValidationError(
                {
                    "catalog_raw": (
                        f"{clash.vendor} {clash.catalog_raw} is already product "
                        f"#{clash.product_id} ({clash.product}). Merge the products instead."
                    )
                }
            )


class ProductCode(models.Model):
    """A barcode (usually a GTIN) printed on a pack. Pack-level and not unique: distributors
    and repackers can print the same code."""

    class Kind(models.TextChoices):
        GTIN = "gtin", "GTIN"
        OTHER = "other", "Other"

    code = models.CharField(max_length=64, db_index=True)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.GTIN)
    supplier_product = models.ForeignKey(
        SupplierProduct, on_delete=models.CASCADE, related_name="codes"
    )

    def __str__(self):
        return self.code

    def save(self, *args, **kwargs):
        self.code = self.code.strip()
        if self.kind == self.Kind.GTIN:
            self.code = "".join(char for char in self.code if char.isdigit()) or self.code
        super().save(*args, **kwargs)


class Lot(models.Model):
    class ExpiryPrecision(models.TextChoices):
        DAY = "day", "Day"
        MONTH = "month", "Month"  # labels often print only "2027-03"

    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="lots")
    lot_key = models.CharField(max_length=100)
    lot_raw = models.CharField("lot as typed", max_length=200, blank=True)
    expiry = models.DateField(null=True, blank=True)
    expiry_precision = models.CharField(
        max_length=10, choices=ExpiryPrecision.choices, default=ExpiryPrecision.DAY
    )
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["product", "lot_key"], name="catalog_lot_unique_product_key"
            ),
        ]

    def __str__(self):
        return f"{self.product} lot {self.lot_raw or self.lot_key}"

    def save(self, *args, **kwargs):
        if not self.lot_key and self.lot_raw:
            self.lot_key = normalize_lot(self.lot_raw) or ""
        super().save(*args, **kwargs)


class LabProductQuerySet(models.QuerySet):
    def for_lab(self, lab):
        return self.filter(lab=lab)


class LabProduct(models.Model):
    """What one lab adds to a global product: minimum stock, local names, notes, and the
    steward who looks after a shared stock."""

    lab = models.ForeignKey("core.Lab", on_delete=models.CASCADE, related_name="lab_products")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="lab_products")
    min_stock_containers = models.PositiveIntegerField(null=True, blank=True)
    synonyms = ArrayField(models.CharField(max_length=500), default=list, blank=True)
    notes = models.TextField(blank=True)
    steward = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stewarded_products",
    )

    objects = LabProductQuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["lab", "product"], name="catalog_labproduct_unique_lab_product"
            ),
        ]

    def __str__(self):
        return f"{self.product} ({self.lab})"


class GenericCatalogKey(models.Model):
    """A catalog key that names no particular product: custom oligos, sequencing services,
    shipping and dry-ice fees. Requests with it are never grouped. No family = every family.

    Seeded from the Phase 0 baseline: keys that carry many different item names.
    """

    vendor_family = models.ForeignKey(
        VendorFamily, on_delete=models.CASCADE, null=True, blank=True, related_name="generic_keys"
    )
    catalog_key = models.CharField(max_length=100)
    reason = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["vendor_family", "catalog_key"],
                nulls_distinct=False,
                name="catalog_generickey_unique_family_key",
            ),
        ]

    def __str__(self):
        return f"{self.catalog_key} ({self.vendor_family or 'any vendor'})"

    def save(self, *args, **kwargs):
        # Accept a catalog # as typed ("51-01-01-01"); store it the way it is matched.
        self.catalog_key = self.normalized().key or self.catalog_key.strip()[:100]
        super().save(*args, **kwargs)

    def normalized(self):
        """``normalize_catalog`` of ``catalog_key`` under the family's rule (no family: none)."""
        rule = self.vendor_family.pack_rule if self.vendor_family_id else None
        return normalize_catalog(self.catalog_key, rule=rule)

    def clean(self):
        if self.catalog_key and self.normalized().key is None:
            raise ValidationError(
                {"catalog_key": "This is already treated as 'no catalog #'; no need to list it."}
            )
