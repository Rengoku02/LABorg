"""What the Quartzy mirror does when rows of other apps change; QuartzyConfig.ready() connects
these receivers. apps.catalog must not import apps.quartzy, so the mirror listens instead.

- An ItemType saved with another identity rule resolves the requests of every Quartzy type
  mapped to it again once the save commits (tasks.resolve_type), as a changed mapping does
  (QuartzyType.save). The rule is the only field of an item type that a request's resolution
  reads (catalog.services._is_generic); the guard reads the others when it checks a request.
  A QuerySet.update() of item types sends no signal, so it resolves nothing again.
"""

from .models import QuartzyType, resolve_types_on_commit

# The ItemType fields catalog.services.resolve_supplier_product decides with.
ITEM_TYPE_RESOLUTION_FIELDS = ("identity_rule",)


def item_type_before_save(sender, instance, raw=False, update_fields=None, **kwargs):
    """pre_save of ItemType: remember the resolution fields as the database has them."""
    instance._quartzy_resolution_fields = None
    if raw or instance._state.adding:  # a fixture load, or a new type nothing maps onto yet
        return
    if update_fields is not None and not set(ITEM_TYPE_RESOLUTION_FIELDS) & set(update_fields):
        return
    stored = sender._base_manager.filter(pk=instance.pk)
    instance._quartzy_resolution_fields = stored.values_list(*ITEM_TYPE_RESOLUTION_FIELDS).first()


def item_type_saved(sender, instance, created, raw=False, **kwargs):
    """post_save of ItemType: if a resolution field changed, the requests of the Quartzy types
    mapped to it are resolved again once this commits."""
    stored = getattr(instance, "_quartzy_resolution_fields", None)
    if created or raw or stored is None:
        return
    if stored != tuple(getattr(instance, name) for name in ITEM_TYPE_RESOLUTION_FIELDS):
        mapped = QuartzyType.objects.filter(item_type=instance).order_by("pk")
        resolve_types_on_commit(list(mapped.values_list("pk", flat=True)))
