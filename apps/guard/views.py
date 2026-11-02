"""The co-owner's labelling page (design §8 "Labelling page").

Shadow matches are labelled same thing / related, not a duplicate / different. Live shadow
matches come first, then the replay of history, each sorted by what the request was worth;
the per-tier gates are at the top. Going live stays a PolicySetting change in the admin.
"""

from dataclasses import dataclass

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import DecimalField, ExpressionWrapper, F
from django.db.models.functions import Coalesce
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.core.permissions import can_label_matches
from apps.inventory.presenters import ItemView, present_items
from apps.inventory.selectors import items_by_ids
from apps.quartzy.models import OrderRequest
from apps.quartzy.presenters import RequestView, present_requests, with_related

from .matcher import MATCHER_VERSION
from .models import DuplicateCheck, DuplicateMatch
from .reports import precision_report

PER_PAGE = 20
LABEL_BUTTONS = (
    (DuplicateMatch.Label.TRUE_DUP, "Same thing"),
    (DuplicateMatch.Label.RELATED, "Related, not a duplicate"),
    (DuplicateMatch.Label.FALSE, "Different"),
)


@dataclass(frozen=True)
class MatchView:
    pk: int
    tier: str
    tier_label: str
    kind: str
    kind_label: str
    why: str
    probably: bool
    replay: bool
    label: str
    label_text: str
    label_suggestion: str
    subject: RequestView | None
    item: ItemView | None
    request: RequestView | None
    snapshot: dict


def match_views(matches, viewer, *, channel="page"):
    """MatchViews of these DuplicateMatches for ``viewer``: the request and the candidate
    through their presenters (stock never read from the model in a template)."""
    matches = list(matches)
    if not matches:
        return []
    lab = matches[0].duplicate_check.lab
    subject_ids = {m.duplicate_check.order_request_id for m in matches}
    request_ids = {m.candidate_order_request_id for m in matches if m.candidate_order_request_id}
    stock_ids = {m.candidate_stock_item_id for m in matches if m.candidate_stock_item_id}
    rows = with_related(OrderRequest.objects.filter(pk__in=subject_ids | request_ids))
    requests = {view.pk: view for view in present_requests(rows, viewer, channel=channel)}
    stock_rows = list(items_by_ids(lab, stock_ids))
    short_ids = {row.pk: row.short_id for row in stock_rows}
    items = {view.short_id: view for view in present_items(stock_rows, viewer, channel=channel)}
    labels = dict(DuplicateMatch.Label.choices)
    views = []
    for match in matches:
        item = None
        if match.candidate_stock_item_id:
            item = items.get(short_ids.get(match.candidate_stock_item_id))
        views.append(
            MatchView(
                pk=match.pk,
                tier=match.tier,
                tier_label=match.get_tier_display(),
                kind=match.candidate_kind,
                kind_label=match.get_candidate_kind_display(),
                why=match.why,
                probably=match.probably,
                replay=match.duplicate_check.replay,
                label=match.label,
                label_text=labels.get(match.label, ""),
                label_suggestion=labels.get(match.label_suggestion, ""),
                subject=requests.get(match.duplicate_check.order_request_id),
                item=item,
                request=requests.get(match.candidate_order_request_id),
                snapshot=match.candidate_snapshot,
            )
        )
    return views


def _labeller(request):
    lab = getattr(request, "lab", None)
    if lab is None or not can_label_matches(request.user, lab):
        raise PermissionDenied
    return lab


def unlabelled_matches(lab):
    """Unlabelled, unsuppressed matches of the current matcher version: live shadow first,
    then replay, each by what the request was worth (largest first)."""
    value = Coalesce(
        F("duplicate_check__order_request__total_price"),
        ExpressionWrapper(
            F("duplicate_check__order_request__unit_price")
            * F("duplicate_check__order_request__quantity"),
            output_field=DecimalField(max_digits=28, decimal_places=8),
        ),
    )
    return (
        DuplicateMatch.objects.filter(
            duplicate_check__lab=lab,
            duplicate_check__matcher_version=MATCHER_VERSION,
            duplicate_check__status__in=DuplicateCheck.WITH_MATCHES,
            suppressed=False,
            label="",
        )
        .select_related("duplicate_check__lab")
        .annotate(value=value)
        .order_by("duplicate_check__replay", F("value").desc(nulls_last=True), "pk")
    )


@require_GET
@login_required
def label_list(request):
    lab = _labeller(request)
    page = Paginator(unlabelled_matches(lab), PER_PAGE).get_page(request.GET.get("page"))
    return render(
        request,
        "guard/label.html",
        {
            "tiers": precision_report(lab),
            "page": page,
            "match_views": match_views(page.object_list, request.user),
            "buttons": LABEL_BUTTONS,
        },
    )


@require_POST
@login_required
def label_match(request, pk):
    lab = _labeller(request)
    match = get_object_or_404(
        DuplicateMatch.objects.select_related("duplicate_check__lab"),
        pk=pk,
        duplicate_check__lab=lab,
    )
    label = request.POST.get("label", "")
    if label and label not in DuplicateMatch.Label.values:
        return HttpResponseBadRequest("Unknown label")
    match.label = label
    match.labelled_by = request.user if label else None
    match.labelled_at = timezone.now() if label else None
    match.save(update_fields=["label", "labelled_by", "labelled_at"])
    if request.headers.get("HX-Request"):
        (match_view,) = match_views([match], request.user)
        return render(
            request,
            "guard/_match_row.html",
            {"match_view": match_view, "buttons": LABEL_BUTTONS},
        )
    return redirect("guard:label_list")
