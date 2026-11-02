"""Search before you buy, asks and the in-flight board (docs/design/mvp-a.md §10.2-§10.4)."""

from django.conf import settings
from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.catalog.models import Product
from apps.core.models import Notification
from apps.core.policies import get_policy
from apps.inventory import asks
from apps.inventory.models import AskReply, AskThread, StockItem
from apps.inventory.search import search
from apps.quartzy.models import OrderRequest
from apps.quartzy.presenters import present_requests, with_related

from .views import member_required


@require_GET
@member_required
def search_page(request):
    q = request.GET.get("q", "")
    result = search(request.lab, request.user, q) if q.strip() else None
    template = (
        "bench/_search_results.html" if request.headers.get("HX-Request") else ("bench/search.html")
    )
    return render(request, template, {"q": q, "result_view": result})


@require_GET
@member_required
def search_setup(request):
    base = settings.SITE_URL
    return render(
        request,
        "bench/search_setup.html",
        {
            "search_url": f"{base}/s?q=%s",
            "bookmarklet": (
                "javascript:(function(){var s=String(window.getSelection()).trim();"
                f"location.href='{base}/s?q='+encodeURIComponent(s||location.href)}})()"
            ),
        },
    )


@require_GET
def opensearch(request):
    """The OpenSearch description: browsers offer LabStock as an address-bar search."""
    body = render_to_string("bench/opensearch.xml", {"site_url": settings.SITE_URL})
    return HttpResponse(body, content_type="application/opensearchdescription+xml")


@require_POST
@member_required
def ask_owner_view(request, short_id):
    item = get_object_or_404(StockItem.objects.for_lab(request.lab), short_id=short_id)
    try:
        asks.ask_owner(lab=request.lab, asker=request.user, stock_item=item, source="item")
    except PermissionError:
        messages.error(request, "You can't ask about that item.")
    else:
        messages.success(request, "Asked. They'll reply to you by email.")
    return redirect("bench:item", short_id=item.short_id)


@require_POST
@member_required
def ask_lab_view(request):
    product = Product.objects.filter(pk=request.POST.get("product") or None).first()
    text = request.POST.get("text", "")
    if product is None and not text.strip():
        messages.error(request, "Say what you're looking for.")
        return redirect("bench:asks")
    try:
        asks.ask_lab(
            lab=request.lab,
            asker=request.user,
            product=product,
            text=text,
            urgent=request.POST.get("urgent") == "on",
        )
    except asks.AskLimit as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Asked the lab. Replies come to you by email.")
    return redirect("bench:asks")


@require_GET
@member_required
def asks_page(request):
    """My asks and their replies; asks waiting for my answer."""
    mine = (
        AskThread.objects.for_lab(request.lab)
        .filter(asker=request.user)
        .select_related("product")
        .prefetch_related("replies__responder")
        .order_by("-created_at")[:20]
    )
    ask_rows = [
        {
            "what": thread.text or (thread.product.name if thread.product_id else ""),
            "kind": thread.get_kind_display(),
            "at": thread.created_at,
            "replies": [
                (str(reply.responder), reply.get_answer_display()) for reply in thread.replies.all()
            ],
        }
        for thread in mine
    ]
    waiting = Notification.objects.filter(
        lab=request.lab,
        recipient=request.user,
        kind__in=(Notification.Kind.ASK_LAB, Notification.Kind.ASK_OWNER),
        status=Notification.Status.SENT,
        acted_at__isnull=True,
    ).order_by("-created_at")[:20]
    answered = set(
        AskReply.objects.filter(responder=request.user).values_list("thread_id", flat=True)
    )
    to_me = [n for n in waiting if int(n.subject_id) not in answered]
    can_ask = AskThread.objects.for_lab(request.lab).filter(
        asker=request.user, kind=AskThread.Kind.LAB
    ).filter(
        created_at__gte=asks._local_day_start(request.lab, timezone.now())
    ).count() < get_policy("asks.max_per_person_per_day", request.lab)
    return render(
        request,
        "bench/asks.html",
        {"ask_rows": ask_rows, "to_me": len(to_me), "can_ask": can_ask},
    )


@require_GET
@member_required
def board(request):
    """The lab's open requests by product: not marked ordered, ordered, probably arrived."""
    lab = request.lab
    arrived_after = get_policy("guard.open_probably_arrived_days", lab)
    rows = with_related(OrderRequest.objects.for_lab(lab).open().order_by("item_name", "pk"))
    views = present_requests(rows, request.user, channel="page")
    sections = {"waiting": [], "ordered": [], "arrived": []}
    for request_view in views:
        if request_view.age_days is not None and request_view.age_days > arrived_after:
            sections["arrived"].append(request_view)
        elif request_view.status in ("ORDERED", "BACKORDERED"):
            sections["ordered"].append(request_view)
        else:
            sections["waiting"].append(request_view)
    return render(
        request,
        "bench/board.html",
        {
            "waiting_views": sections["waiting"],
            "ordered_views": sections["ordered"],
            "arrived_views": sections["arrived"],
            "arrived_after": arrived_after,
        },
    )
