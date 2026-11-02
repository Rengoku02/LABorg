from django.apps import AppConfig


class GuardConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.guard"
    label = "guard"
    verbose_name = "Duplicate guard"

    def ready(self):
        from apps.core import schedules
        from apps.quartzy.signals import requests_synced

        from . import receivers, tasks

        requests_synced.connect(receivers.requests_synced, dispatch_uid="guard.requests_synced")
        schedules.register(tasks.SCHEDULE_PREFIX, tasks.wanted_schedules)
