"""Map Quartzy user objects (``{id, email, first_name, last_name}``) onto Users."""

from django.db import transaction

from .models import PersonAlias, User


def _display_name(quartzy_user):
    parts = [quartzy_user.get("first_name") or "", quartzy_user.get("last_name") or ""]
    return " ".join(p.strip() for p in parts if p and p.strip())


@transaction.atomic
def resolve_person(quartzy_user):
    """Return the PersonAlias for a Quartzy user, creating aliases as needed.

    The alias's ``user`` is filled when the person can be matched by Quartzy id or email;
    otherwise it stays empty and the alias shows up in the admin's unresolved queue.
    Returns None when Quartzy sent no user at all.
    """
    if not quartzy_user or not quartzy_user.get("id"):
        return None
    quartzy_id = str(quartzy_user["id"]).lower()
    email = (quartzy_user.get("email") or "").strip().lower() or None
    name = _display_name(quartzy_user)

    id_alias, _ = PersonAlias.objects.select_for_update().get_or_create(
        kind=PersonAlias.Kind.QUARTZY_USER_ID,
        value=quartzy_id,
        defaults={"display_name": name},
    )
    email_alias = None
    if email:
        email_alias, _ = PersonAlias.objects.select_for_update().get_or_create(
            kind=PersonAlias.Kind.EMAIL, value=email, defaults={"display_name": name}
        )

    user = id_alias.user or (email_alias.user if email_alias else None)
    if user is None and email:
        user = User.objects.filter(email=email).first()

    for alias in filter(None, [id_alias, email_alias]):
        changed = []
        if user and alias.user_id is None:
            alias.user = user
            changed.append("user")
        if name and alias.display_name != name:
            alias.display_name = name
            changed.append("display_name")
        if changed:
            alias.save(update_fields=[*changed, "last_seen_at"])
    return id_alias


def alias_email(alias):
    """The best email for an alias: the linked user's, else the one Quartzy sent."""
    if alias is None:
        return None
    if alias.user and alias.user.email:
        return alias.user.email
    if alias.kind == PersonAlias.Kind.EMAIL:
        return alias.value
    sibling = (
        PersonAlias.objects.filter(kind=PersonAlias.Kind.EMAIL, user=alias.user)
        .exclude(user=None)
        .first()
    )
    return sibling.value if sibling else None
