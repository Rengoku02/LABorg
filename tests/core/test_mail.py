import pytest
from django.core import mail

from apps.core import mail as mail_module
from apps.core.mail import Suppressed, queue_notification, send_notification
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


def _queue(lab, member, **extra):
    notification, _ = queue_notification(
        lab=lab,
        kind=Notification.Kind.SYSTEM,
        subject_obj=member,
        recipient_email=member.email,
        context={"title": "x", "message": "y"},
        **extra,
    )
    notification.refresh_from_db()
    return notification


@pytest.mark.django_db(transaction=True)
def test_send_checks_run_in_order_and_the_first_objection_decides(lab, member, monkeypatch):
    seen = []

    def allow(notification):
        seen.append("allow")

    def hold(notification):
        seen.append("hold")
        return "kill switch"

    def drop(notification):  # never reached: hold objects first
        seen.append("drop")
        return Suppressed("budget")

    monkeypatch.setitem(mail_module._SEND_CHECKS, "system", [allow, hold, drop])
    notification = _queue(lab, member)
    assert seen == ["allow", "hold"]
    assert (notification.status, notification.hold_reason) == ("held", "kill switch")
    assert len(mail.outbox) == 0


@pytest.mark.django_db(transaction=True)
def test_a_suppressed_mail_is_dropped_for_good(lab, member, monkeypatch):
    monkeypatch.setitem(mail_module._SEND_CHECKS, "system", [lambda n: Suppressed("budget")])
    notification = _queue(lab, member)
    assert (notification.status, notification.hold_reason) == ("suppressed", "budget")
    assert send_notification(notification.pk) == "skipped" and len(mail.outbox) == 0


def test_register_send_check_takes_several_kinds_once_each(monkeypatch):
    monkeypatch.setattr(mail_module, "_SEND_CHECKS", {})

    @mail_module.register_send_check("ask_lab", "claims")
    def budget(notification):
        return None

    mail_module.register_send_check("claims")(budget)
    assert mail_module.send_checks("ask_lab") == [budget]
    assert mail_module.send_checks("claims") == [budget]


@pytest.mark.django_db(transaction=True)
def test_notify_admins_mails_system_owners_and_superusers(lab, make_member):
    from apps.core.mail import notify_admins
    from apps.core.models import Membership, User

    owner = make_member("owner@lab.test", role=Membership.Role.ADMIN)
    make_member("plain@lab.test")
    root = User.objects.create_superuser(email="root@lab.test", full_name="Root")
    notify_admins(lab, title="Heads up", message="Something happened", subject_obj=lab)
    assert sorted(message.to[0] for message in mail.outbox) == sorted([owner.email, root.email])
    assert {message.subject for message in mail.outbox} == {"[LabStock] Heads up"}
