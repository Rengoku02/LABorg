"""The bench pages (docs/design/mvp-a.md §4): mobile-first, members only, stock shown only
through the presenters.

- home; ``/i/<short_id>`` an item (its custodian may change who sees where it is);
  ``/l/<code>`` a location with what's in it; ``/l/<code>/photo`` a box lid
- ``/me/stock`` what I hold
- ``/claims/``: claim your history (also through the email's scoped link,
  ``/claims/t/<token>/``); ``/claims/champions/`` the lab pool's list
- ``/add/`` add what I have; ``/boxes/new`` register a box
"""

import uuid
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import redirect_to_login
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.catalog.models import ItemType, Product
from apps.core.models import Lab, User
from apps.core.permissions import can_label_matches, is_champion, is_member
from apps.inventory import claims as claims_service
from apps.inventory import intake, services
from apps.inventory.claims import ClaimError
from apps.inventory.models import HistoryClaim, Location, StockItem
from apps.inventory.presenters import (
    present_item,
    present_items,
    present_location,
    present_locations,
)
from apps.inventory.selectors import (
    child_locations,
    location_contents,
    resolve_code,
    stock_held_by,
)
from apps.reports.views import may_see_dashboard

from .forms import AddForm, BoxForm, ClaimForm, ShareForm


def member_required(view):
    """Signed in and a current member of the lab (``request.lab``)."""

    @login_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        lab = getattr(request, "lab", None)
        if lab is None or not is_member(request.user, lab):
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapper


def claims_access(view):
    """A member's own claims, or those of a scoped session opened by the claims email's link
    (it grants these pages only). Sets ``request.claims_person`` and ``request.claims_lab``."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        lab = getattr(request, "lab", None)
        if request.user.is_authenticated and lab is not None and is_member(request.user, lab):
            request.claims_person, request.claims_lab = request.user, lab
            return view(request, *args, **kwargs)
        scope = claims_service.claim_scope(request)
        if scope is None:
            return redirect_to_login(request.get_full_path())
        person = User.objects.filter(pk=scope[0]).first()
        scoped_lab = Lab.objects.filter(pk=scope[1]).first()
        if person is None or scoped_lab is None or not is_member(person, scoped_lab):
            raise PermissionDenied
        request.claims_person, request.claims_lab = person, scoped_lab
        return view(request, *args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------------------
# Home, items, locations


def home_context(request):
    """What the home page shows a member: their next steps, never anyone else's."""
    lab, user = request.lab, request.user
    return {
        "pending_claims": claims_service.pending_claims(user, lab).count(),
        "held": stock_held_by(user, lab=lab).count(),
        "can_label": can_label_matches(user, lab),
        "is_champion": is_champion(user, lab),
        "can_see_dashboard": may_see_dashboard(user, lab),
    }


@require_GET
@member_required
def item_page(request, short_id):
    found = resolve_code(short_id, lab=request.lab)
    if not isinstance(found, StockItem):
        raise Http404("No such item")
    if found.short_id != short_id:
        return redirect("bench:item", short_id=found.short_id)
    item_view = present_item(found, request.user, channel="page")
    if item_view is None:
        raise Http404("No such item")
    share_form = None
    if item_view.viewer_is_custodian and item_view.is_active:
        share_form = ShareForm(
            initial={"share": item_view.visibility, "version": item_view.version}
        )
    return render(request, "bench/item.html", {"item_view": item_view, "share_form": share_form})


@require_POST
@member_required
def item_share(request, short_id):
    """The custodian changes who may see where it is (the one stock change in MVP-A's UI)."""
    item = get_object_or_404(StockItem.objects.for_lab(request.lab), short_id=short_id)
    if item.custodian_id != request.user.pk:
        raise PermissionDenied
    form = ShareForm(request.POST)
    if not form.is_valid():
        return HttpResponseBadRequest("Choose who may see it.")
    try:
        services.apply_event(
            item,
            "visibility_changed",
            user=request.user,
            expected_version=form.cleaned_data["version"],
            visibility=form.cleaned_data["share"],
        )
    except services.InventoryError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Saved.")
    return redirect("bench:item", short_id=item.short_id)


@require_GET
@member_required
def location_page(request, code):
    found = resolve_code(code, lab=request.lab)
    if not isinstance(found, Location):
        raise Http404("No such location")
    if found.code != code:
        return redirect("bench:location", code=found.code)
    location_view = present_location(found, request.user, channel="page")
    if location_view is None:
        raise Http404("No such location")
    item_views = present_items(
        location_contents(found), request.user, channel="page", listed_in=found
    )
    child_views = present_locations(
        child_locations(found), request.user, channel="page", listed_in=found
    )
    return render(
        request,
        "bench/location.html",
        {"location_view": location_view, "item_views": item_views, "child_views": child_views},
    )


@require_GET
@member_required
def location_photo(request, code):
    """A box lid photo, only to those the presenter shows it to; never public media."""
    found = resolve_code(code, lab=request.lab)
    if not isinstance(found, Location) or not found.photo:
        raise Http404("No photo")
    location_view = present_location(found, request.user, channel="page")
    if location_view is None or location_view.redacted or not location_view.has_photo:
        raise Http404("No photo")
    response = FileResponse(found.photo.open("rb"))
    response["Cache-Control"] = "private, max-age=3600"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@require_GET
@member_required
def my_stock(request):
    item_views = present_items(
        stock_held_by(request.user, lab=request.lab), request.user, channel="page"
    )
    return render(request, "bench/my_stock.html", {"item_views": item_views})


