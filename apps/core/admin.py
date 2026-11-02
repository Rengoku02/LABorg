from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.translation import gettext_lazy as _

from .mail import release_notification
from .models import Lab, Membership, Notification, PersonAlias, PolicySetting, User


class MembershipInline(admin.TabularInline):
    model = Membership
    extra = 0
    fields = ("lab", "role", "status", "start_date", "end_date", "is_quartzy_lab_admin")


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    ordering = ("full_name",)
    list_display = ("full_name", "email", "kind", "is_active", "is_staff")
    list_filter = ("kind", "is_active", "is_staff", "is_superuser")
    search_fields = ("full_name", "email")
    inlines = [MembershipInline]
    fieldsets = (
        (None, {"fields": ("email", "full_name", "kind", "password")}),
        (
            _("Permissions"),
            {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")},
        ),
        (_("Dates"), {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = ((None, {"classes": ("wide",), "fields": ("email", "full_name", "kind")}),)
    readonly_fields = ("last_login", "date_joined")


@admin.register(Lab)
class LabAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "time_zone", "pool_user")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "lab",
        "role",
        "status",
        "start_date",
        "end_date",
        "is_quartzy_lab_admin",
    )
    list_filter = ("lab", "role", "status", "is_quartzy_lab_admin")
    search_fields = ("user__full_name", "user__email")
    autocomplete_fields = ("user",)


class UnresolvedFilter(admin.SimpleListFilter):
    title = "resolution"
    parameter_name = "resolution"

    def lookups(self, request, model_admin):
        return (("unresolved", "Needs a person"), ("resolved", "Resolved"))

    def queryset(self, request, queryset):
        if self.value() == "unresolved":
            return queryset.filter(user__isnull=True)
        if self.value() == "resolved":
            return queryset.filter(user__isnull=False)
        return queryset


@admin.register(PersonAlias)
class PersonAliasAdmin(admin.ModelAdmin):
    list_display = ("display_name", "kind", "value", "user", "last_seen_at")
    list_filter = (UnresolvedFilter, "kind")
    search_fields = ("display_name", "value", "user__full_name", "user__email")
    autocomplete_fields = ("user",)
    readonly_fields = ("first_seen_at", "last_seen_at")


@admin.register(PolicySetting)
class PolicySettingAdmin(admin.ModelAdmin):
    list_display = ("key", "value", "lab", "updated_at", "updated_by")
    list_filter = ("lab",)
    search_fields = ("key", "note")
    readonly_fields = ("updated_at", "updated_by")

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("kind", "recipient_email", "status", "created_at", "sent_at", "action")
    list_filter = ("kind", "status", "lab")
    search_fields = ("recipient_email", "subject_id", "hold_reason")
    # The nonce is deliberately not shown anywhere in the admin.
    exclude = ("nonce",)
    readonly_fields = [f.name for f in Notification._meta.fields if f.name not in {"nonce", "id"}]
    actions = ["release_held", "revoke_links"]

    def has_add_permission(self, request):
        return False

    @admin.action(description="Release held notifications (send now)")
    def release_held(self, request, queryset):
        released = sum(release_notification(n) for n in queryset)
        self.message_user(request, f"Released {released} notification(s).", messages.SUCCESS)

    @admin.action(description="Revoke emailed links (rotate nonce)")
    def revoke_links(self, request, queryset):
        for notification in queryset:
            notification.rotate_nonce()
        self.message_user(request, "Links revoked.", messages.SUCCESS)
