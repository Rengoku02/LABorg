"""The guard in the admin: checks and their matches to look at, pair suppressions to manage.

Labelling happens on /guard/label/ (it shows stock through the presenters); going live is a
PolicySetting change (``alerts.enabled``, ``alerts.tiers_live``): see docs/runbook.md.
"""

from django.contrib import admin

from apps.quartzy.admin import ReadOnlyAdmin, ReadOnlyInline

from .models import CheckRun, DuplicateCheck, DuplicateMatch, Outcome, PairSuppression


class MatchInline(ReadOnlyInline):
    model = DuplicateMatch
    fk_name = "duplicate_check"
    fields = (
        "tier",
        "candidate_kind",
        "score",
        "why",
        "candidate_order_request",
        "suppressed",
        "suppression_reason",
        "label",
        "labelled_by",
        "label_suggestion",
    )
    readonly_fields = fields


class OutcomeInline(ReadOnlyInline):
    model = Outcome
    fk_name = "duplicate_check"
    fields = ("kind", "reason", "decided_by", "decided_at", "verified_at", "est_value_avoided")
    readonly_fields = fields


@admin.register(DuplicateCheck)
class DuplicateCheckAdmin(ReadOnlyAdmin):
    list_display = (
        "order_request",
        "status",
        "best_tier",
        "replay",
        "late",
        "quartzy_native_would_fire_raw",
        "created_at",
    )
    list_filter = ("status", "replay", "best_tier", "matcher_version", "lab")
    search_fields = ("order_request__item_name", "order_request__catalog_number_raw")
    date_hierarchy = "created_at"
    inlines = [MatchInline, OutcomeInline]
    list_select_related = ("order_request",)


@admin.register(CheckRun)
class CheckRunAdmin(ReadOnlyAdmin):
    list_display = ("via", "lab", "started_at", "finished_at", "requests", "alerts_planned", "held")
    list_filter = ("via", "held", "lab")


@admin.register(PairSuppression)
class PairSuppressionAdmin(admin.ModelAdmin):
    """Lab-wide suppressions are the co-owner's call (design §5); requester-scoped ones come
    from people's "not the same thing" / "need my own" answers."""

    list_display = (
        "subject_product",
        "candidate_product",
        "scope",
        "requester",
        "expires_at",
        "reason",
    )
    list_filter = ("scope", "lab")
    autocomplete_fields = ("subject_product", "candidate_product", "requester")
    readonly_fields = ("created_by", "created_at")

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)
