import pytest
from django.core import mail

from apps.core.mail import queue_notification, send_notification
from apps.core.models import Notification


@pytest.mark.django_db(transaction=True)
def test_queue_is_idempotent_and_sends_once(lab, member):
    kwargs = dict(
        lab=lab,
        kind=Notification.Kind.SYSTEM,
        subject_obj=member,
        recipient_email=member.email,
        recipient=member,
        context={"title": "Hello", "message": "Testing"},
    )
    first, created = queue_notification(**kwargs)
    second, created_again = queue_notification(**kwargs)
    assert created and not created_again and first.pk == second.pk
    assert len(mail.outbox) == 1  # Q2 runs synchronously in tests, after commit
    assert mail.outbox[0].subject == "[LabStock] Hello"
    assert mail.outbox[0].from_email.startswith("LabStock <alerts@")
    # A redelivered task is a no-op.
    assert send_notification(first.pk) == "skipped"
    assert len(mail.outbox) == 1
    first.refresh_from_db()
    assert first.status == Notification.Status.SENT and first.sent_at


@pytest.mark.django_db(transaction=True)
def test_held_notifications_are_not_sent(lab, member):
    notification, _ = queue_notification(
        lab=lab,
        kind=Notification.Kind.SYSTEM,
        subject_obj=member,
        recipient_email=member.email,
        context={"title": "x", "message": "y"},
        hold_reason="circuit breaker",
    )
    assert notification.status == Notification.Status.HELD
    assert len(mail.outbox) == 0
