"""Queue and send notification emails, exactly once each.

``queue_notification`` creates the Notification row (its unique constraint is the
idempotency key) and enqueues the send only after the surrounding transaction commits.
``send_notification`` locks the row, re-checks that it hasn't been sent, runs the send checks
of its kind, then sends.

Send checks (``register_send_check``) run at send time, so a kill switch or a budget turned
on while mail is queued still applies. A check returns None to let the mail go, a reason
string to hold it (a person can release it later), or ``Suppressed(reason)`` to drop it for
good (status SUPPRESSED). The first check that objects decides.
"""

import logging
from dataclasses import dataclass

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.mail import EmailMultiAlternatives
from django.db import IntegrityError, transaction
from django.template.loader import render_to_string
from django.utils import timezone

from .models import Membership, Notification, User

logger = logging.getLogger(__name__)

# Action-needed mail comes from alerts@, everything else from news@ (plan §2 "Email").
ALERT_KINDS = {
    Notification.Kind.DUPLICATE_ALERT,
    Notification.Kind.CLEAR_TO_ORDER,
    Notification.Kind.STALE_NUDGE,
    Notification.Kind.SYSTEM,
}

# kind -> callable(notification) returning extra template context.
_CONTEXT_BUILDERS = {}
# kind -> [callable(notification) returning None to send, a reason string to hold, or
# Suppressed(reason) to drop the mail].
_SEND_CHECKS = {}


@dataclass(frozen=True)
class Suppressed:
    """What a send check returns to drop a mail for good (e.g. over the email budget)."""

    reason: str


def register_context_builder(kind):
    def decorator(func):
        _CONTEXT_BUILDERS[str(kind)] = func
        return func

    return decorator


def register_send_check(*kinds):
    """Register a send check for one or more kinds. Checks run in registration order; a
    function registered twice for a kind runs once."""

    def decorator(func):
        for kind in kinds:
            checks = _SEND_CHECKS.setdefault(str(kind), [])
            if func not in checks:
                checks.append(func)
        return func

    return decorator


def send_checks(kind):
    return list(_SEND_CHECKS.get(str(kind), ()))


def _check(notification):
    """The first objection of the kind's send checks: None, a hold reason or Suppressed."""
    for check in _SEND_CHECKS.get(notification.kind, ()):
        verdict = check(notification)
        if verdict:
            return verdict
    return None


def queue_notification(
    *,
    lab,
    kind,
    subject_obj,
    recipient_email,
    recipient=None,
    period_key="",
    context=None,
    hold_reason="",
):
    """Create (or find) the notification and enqueue its send. Returns (notification, created)."""
    from django_q.tasks import async_task

    subject_type = ContentType.objects.get_for_model(subject_obj)
    lookup = {
        "kind": kind,
        "subject_type": subject_type,
        "subject_id": str(subject_obj.pk),
        "recipient_email": recipient_email.strip().lower(),
        "period_key": period_key,
    }
    defaults = {
        "lab": lab,
        "recipient": recipient,
        "context": context or {},
        "status": Notification.Status.HELD if hold_reason else Notification.Status.PENDING,
        "hold_reason": hold_reason,
    }
    try:
        with transaction.atomic():
            notification, created = Notification.objects.get_or_create(**lookup, defaults=defaults)
    except IntegrityError:
        notification, created = Notification.objects.get(**lookup), False

    if created and notification.status == Notification.Status.PENDING:
        notification_id = notification.pk
        transaction.on_commit(
            lambda: async_task("apps.core.mail.send_notification", notification_id)
        )
    return notification, created


def release_notification(notification):
    """Move a held notification to pending and enqueue it (e.g. after the circuit breaker)."""
    from django_q.tasks import async_task

    updated = Notification.objects.filter(
        pk=notification.pk, status=Notification.Status.HELD
    ).update(status=Notification.Status.PENDING, hold_reason="")
    if updated:
        notification_id = notification.pk
        transaction.on_commit(
            lambda: async_task("apps.core.mail.send_notification", notification_id)
        )
    return bool(updated)


def build_message(notification):
    kind = notification.kind
    context = {
        "notification": notification,
        "site_url": settings.SITE_URL,
        **notification.context,
    }
    builder = _CONTEXT_BUILDERS.get(kind)
    if builder:
        context.update(builder(notification) or {})
    subject = render_to_string(f"emails/{kind}/subject.txt", context).strip()
    text_body = render_to_string(f"emails/{kind}/body.txt", context)
    html_body = render_to_string(f"emails/{kind}/body.html", context)
    from_email = (
        settings.LABSTOCK_ALERTS_FROM if kind in ALERT_KINDS else settings.LABSTOCK_NEWS_FROM
    )
    reply_to = [context["reply_to"]] if context.get("reply_to") else None
    message = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=from_email,
        to=[notification.recipient_email],
        reply_to=reply_to,
    )
    message.attach_alternative(html_body, "text/html")
    return message


def send_notification(notification_id):
    """Django-Q2 task. Safe to run twice: the row lock and sent_at check send at most once."""
    with transaction.atomic():
        notification = Notification.objects.select_for_update().filter(pk=notification_id).first()
        if notification is None:
            return "missing"
        if notification.sent_at or notification.status != Notification.Status.PENDING:
            return "skipped"
        verdict = _check(notification)
        if isinstance(verdict, Suppressed):
            notification.status = Notification.Status.SUPPRESSED
            notification.hold_reason = verdict.reason[:200]
            notification.save(update_fields=["status", "hold_reason"])
            return "suppressed"
        if verdict:
            notification.status = Notification.Status.HELD
            notification.hold_reason = str(verdict)[:200]
            notification.save(update_fields=["status", "hold_reason"])
            return "held"
        try:
            message = build_message(notification)
            message.send()
        except Exception as exc:
            logger.exception("Sending notification %s failed", notification_id)
            notification.status = Notification.Status.FAILED
            notification.error = str(exc)[:2000]
            notification.save(update_fields=["status", "error"])
            return "failed"
        status = getattr(message, "anymail_status", None)
        notification.esp_message_id = (getattr(status, "message_id", None) or "")[:200]
        notification.sent_at = timezone.now()
        notification.status = Notification.Status.SENT
        notification.save(update_fields=["esp_message_id", "sent_at", "status"])
        return "sent"


def notify_admins(lab, *, title, message, subject_obj, period_key=""):
    """Queue a SYSTEM email to the lab's system owners and to superusers."""
    from django.db.models import Q

    admins = (
        User.objects.filter(is_active=True, email__isnull=False)
        .filter(
            Q(is_superuser=True) | Q(memberships__lab=lab, memberships__role=Membership.Role.ADMIN)
        )
        .distinct()
    )
    for admin in admins:
        queue_notification(
            lab=lab,
            kind=Notification.Kind.SYSTEM,
            subject_obj=subject_obj,
            recipient_email=admin.email,
            recipient=admin,
            period_key=period_key,
            context={"title": title, "message": message},
        )
