"""Search before you buy, asks of the lab and the in-flight board (design §10.2-§10.4)."""

from datetime import timedelta

import pytest
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from apps.core.models import Notification
from apps.core.policies import set_policy
from apps.inventory import asks
from apps.inventory.models import AskReply, AskThread, SearchLog
from apps.inventory.search import purge_search_logs, read_query, search, searched_before
from tests.inventory.test_templates import stock_rows_in

pytestmark = pytest.mark.django_db


def test_reading_queries():
    assert read_query("2118S").kind == "catalog"
    assert (read_query("CST 2118S").family, read_query("CST 2118S").catalog) == ("cst", "2118S")
    url = read_query("https://www.cellsignal.cn/products/2118")
    assert (url.kind, url.family, url.catalog) == ("url", "cst", "2118")
    assert read_query("GAPDH rabbit antibody").kind == "name"
    assert read_query("  ").kind == ""


def test_a_catalog_search_groups_what_the_lab_has(lab, member, raj, make_item, bought):
    make_item(custodian=raj, visibility="shared")
    make_item(custodian=raj, visibility="restricted", notes="secret spot")
    bought(status="ORDERED", days_ago=3, catalog="2118L")
    bought(status="RECEIVED", days_ago=200)
    result = search(lab, member, "2118S")
    assert result.query_kind == "catalog"
    assert [view.visibility for view in result.in_lab] == ["shared", "restricted"]  # shared first
    restricted = result.in_lab[1]
    assert restricted.redacted and restricted.location_path == "" and restricted.ask_text
    assert [view.catalog_number for view in result.on_order] == ["2118L"]  # another pack size
    assert len(result.bought_before) == 1


def test_formatting_and_pasted_pages_find_the_same_product(lab, member, make_item):
    make_item()
    assert search(lab, member, "2118 S").in_lab  # typed with a space
    assert search(lab, member, "CST 2118S").in_lab
    assert search(lab, member, "https://www.cellsignal.cn/products/2118").in_lab  # the base
    assert search(lab, member, "GAPDH 14C10").in_lab  # by name


def test_unverified_stock_is_probably(lab, member, raj, make_item):
    make_item(custodian=raj, lifecycle="unverified", location=None, temperature="")
    result = search(lab, member, "2118S")
    assert not result.in_lab and result.probably[0].unverified_text == "Probably: ask Raj Rao"


def test_searches_are_logged_without_their_text(lab, member, make_item, bought):
    make_item()
    search(lab, member, "my secret project 2118S")
    search(lab, member, "2118S")
    log = SearchLog.objects.filter(kind="catalog").get()
    assert log.catalog_key == "2118S" and log.in_lab == 1 and log.product_ids
    assert not any("secret" in str(value) for value in log.__dict__.values())
    later = bought(days_ago=0)  # Priya then requests it
    assert searched_before(later) is True
    SearchLog.objects.update(at=timezone.now() - timedelta(days=31))
    assert searched_before(later) is False
    assert purge_search_logs() == 2 and not SearchLog.objects.exists()


def test_the_search_pages(client, member, make_item):
    make_item(visibility="restricted")
    client.force_login(member)
    page = client.get(reverse("bench:search"), {"q": "2118S"})
    assert page.status_code == 200 and stock_rows_in(page.context) == []
    assert "In the lab" in page.content.decode()
    partial = client.get(reverse("bench:search"), {"q": "2118S"}, headers={"HX-Request": "true"})
    assert "<html" not in partial.content.decode()
    nothing = client.get(reverse("bench:search"), {"q": "zz-no-such-thing-99"})
    assert "Ask the lab" in nothing.content.decode()
    setup = client.get(reverse("bench:search_setup"))
    assert "javascript:(function()" in setup.content.decode()
    opensearch = client.get("/opensearch.xml")
    assert opensearch["Content-Type"].startswith("application/opensearchdescription+xml")
    assert b"https://labstock.test/s?q={searchTerms}" in opensearch.content


# ---------------------------------------------------------------------------------------
# Asks


@pytest.mark.django_db(transaction=True)
def test_asking_the_holder_from_the_item_page(client, member, raj, make_item):
    item = make_item(custodian=raj, visibility="restricted")
    client.force_login(member)
    client.post(reverse("bench:ask_owner", args=[item.short_id]))
    (ask,) = mail.outbox
    assert ask.to == ["raj@lab.test"] and ask.reply_to == ["priya@lab.test"]
    assert "Box 4" in ask.body  # the holder sees where their own item is


