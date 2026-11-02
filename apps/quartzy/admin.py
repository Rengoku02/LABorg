"""The Quartzy mirror in the admin.

Mirrored data is read-only here because the sync owns it. People map and arm Quartzy labs,
curate the Quartzy Type → ItemType mapping, set ``requested_for`` and flag requests for the
hot set.
"""

import json

from django.contrib import admin, messages
from django.db.models import Count
from django.utils import timezone
from django.utils.html import format_html

from .models import (
    OrderRequest,
    OrderStatusObservation,
    QuartzyInventoryInstance,
    QuartzyInventoryItem,
    QuartzyLab,
    QuartzyType,
    SpecSnapshot,
    SyncError,
    SyncRun,
)
from .services import WATCH_DAYS, watch_requests


def _pretty(data):
    text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False, default=str)
    return format_html("<pre>{}</pre>", text)


# What people decide about a Quartzy lab; the rest is the sync's.
_LAB_EDITABLE = ("lab", "is_active", "alerts_armed_at", "cutover_at", "backoff_until")


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class ReadOnlyInline(admin.TabularInline):
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(QuartzyLab)
class QuartzyLabAdmin(admin.ModelAdmin):
    list_display = ("__str__", "lab", "is_active", "alerts_armed_at", "last_synced_at")
    list_filter = ("is_active", "lab")
    search_fields = ("name", "quartzy_id", "organization_name")
    readonly_fields = [
        f.name for f in QuartzyLab._meta.fields if f.name not in {"id", *_LAB_EDITABLE}
    ]

    def has_add_permission(self, request):
        return False  # sync_labs creates them

    def has_delete_permission(self, request, obj=None):
        return False  # manage.py quartzy_purge_lab deletes an inactive one with its rows

    def save_model(self, request, obj, form, change):
        # QuartzyLab.save moves the requests to the new lab, in this same transaction.
        super().save_model(request, obj, form, change)
        if change and "lab" in form.changed_data:
            count = obj.order_requests.count()
            self.message_user(request, f"{obj}: its {count} request(s) moved to {obj.lab}.")


@admin.register(QuartzyType)
class QuartzyTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "quartzy_lab", "item_type", "suggested", "reviewed_at", "request_count")
    list_display_links = ("name",)
    list_editable = ("item_type",)
    list_filter = ("suggested", "quartzy_lab", "item_type")
    list_select_related = ("quartzy_lab", "item_type")
    search_fields = ("name", "quartzy_id")
    fields = ("name", "quartzy_lab", "quartzy_id", "item_type", "suggested", "reviewed_at")
    readonly_fields = ("name", "quartzy_lab", "quartzy_id", "suggested", "reviewed_at")
    actions = ["confirm_suggestions"]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(requests=Count("order_requests"))

    @admin.display(description="Requests", ordering="requests")
    def request_count(self, obj):
        return obj.requests

    def save_model(self, request, obj, form, change):
        # Saved by a person, in the form or in the list: the mapping is theirs now. A changed
        # item type re-resolves the type's requests once this commits (QuartzyType.save).
        obj.suggested = False
        obj.reviewed_at = timezone.now()
        super().save_model(request, obj, form, change)
        count = obj.order_requests.count() if "item_type" in form.changed_data else 0
        if count:
            message = f"{obj}: its {count} request(s) are being resolved again in the background."
            self.message_user(request, message)

    @admin.action(description="Confirm the suggested item types")
    def confirm_suggestions(self, request, queryset):
        confirmed = queryset.filter(suggested=True).update(
            suggested=False, reviewed_at=timezone.now()
        )
        self.message_user(request, f"Confirmed {confirmed} mapping(s).", messages.SUCCESS)


class ObservationInline(ReadOnlyInline):
    model = OrderStatusObservation
    fields = ("observed_at", "from_status", "to_status", "via", "quartzy_updated_at")
    readonly_fields = fields


@admin.register(OrderRequest)
class OrderRequestAdmin(admin.ModelAdmin):
    list_display = (
        "__str__",
        "vendor_name_raw",
        "catalog_number_raw",
        "status",
        "requested_by",
        "requested_at",
        "resolution_status",
        "source",
    )
    list_filter = ("status", "source", "resolution_status", "lab", "quartzy_lab")
    list_select_related = ("requested_by",)
    search_fields = ("item_name", "catalog_number_raw", "vendor_name_raw", "quartzy_id")
    date_hierarchy = "requested_at"
    ordering = ("-requested_at",)
    autocomplete_fields = ("requested_for",)
    exclude = ("raw_payload",)
    readonly_fields = [
        *(
            f.name
            for f in OrderRequest._meta.fields
            if f.name not in {"id", "requested_for", "hot_until", "raw_payload"}
        ),
        "payload",
    ]
    inlines = [ObservationInline]
    actions = ["watch"]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Payload as Quartzy sent it")
    def payload(self, obj):
        return _pretty(obj.raw_payload)

    @admin.action(description="Watch: re-read by id (hot set) for 7 days")
    def watch(self, request, queryset):
        watched = watch_requests(queryset, days=WATCH_DAYS)
        self.message_user(
            request, f"Watching {watched} request(s) for {WATCH_DAYS} days.", messages.SUCCESS
        )


class SyncErrorInline(ReadOnlyInline):
    model = SyncError
    fields = ("created_at", "kind", "quartzy_id", "message")
    readonly_fields = fields


@admin.register(SyncRun)
class SyncRunAdmin(ReadOnlyAdmin):
    list_display = (
        "kind",
        "quartzy_lab",
        "status",
        "started_at",
        "finished_at",
        "calls",
        "pages",
        "seen",
        "new",
        "changed",
        "errors",
    )
    list_filter = ("kind", "status", "quartzy_lab")
    date_hierarchy = "started_at"
    inlines = [SyncErrorInline]


@admin.register(SyncError)
class SyncErrorAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "kind", "quartzy_id", "message", "sync_run")
    list_filter = ("kind",)
    search_fields = ("quartzy_id", "message")
    exclude = ("payload",)
    readonly_fields = ("sync_run", "created_at", "kind", "quartzy_id", "message", "payload_shown")

    @admin.display(description="Payload")
    def payload_shown(self, obj):
        return _pretty(obj.payload)


class InstanceInline(ReadOnlyInline):
    model = QuartzyInventoryInstance
    fields = ("quartzy_id", "quantity_raw", "lot_number", "expiration_date", "location_path")
    readonly_fields = fields


@admin.register(QuartzyInventoryItem)
class QuartzyInventoryItemAdmin(ReadOnlyAdmin):
    list_display = (
        "name",
        "vendor_name",
        "catalog_number",
        "quantity_raw",
        "quartzy_lab",
        "deleted_in_quartzy_at",
    )
    list_filter = ("quartzy_lab",)
    search_fields = ("name", "vendor_name", "catalog_number", "cas_number", "quartzy_id")
    exclude = ("raw_payload",)
    inlines = [InstanceInline]


@admin.register(SpecSnapshot)
class SpecSnapshotAdmin(ReadOnlyAdmin):
    list_display = ("sha256", "fetched_at")
