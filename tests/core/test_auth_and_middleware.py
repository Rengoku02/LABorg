import logging

import pytest
from allauth.mfa.models import Authenticator
from django.urls import reverse

from apps.core.logging import RedactActionTokens
from apps.core.models import Membership


@pytest.mark.django_db
def test_signup_is_closed(client):
    response = client.get(reverse("account_signup"))
    assert b"Sign Up Closed" in response.content or response.status_code in (302, 403)


@pytest.mark.django_db
def test_home_requires_login(client):
    response = client.get("/")
    assert response.status_code == 302 and reverse("account_login") in response["Location"]


@pytest.mark.django_db
def test_member_sees_home_with_csp_and_lab(member_client, lab):
    response = member_client.get("/")
    assert response.status_code == 200
    csp = response["Content-Security-Policy"]
    assert "script-src 'self' 'nonce-" in csp and "unsafe-eval" not in csp
    assert response.context["current_lab"] == lab
    from html import unescape

    assert '"allowEval": false' in unescape(response.content.decode())


@pytest.mark.django_db
def test_leaver_cannot_log_in(member):
    from allauth.account.adapter import get_adapter
    from django.contrib.sessions.middleware import SessionMiddleware
    from django.test import RequestFactory

    Membership.objects.filter(user=member).update(status=Membership.Status.LEAVER)
    request = RequestFactory().get("/")
    SessionMiddleware(lambda r: None).process_request(request)
    response = get_adapter().pre_login(
        request,
        member,
        email_verification="none",
        signal_kwargs=None,
        email=None,
        signup=False,
        redirect_url=None,
    )
    assert response is not None  # login refused


@pytest.mark.django_db
def test_staff_without_totp_is_sent_to_mfa_setup(client, member):
    member.is_staff = True
    member.save()
    client.force_login(member)
    response = client.get("/")
    assert response.status_code == 302 and reverse("mfa_activate_totp") in response["Location"]
    Authenticator.objects.create(user=member, type=Authenticator.Type.TOTP, data={})
    assert client.get("/").status_code == 200


@pytest.mark.django_db
def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200 and response.json() == {"ok": True}


def test_log_filter_redacts_action_tokens():
    record = logging.LogRecord(
        "x", logging.INFO, "f", 1, "GET %s 200", ("/a/eyJuIjo:1x:sig/ack/",), None
    )
    RedactActionTokens().filter(record)
    assert record.getMessage() == "GET /a/[redacted]/ack/ 200"


def test_logs_never_hold_tokens_or_searches():
    import logging

    paths = ("/a/TOKEN/cancel/", "/claims/t/T/", "/s?q=my+secret")
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "%s %s %s", paths, None)
    RedactActionTokens().filter(record)
    assert record.getMessage() == "/a/[redacted]/cancel/ /claims/t/[redacted]/ /s?[redacted]"
