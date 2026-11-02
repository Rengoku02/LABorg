import pytest
from django.db import IntegrityError

from apps.core.models import Membership, PolicySetting, User
from apps.core.policies import DEFAULTS, get_policy, set_policy


@pytest.mark.django_db
def test_email_is_lowercased_and_unique():
    User.objects.create_user(email="Priya.Patel@Lab.TEST", full_name="Priya")
    assert User.objects.get().email == "priya.patel@lab.test"
    with pytest.raises(IntegrityError):
        User.objects.create_user(email="PRIYA.PATEL@lab.test", full_name="Dup")


@pytest.mark.django_db
def test_placeholder_and_pool_users_can_never_log_in(lab):
    ghost = User.objects.create_user(
        email=None, full_name="Old Postdoc", kind=User.Kind.PLACEHOLDER
    )
    assert ghost.is_active is False and ghost.email is None
    assert lab.pool_user.kind == User.Kind.POOL and lab.pool_user.is_active is False
    ghost.is_active = True
    ghost.save()  # save() forces non-members inactive
    ghost.refresh_from_db()
    assert ghost.is_active is False


@pytest.mark.django_db
def test_membership_is_current(make_member, lab):
    user = make_member(end_date=None)
    membership = Membership.objects.get(user=user)
    assert membership.is_current()
    membership.status = Membership.Status.LEAVER
    assert not membership.is_current()


@pytest.mark.django_db
def test_policy_defaults_and_overrides(lab):
    assert get_policy("nudges.stale_days") == DEFAULTS["nudges.stale_days"]
    set_policy("nudges.stale_days", 30)
    assert get_policy("nudges.stale_days") == 30
    assert get_policy("nudges.stale_days", lab=lab) == 30
    set_policy("nudges.stale_days", 14, lab=lab)
    assert get_policy("nudges.stale_days", lab=lab) == 14
    assert get_policy("nudges.stale_days") == 30
    assert PolicySetting.objects.count() == 2
    with pytest.raises(KeyError):
        get_policy("no.such.policy")


@pytest.mark.django_db
def test_global_policy_row_is_unique():
    set_policy("alerts.enabled", True)
    with pytest.raises(IntegrityError):
        PolicySetting.objects.create(key="alerts.enabled", value=False)
