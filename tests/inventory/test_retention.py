from datetime import timedelta

import pytest
from django.utils import timezone

from apps.core.policies import set_policy
from apps.inventory.models import StockEvent
from apps.inventory.retention import pseudonymize_event_users
from apps.inventory.services import apply_event

pytestmark = pytest.mark.django_db


def test_old_events_stop_naming_people_once_the_pi_decides(lab, make_item, member, raj):
    item = make_item()
    apply_event(item, "marked_low", user=raj)
    assert pseudonymize_event_users(lab) == 0  # no policy yet: nothing changes
    set_policy("inventory.pseudonymize_after_days", 30, lab=lab)
    later = timezone.now() + timedelta(days=31)
    assert pseudonymize_event_users(lab, now=later) == 2
    users = list(StockEvent.objects.order_by("seq").values_list("user__full_name", flat=True))
    assert all(name.startswith("Former actor ") for name in users)
    assert users[0] != users[1]  # Priya and Raj stay two different pseudonyms
    assert pseudonymize_event_users(lab, now=later) == 0  # already done
    item.refresh_from_db()
    assert item.custodian == member  # who holds it now is not history
