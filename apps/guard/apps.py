from django.apps import AppConfig


class GuardConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.guard"
    label = "guard"
    verbose_name = "Duplicate guard"