# ---------------------------------------------------------------------------------------
# Claims


@require_GET
def claims_token(request, token):
    """The claims email's link: scope this session to the recipient's claims, then show
    them. It logs nobody in."""
    try:
        claims_service.open_claim_session(request, token)
    except signing.BadSignature:
        return render(
            request,
            "core/action_invalid.html",
            {"reason": "This link has expired or is no longer valid. Sign in to see your claims."},
            status=400,
        )
    return redirect("bench:claims")


@require_GET
@claims_access
def claims_list(request):
    person, lab = request.claims_person, request.claims_lab
    pending = claims_service.pending_claims(person, lab).select_related("order_request", "lab")
    claim_views = claims_service.claim_views(pending, person)
    return render(
        request,
        "bench/claims.html",
        {
            "claim_views": claim_views,
            "form": ClaimForm(lab=lab, viewer=person),
            "person_name": str(person),
            "scoped": not request.user.is_authenticated,
        },
    )


@require_POST
@claims_access
def claim_decide(request, pk):
    person, lab = request.claims_person, request.claims_lab
    claim = get_object_or_404(HistoryClaim.objects.for_lab(lab).select_related("lab"), pk=pk)
    champions = request.user.is_authenticated and is_champion(request.user, lab)
    if claim.person_id != person.pk and not champions:
        raise PermissionDenied
    form = ClaimForm(request.POST, lab=lab, viewer=person)
    error = None
    if form.is_valid():
        data = form.cleaned_data
        place = _location(lab, data.get("place"))
        target = User.objects.filter(pk=data.get("given_to") or None).first()
        same_item = None
        if data.get("same_item"):
            same_item = StockItem.objects.for_lab(lab).filter(short_id=data["same_item"]).first()
        try:
            claims_service.decide(
                claim,
                data["outcome"],
                by=request.user if request.user.is_authenticated else person,
                count_kept=data.get("count") or 1,
                location=place,
                temperature=data.get("temp") or "",
                target_person=target,
                same_item=same_item,
            )
        except ClaimError as exc:
            error = str(exc)
    else:
        error = "Please check the answer."
    claim.refresh_from_db()
    if request.headers.get("HX-Request"):
        (claim_view,) = claims_service.claim_views([claim], person)
        return render(
            request,
            "bench/_claim_row.html",
            {"claim_view": claim_view, "form": ClaimForm(lab=lab, viewer=person), "error": error},
        )
    if error:
        messages.error(request, error)
    return redirect("bench:champions" if claim.person_id != person.pk else "bench:claims")


@require_GET
@member_required
def champions(request):
    if not is_champion(request.user, request.lab):
        raise PermissionDenied
    rows = claims_service.champions_list(request.lab)
    claim_views = claims_service.claim_views(rows, request.user)
    return render(
        request,
        "bench/champions.html",
        {"claim_views": claim_views, "form": ClaimForm(lab=request.lab, viewer=request.user)},
    )


# ---------------------------------------------------------------------------------------
# Add what I have, register a box


@require_http_methods(["GET", "POST"])
@member_required
def add(request):
    lab, user = request.lab, request.user
    form = AddForm(request.POST or None, lab=lab, viewer=user)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        expiry, precision = data["expiry"]
        product = None
        if data.get("product"):
            product = Product.objects.filter(pk=data["product"]).first()
        try:
            result = intake.add_what_i_have(
                lab=lab,
                person=user,
                user=user,
                source=data["source"],
                product=product,
                name=data["name"],
                vendor=data["vendor"],
                catalog_number=data["catalog_number"],
                item_type=ItemType.objects.filter(key=data["item_type"]).first(),
                count=data["count"],
                location=_location(lab, data.get("place")),
                temperature=data.get("temp") or "",
                visibility=data.get("share") or None,
                lot=data["lot_number"],
                expiry=expiry,
                expiry_precision=precision,
                notes=data["remarks"],
                mta=data["mta"],
                client_uuid=data["client_uuid"],
            )
        except services.InventoryError as exc:
            form.add_error(None, str(exc))
        else:
            count = len(result.items)
            messages.success(request, f"Added {count} item{'s' if count != 1 else ''}.")
            return redirect("bench:item", short_id=result.items[0].short_id)
    if not form.is_bound:
        form.initial["client_uuid"] = uuid.uuid4()
    return render(request, "bench/add.html", {"form": form})


@require_GET
@member_required
def add_products(request):
    """The add form's typeahead (an htmx partial)."""
    options = intake.product_typeahead(request.lab, request.GET.get("q", ""))
    return render(request, "bench/_product_options.html", {"options": options})


@require_http_methods(["GET", "POST"])
@member_required
def box_new(request):
    lab, user = request.lab, request.user
    form = BoxForm(request.POST or None, request.FILES or None, lab=lab, viewer=user)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        parent = _location(lab, data["parent"])
        owner = User.objects.filter(pk=data["owner"]).first()
        try:
            box = intake.register_box(
                lab=lab,
                owner=owner,
                parent=parent,
                name=data["name"],
                user=user,
                kind=data["kind"],
                tags=data["tags"],
                photo=data.get("photo"),
                visibility=data.get("share") or None,
                notes=data["remarks"],
            )
        except services.InventoryError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Box registered.")
            return redirect("bench:location", code=box.code)
    return render(request, "bench/box_new.html", {"form": form})


def _location(lab, code):
    """The location a form picked by its code; None for "place later"."""
    if not code:
        return None
    found = Location.objects.for_lab(lab).filter(code=code).first()
    if found is None:
        raise Http404("No such location")
    return found
