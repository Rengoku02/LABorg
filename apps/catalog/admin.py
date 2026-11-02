"""The catalog's back office: the review queue for what the Quartzy sync created.

Provisional products, vendors and families are reviewed here. Duplicates are merged with the
"Merge selected into..." actions (``services.merge_*``), never deleted, so every id stored
elsewhere keeps working through the redirect.
"""

from functools import cache

from django import forms
from django.contrib import admin, messages
from django.contrib.admin import helpers
from django.contrib.postgres.search import SearchQuery
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse
from django.template import engines
from simple_history.admin import SimpleHistoryAdmin

from . import services
from .models import (
    GenericCatalogKey,
    ItemType,
    LabProduct,
    Lot,
    Product,
    ProductCode,
    SupplierProduct,
    Vendor,
    VendorAlias,
    VendorFamily,
)
from .normalization import normalize_catalog, normalize_vendor_name
from .vendor_rules import DOTTED_RULES, RULES

# The merge page is the catalog's only admin template, so it lives here as a string.
_MERGE_PAGE = """{% extends "admin/base_site.html" %}
{% block content %}
<form method="post">{% csrf_token %}
  <p>Pick the one to keep. The others become redirects to it: nothing is deleted, and
  everything that pointed at them (supplier products, lots, stock, requests) moves to it.</p>
  {{ form.non_field_errors }}
  {{ form.as_p }}
  {% for obj in objects %}
    <input type="hidden" name="{{ checkbox_name }}" value="{{ obj.pk }}">
  {% endfor %}
  <input type="hidden" name="action" value="merge_selected">
  <input type="submit" name="apply" value="Merge">
  <a href="{{ request.get_full_path }}" class="button cancel-link">Cancel</a>
</form>
{% endblock %}"""


@cache
def _merge_page():
    return engines["django"].from_string(_MERGE_PAGE)


