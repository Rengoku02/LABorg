"""The site-wide "Quartzy sync is behind" banner (plan §6 "Monitoring").

New requests reach LabStock, and so the duplicate guard, only through the head sync, so every
page tells the lab's members when it has not succeeded for 30 minutes.
"""

from django.core.cache import cache
from django.utils.functional import SimpleLazyObject

from .models import QuartzyLab

BEHIND_CACHE_SECONDS = 60


def quartzy(request):
    # request.lab is set only for the current lab's members (and for the superuser of a
    # one-lab install); the check runs only when a page renders the banner.
    lab = getattr(request, "lab", None)
    if lab is None or not request.user.is_authenticated:
        return {}
    return {"quartzy_sync_behind": SimpleLazyObject(lambda: sync_behind(lab))}


def sync_behind(lab):
    """None while every active Quartzy lab of ``lab`` had a successful head sync in the last
    30 minutes; else ``{"last_update": ...}``, the oldest of their last ones (None: never).
    One query, then cached for a minute."""
    key = f"quartzy:behind:{lab.pk}"
    found = cache.get(key)
    if found is None:
        stale = QuartzyLab.objects.for_lab(lab).stale()
        last = list(stale.values_list("last_head_ok", flat=True))
        found = (bool(last), None if None in last else min(last, default=None))
        cache.set(key, found, BEHIND_CACHE_SECONDS)
    behind, last_update = found
    return {"last_update": last_update} if behind else None
