"""Live duplicate alerts (design §7.4 to §8): who gets mail, the safety limits, the kill
switches, the one-tap answers and the verified outcomes."""

import re
from datetime import date, timedelta

import pytest
from django.core import mail
from django.utils import timezone

from apps.core.models import Membership, Notification
from apps.guard import alerts, outcomes, tasks
from apps.guard.checks import check_request
from apps.guard.models import CheckRun, DuplicateCheck, Outcome, PairSuppression
from apps.inventory.models import AskReply
from apps.quartzy.models import OrderRequest, OrderStatusObservation

pytestmark = pytest.mark.django_db
Status = DuplicateCheck.Status


@pytest.fixture
def live(policy):
    """Alerts on, with the given tiers live (default: T1 against open requests)."""

    def _live(*tiers):
        policy("alerts.enabled", True)
        policy("alerts.tiers_live", list(tiers or ["T1/open"]))

    return _live


@pytest.fixture
def committed(django_capture_on_commit_callbacks):
    return lambda: django_capture_on_commit_callbacks(execute=True)


def ago(**delta):
    return timezone.now() - timedelta(**delta)


def links(message):
    return dict(re.findall(r"- (.+?): (https://labstock\.test/a/\S+)", message.body))


def path(url):
    return url.replace("https://labstock.test", "")


# ---------------------------------------------------------------------------------------
# Who gets mail


def test_a_live_t1_open_match_alerts_both_requesters(requested, armed, live, committed):
    live()
    requested(by="raj", at=ago(days=2))
    mine = requested(at=ago(minutes=3))
    with committed():
        check = check_request(mine.pk)
    assert (check.status, check.shadow) == (Status.ALERTED, False)
    by_role = {n.context["role"]: n for n in Notification.objects.filter(kind="duplicate_alert")}
    assert set(by_role) == {"requester", "earlier"}
    assert {m.to[0] for m in mail.outbox} == {"priya@lab.test", "raj@lab.test"}
    alert = next(m for m in mail.outbox if m.to == ["priya@lab.test"])
    assert alert.subject.startswith("Before you order: Raj Rao requested Cell Signaling")
    assert "Have you placed the vendor order yet?" in alert.body
    assert "Only you receive this; the PI sees lab totals only." in alert.body
    assert set(links(alert)) >= {"Share Raj Rao's order", "Cancel my Quartzy request"}
    heads_up = next(m for m in mail.outbox if m.to == ["raj@lab.test"])
    assert heads_up.reply_to == ["priya@lab.test"] and "/a/" not in heads_up.body


def test_a_tier_that_isnt_live_stays_in_shadow(requested, armed, live, stock, raj):
    live("T1/open")
    stock(custodian=raj)  # the best match is stock, and T1/stock isn't live
    check = check_request(requested().pk)
    assert check.status == Status.SHADOW_ONLY and not Notification.objects.exists()


def test_restricted_stock_is_never_located_in_the_alert(
    requested, armed, live, stock, raj, committed
):
    live("T1")
    stock(custodian=raj, visibility="restricted")
    with committed():
        check_request(requested().pk)
    (alert,) = mail.outbox
    assert alert.subject == (
        "Before you order: Raj Rao has Cell Signaling Technology 2118S (GAPDH (14C10) Rabbit mAb)"
    )
    assert "Ask Raj Rao" in alert.body and "Box 4" not in alert.body and "-80" not in alert.body
    assert "Borrow from Raj Rao" in links(alert)


def test_a_shared_item_says_where_it_is(requested, armed, live, stock, raj, committed):
    live("T1")
    stock(custodian=raj, visibility="shared")
    with committed():
        check_request(requested().pk)
    (alert,) = mail.outbox
    assert alert.subject.endswith("in -80 A")
    assert "Room 210 / -80 A / Shelf 1 / Box 4" in alert.body


def test_nobody_is_alerted_twice(requested, armed, live, committed):
    live()
    requested(by="raj", at=ago(days=2))
    mine = requested()
    with committed():
        check_request(mine.pk)
        check_request(mine.pk)
        tasks.check_requests([mine.pk], "head")
    assert len([m for m in mail.outbox if m.to == ["priya@lab.test"]]) == 1


# ---------------------------------------------------------------------------------------
# Safety limits and kill switches


def test_a_burst_of_requests_is_held(requested, armed, live, policy, committed):
    live("T1")
    policy("alerts.burst_max_requests", 2)
    requested(by="raj", at=ago(days=2))
    for minutes in (9, 6, 3):
        requested(catalog="2119S", at=ago(minutes=minutes))  # 3 from Priya within 15 min
    mine = requested(at=ago(minutes=1))
    with committed():
        check = check_request(mine.pk)
    assert (check.status, check.hold_reason) == (Status.HELD, "burst")
    held = Notification.objects.get(kind="duplicate_alert", recipient__email="priya@lab.test")
    assert (held.status, held.hold_reason) == ("held", "burst")
    assert not [m for m in mail.outbox if m.to == ["priya@lab.test"]]