class LinesField(forms.Field):
    """A list of strings edited as text: one per line, or split on ``separator``."""

    def __init__(self, *, separator="\n", max_item_length=None, **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault("widget", forms.Textarea(attrs={"rows": 3}))
        super().__init__(**kwargs)
        self.separator, self.max_item_length = separator, max_item_length

    def prepare_value(self, value):
        return self.separator.join(value) if isinstance(value, list | tuple) else value

    def to_python(self, value):
        if isinstance(value, list | tuple):
            return list(value)
        text = value or ""
        parts = text.splitlines() if self.separator == "\n" else text.split(self.separator)
        return [part.strip() for part in parts if part.strip()]

    def validate(self, value):
        super().validate(value)
        limit = self.max_item_length
        too_long = [item for item in value if limit and len(item) > limit]
        if too_long:
            raise ValidationError(f"Longer than {self.max_item_length} characters: {too_long[0]}")


class MergeForm(forms.Form):
    keep = forms.ModelChoiceField(
        queryset=None, widget=forms.RadioSelect, required=False, empty_label=None
    )
    other = forms.IntegerField(
        required=False, min_value=1, label="...or keep the record with this id instead"
    )

    def __init__(self, *args, model, choices, **kwargs):
        super().__init__(*args, **kwargs)
        self.model = model
        self.fields["keep"].queryset = choices
        self.fields["keep"].label_from_instance = lambda obj: f"#{obj.pk} {obj}"

    def clean(self):
        cleaned = super().clean()
        keep = cleaned.get("keep")
        if cleaned.get("other"):
            keep = self.model._base_manager.filter(pk=cleaned["other"]).first()
            if keep is None:
                raise ValidationError(f"There is no record with id {cleaned['other']}.")
        if keep is None:
            raise ValidationError("Pick the record to keep.")
        cleaned["target"] = services.canonical(keep)
        return cleaned


def merge_action(merge, label):
    """An admin action that merges the selected rows into one picked on a second page."""

    def merge_selected(modeladmin, request, queryset):
        queryset = queryset.filter(merged_into__isnull=True)
        data = request.POST if "apply" in request.POST else None
        form = MergeForm(data, model=modeladmin.model, choices=queryset)
        if form.is_bound and form.is_valid():
            target = form.cleaned_data["target"]
            sources = [obj for obj in queryset if obj.pk != target.pk]
            try:
                with transaction.atomic():
                    for obj in sources:
                        merge(obj, target, user=request.user)
            except services.MergeError as exc:
                modeladmin.message_user(request, str(exc), messages.ERROR)
                return None
            modeladmin.message_user(
                request,
                f"Merged {len(sources)} {label} into {target}; they now redirect to it.",
                messages.SUCCESS,
            )
            return None
        context = {
            **modeladmin.admin_site.each_context(request),
            "title": f"Merge {label}",
            "opts": modeladmin.model._meta,
            "form": form,
            "objects": queryset,
            "checkbox_name": helpers.ACTION_CHECKBOX_NAME,
        }
        return HttpResponse(_merge_page().render(context, request))

    merge_selected.short_description = f"Merge selected {label} into..."
    merge_selected.allowed_permissions = ("change",)
    return merge_selected


def mark_reviewed(modeladmin, request, queryset):
    count = 0
    for obj in queryset.filter(provisional=True):
        obj.provisional = False
        obj.save()
        count += 1
    if modeladmin.model is Product:
        for supplier_product in SupplierProduct.objects.filter(
            product__in=queryset, provisional=True
        ):
            supplier_product.provisional = False
            supplier_product.save(update_fields=["provisional"])
    modeladmin.message_user(request, f"Marked {count} reviewed.", messages.SUCCESS)


mark_reviewed.short_description = "Mark reviewed (no longer provisional)"
mark_reviewed.allowed_permissions = ("change",)


class RedirectFilter(admin.SimpleListFilter):
    title = "merged"
    parameter_name = "merged"

    def lookups(self, request, model_admin):
        return (("no", "Live records"), ("yes", "Redirects (merged away)"))

    def queryset(self, request, queryset):
        if self.value() == "no":
            return queryset.filter(merged_into__isnull=True)
        if self.value() == "yes":
            return queryset.filter(merged_into__isnull=False)
        return queryset


class NoDeleteMixin:
    """Merge instead: other tables point at these rows."""

    def has_delete_permission(self, request, obj=None):
        return False


# ---------------------------------------------------------------------------------------
# Vendors


class VendorAliasForm(forms.ModelForm):
    class Meta:
        model = VendorAlias
        fields = ("alias_key", "raw_example", "vendor")

    def clean_alias_key(self):
        typed = self.cleaned_data["alias_key"]
        key = normalize_vendor_name(typed)
        if not key:
            raise ValidationError("That name has no letters or digits.")
        self.typed_alias = typed.strip()
        return key

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("raw_example") and getattr(self, "typed_alias", ""):
            cleaned["raw_example"] = self.typed_alias
        return cleaned


class VendorAliasInline(admin.TabularInline):
    model = VendorAlias
    form = VendorAliasForm
    fields = ("alias_key", "raw_example")
    extra = 1


class VendorInline(admin.TabularInline):
    model = Vendor
    fk_name = "family"
    fields = ("name", "provisional", "merged_into")
    readonly_fields = fields
    extra = 0
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False


class VendorFamilyForm(forms.ModelForm):
    # Choices built here rather than on the model, so a new rule needs no migration.
    catalog_rule = forms.ChoiceField(
        choices=[("", "(none)"), *((name, name) for name in sorted(RULES))],
        required=False,
        help_text="The pack rule (apps/catalog/vendor_rules.py) that splits pack sizes off.",
    )

    class Meta:
        model = VendorFamily
        fields = ("slug", "name", "is_distributor", "catalog_rule", "provisional")


@admin.register(VendorFamily)
class VendorFamilyAdmin(NoDeleteMixin, SimpleHistoryAdmin):
    form = VendorFamilyForm
    list_display = (
        "name",
        "slug",
        "is_distributor",
        "catalog_rule",
        "provisional",
        "vendor_count",
        "merged_into",
    )
    list_filter = ("provisional", "is_distributor", RedirectFilter)
    search_fields = ("name", "slug", "vendors__name")
    readonly_fields = ("merged_into",)
    prepopulated_fields = {"slug": ("name",)}
    inlines = [VendorInline]
    actions = [merge_action(services.merge_vendor_families, "vendor families"), mark_reviewed]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(vendor_count=Count("vendors", distinct=True))

    @admin.display(description="vendors", ordering="vendor_count")
    def vendor_count(self, obj):
        return obj.vendor_count

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if change and "catalog_rule" in form.changed_data:
            changed, folded = services.refresh_catalog_keys(family=obj, user=request.user)
            self.message_user(
                request,
                f"Pack rule changed: {changed} supplier product(s) re-keyed, {folded} merged.",
                messages.INFO,
            )


@admin.register(Vendor)
class VendorAdmin(NoDeleteMixin, SimpleHistoryAdmin):
    list_display = ("name", "family", "provisional", "merged_into")
    list_filter = ("provisional", "family__is_distributor", RedirectFilter)
    search_fields = ("name", "aliases__alias_key", "family__name")
    list_select_related = ("family", "merged_into")
    autocomplete_fields = ("family",)
    readonly_fields = ("merged_into",)
    inlines = [VendorAliasInline]
    actions = [merge_action(services.merge_vendors, "vendors"), mark_reviewed]

    def save_model(self, request, obj, form, change):
        if change and "family" in form.changed_data:
            # Its supplier products move with it and take the new family's pack rule.
            folded = services.move_vendor(obj, obj.family, user=request.user)
            if folded:
                self.message_user(
                    request,
                    f"{folded} supplier product(s) matched ones {obj.family} already had "
                    "and were merged into them.",
                    messages.WARNING,
                )
        else:
            super().save_model(request, obj, form, change)


@admin.register(VendorAlias)
class VendorAliasAdmin(admin.ModelAdmin):
    form = VendorAliasForm
    list_display = ("alias_key", "raw_example", "vendor")
    search_fields = ("alias_key", "raw_example", "vendor__name")
    list_select_related = ("vendor",)
    autocomplete_fields = ("vendor",)


# ---------------------------------------------------------------------------------------
# Item types


class ItemTypeForm(forms.ModelForm):
    quartzy_type_keywords = LinesField(
        separator=",",
        widget=forms.TextInput(attrs={"size": 80}),
        help_text="Comma-separated. Each matches the start of a word in a Quartzy Type name.",
    )

    class Meta:
        model = ItemType
        fields = (
            "key",
            "label",
            "tier",
            "durable",
            "lookback_months",
            "in_scope",
            "identity_rule",
            "tracking_mode",
            "quartzy_type_keywords",
            "product_schema",
            "instance_schema",
            "sort_order",
        )


@admin.register(ItemType)
class ItemTypeAdmin(admin.ModelAdmin):
    form = ItemTypeForm
    list_display = (
        "label",
        "key",
        "tier",
        "durable",
        "lookback_months",
        "in_scope",
        "identity_rule",
        "tracking_mode",
        "sort_order",
    )
    list_editable = ("durable", "lookback_months", "in_scope")
    list_filter = ("tier", "durable", "in_scope", "identity_rule")
    search_fields = ("key", "label")


# ---------------------------------------------------------------------------------------
# Products


class NeedsReviewFilter(admin.SimpleListFilter):
    title = "review"
    parameter_name = "review"

    def lookups(self, request, model_admin):
        return (
            ("needed", "Needs review"),
            ("done", "Reviewed"),
            ("merged", "Merged away (redirects)"),
        )

    def queryset(self, request, queryset):
        live = queryset.filter(merged_into__isnull=True)
        pending = Q(provisional=True) | Q(supplier_products__provisional=True)
        if self.value() == "needed":
            return live.filter(pending).distinct()
        if self.value() == "done":
            return live.exclude(pending)
        if self.value() == "merged":
            return queryset.filter(merged_into__isnull=False)
        return queryset


class ProductForm(forms.ModelForm):
    synonyms = LinesField(max_item_length=500, help_text="Other names, one per line.")

    class Meta:
        model = Product
        fields = (
            "name",
            "item_type",
            "synonyms",
            "provisional",
            "lab_made",
            "cas_number",
            "rrid",
            "cellosaurus_ac",
            "addgene_id",
            "attributes",
        )


class SupplierProductInline(admin.TabularInline):
    model = SupplierProduct
    fk_name = "product"
    fields = (
        "vendor",
        "catalog_raw",
        "catalog_key",
        "pack_code",
        "identity",
        "unit_size_raw",
        "last_unit_price",
        "currency",
        "provisional",
    )
    readonly_fields = ("catalog_key", "pack_code")
    autocomplete_fields = ("vendor",)
    extra = 0
    show_change_link = True


@admin.register(Product)
class ProductAdmin(NoDeleteMixin, SimpleHistoryAdmin):
    form = ProductForm
    list_display = (
        "name",
        "item_type",
        "provisional",
        "supplier_product_count",
        "created_at",
        "merged_into",
    )
    list_filter = (NeedsReviewFilter, "provisional", "item_type", "lab_made")
    search_fields = (
        "name",
        "supplier_products__catalog_raw",
        "cas_number",
        "rrid",
        "cellosaurus_ac",
        "addgene_id",
    )
    list_select_related = ("item_type", "merged_into")
    readonly_fields = ("merged_into", "created_at", "updated_at")
    fieldsets = (
        (None, {"fields": ("name", "item_type", "synonyms", "provisional", "lab_made")}),
        ("Identifiers", {"fields": ("cas_number", "rrid", "cellosaurus_ac", "addgene_id")}),
        ("More", {"fields": ("attributes", "merged_into", "created_at", "updated_at")}),
    )
    inlines = [SupplierProductInline]
    actions = [merge_action(services.merge_products, "products"), mark_reviewed]

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(supplier_product_count=Count("supplier_products", distinct=True))
        )

    @admin.display(description="supplier products", ordering="supplier_product_count")
    def supplier_product_count(self, obj):
        return obj.supplier_product_count

    def get_search_results(self, request, queryset, search_term):
        found, may_have_duplicates = super().get_search_results(request, queryset, search_term)
        term = search_term.strip()
        if not term:
            return found, may_have_duplicates
        # Also match words in names and synonyms, and the catalog # however it is written
        # (with its dot, for families whose rule keeps one: ATCC CCL-2.2).
        extra = Q(search_vector=SearchQuery(term, config="english"))
        keys = {normalize_catalog(term, rule=rule).key for rule in [None, *DOTTED_RULES]} - {None}
        if keys:
            extra |= Q(supplier_products__catalog_key__in=keys)
        return queryset.filter(Q(pk__in=found.values("pk")) | extra), True


