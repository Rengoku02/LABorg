"""Plain permission predicates (no ``rules`` dependency; plan §12).

Views, presenters and emails call these; nothing else decides who may see what.
"""

from .models import Membership


def current_membership(user, lab):
    if lab is None or not getattr(user, "is_authenticated", False):
        return None
    membership = Membership.objects.filter(user=user, lab=lab).first()
    if membership and membership.is_current():
        return membership
    return None


def is_member(user, lab):
    return current_membership(user, lab) is not None


def has_role(user, lab, *roles):
    membership = current_membership(user, lab)
    return membership is not None and membership.role in roles


def is_system_admin(user, lab=None):
    if getattr(user, "is_superuser", False):
        return True
    return lab is not None and has_role(user, lab, Membership.Role.ADMIN)


def is_co_owner(user, lab):
    return is_system_admin(user, lab) or has_role(user, lab, Membership.Role.CO_OWNER)


def is_pi(user, lab):
    return has_role(user, lab, Membership.Role.PI)


def is_champion(user, lab):
    """Bench champions work the lab-pool claims list; co-owners and system owners may too."""
    membership = current_membership(user, lab)
    return (membership is not None and membership.is_champion) or is_co_owner(user, lab)


def can_label_matches(user, lab):
    """Labelling shadow-mode matches needs wet-lab judgement: co-owner or system owner."""
    return is_co_owner(user, lab)