def test_the_circuit_breaker_holds_a_cycle_and_releases_on_a_tap(
    requested, armed, live, policy, committed, client, superuser
):
    live("T1")
    policy("alerts.circuit_breaker_max", 2)
    ids = []
    for n in range(3):
        catalog = f"21{n}0S"
        requested(by="raj", catalog=catalog, at=ago(days=2))
        ids.append(requested(catalog=catalog, at=ago(minutes=1)).pk)
    with committed():
        tasks.check_requests(ids, "head")
    run = CheckRun.objects.get()
    assert (run.alerts_planned, run.held) == (3, True)
    assert set(DuplicateCheck.objects.filter(run=run).values_list("status", flat=True)) == {"held"}
    (system,) = [m for m in mail.outbox if "circuit breaker" in m.subject]
    assert system.to == [superuser.email]
    release = re.search(r"Release the held alerts: (\S+)", system.body)[1]
    with committed():
        assert client.post(path(release)).status_code == 200
    assert DuplicateCheck.objects.filter(status="alerted").count() == 3
    assert len([m for m in mail.outbox if m.subject.startswith("Before you order")]) == 3
    assert len([m for m in mail.outbox if m.subject.startswith("Heads up")]) == 3


def test_a_cycle_under_the_limit_goes_out_when_the_run_ends(requested, armed, live, committed):
    live()
    requested(by="raj", at=ago(days=2))
    mine = requested()
    with committed():
        tasks.check_requests([mine.pk], "head")
    assert CheckRun.objects.get().held is False
    assert len(mail.outbox) == 2


def test_switching_alerts_off_holds_queued_mail(requested, armed, live, policy, committed):
    live()
    requested(by="raj", at=ago(days=2))
    mine = requested()
    run = CheckRun.objects.create(lab=mine.lab, via="head")
    check_request(mine.pk, run=run)  # held for the cycle
    policy("alerts.enabled", False)
    with committed():
        alerts.finish_cycle(run)
    assert not mail.outbox
    assert set(
        Notification.objects.filter(kind="duplicate_alert").values_list("hold_reason", flat=True)
    ) == {"alerts are switched off"}


def test_a_request_cancelled_before_the_send_is_suppressed(requested, armed, live, committed):
    live()
    requested(by="raj", at=ago(days=2))
    mine = requested()
    run = CheckRun.objects.create(lab=mine.lab, via="head")
    check_request(mine.pk, run=run)
    OrderRequest.objects.filter(pk=mine.pk).update(status="CANCELLED")
    with committed():
        alerts.finish_cycle(run)
    statuses = set(Notification.objects.values_list("status", flat=True))
    assert statuses == {"suppressed"} and not mail.outbox


# ---------------------------------------------------------------------------------------
# One-tap answers


def alert_links(requested, check_request, committed, **extra):
    requested(by="raj", at=ago(days=2), **extra)
    mine = requested(**extra)
    with committed():
        check = check_request(mine.pk)
    alert = next(m for m in mail.outbox if m.to == ["priya@lab.test"])
    return check, links(alert)


def test_cancel_shows_quartzy_and_verifies_from_the_sync(
    client, requested, armed, live, committed, make_member, member
):
    live()
    Membership.objects.filter(user=member).update(is_quartzy_lab_admin=False)
    make_member("admin@lab.test", full_name="Morgan Admin", is_quartzy_lab_admin=True)
    check, found = alert_links(requested, check_request, committed)
    url = path(found["Cancel my Quartzy request"])
    page = client.get(url)
    assert "Open the request in Quartzy" in page.content.decode()
    assert Outcome.objects.count() == 0  # GET never acts
    client.post(url)
    intent = Outcome.objects.get()
    watched = OrderRequest.objects.get(pk=check.order_request_id).hot_until
    assert intent.kind == "cancel_intended" and watched is not None
    check.refresh_from_db()
    assert check.alert_in_time is True
    assert outcomes.verify_outcomes() == 0
    OrderStatusObservation.objects.create(
        order_request=check.order_request,
        from_status="CREATED",
        to_status="CANCELLED",
        observed_at=timezone.now(),
        via="poll",
    )
    assert outcomes.verify_outcomes() == 1
    verified = Outcome.objects.get(kind="cancelled")
    assert verified.verified_at and verified.est_value_avoided == 289
    today = timezone.now()
    saved = outcomes.value_avoided(check.lab, today - timedelta(days=1), today + timedelta(days=1))
    assert saved["count"] == 0 and saved["native"] == 1  # Quartzy's own warning would fire


