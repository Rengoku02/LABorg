from pathlib import Path

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core import signing

from apps.core import actions
from apps.core.models import Notification


@actions.register_action(Notification.Kind.SYSTEM, "ack", label="Acknowledge")
def _ack(notification, request, data):
    return {"handled": True}


@actions.register_action(
    Notification.Kind.SYSTEM, "keep", label="Need my own", reasons=(("lot", "Specific lot"),)
)
def _keep(notification, request, data):
    return {}


@pytest.fixture
def notification(lab, member):
    return Notification.objects.create(
        lab=lab,
        kind=Notification.Kind.SYSTEM,
        recipient=member,
        recipient_email=member.email,
        subject_type=ContentType.objects.get_for_model(member),
        subject_id=str(member.pk),
    )


def _path(url):
    return url.replace("https://labstock.test", "")


@pytest.mark.django_db
def test_get_and_head_never_mutate(client, notification):
    url = _path(actions.action_url(notification, "ack"))
    assert client.get(url).status_code == 200
    assert client.head(url).status_code == 200
    notification.refresh_from_db()
    assert notification.acted_at is None


@pytest.mark.django_db
def test_post_acts_exactly_once(client, notification):
    url = _path(actions.action_url(notification, "ack"))
    response = client.post(url)
    assert b"recorded" in response.content
    notification.refresh_from_db()
    assert notification.acted_at is not None
    assert notification.action == "ack"
    assert notification.action_detail == {"handled": True}
    again = client.post(url)
    assert b"Already recorded" in again.content


@pytest.mark.django_db
def test_reason_must_come_from_pick_list(client, notification):
    url = _path(actions.action_url(notification, "keep"))
    client.post(url, {"reason": "<script>"})
    notification.refresh_from_db()
    assert notification.action_detail == {}


@pytest.mark.django_db
def test_rotating_nonce_revokes_links(client, notification):
    url = _path(actions.action_url(notification, "ack"))
    notification.rotate_nonce()
    assert client.get(url).status_code == 400
    assert client.post(url).status_code == 400


@pytest.mark.django_db
def test_tampered_and_expired_tokens_rejected(client, notification, time_machine):
    from django.utils import timezone

    time_machine.move_to(timezone.now(), tick=False)
    token = actions.make_token(notification)
    assert client.get(f"/a/{token}x/ack/").status_code == 400
    assert client.get(f"/a/{token}/ack/").status_code == 200
    time_machine.shift(actions.MAX_AGE * 1.1)
    assert client.get(f"/a/{token}/ack/").status_code == 400


@pytest.mark.django_db
def test_token_for_other_recipient_rejected(notification, make_member):
    forged = signing.TimestampSigner(salt=actions.SALT).sign_object(
        {"n": notification.pk, "r": make_member().pk, "k": notification.nonce}
    )
    with pytest.raises(actions.InvalidActionLink):
        actions.read_token(forged)


def test_confirm_template_has_no_htmx_or_scripts():
    source = Path("templates/core/action_confirm.html").read_text()
    assert "hx-" not in source and "<script" not in source


@actions.register_action(
    Notification.Kind.SYSTEM,
    "peek",
    label="Open in Quartzy",
    confirm_context=lambda notification: {"quartzy_link": f"q/{notification.pk}"},
)
def _peek(notification, request, data):
    return {"peeked": True}


@pytest.mark.django_db
def test_an_action_can_add_to_its_confirm_page_without_acting(client, notification):
    url = _path(actions.action_url(notification, "peek"))
    response = client.get(url)
    assert response.status_code == 200
    assert response.context["quartzy_link"] == f"q/{notification.pk}"
    assert response.templates[0].name == "core/action_confirm.html"
    notification.refresh_from_db()
    assert notification.acted_at is None
    done = client.post(url)
    assert done.templates[0].name == "core/action_done.html"
    notification.refresh_from_db()
    assert notification.action_detail == {"peeked": True}


def test_actions_for_lists_a_kinds_actions_in_order():
    names = [action.name for action in actions.actions_for(Notification.Kind.SYSTEM)]
    assert names[:2] == ["ack", "keep"] and "peek" in names
