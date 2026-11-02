from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.management import call_command
from django.utils import timezone

from apps.core.models import Lab, Membership, PersonAlias, User
from apps.guard.anonymize import NameScrubber
from apps.quartzy.models import OrderRequest
from apps.quartzy.presenters import (
    money_text,
    present_request,
    present_requests,
    request_value,
    with_related,
)


@pytest.fixture
def other_lab_member(db):
    call_command("bootstrap_lab", name="Other Lab", slug="other-lab", verbosity=0)
    user = User.objects.create_user(email="olu@other.test", full_name="Olu Other")
    Membership.objects.create(user=user, lab=Lab.objects.get(slug="other-lab"))
    return user


@pytest.fixture
def order(lab, member):
    return OrderRequest.objects.create(
        lab=lab,
        quartzy_id="q-1",
        item_name="GAPDH antibody for Priya Patel",
        vendor_name_raw="CST",
        catalog_number_raw="2118S",
        unit_size_raw="100 uL",
        quantity=Decimal("2"),
        unit_price=Decimal("289.00"),
        currency="USD",
        status=OrderRequest.Status.ORDERED,
        requested_by=member,
        requested_at=timezone.now() - timedelta(days=10, hours=1),
        notes="for the westerns",
        app_url="https://app.quartzy.com/groups/x/requests?id=q-1",
    )


@pytest.mark.django_db
def test_members_see_the_request_with_its_value_and_requester(order, member, make_member):
    raj = make_member("raj@lab.test", full_name="Raj Rao")
    view = present_request(order, raj, channel="page")
    assert (view.item_name, view.vendor, view.catalog_number) == (
        "GAPDH antibody for Priya Patel",
        "CST",
        "2118S",
    )
    assert view.value == Decimal("578.00") and view.value_text == "$578.00"
    assert (view.requester_name, view.viewer_is_requester) == ("Priya Patel", False)
    assert (view.status_label, view.is_open, view.age_days) == ("Ordered", True, 10)
    assert view.quantity_text == "2" and view.notes == "for the westerns"
    assert present_request(order, member, channel="email").viewer_is_requester


@pytest.mark.django_db
def test_people_outside_the_lab_see_nothing(order, make_member, other_lab_member):
    leaver = make_member("lee@lab.test", status=Membership.Status.LEAVER)
    for viewer in (leaver, other_lab_member, AnonymousUser(), None):
        assert present_request(order, viewer, channel="page") is None


@pytest.mark.django_db
def test_scrubbed_views_name_nobody(order, member):
    scrub = NameScrubber(["Priya Patel"])
    view = present_request(order, None, channel="digest", scrub=scrub)
    assert view.item_name.startswith("GAPDH antibody for [name]")
    assert "Priya" not in view.item_name and "Patel" not in view.item_name
    assert (view.requester_name, view.requester_id, view.notes) == ("", None, "")


@pytest.mark.django_db
def test_the_requester_falls_back_to_what_quartzy_sent(order, member):
    alias = PersonAlias.objects.create(
        kind=PersonAlias.Kind.QUARTZY_USER_ID, value="u-9", display_name="Dana Doe"
    )
    order.requested_by = None
    order.created_by_alias = alias
    order.save()
    rows = with_related(OrderRequest.objects.filter(pk=order.pk))
    (view,) = present_requests(rows, member, channel="page")
    assert view.requester_name == "Dana Doe" and not view.viewer_is_requester


def test_value_and_money_text():
    request = OrderRequest(total_price=Decimal("12.5"), unit_price=Decimal("1"), quantity=None)
    assert request_value(request) == Decimal("12.5")
    request.total_price = None
    assert request_value(request) is None
    assert money_text(Decimal("1234.5"), "USD") == "$1,234.50"
    assert money_text(Decimal("-3"), "EUR") == "-€3.00"
    assert money_text(Decimal("7"), "CHF") == "7.00 CHF"
    assert money_text(None, "USD") == ""


def test_unknown_channel_is_refused():
    with pytest.raises(ValueError, match="Unknown channel"):
        present_requests([], None, channel="sms")
