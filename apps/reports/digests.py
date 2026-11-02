"""The weekly emails (design §10.6, §10.7).

- **Personal digest** (Monday from 08:00 lab time, kind ``personal_digest``): what is waiting
  for one person, sent only when something is: alerts found after they ordered (post-order
  wording), alerts not answered yet, replies to their asks, asks waiting for them, claims to
  sort, and requests they were nudged about that are still open. The stored context holds ids
  only; the views are built when the email is sent.
- **Lab summary** (Monday from 08:30, kind ``lab_summary``, to PIs and co-owners): lab totals
  for the week, never a person's behaviour. Counts of 1 or 2 show as "<3"; item text goes
  through ``NameScrubber``; lenders are thanked by name only if they opted in
  (``Membership.thank_by_name``).
"""

from datetime import timedelta

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from apps.core.mail import queue_notification, register_context_builder
from apps.core.models import Membership, Notification, PersonAlias
from apps.guard.anonymize import NameScrubber
from apps.guard.models import DuplicateCheck, Outcome
from apps.inventory.claims import pending_claims, week_key
from apps.inventory.models import AskReply, StockItem
from apps.quartzy.models import OrderRequest
from apps.quartzy.presenters import present_requests, with_related

from . import metrics

Kind = Notification.Kind
LOOKBACK = timedelta(days=8)


def digest_content(person, lab, *, now=None):
    """{section: [ids] or count}: what the person's digest would say (empty: no digest)."""
    now = now or timezone.now()
    since = now - LOOKBACK
    mine = DuplicateCheck.objects.filter(
        lab=lab, replay=False, order_request__requested_by=person, created_at__gte=since
    )
    late = list(
        mine.filter(status=DuplicateCheck.Status.LATE).values_list("order_request_id", flat=True)
    )
    unanswered = list(
        mine.filter(status=DuplicateCheck.Status.ALERTED, outcomes__isnull=True).values_list(
            "order_request_id", flat=True
        )
    )
    replies = list(
        AskReply.objects.filter(thread__asker=person, thread__lab=lab, created_at__gte=since)
        .exclude(responder=person)
        .values_list("pk", flat=True)
    )
    answered = set(AskReply.objects.filter(responder=person).values_list("thread_id", flat=True))
    waiting = [
        int(n.subject_id)
        for n in Notification.objects.filter(
            lab=lab,
            recipient=person,
            kind__in=(Kind.ASK_LAB, Kind.ASK_OWNER),
            status=Notification.Status.SENT,
            acted_at__isnull=True,
            sent_at__gte=since,
        )
        if int(n.subject_id) not in answered
    ]
    nudged = [
        int(n.subject_id)
        for n in Notification.objects.filter(
            lab=lab, recipient=person, kind=Kind.STALE_NUDGE, acted_at__isnull=True
        )
    ]
    still_open = list(
        OrderRequest.objects.for_lab(lab).open().filter(pk__in=nudged).values_list("pk", flat=True)
    )
    content = {
        "late": late,
        "unanswered": unanswered,
        "replies": replies,
        "asks_waiting": len(waiting),
        "claims": pending_claims(person, lab).count(),
        "still_open": still_open,
    }
    return content if any(content.values()) else {}


def queue_personal_digests(lab, *, now=None):
    """This week's digests for the current members with something waiting."""
    now = now or timezone.now()
    queued = 0
    for membership in Membership.objects.filter(lab=lab).select_related("user"):
        person = membership.user
        if not membership.is_current() or not person.email or not person.is_active:
            continue
        content = digest_content(person, lab, now=now)
        if not content:
            continue
        _n, created = queue_notification(
            lab=lab,
            kind=Kind.PERSONAL_DIGEST,
            subject_obj=person,
            recipient_email=person.email,
            recipient=person,
            period_key=week_key(now),
            context=content,
        )
        queued += created
    return queued


