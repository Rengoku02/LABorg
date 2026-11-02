"""Nudges, the email budget, the weekly emails and the dashboard (design §10.5-§10.9)."""

import re
from datetime import datetime, timedelta

import pytest
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from apps.core.models import Membership, Notification
from apps.core.policies import set_policy
from apps.guard.models import DuplicateCheck
from apps.inventory.claims import queue_claims_emails, surface_claims
from apps.reports import digests, nudges
from apps.reports.tasks import tick

pytestmark = pytest.mark.django_db
MONDAY_0905 = datetime(2026, 11, 9, 9, 5, tzinfo=timezone.UTC)


@pytest.fixture
def armed(qlab):
    qlab.alerts_armed_at = timezone.now() - timedelta(days=365)
    qlab.save()
    return qlab


@pytest.fixture
def pi(make_member):
    return make_member("pat@lab.test", full_name="Pat Investigator", role=Membership.Role.PI)


# ---------------------------------------------------------------------------------------
# Nudges


@pytest.mark.django_db(transaction=True)
def test_one_nudge_per_stale_request_never_for_the_backlog(lab, bought, armed, client):
    stale = bought(status="ORDERED", days_ago=30, name="Slow enzyme")
    bought(status="ORDERED", days_ago=5, catalog="1234S")  # not stale yet
    armed.alerts_armed_at = timezone.now() - timedelta(days=40)
    armed.save()
    bought(status="CREATED", days_ago=60, catalog="9999S")  # before arming: backlog
    assert nudges.nudge_stale_requests(lab) == 1
    assert nudges.nudge_stale_requests(lab) == 0
    (nudge,) = mail.outbox
    assert nudge.subject == "Did this arrive? Slow enzyme"
    arrived = re.search(r"It arrived: https://labstock\.test(\S+)", nudge.body)[1]
    client.post(arrived)
    stale.refresh_from_db()
    assert stale.hot_until is not None


# ---------------------------------------------------------------------------------------
# Email budget


@pytest.mark.django_db(transaction=True)
def test_the_budget_drops_extra_non_alert_mail(lab, member, bought, raj):
    set_policy("email.max_non_alert_per_week", 1, lab=lab)
    bought(days_ago=30)
    surface_claims(lab)
    queue_claims_emails(lab)  # 1st non-alert email: sent
    from apps.inventory import asks

    asks.ask_lab(lab=lab, asker=raj, text="DAPI?", urgent=True)  # 2nd: over budget
    statuses = dict(Notification.objects.values_list("kind", "status"))
    assert statuses == {"claims": "sent", "ask_lab": "suppressed"}
    assert len(mail.outbox) == 1


# ---------------------------------------------------------------------------------------
# Personal digest and lab summary


@pytest.mark.django_db(transaction=True)
def test_digests_go_only_to_people_with_something_waiting(lab, member, raj, bought, time_machine):
    time_machine.move_to(MONDAY_0905, tick=False)
    bought(days_ago=30)  # Priya has a claim to sort
    surface_claims(lab)
    assert digests.queue_personal_digests(lab) == 1
    assert digests.queue_personal_digests(lab) == 0  # once a week
    (digest,) = mail.outbox
    assert digest.to == ["priya@lab.test"] and digest.subject == "[LabStock] Your week"
    assert "1 past purchase to sort" in digest.body


@pytest.mark.django_db(transaction=True)
def test_the_lab_summary_names_nobody(lab, member, raj, pi, bought, time_machine):
    from apps.inventory.models import AskReply, AskThread

    time_machine.move_to(MONDAY_0905, tick=False)
    request = bought(days_ago=1, name="Antibody for Priya Patel")
    DuplicateCheck.objects.create(
        lab=lab,
        order_request=request,
        matcher_version=1,
        status=DuplicateCheck.Status.ALERTED,
        as_of=timezone.now(),
        created_at=timezone.now() - timedelta(hours=2),
    )
    thread = AskThread.objects.create(lab=lab, kind="lab", source="search", asker=member)
    AskReply.objects.create(thread=thread, responder=raj, answer="lend")
    Membership.objects.filter(user=raj).update(thank_by_name=True)
    assert digests.queue_lab_summary(lab) == 1  # the PI (no co-owner in this lab)
    (summary,) = mail.outbox
    assert summary.to == ["pat@lab.test"]
    assert "Duplicates caught: <3" in summary.body
    assert "Priya" not in summary.body and "Patel" not in summary.body
    assert "Thanks to Raj Rao for lending." in summary.body


def test_the_reports_tick_follows_lab_time(lab, member, bought, armed, time_machine):
    time_machine.move_to(datetime(2026, 11, 9, 7, 59, tzinfo=timezone.UTC), tick=False)
    assert tick() == "not due"
    time_machine.move_to(datetime(2026, 11, 9, 8, 10, tzinfo=timezone.UTC), tick=False)
    assert "digests" in tick() and "summaries" not in tick()
    time_machine.move_to(MONDAY_0905, tick=False)
    assert "nudges" in tick() and "summaries" in tick()


# ---------------------------------------------------------------------------------------
# Dashboard


def test_the_dashboard_is_for_the_pi_and_co_owners(client, lab, member, pi, make_member):
    url = reverse("reports:dashboard")
    client.force_login(member)
    assert client.get(url).status_code == 403
    client.force_login(pi)
    page = client.get(url)
    assert page.status_code == 200 and "Lab totals" in page.content.decode()
    co_owner = make_member("co@lab.test", role=Membership.Role.CO_OWNER)
    client.force_login(co_owner)
    assert client.get(url).status_code == 200
    set_policy("reports.dashboard_audience", "members", lab=lab)
    client.force_login(member)
    assert client.get(url).status_code == 200


def test_the_dashboard_shows_totals_not_people(client, lab, member, pi, bought, make_item):
    make_item()
    request = bought(days_ago=1, name="Priya's antibody")
    DuplicateCheck.objects.create(
        lab=lab,
        order_request=request,
        matcher_version=1,
        status=DuplicateCheck.Status.ALERTED,
        as_of=timezone.now(),
        searched_before=True,
        alert_in_time=True,
    )
    client.force_login(pi)
    body = client.get(reverse("reports:dashboard")).content.decode()
    assert "100%" in body  # searched first, and in time
    assert "Priya" not in body and "Patel" not in body
