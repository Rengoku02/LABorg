"""The inventory's back office for the steward (docs/design/mvp-a.md §1).

Stock state is read-only here: it changes only through ``services.apply_event``. So the
stock item form shows no state field as editable, there is no "add" (stock is created by
``services.create_stock_item``: claims, intake forms and imports), and events can't be
added, changed or deleted. Locations are added and moved through
``services.create_location`` and ``move_location``, not treebeard's admin, so the lab, the
tree and the move history stay right; they are retired with "active", never deleted.
"""

from django import forms
from django.contrib import admin
from django.contrib.admin.views.main import ChangeList
from django.core.exceptions import ValidationError
from django.db import transaction

from apps.catalog.admin import LinesField
from apps.catalog.services import canonical

from . import selectors, services
from .models import (
    STATE_FIELDS,
    ContainerCode,
    ExternalRef,
    HistoryClaim,
    Location,
    LocationEvent,
    LocationType,
    StockEvent,
    StockItem,
    Visibility,
)
from .selectors import ancestor_chains, chain_text
from .shortid import as_short_id


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class PathsChangeList(ChangeList):
    """A change list that builds the location path of every row on the page in one query
    (``row.listed_path``). The admin says which location a row shows: ``location_of``."""

    def get_results(self, request):
        super().get_results(request)
        location_of = self.model_admin.location_of
        locations = {location_of(row).pk: location_of(row) for row in self.result_list}
        chains = ancestor_chains(locations.values())
        for row in self.result_list:
            row.listed_path = chain_text(chains[location_of(row).pk])


def _listed_path(row, location):
    return getattr(row, "listed_path", None) or location.path_text()


@admin.register(LocationType)
class LocationTypeAdmin(admin.ModelAdmin):
    list_display = ("label", "key", "structural", "position_scheme", "rows", "cols", "sort_order")
    list_editable = ("sort_order",)
    search_fields = ("key", "label")

    def get_readonly_fields(self, request, obj=None):
        # The code, the importers and location_types.yaml refer to types by key.
        return ("key",) if obj is not None else ()


