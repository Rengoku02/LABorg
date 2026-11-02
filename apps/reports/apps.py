from django.apps import AppConfig


class ReportsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.reports"
    label = "reports"
    verbose_name = "Reports"

    def ready(self):
        from apps.core import schedules

        from . import budget, digests, nudges, tasks  # noqa: F401  (they register hooks)

        schedules.register(tasks.SCHEDULE_PREFIX, tasks.wanted_schedules)
