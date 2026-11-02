from django.apps import AppConfig


class GuardConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.guard"
    label = "guard"
    verbose_name = "Duplicate guard"

    def ready(self):
        from apps.core import schedules
        from apps.inventory.signals import ask_answered
        from apps.quartzy.signals import requests_synced

        from . import actions, alerts, receivers, tasks  # noqa: F401  (they register hooks)

        requests_synced.connect(receivers.requests_synced, dispatch_uid="guard.requests_synced")
        ask_answered.connect(receivers.ask_answered, dispatch_uid="guard.ask_answered")
        schedules.register(tasks.SCHEDULE_PREFIX, tasks.wanted_schedules)