@pytest.mark.django_db(transaction=True)
def test_ask_the_lab_caps_both_sides_and_batches(lab, member, raj, make_member, time_machine):
    sam = make_member("sam@lab.test", full_name="Sam Ortiz")
    time_machine.move_to(timezone.datetime(2026, 11, 9, 8, 0, tzinfo=timezone.UTC), tick=False)
    first = asks.ask_lab(lab=lab, asker=member, text="Does anyone have DAPI?")
    with pytest.raises(asks.AskLimit):
        asks.ask_lab(lab=lab, asker=member, text="And Hoechst?")
    asks.ask_lab(lab=lab, asker=raj, text="Anyone have Tween-20?")
    assert not mail.outbox  # held for the batch
    assert asks.release_ask_batches(lab) == 3  # Raj, Sam get Priya's; Priya, Sam get Raj's...
    # ...but each person at most one a day: Sam got one, the other waits for tomorrow.
    received = sorted(m.to[0] for m in mail.outbox)
    assert received == ["priya@lab.test", "raj@lab.test", "sam@lab.test"]
    time_machine.move_to(timezone.datetime(2026, 11, 10, 10, 0, tzinfo=timezone.UTC), tick=False)
    assert asks.release_ask_batches(lab) == 1
    assert sorted(m.to[0] for m in mail.outbox).count("sam@lab.test") == 2
    reply_link = next(m for m in mail.outbox if m.to == ["raj@lab.test"])
    assert reply_link.reply_to == ["priya@lab.test"] and "I have it" in reply_link.body
    assert first.kind == AskThread.Kind.LAB and sam is not None


@pytest.mark.django_db(transaction=True)
def test_urgent_asks_go_now_and_stale_ones_expire(lab, member, raj, time_machine):
    time_machine.move_to(timezone.datetime(2026, 11, 9, 8, 0, tzinfo=timezone.UTC), tick=False)
    asks.ask_lab(lab=lab, asker=member, text="Need PBS now", urgent=True)
    assert [m.to[0] for m in mail.outbox] == ["raj@lab.test"]
    asks.ask_lab(lab=lab, asker=raj, text="Anyone have agar?")  # Priya: held
    time_machine.move_to(timezone.datetime(2026, 11, 12, 10, 0, tzinfo=timezone.UTC), tick=False)
    assert asks.release_ask_batches(lab) == 0
    assert Notification.objects.filter(hold_reason="ask expired").count() == 1


@pytest.mark.django_db(transaction=True)
def test_replies_show_on_my_asks(client, lab, member, raj, time_machine):
    import re

    time_machine.move_to(timezone.datetime(2026, 11, 9, 8, 0, tzinfo=timezone.UTC), tick=False)
    asks.ask_lab(lab=lab, asker=member, text="DAPI?", urgent=True)
    link = re.search(r"I have it: https://labstock\.test(\S+)", mail.outbox[0].body)[1]
    client.post(link)
    assert AskReply.objects.get().answer == "have_it"
    client.force_login(member)
    page = client.get(reverse("bench:asks"))
    assert "Raj Rao: I have it" in page.content.decode()
    assert "asked the whole lab today" in page.content.decode()


def test_the_cap_follows_the_policy(lab, member):
    set_policy("asks.max_per_person_per_day", 2, lab=lab)
    asks.ask_lab(lab=lab, asker=member, text="one")
    asks.ask_lab(lab=lab, asker=member, text="two")
    with pytest.raises(asks.AskLimit):
        asks.ask_lab(lab=lab, asker=member, text="three")


# ---------------------------------------------------------------------------------------
# The in-flight board


def test_the_board_groups_open_requests(client, member, bought):
    bought(status="CREATED", days_ago=3, catalog="1111S", name="Fresh request")
    bought(status="ORDERED", days_ago=10, catalog="2222S", name="Ordered thing")
    bought(status="ORDERED", days_ago=90, catalog="3333S", name="Old order")
    bought(status="RECEIVED", days_ago=5, catalog="4444S", name="Arrived thing")
    client.force_login(member)
    page = client.get(reverse("bench:board"))
    assert stock_rows_in(page.context) == []
    names = lambda key: [view.item_name for view in page.context[key]]  # noqa: E731
    assert names("waiting_views") == ["Fresh request"]
    assert names("ordered_views") == ["Ordered thing"]
    assert names("arrived_views") == ["Old order"]
    assert "Arrived thing" not in page.content.decode()
