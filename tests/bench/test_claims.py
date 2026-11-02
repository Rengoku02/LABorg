"""Claim your history (design §4): surfacing, answers, the champions' list, the email."""

from datetime import timedelta

import pytest
from django.core import mail
from django.utils import timezone

from apps.core.models import Membership, Notification
from apps.core.policies import set_policy
from apps.inventory import claims
from apps.inventory.claims import ClaimError, Outcome, decide, surface_claims
from apps.inventory.models import HistoryClaim, StockItem
from apps.inventory.tasks import tick
from apps.quartzy.models import OrderRequest

pytestmark = pytest.mark.django_db


def claim_for(order_request):
    return HistoryClaim.objects.get(order_request=order_request)


# ---------------------------------------------------------------------------------------
# Surfacing


def test_durable_purchases_surface_after_receipt_or_age(lab, bought, member):
    recent_received = bought(days_ago=10, received_days_ago=8)
    too_fresh = bought(days_ago=10, received_days_ago=3, catalog="2119S")
    old_enough = bought(days_ago=25, status="ORDERED", catalog="2120S")
    young = bought(days_ago=5, status="ORDERED", catalog="2121S")
    consumable = bought(days_ago=40, kind="consumable", vendor="Corning", catalog="4488")
    cancelled = bought(days_ago=40, status="CANCELLED", catalog="2122S")
    ancient = bought(days_ago=800, catalog="2123S")
    assert surface_claims(lab) == 2
    surfaced = set(HistoryClaim.objects.values_list("order_request_id", flat=True))
    assert surfaced == {recent_received.pk, old_enough.pk}
    for row in (too_fresh, young, consumable, cancelled, ancient):
        assert row.pk not in surfaced
    assert surface_claims(lab) == 0  # each request surfaces once


def test_a_request_made_for_someone_is_theirs_to_claim(lab, bought, member, raj):
    row = bought(by=raj, requested_for=member)
    surface_claims(lab)
    assert claim_for(row).person == member


def test_later_comes_back_next_week_until_the_last_round(lab, bought, member):
    set_policy("claims.max_rounds", 2, lab=lab)
    row = bought()
    surface_claims(lab)
    decide(claim_for(row), Outcome.SKIPPED, by=member)
    assert surface_claims(lab) == 0  # not this week
    week_later = timezone.now() + timedelta(days=7)
    assert surface_claims(lab, now=week_later) == 1
    claim = claim_for(row)
    assert (claim.outcome, claim.round) == ("pending", 2)
    decide(claim, Outcome.SKIPPED, by=member)
    assert surface_claims(lab, now=week_later + timedelta(days=7)) == 0  # round 2 was the last


# ---------------------------------------------------------------------------------------
# Answers


def test_still_have_it_makes_stock_where_they_say(lab, bought, member, box):
    row = bought()
    surface_claims(lab)
    claim = decide(claim_for(row), Outcome.STILL_HAVE, by=member, count_kept=2, location=box)
    items = list(claim.stock_items.order_by("pk"))
    assert len(items) == 2 and claim.count_kept == 2 and claim.decided_by == member
    for item in items:
        assert (item.custodian, item.location, item.lifecycle) == (member, box, "available")
        assert item.source_order_request == row
    with pytest.raises(ClaimError, match="answered already"):
        decide(claim, Outcome.USED_UP, by=member)


def test_place_later_uses_the_unsorted_container(lab, bought, member):
    row = bought()
    surface_claims(lab)
    claim = decide(claim_for(row), Outcome.STILL_HAVE, by=member, temperature="-20C")
    (item,) = claim.stock_items.all()
    assert item.location.is_unsorted and item.location.temperature == "-20C"


def test_this_is_the_same_bottle_verifies_existing_stock(lab, bought, member, raj, make_item):
    row = bought()
    quartzy_copy = make_item(custodian=raj, lifecycle="unverified", location=None, temperature="")
    surface_claims(lab)
    claim = claim_for(row)
    (view,) = claims.claim_views([claim], member)
    assert [item.short_id for item in view.same_items] == [quartzy_copy.short_id]
    decide(claim, Outcome.STILL_HAVE, by=member, same_item=quartzy_copy)
    quartzy_copy.refresh_from_db()
    assert (quartzy_copy.custodian, quartzy_copy.lifecycle) == (member, "available")
    assert StockItem.objects.count() == 1


def test_gave_it_to_someone_in_the_lab(lab, bought, member, raj, leaver):
    row = bought()
    surface_claims(lab)
    with pytest.raises(ClaimError, match="can't hold"):
        decide(claim_for(row), Outcome.GAVE_AWAY, by=member, target_person=leaver)
    claim = decide(claim_for(row), Outcome.GAVE_AWAY, by=member, target_person=raj)
    (item,) = claim.stock_items.all()
    assert (item.custodian, item.lifecycle, claim.target_person) == (raj, "unverified", raj)


def test_shared_stock_goes_to_the_lab_pool(lab, bought, member, pool):
    row = bought()
    surface_claims(lab)
    claim = decide(claim_for(row), Outcome.SHARED_STOCK, by=member, temperature="4C")
    (item,) = claim.stock_items.all()
    assert (item.custodian, item.visibility) == (pool, "shared")


def test_did_this_arrive_watches_the_request(lab, bought, member):
    row = bought(status="ORDERED", days_ago=30)
    surface_claims(lab)
    decide(claim_for(row), Outcome.NOT_ARRIVED, by=member)
    row.refresh_from_db()
    assert row.hot_until is not None


def test_the_champions_list(lab, bought, member, raj):
    gone = bought(by=raj)
    not_mine = bought(catalog="2119S")
    mine = bought(catalog="2120S")
    surface_claims(lab)
    Membership.objects.filter(user=raj).update(status=Membership.Status.LEAVER)
    decide(claim_for(not_mine), Outcome.NOT_MINE, by=member)
    listed = set(claims.champions_list(lab).values_list("order_request_id", flat=True))
    assert listed == {gone.pk, not_mine.pk} and mine.pk not in listed


# ---------------------------------------------------------------------------------------
# The weekly round and email


@pytest.mark.django_db(transaction=True)
def test_the_weekly_round_emails_each_member_once(lab, bought, member, time_machine):
    bought(total="50.00", catalog="2119S")
    bought(total="900.00", name="Expensive kit", catalog="2120S")
    monday_9am = timezone.datetime(2026, 11, 9, 9, 0, tzinfo=timezone.UTC)
    time_machine.move_to(monday_9am, tick=False)
    assert "2 surfaced, 1 emailed" in tick()
    assert "0 surfaced, 0 emailed" in tick()  # an hour later: nothing new
    (message,) = mail.outbox
    assert message.subject == "[LabStock] Claim your history: 2 items to sort"
    body = message.body
    assert body.index("Expensive kit") < body.index("GAPDH")  # most valuable first
    assert "https://labstock.test/claims/t/" in body
    assert message.from_email.startswith("LabStock <news@")
    notification = Notification.objects.get(kind="claims")
    assert notification.period_key == "2026-W46"


def test_no_round_outside_monday_morning(lab, bought, time_machine):
    bought()
    time_machine.move_to(timezone.datetime(2026, 11, 9, 7, 0, tzinfo=timezone.UTC), tick=False)
    assert tick() == "not due"
    time_machine.move_to(timezone.datetime(2026, 11, 10, 9, 0, tzinfo=timezone.UTC), tick=False)
    assert tick() == "not due"
    assert not OrderRequest.objects.filter(history_claims__isnull=False).exists()
