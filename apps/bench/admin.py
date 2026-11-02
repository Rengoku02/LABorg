from django.contrib import admin

from .models import FrictionReport


@admin.register(FrictionReport)
class FrictionReportAdmin(admin.ModelAdmin):
    list_display = ("created_at", "page", "text", "resolved")
    list_filter = ("resolved", "lab")
    readonly_fields = ("lab", "user", "page", "text", "created_at")
