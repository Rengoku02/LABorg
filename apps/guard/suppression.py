"""Matches the guard records but doesn't act on (design §6 "Suppression").

- ``recurring``: the product was requested ``guard.recurring_per_year`` times or more in the
  12 months before (a consumable the lab buys again and again), unless the match is another
  open request: two open at once is still a double order.
- ``pair``: someone said "not the same thing" or "need my own" about this pair of products
  (PairSuppression, either way round): for that requester, or for the whole lab when the
  co-owner decided so, until it expires.

Out-of-scope item types never get this far: their checks are ``out_of_scope``.
"""

from datetime import timedelta

from django.db.models import Q
from django.db.models.functions import Coalesce

from apps.core.policies import get_policy
from apps.inventory.selectors import product_family_ids
from apps.quartzy.models import OrderRequest

from .models import PairSuppression

RECURRING = "recurring"
PAIR = "pair"


def reasons(subject, candidates, *, as_of):
    """{candidate.key: reason} for the suppressed ones among ``candidates``."""
    if not candidates:
        return {}
    request = subject.request
    recurring = is_recurring(request.lab, subject.product_ids, as_of)
    paired = paired_products(request.lab, subject.product_ids, subject.requester_id, as_of)
    found = {}
    for candidate in candidates:
        if recurring and candidate.kind != "open":
            found[candidate.key] = RECURRING
        elif candidate.product_id is not None and candidate.product_id in paired:
            found[candidate.key] = PAIR
    return found


def is_recurring(lab, product_ids, as_of):
    """Was the product requested at least ``guard.recurring_per_year`` times in the 12
    months up to ``as_of`` (cancelled requests left out)?"""
    threshold = get_policy("guard.recurring_per_year", lab)
    if not threshold:
        return False
    count = (
        OrderRequest.objects.for_lab(lab)
        .live()
        .filter(product_id__in=product_ids)
        .exclude(status=OrderRequest.Status.CANCELLED)
        .annotate(at=Coalesce("requested_at", "first_seen_at"))
        .filter(at__gt=as_of - timedelta(days=365), at__lte=as_of)
        .count()
    )
    return count >= threshold


def paired_products(lab, product_ids, requester_id, as_of):
    """The product ids an active PairSuppression says these products are not to be matched
    with, for this requester (or anyone in the lab)."""
    scope = Q(scope=PairSuppression.Scope.LAB)
    if requester_id is not None:
        scope |= Q(scope=PairSuppression.Scope.REQUESTER, requester_id=requester_id)
    rows = (
        PairSuppression.objects.for_lab(lab)
        .active(as_of)
        .filter(scope)
        .filter(Q(subject_product_id__in=product_ids) | Q(candidate_product_id__in=product_ids))
    )
    others = set()
    for row in rows:
        other = (
            row.candidate_product_id
            if row.subject_product_id in product_ids
            else row.subject_product_id
        )
        others |= product_family_ids([other])
    return others
