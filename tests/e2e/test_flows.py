"""The three MVP-A flows in a phone-sized browser (plan §13)."""

import re
from datetime import timedelta

import pytest
from django.core import mail
from django.utils import timezone

from apps.guard.checks import check_request
from apps.guard.models import Outcome
from apps.inventory.claims import queue_claims_emails, surface_claims
from apps.inventory.models import HistoryClaim

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]


def test_a_pasted_vendor_page_finds_stock_and_asks_its_holder(
    page, live_server, sign_in, csp_violations, member, raj, make_item
):
    make_item(custodian=raj, visibility="shared")
    sign_in(member)
    page.goto(f"{live_server.url}/s")
    page.fill("#q", "https://www.cellsignal.cn/products/2118")
    page.click("button:has-text('Search')")
    page.wait_for_selector("text=In the lab")
    assert page.locator("text=Raj Rao").first.is_visible()
    assert page.locator("text=Room 210 / -80 A / Shelf 1 / Box 4").first.is_visible()
    page.click("button:has-text('Ask to borrow some')")
    page.wait_for_selector("text=Asked.")
    assert [message.to for message in mail.outbox] == [["raj@lab.test"]]
    assert csp_violations == []


def test_claim_history_through_the_email_link(
    page, live_server, local, csp_violations, lab, member, bought
):
    bought(days_ago=30)
    surface_claims(lab)
    queue_claims_emails(lab)
    link = re.search(r"https://labstock\.test/claims/t/\S+/", mail.outbox[0].body)[0]
    page.goto(local(link))  # signed out: the link opens only the claims list
    page.wait_for_selector("text=Claim your history")
    assert page.locator("text=GAPDH (14C10) Rabbit mAb").first.is_visible()
    page.select_option("select[name=outcome]", "used_up")
    page.click("button:has-text('Save')")
    page.wait_for_selector("text=Used up")
    assert HistoryClaim.objects.get().outcome == "used_up"
    assert csp_violations == []


def test_an_alert_answer_is_confirmed_on_a_page(
    page, local, csp_violations, requested, armed, policy
):
    policy("alerts.enabled", True)
    policy("alerts.tiers_live", ["T1/open"])
    requested(by="raj", at=timezone.now() - timedelta(days=2))
    mine = requested()
    check_request(mine.pk)
    alert = next(message for message in mail.outbox if message.to == ["priya@lab.test"])
    link = re.search(r"Not the same thing: (\S+)", alert.body)[1]
    page.goto(local(link))  # opening the link records nothing
    page.wait_for_selector("text=Recording as")
    assert not Outcome.objects.exists()
    page.click("button:has-text('Confirm')")
    page.wait_for_selector("text=recorded")
    assert Outcome.objects.get().kind == "not_same"
    assert csp_violations == []
