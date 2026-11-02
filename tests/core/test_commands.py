import pytest
from django.core.management import call_command

from apps.core.models import Membership, User


@pytest.mark.django_db
def test_provision_members_dry_run_then_commit(lab, tmp_path):
    roster = tmp_path / "roster.csv"
    roster.write_text(
        "email,full_name,role,status,start_date,end_date,quartzy_lab_admin\n"
        "Priya@Lab.test,Priya Patel,co_owner,active,2024-09-01,,yes\n"
        "raj@lab.test,Raj Kumar,,rotator,,2026-12-31,\n"
    )
    call_command("provision_members", str(roster), lab="test-lab", verbosity=0)
    assert not User.objects.filter(email="priya@lab.test").exists()
    call_command("provision_members", str(roster), lab="test-lab", commit=True, verbosity=0)
    priya = Membership.objects.get(user__email="priya@lab.test")
    assert priya.role == "co_owner" and priya.is_quartzy_lab_admin
    raj = Membership.objects.get(user__email="raj@lab.test")
    assert raj.status == "rotator" and str(raj.end_date) == "2026-12-31"
    # Re-running is safe.
    call_command("provision_members", str(roster), lab="test-lab", commit=True, verbosity=0)
    assert User.objects.filter(kind="member").count() == 2


@pytest.mark.django_db
def test_bootstrap_lab_is_idempotent(lab):
    call_command("bootstrap_lab", name="Test Lab", slug="test-lab", verbosity=0)
    assert User.objects.filter(kind=User.Kind.POOL).count() == 1


@pytest.mark.django_db
def test_roster_marks_champions_and_the_template_loads(lab):
    from apps.core.permissions import is_champion

    call_command(
        "provision_members", "docs/templates/roster.csv", lab="test-lab", commit=True, verbosity=0
    )
    champions = Membership.objects.filter(lab=lab, is_champion=True)
    assert [m.user.email for m in champions] == ["priya@example.edu"]
    priya = champions[0].user
    pat = Membership.objects.get(user__email="pi@example.edu").user
    manager = Membership.objects.get(user__email="manager@example.edu").user
    assert is_champion(priya, lab) and is_champion(manager, lab)  # co-owners may too
    assert not is_champion(pat, lab)
