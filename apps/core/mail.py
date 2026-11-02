"""Queue and send notification emails, exactly once each.

``queue_notification`` creates the Notification row (its unique constraint is the
idempotency key) and enqueues the send only after the surrounding transaction commits.
``send_notification`` locks the row, re-checks that it hasn't been sent, then sends.
"""

import logging

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.mail import EmailMultiAlternatives
from django.db import IntegrityError, transaction
from django.template.loader import render_to_string
from django.utils import timezone

from .models import Notification

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
# kind -> callable(notification) returning None to send, or a reason string to hold.
_SEND_CHECKS = {}


def register_context_builder(kind):
    def decorator(func):
        _CONTEXT_BUILDERS[str(kind)] = func
        return func

    return decorator


def register_send_check(kind):
    def decorator(func):
        _SEND_CHECKS[str(kind)] = func
        return func

    return decorator


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
        check = _SEND_CHECKS.get(notification.kind)
        hold_reason = check(notification) if check else None
        if hold_reason:
            notification.status = Notification.Status.HELD
            notification.hold_reason = hold_reason[:200]
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
