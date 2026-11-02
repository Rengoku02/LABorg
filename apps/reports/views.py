"""The lab-totals dashboard (plan §11; design §10.8)."""

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import render
from django.views.decorators.http import require_GET

from apps.core.permissions import is_co_owner, is_member, is_pi
from apps.core.policies import get_policy

from . import metrics


def may_see_dashboard(user, lab):
    """The PI, co-owners and system owners; every member when
    ``reports.dashboard_audience`` is "members"."""
    if lab is None or (not is_member(user, lab) and not is_co_owner(user, lab)):
        return False
    if is_pi(user, lab) or is_co_owner(user, lab):
        return True
    return get_policy("reports.dashboard_audience", lab) == "members"


@require_GET
@login_required
def dashboard(request):
    lab = getattr(request, "lab", None)
    if not may_see_dashboard(request.user, lab):
        raise PermissionDenied
    period = metrics.quarter(lab)
    return render(request, "reports/dashboard.html", {"o": metrics.overview(lab, period)})
