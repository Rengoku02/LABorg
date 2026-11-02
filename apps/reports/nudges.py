"""Nudges (design §10.5): "Did this arrive?" for requests still open weeks later.

Once a day (from 09:00 lab time) each Quartzy request that is still open
``nudges.stale_days`` after it was made, and was made after alerts were armed (so the
backlog is never nudged), gets one ``stale_nudge`` to its requester. The answers: *It
arrived* and *Never ordered: cancel it* (both make the sync watch the request; the confirm
page links to Quartzy), or *Still waiting*.
"""

from datetime import timedelta

from django.contrib.contenttypes.models import ContentType
from django.db.models import CharField, Exists, F, OuterRef
from django.db.models.functions import Cast, Coalesce
from django.utils import timezone

from apps.core.actions import action_url, register_action
from apps.core.mail import queue_notification, register_context_builder
from apps.core.models import Notification
from apps.core.permissions import is_member
from apps.core.policies import get_policy
from apps.quartzy.models import OrderRequest
from apps.quartzy.presenters import present_request, with_related
from apps.quartzy.services import watch_requests

Kind = Notification.Kind
ANSWERS = (
    ("arrived", "It arrived"),
    ("waiting", "Still waiting"),
    ("never_ordered", "Never ordered: I'll cancel it"),
)


def stale_requests(lab, *, now=None):
    now = now or timezone.now()
    cutoff = now - timedelta(days=get_policy("nudges.stale_days", lab))
    nudged = Notification.objects.filter(
        kind=Kind.STALE_NUDGE,
        subject_type=ContentType.objects.get_for_model(OrderRequest),
        subject_id=Cast(OuterRef("pk"), CharField()),
    )
    return (
        OrderRequest.objects.for_lab(lab)
        .open()
        .filter(quartzy_lab__alerts_armed_at__isnull=False, requested_by__isnull=False)
        .annotate(at=Coalesce("requested_at", "first_seen_at"))
        .filter(at__lte=cutoff, at__gte=F("quartzy_lab__alerts_armed_at"))
        .exclude(Exists(nudged))
    )


def nudge_stale_requests(lab, *, now=None):
    """Queue today's nudges; returns how many."""
    queued = 0
    for row in stale_requests(lab, now=now).select_related("requested_by"):
        person = row.requested_by
        if not person.email or not is_member(person, lab):
            continue
        _notification, created = queue_notification(
            lab=lab,
            kind=Kind.STALE_NUDGE,
            subject_obj=row,
            recipient_email=person.email,
            recipient=person,
        )
        queued += created
    return queued


@register_context_builder(Kind.STALE_NUDGE)
def _nudge_context(notification):
    rows = with_related(OrderRequest.objects.filter(pk=notification.subject_id))
    request_view = present_request(rows.get(), notification.recipient, channel="email")
    return {
        "request_view": request_view,
        "answer_links": [(label, action_url(notification, name)) for name, label in ANSWERS],
    }


def _watch(notification, request, data):
    watch_requests([int(notification.subject_id)])
    return {}


def _quartzy_link(notification):
    row = OrderRequest.objects.filter(pk=notification.subject_id).first()
    return {"quartzy_url": row.app_url if row else ""}


register_action(Kind.STALE_NUDGE, "arrived", label="It arrived")(_watch)


def _noted(notification, request, data):
    return {}


register_action(Kind.STALE_NUDGE, "waiting", label="Still waiting")(_noted)
register_action(
    Kind.STALE_NUDGE,
    "never_ordered",
    label="Never ordered: I'll cancel it",
    confirm_template="guard/action_cancel_confirm.html",
    confirm_context=_quartzy_link,
)(_watch)
