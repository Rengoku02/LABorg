import pytest

from apps.core.models import PersonAlias, User
from apps.core.people import alias_email, resolve_person

QUARTZY_USER = {
    "id": "5D3B8C1E-0000-4000-8000-000000000001",
    "email": "Priya@Lab.test",
    "first_name": "Priya",
    "last_name": "Patel",
}


@pytest.mark.django_db
def test_resolves_existing_user_by_email_and_links_both_aliases(member):
    alias = resolve_person(QUARTZY_USER)
    assert alias.kind == PersonAlias.Kind.QUARTZY_USER_ID
    assert alias.value == QUARTZY_USER["id"].lower()
    assert alias.user == member
    assert PersonAlias.objects.get(kind="email").user == member
    assert alias_email(alias) == "priya@lab.test"


@pytest.mark.django_db
def test_unknown_person_goes_to_manual_queue_then_resolves_by_id(db):
    alias = resolve_person({**QUARTZY_USER, "email": None})
    assert alias.user is None and alias.display_name == "Priya Patel"
    user = User.objects.create_user(email="someone@else.test", full_name="Priya P.")
    alias.user = user
    alias.save()
    # Quartzy later sends a different email; the id alias still wins.
    again = resolve_person({**QUARTZY_USER, "email": "p.patel@other.test"})
    assert again.user == user
    assert PersonAlias.objects.get(kind="email", value="p.patel@other.test").user == user


@pytest.mark.django_db
def test_no_user_returns_none(db):
    assert resolve_person(None) is None
    assert resolve_person({"first_name": "x"}) is None