@register_context_builder(Kind.PERSONAL_DIGEST)
def _digest_context(notification):
    person, context = notification.recipient, notification.context

    def views(ids):
        rows = with_related(OrderRequest.objects.filter(pk__in=ids or []))
        return present_requests(rows, person, channel="email")

    replies = (
        AskReply.objects.filter(pk__in=context.get("replies") or [])
        .select_related("responder", "thread__product")
        .order_by("created_at")
    )
    return {
        "late_views": views(context.get("late")),
        "unanswered_views": views(context.get("unanswered")),
        "reply_rows": [
            (
                str(reply.responder),
                reply.get_answer_display(),
                reply.thread.text or (reply.thread.product.name if reply.thread.product_id else ""),
            )
            for reply in replies
        ],
        "still_open_views": views(context.get("still_open")),
        "links": {
            "claims": f"{settings.SITE_URL}{reverse('bench:claims')}",
            "asks": f"{settings.SITE_URL}{reverse('bench:asks')}",
            "alerts": f"{settings.SITE_URL}{reverse('guard:mine')}",
        },
    }


# ---------------------------------------------------------------------------------------
# The PI's weekly lab summary


def summary_recipients(lab):
    people = []
    for membership in Membership.objects.filter(
        lab=lab, role__in=(Membership.Role.PI, Membership.Role.CO_OWNER)
    ).select_related("user"):
        if membership.is_current() and membership.user.email:
            people.append(membership.user)
    return people


def queue_lab_summary(lab, *, now=None):
    now = now or timezone.now()
    queued = 0
    for person in summary_recipients(lab):
        _n, created = queue_notification(
            lab=lab,
            kind=Kind.LAB_SUMMARY,
            subject_obj=lab,
            recipient_email=person.email,
            recipient=person,
            period_key=week_key(now),
        )
        queued += created
    return queued


def scrubber_for(lab):
    """A NameScrubber of everyone the lab knows by name: members and Quartzy requesters."""
    names = [str(m.user) for m in Membership.objects.filter(lab=lab).select_related("user")]
    names += [
        alias.display_name
        for alias in PersonAlias.objects.filter(requested_order_requests__lab=lab).distinct()
        if alias.display_name
    ]
    return NameScrubber(names)


@register_context_builder(Kind.LAB_SUMMARY)
def _summary_context(notification):
    lab = notification.lab
    period = metrics.last_week(lab, notification.created_at)
    checks = DuplicateCheck.objects.filter(
        lab=lab, replay=False, created_at__gte=period.start, created_at__lt=period.end
    )
    caught = checks.filter(status__in=(DuplicateCheck.Status.ALERTED, DuplicateCheck.Status.HELD))
    scrub = scrubber_for(lab)
    rows = with_related(OrderRequest.objects.filter(pk__in=caught.values("order_request_id"))[:5])
    caught_views = present_requests(rows, None, channel="digest", scrub=scrub)
    saved = metrics.value_avoided(lab, period.start, period.end)
    lenders = sorted(
        {
            str(reply.responder)
            for reply in AskReply.objects.filter(
                thread__lab=lab,
                answer__in=(AskReply.Answer.LEND, AskReply.Answer.HAVE_IT),
                created_at__gte=period.start,
            ).select_related("responder")
            if Membership.objects.filter(lab=lab, user=reply.responder, thank_by_name=True).exists()
        }
    )
    borrowed = Outcome.objects.filter(
        duplicate_check__lab=lab, kind=Outcome.Kind.BORROWED, decided_at__gte=period.start
    ).count()
    return {
        "period": period,
        "requests": metrics.small(
            OrderRequest.objects.for_lab(lab)
            .filter(requested_at__gte=period.start, requested_at__lt=period.end)
            .count()
        ),
        "checked": metrics.small(checks.count()),
        "caught": metrics.small(caught.count()),
        "avoided_count": metrics.small(saved["count"]),
        "avoided_amount": saved["amount"],
        "borrowed": metrics.small(borrowed),
        "stock_added": metrics.small(
            StockItem.objects.for_lab(lab).filter(created_at__gte=period.start).count()
        ),
        "caught_views": caught_views,
        "lenders": lenders,
        "dashboard_url": f"{settings.SITE_URL}{reverse('reports:dashboard')}",
    }
