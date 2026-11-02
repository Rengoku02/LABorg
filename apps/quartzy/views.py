"""The staff-only sync health panel (plan §6 "Monitoring", §8)."""

from datetime import timedelta
from functools import wraps

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q, Sum
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET

from apps.catalog.models import Product
from apps.core.models import PersonAlias

from . import sync
from .client import QuartzyError
from .models import STALE_AFTER, OrderRequest, QuartzyLab, SyncRun

ACCESS_CHECK_CACHE_KEY = "quartzy:token-ok"
ACCESS_CHECK_SECONDS = 600
ACCESS_CHECK_TIMEOUT = 5
# Catalog re-resolutions are listed too, so the co-owner sees what a mapping change did; like
# every run but a head run, they never vouch for the sync (tasks._sync_job).
PANEL_KINDS = (
    SyncRun.Kind.HEAD,
    SyncRun.Kind.SWEEP,
    SyncRun.Kind.HOT,
    SyncRun.Kind.TYPES,
    SyncRun.Kind.INVENTORY,
    SyncRun.Kind.BACKFILL,
    SyncRun.Kind.RESOLVE,
)


def staff_required(view):
    @login_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_staff:
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapper


@require_GET
@staff_required
def health(request):
    now = timezone.now()
    qlabs = (
        QuartzyLab.objects.select_related("lab").with_last_head_ok().order_by("lab__name", "name")
    )
    stale = set(QuartzyLab.objects.stale(now).values_list("pk", flat=True))
    labs_run = (
        SyncRun.objects.filter(kind=SyncRun.Kind.LABS, status=SyncRun.Status.OK)
        .order_by("-started_at")
        .first()
    )
    context = {
        "panels": [_panel(qlab, now, stale=qlab.pk in stale) for qlab in qlabs.active()],
        # Found by the sync but not mirrored, e.g. a collaborator's lab the token also sees.
        "inactive": qlabs.filter(is_active=False),
        "labs_run": labs_run,
        "token_ok": token_ok(),
        "stale_minutes": int(STALE_AFTER.total_seconds() // 60),
    }
    return render(request, "quartzy/health.html", context)


def token_ok():
    """Whether the Quartzy token works: checked at most every 10 minutes, with a short
    timeout and no retries, so the panel never waits long and never calls Quartzy per view."""
    ok = cache.get(ACCESS_CHECK_CACHE_KEY)
    if ok is None:
        ok = _check_token()
        cache.set(ACCESS_CHECK_CACHE_KEY, ok, ACCESS_CHECK_SECONDS)
    return ok


def _check_token():
    if not settings.QUARTZY_ACCESS_TOKEN:
        return False
    try:
        with sync.client_from_settings(timeout=ACCESS_CHECK_TIMEOUT, max_attempts=1) as client:
            client.me()
    except (QuartzyError, ValueError):
        return False
    return True


def _panel(qlab, now, *, stale):
    runs = SyncRun.objects.filter(quartzy_lab=qlab)
    kinds = []
    for kind in PANEL_KINDS:
        of_kind = runs.filter(kind=kind).exclude(status=SyncRun.Status.SKIPPED_LOCKED)
        kinds.append(
            {
                "kind": kind.value,
                "label": kind.label,
                "last": of_kind.order_by("-started_at").first(),
                "last_ok": of_kind.filter(status=SyncRun.Status.OK).order_by("-started_at").first(),
            }
        )
    requests = OrderRequest.objects.filter(quartzy_lab=qlab).live()
    counts = requests.aggregate(
        total=Count("pk"),
        open=Count("pk", filter=~Q(status__in=OrderRequest.CLOSED_STATUSES)),
        uncheckable=Count("pk", filter=Q(resolution_status__in=OrderRequest.UNCHECKABLE)),
    )
    unresolved = (
        PersonAlias.objects.filter(user__isnull=True)
        .filter(
            Q(requested_order_requests__quartzy_lab=qlab)
            | Q(created_order_requests__quartzy_lab=qlab)
        )
        .distinct()
        .count()
    )
    provisional = (
        Product.objects.canonical()
        .filter(provisional=True, order_requests__quartzy_lab=qlab)
        .distinct()
        .count()
    )
    calls = runs.filter(started_at__gte=now - timedelta(hours=1)).aggregate(n=Sum("calls"))["n"]
    total = counts["total"]
    return {
        "qlab": qlab,
        "kinds": kinds,
        "stale": stale,
        "last_head_ok": qlab.last_head_ok,
        "calls_last_hour": calls or 0,
        "requests": total,
        "open": counts["open"],
        "uncheckable": counts["uncheckable"],
        "uncheckable_share": round(100 * counts["uncheckable"] / total) if total else None,
        "unresolved_people": unresolved,
        "provisional_products": provisional,
    }
