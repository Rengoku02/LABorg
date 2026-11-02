"""Retention (plan §11; docs/privacy.md): stock events are kept, but after
``inventory.pseudonymize_after_days`` they stop naming who did them.

Each person becomes a stable pseudonym per lab (a placeholder user named by an HMAC of the
lab and the person), so "the same person moved these" survives while who it was doesn't.
The ledger allows exactly this one change (``StockEvent.user``). Off until the PI sets the
policy (None).
"""

import hashlib
import hmac
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.core.models import User
from apps.core.policies import get_policy

from .models import StockEvent

NAME_PREFIX = "Former actor "


def pseudonym(lab, user_id):
    key = settings.SECRET_KEY.encode()
    digest = hmac.new(key, f"labstock:{lab.pk}:{user_id}".encode(), hashlib.sha256).hexdigest()
    name = f"{NAME_PREFIX}{digest[:10]}"
    user = User.objects.filter(kind=User.Kind.PLACEHOLDER, full_name=name).first()
    if user is None:
        user = User.objects.create_user(
            email=None, full_name=name, kind=User.Kind.PLACEHOLDER, is_active=False
        )
    return user


def pseudonymize_event_users(lab, *, now=None):
    """Replace who did stock events older than the retention period with pseudonyms.
    Returns how many events changed; 0 while the policy is unset."""
    days = get_policy("inventory.pseudonymize_after_days", lab)
    if not days:
        return 0
    cutoff = (now or timezone.now()) - timedelta(days=int(days))
    old = StockEvent.objects.filter(lab=lab, recorded_at__lt=cutoff).exclude(
        user__full_name__startswith=NAME_PREFIX, user__kind=User.Kind.PLACEHOLDER
    )
    changed = 0
    for user_id in old.exclude(user=None).values_list("user_id", flat=True).distinct():
        changed += old.filter(user_id=user_id).update(user=pseudonym(lab, user_id))
    return changed