class ProductCodeInline(admin.TabularInline):
    model = ProductCode
    fields = ("kind", "code")
    extra = 0


@admin.register(SupplierProduct)
class SupplierProductAdmin(SimpleHistoryAdmin):
    list_display = (
        "__str__",
        "product",
        "vendor_family",
        "catalog_key",
        "catalog_base",
        "identity",
        "provisional",
    )
    list_filter = ("identity", "provisional", "vendor_family__is_distributor")
    search_fields = ("catalog_raw", "catalog_key", "product__name", "vendor__name")
    list_select_related = ("vendor", "product", "vendor_family")
    autocomplete_fields = ("product", "vendor", "mfr_family")
    readonly_fields = ("vendor_family", "catalog_key", "catalog_base", "pack_code", "created_at")
    fields = (
        "product",
        "vendor",
        "vendor_family",
        "catalog_raw",
        "catalog_key",
        "catalog_base",
        "pack_code",
        "identity",
        "mfr_family",
        "mfr_catalog_key",
        "containers_per_pack",
        "amount_per_container",
        "amount_unit",
        "unit_size_raw",
        "last_unit_price",
        "currency",
        "url",
        "provisional",
        "created_at",
    )
    inlines = [ProductCodeInline]


@admin.register(Lot)
class LotAdmin(SimpleHistoryAdmin):
    list_display = ("product", "lot_raw", "lot_key", "expiry", "expiry_precision")
    search_fields = ("lot_key", "lot_raw", "product__name")
    list_select_related = ("product",)
    autocomplete_fields = ("product",)
    readonly_fields = ("created_at",)


class LabProductForm(forms.ModelForm):
    synonyms = LinesField(max_item_length=500, help_text="This lab's names, one per line.")

    class Meta:
        model = LabProduct
        fields = ("lab", "product", "min_stock_containers", "synonyms", "notes", "steward")


@admin.register(LabProduct)
class LabProductAdmin(admin.ModelAdmin):
    form = LabProductForm
    list_display = ("product", "lab", "min_stock_containers", "steward")
    list_filter = ("lab",)
    search_fields = ("product__name", "notes")
    list_select_related = ("product", "lab", "steward")
    autocomplete_fields = ("product", "steward")


@admin.register(GenericCatalogKey)
class GenericCatalogKeyAdmin(admin.ModelAdmin):
    list_display = ("catalog_key", "vendor_family", "reason", "created_at")
    search_fields = ("catalog_key", "reason", "vendor_family__name")
    list_select_related = ("vendor_family",)
    autocomplete_fields = ("vendor_family",)
    readonly_fields = ("created_at",)
