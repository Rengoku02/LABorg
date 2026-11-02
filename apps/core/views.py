from django.contrib.auth.decorators import login_required
from django.db import connection
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_http_methods

from . import actions
from .permissions import is_member


@login_required
def home(request):
    """The member's home: what's waiting for them (claims), and the bench's main pages."""
    context = {}
    lab = getattr(request, "lab", None)
    if lab is not None and is_member(request.user, lab):
        from apps.bench.views import home_context

        context = home_context(request)
    return render(request, "core/home.html", context)


@require_GET
def healthz(request):
    """Liveness/readiness for Docker and Caddy. No auth, no details beyond ok/not ok."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return JsonResponse({"ok": True})
    except Exception:
        return JsonResponse({"ok": False}, status=503)


@require_http_methods(["GET", "HEAD", "POST"])
def action(request, token, action):
    """Signed one-action link. GET/HEAD only render a confirm page; POST acts once."""
    try:
        notification = actions.read_token(token)
    except actions.InvalidActionLink as exc:
        return render(request, "core/action_invalid.html", {"reason": str(exc)}, status=400)
    registered = actions.get_action(notification.kind, action)
    if registered is None:
        raise Http404("Unknown action")

    if request.method in ("GET", "HEAD"):
        context = {"notification": notification, "action": registered}
        if registered.confirm_context is not None:
            context.update(registered.confirm_context(notification) or {})
        return render(request, registered.confirm_template, context)

    reason = request.POST.get("reason", "")
    if registered.reasons and reason not in {value for value, _ in registered.reasons}:
        reason = ""
    data = {"reason": reason} if reason else {}
    try:
        notification, registered = actions.perform(token, action, request, data)
    except actions.AlreadyRecorded as exc:
        return render(
            request, "core/action_done.html", {"notification": exc.notification, "repeat": True}
        )
    except actions.InvalidActionLink as exc:
        return render(request, "core/action_invalid.html", {"reason": str(exc)}, status=400)
    return render(
        request,
        registered.done_template,
        {"notification": notification, "action": registered, "repeat": False},
    )
