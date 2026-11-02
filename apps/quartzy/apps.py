from django.apps import AppConfig


class QuartzyConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.quartzy"
    label = "quartzy"
    verbose_name = "Quartzy mirror"

    def ready(self):
        from django.db.models.signals import post_save, pre_save

        from apps.catalog.models import ItemType

        from . import receivers

        pre_save.connect(
            receivers.item_type_before_save,
            sender=ItemType,
            dispatch_uid="quartzy.item_type_before_save",
        )
        post_save.connect(
            receivers.item_type_saved, sender=ItemType, dispatch_uid="quartzy.item_type_saved"
        )