class ParentField(forms.ModelChoiceField):
    """Locations shown by their path, all paths built in one query."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._paths = None

    def label_from_instance(self, obj):
        if self._paths is None:
            locations = list(self.queryset)
            self._paths = {
                pk: chain_text(chain) for pk, chain in ancestor_chains(locations).items()
            }
        return self._paths.get(obj.pk) or obj.display_name


class LocationAdminForm(forms.ModelForm):
    parent = ParentField(
        queryset=Location.objects.none(),
        required=False,
        help_text="Leave empty for a top-level location (a room). Changing it moves the "
        "location and everything in it.",
    )
    visibility = forms.ChoiceField(
        choices=[("", "Lab default (shared for rooms, freezers, shelves…)"), *Visibility.choices],
        required=False,
        help_text="Rooms, freezers, shelves and racks are always shared; restrict a box.",
    )
    tags = LinesField(separator=",", help_text="Item type keys, e.g. antibody, kit.")

    class Meta:
        model = Location
        fields = (
            "lab",
            "parent",
            "name",
            "location_type",
            "code",
            "temperature",
            "default_custodian",
            "visibility",
            "tags",
            "notes",
            "photo",
            "active",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "parent" in self.fields:  # not on the view-only page of an unsorted container
            parents = Location.objects.physical().select_related("lab")
            if self.instance.pk:
                parents = parents.filter(lab=self.instance.lab).exclude(
                    path__startswith=self.instance.path
                )
                self.initial["parent"] = Location.objects.get_parent(self.instance)
            self.fields["parent"].queryset = parents
        if "location_type" in self.fields:
            # Unsorted containers are made by services.unsorted_location only.
            kinds = self.fields["location_type"].queryset.exclude(key=services.UNSORTED)
            self.fields["location_type"].queryset = kinds
        if "code" in self.fields:
            self.fields["code"].help_text = (
                "Printed on the label. Leave empty to generate one; 1-12 letters, digits or "
                "hyphens, never 6 that read like a stock item id."
            )

    def clean_visibility(self):
        value = self.cleaned_data["visibility"]
        return value or (self.instance.visibility if self.instance.pk else "")

    def clean_name(self):
        return self._service_check(services.clean_location_name, self.cleaned_data["name"])

    def clean_code(self):
        return self._service_check(
            services.clean_location_code, self.cleaned_data["code"], location=self.instance
        )

    def clean_tags(self):
        return self._service_check(services.clean_location_tags, self.cleaned_data["tags"])

    def clean(self):
        cleaned = super().clean()
        lab = cleaned.get("lab") or getattr(self.instance, "lab", None)
        kind, name = cleaned.get("location_type"), cleaned.get("name")
        if lab is None or kind is None or not name:
            return cleaned
        # The admin checks and saves in one transaction: hold the lab's tree still from this
        # check to the save, as create_location and move_location do.
        services.lock_location_tree(lab)
        location = self.instance if self.instance.pk else None
        self._service_check(
            services.validate_placement,
            lab=lab,
            parent=cleaned.get("parent"),
            location_type=kind,
            name=name,
            location=location,
            # A new location is always created in use (save_model retires it after).
            active=cleaned.get("active", True) if location is not None else True,
        )
        try:
            services.clean_location_visibility(
                cleaned.get("visibility") or None, location_type=kind, lab=lab
            )
        except services.InventoryError as exc:
            self.add_error("visibility", str(exc))
        custodian = cleaned.get("default_custodian")
        if custodian is not None and not services.may_hold_stock(custodian, lab):
            self.add_error("default_custodian", f"{custodian} isn't in {lab}.")
        if location is not None and location.active and cleaned.get("active") is False:
            self._check_empty(location)
        return cleaned

    def _check_empty(self, location):
        """A location is retired only once nothing active is left inside it. Its row is
        locked first, until the save: stock going into it now share-locks it, so it is
        either counted here or refused as no longer in use."""
        with transaction.atomic():
            Location.objects.select_for_update().get(pk=location.pk)
            held = selectors.location_contents(location, include_sublocations=True).count()
        if held:
            self.add_error(
                "active", f"{location} still holds {held} active stock item(s); move them first."
            )
        inner = selectors.child_locations(location).first()
        if inner is not None:
            self.add_error("active", f"{inner} is still in use inside it; retire or move it first.")

    def _get_validation_exclusions(self):
        exclude = super()._get_validation_exclusions()
        cleaned = getattr(self, "cleaned_data", {})
        # Filled in by create_location: a generated code, the lab's default visibility.
        exclude.update(name for name in ("code", "visibility") if not cleaned.get(name))
        return exclude

    @staticmethod
    def _service_check(check, *args, **kwargs):
        try:
            return check(*args, **kwargs)
        except services.InventoryError as exc:
            raise ValidationError(str(exc)) from exc


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    form = LocationAdminForm
    list_display = (
        "full_path",
        "code",
        "location_type",
        "temperature",
        "default_custodian",
        "active",
    )
    list_filter = ("lab", "location_type", "active", "is_unsorted", "visibility")
    search_fields = ("name", "code", "notes")
    list_select_related = ("location_type", "default_custodian", "unsorted_owner")
    ordering = ("path",)
    autocomplete_fields = ("default_custodian",)
    readonly_fields = ("full_path", "is_unsorted", "unsorted_owner", "created_at", "created_by")

    def location_of(self, obj):
        return obj

    def get_changelist(self, request, **kwargs):
        return PathsChangeList

    def get_fields(self, request, obj=None):
        fields = list(self.form.Meta.fields)
        if obj is not None:
            fields.remove("lab")
            fields = ["full_path", "lab", *fields]
        return [*fields, "is_unsorted", "unsorted_owner", "created_at", "created_by"]

    def get_readonly_fields(self, request, obj=None):
        if obj is not None:
            return [*self.readonly_fields, "lab"]
        return self.readonly_fields

    def has_change_permission(self, request, obj=None):
        # Unsorted containers are made and kept by unsorted_location.
        if obj is not None and obj.is_unsorted:
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return False  # untick "active" instead; stock history points at locations

    @admin.display(description="path")
    def full_path(self, obj):
        return _listed_path(obj, obj)

    def save_model(self, request, obj, form, change):
        data = form.cleaned_data
        if not change:
            created = services.create_location(
                lab=obj.lab,
                parent=data.get("parent"),
                name=obj.name,
                location_type=obj.location_type,
                user=request.user,
                code=obj.code,
                temperature=obj.temperature,
                default_custodian=obj.default_custodian,
                visibility=data.get("visibility") or None,
                tags=obj.tags,
                notes=obj.notes,
                photo=obj.photo,
            )
            if not obj.active:
                created.active = False
                created.save(update_fields=["active"])
            obj.pk = created.pk
            obj.refresh_from_db()
            obj._state.adding = False
            return
        # Only what the form changed (the tree fields belong to treebeard), and before the
        # move, which checks the location as it now is (a new kind, a new name).
        obj.save(update_fields=[name for name in form.changed_data if name != "parent"])
        if "parent" in form.changed_data:
            services.move_location(obj, data.get("parent"), user=request.user, reason="admin")


@admin.register(LocationEvent)
class LocationEventAdmin(ReadOnlyAdmin):
    list_display = ("location", "from_parent", "to_parent", "user", "at", "reason")
    list_filter = ("lab",)
    list_select_related = ("location", "from_parent", "to_parent", "user")
    search_fields = ("location__name", "location__code", "reason")


class StockEventInline(admin.TabularInline):
    model = StockEvent
    fields = (
        "seq",
        "kind",
        "occurred_at",
        "user",
        "source",
        "from_location",
        "to_location",
        "from_custodian",
        "to_custodian",
        "qty_delta",
        "reason",
    )
    readonly_fields = fields
    ordering = ("seq",)
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class ExternalRefInline(admin.TabularInline):
    model = ExternalRef
    fields = ("system", "external_id", "created_at")
    readonly_fields = ("created_at",)
    extra = 0


class ContainerCodeInline(admin.TabularInline):
    model = ContainerCode
    fields = ("code", "kind", "created_at")
    readonly_fields = ("created_at",)
    extra = 0


# What the steward may correct; everything else on a stock item is the ledger's.
_ITEM_EDITABLE = (
    "product",
    "supplier_product",
    "lot",
    "expiry_date",
    "expiry_precision",
    "source",
    "source_order_request",
    "attributes",
    "notes",
)


class StockItemForm(forms.ModelForm):
    class Meta:
        model = StockItem
        fields = _ITEM_EDITABLE

    def clean_product(self):
        # A merged product is a redirect; stock points at the product it leads to.
        return canonical(self.cleaned_data["product"])

    def clean(self):
        cleaned = super().clean()
        product = cleaned.get("product")
        if product is None:
            return cleaned
        supplier_product, lot = cleaned.get("supplier_product"), cleaned.get("lot")
        if supplier_product is not None and canonical(supplier_product.product).pk != product.pk:
            self.add_error("supplier_product", "That is a listing of another product.")
        if lot is not None and canonical(lot.product).pk != product.pk:
            self.add_error("lot", "That lot is of another product.")
        request = cleaned.get("source_order_request")
        if request is not None and request.lab_id != self.instance.lab_id:
            self.add_error("source_order_request", "That request belongs to another lab.")
        return cleaned


@admin.register(StockItem)
class StockItemAdmin(admin.ModelAdmin):
    form = StockItemForm
    list_display = (
        "short_id",
        "product",
        "custodian",
        "location_path",
        "lifecycle",
        "visibility",
        "lab",
    )
    list_filter = ("lab", "lifecycle", "visibility", "level", "source", "mta")
    search_fields = ("short_id", "product__name", "custodian__full_name", "custodian__email")
    list_select_related = (
        "product",
        "custodian",
        "location__location_type",
        "location__unsorted_owner",
        "lab",
    )
    ordering = ("-created_at",)
    autocomplete_fields = ("product", "supplier_product", "lot", "source_order_request")
    readonly_fields = (
        "short_id",
        "lab",
        "location_path",
        *STATE_FIELDS,
        "mta",
        "is_aggregate",
        "parent",
        "version",
        "created_at",
        "updated_at",
        "created_by",
    )
    fieldsets = (
        (None, {"fields": ("short_id", "lab", "product", "supplier_product", "lot")}),
        (
            "State (changes only through the stock ledger)",
            {"fields": ("location_path", *STATE_FIELDS, "version")},
        ),
        (
            "More",
            {
                "fields": (
                    "expiry_date",
                    "expiry_precision",
                    "source",
                    "source_order_request",
                    "mta",
                    "is_aggregate",
                    "parent",
                    "attributes",
                    "notes",
                    "created_at",
                    "updated_at",
                    "created_by",
                )
            },
        ),
    )
    inlines = [StockEventInline, ExternalRefInline, ContainerCodeInline]

    def location_of(self, obj):
        return obj.location

    def get_changelist(self, request, **kwargs):
        return PathsChangeList

    def get_search_results(self, request, queryset, search_term):
        # Short ids as people copy them off a label: "7k3m-9q", O for 0, I or L for 1.
        results, may_have_duplicates = super().get_search_results(request, queryset, search_term)
        short_id = as_short_id(search_term)
        if short_id:
            results |= queryset.filter(short_id=short_id)
        return results, may_have_duplicates

    def has_add_permission(self, request):
        return False  # services.create_stock_item writes the "created" event

    def has_delete_permission(self, request, obj=None):
        return False  # its events keep it forever

    @admin.display(description="where")
    def location_path(self, obj):
        return _listed_path(obj, obj.location)


@admin.register(StockEvent)
class StockEventAdmin(ReadOnlyAdmin):
    list_display = ("stock_item", "seq", "kind", "occurred_at", "user", "source")
    list_filter = ("lab", "kind", "source")
    list_select_related = ("stock_item__product", "user")
    search_fields = ("stock_item__short_id", "reason")
    ordering = ("-recorded_at",)


@admin.register(ExternalRef)
class ExternalRefAdmin(admin.ModelAdmin):
    list_display = ("external_id", "system", "stock_item", "created_at")
    list_filter = ("system",)
    search_fields = ("external_id", "stock_item__short_id")
    autocomplete_fields = ("stock_item",)


@admin.register(ContainerCode)
class ContainerCodeAdmin(admin.ModelAdmin):
    list_display = ("code", "kind", "stock_item", "created_at")
    list_filter = ("kind",)
    search_fields = ("code", "stock_item__short_id")
    autocomplete_fields = ("stock_item",)


class HistoryClaimForm(forms.ModelForm):
    class Meta:
        model = HistoryClaim
        fields = (
            "lab",
            "order_request",
            "person",
            "round",
            "outcome",
            "count_kept",
            "target_person",
            "stock_items",
            "surfaced_at",
            "decided_at",
            "notes",
        )

    def clean(self):
        """The request, the people and the stock all belong to the claim's lab."""
        cleaned = super().clean()
        lab = cleaned.get("lab")
        if lab is None:
            return cleaned
        request = cleaned.get("order_request")
        if request is not None and request.lab_id != lab.pk:
            self.add_error("order_request", "That request belongs to another lab.")
        for name in ("person", "target_person"):
            person = cleaned.get(name)
            if person is not None and not services.may_hold_stock(person, lab):
                self.add_error(name, f"{person} isn't in {lab}.")
        stock = cleaned.get("stock_items") or ()
        foreign = [item.short_id for item in stock if item.lab_id != lab.pk]
        if foreign:
            self.add_error("stock_items", f"Not stock of {lab}: {', '.join(foreign)}.")
        return cleaned


@admin.register(HistoryClaim)
class HistoryClaimAdmin(admin.ModelAdmin):
    form = HistoryClaimForm
    list_display = ("order_request", "person", "round", "outcome", "surfaced_at", "decided_at")
    list_filter = ("lab", "outcome", "round")
    search_fields = ("order_request__item_name", "person__full_name", "person__email")
    list_select_related = ("order_request", "person")
    autocomplete_fields = ("order_request", "person", "target_person", "stock_items")
