from django.apps import AppConfig


class InventoryConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.inventory"
    label = "inventory"
    verbose_name = "Inventory"

    def ready(self):
        from apps.core import schedules

        from . import asks, claims, tasks  # noqa: F401  (they register email hooks)

        schedules.register(tasks.SCHEDULE_PREFIX, tasks.wanted_schedules)