def test_borrow_asks_the_holder_and_lend_verifies_it(
    client, requested, armed, live, stock, raj, committed, member
):
    live("T1")
    stock(custodian=raj, visibility="restricted")
    with committed():
        check = check_request(requested().pk)
    found = links(mail.outbox[0])
    with committed():
        client.post(path(found["Borrow from Raj Rao"]))
    ask = mail.outbox[-1]
    assert ask.to == ["raj@lab.test"] and ask.reply_to == ["priya@lab.test"]
    assert "would like to borrow some" in ask.subject
    lend = re.search(r"Yes, borrow some: (\S+)", ask.body)[1]
    with committed():
        client.post(path(lend))
    assert AskReply.objects.get().answer == "lend"
    borrowed = Outcome.objects.get(kind="borrowed", duplicate_check=check)
    assert borrowed.verified_at and borrowed.decided_by == member


def test_not_the_same_thing_suppresses_the_pair_for_the_requester(
    client, requested, armed, live, committed, member
):
    live()
    check, found = alert_links(requested, check_request, committed)
    client.post(path(found["Not the same thing"]))
    suppression = PairSuppression.objects.get()
    assert (suppression.scope, suppression.requester) == ("requester", member)
    match = check.matches.get()
    match.refresh_from_db()
    assert (match.label, match.label_suggestion) == ("", "false")  # a suggestion only


def test_need_my_own_takes_a_reason(client, requested, armed, live, committed):
    live()
    _check, found = alert_links(requested, check_request, committed)
    response = client.post(path(found["I need my own"]), {"reason": "lot"})
    assert response.status_code == 200
    outcome = Outcome.objects.get()
    assert (outcome.kind, outcome.reason) == ("need_own", "lot")


def test_one_answer_per_alert(client, requested, armed, live, committed):
    live()
    _check, found = alert_links(requested, check_request, committed)
    client.post(path(found["Keep it, offer the spare"]))
    again = client.post(path(found["Not the same thing"]))
    assert b"Already recorded" in again.content and Outcome.objects.count() == 1


def test_the_earlier_requesters_copy_cannot_act(client, requested, armed, live, committed):
    from apps.core.actions import action_url

    live()
    alert_links(requested, check_request, committed)
    heads_up = Notification.objects.get(kind="duplicate_alert", context__role="earlier")
    response = client.post(path(action_url(heads_up, "cancel")))
    assert response.status_code == 400 and not Outcome.objects.exists()


def test_vendor_cancel_is_confirmed_on_my_alerts(client, requested, armed, live, committed, member):
    live()
    check, found = alert_links(requested, check_request, committed)
    client.post(path(found["I'll cancel with the vendor"]))
    check.refresh_from_db()
    assert check.alert_in_time is False
    client.force_login(member)
    page = client.get("/guard/mine/")
    assert "The vendor confirmed the cancellation" in page.content.decode()
    client.post(f"/guard/mine/{check.pk}/vendor_confirmed/")
    assert Outcome.objects.get(kind="vendor_cancel_confirmed").verified_at


# ---------------------------------------------------------------------------------------
# Clear to order


def test_clear_to_order_receipts_while_the_launch_period_lasts(
    requested, armed, live, policy, committed
):
    live()
    policy("alerts.clear_to_order_until", (date.today() + timedelta(days=30)).isoformat())
    with committed():
        check = check_request(requested().pk)
    assert check.status == Status.CLEAR
    (receipt,) = mail.outbox
    assert receipt.subject.startswith("No match: clear to order")
    policy("alerts.clear_to_order_until", (date.today() - timedelta(days=1)).isoformat())
    with committed():
        later = check_request(requested(catalog="7777S").pk)
    assert later.status == Status.NO_MATCH and len(mail.outbox) == 1


def test_value_avoided_leaves_out_reorders(requested, armed, live, committed, member):
    live()
    raj_req = requested(by="raj", at=ago(days=2), catalog="2119S")
    mine = requested(catalog="2119-S")  # formatting differs: Quartzy wouldn't have warned
    with committed():
        check = check_request(mine.pk)
    assert check.matches.get().candidate_order_request == raj_req
    assert not check.quartzy_native_would_fire_raw
    outcomes.record(check, Outcome.Kind.CANCELLED, by=member, verified=True)
    now = timezone.now()
    saved = outcomes.value_avoided(check.lab, now - timedelta(days=1), now + timedelta(days=1))
    assert (saved["count"], saved["amount"]) == (1, 289)
    requested(catalog="2119S", at=now + timedelta(days=3))  # bought it anyway
    saved = outcomes.value_avoided(check.lab, now - timedelta(days=1), now + timedelta(days=1))
    assert (saved["count"], saved["reordered"]) == (0, 1)
