"""The co-owner's labelling page (design §8)."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.core.models import Membership
from apps.guard.checks import check_request
from apps.guard.models import DuplicateMatch
from tests.inventory.test_templates import stock_rows_in

pytestmark = pytest.mark.django_db


@pytest.fixture
def co_owner(make_member):
    return make_member("morgan@lab.test", full_name="Morgan Hale", role=Membership.Role.CO_OWNER)


@pytest.fixture
def match(requested, armed, stock, raj):
    stock(custodian=raj, visibility="restricted")
    requested(by="raj", at=timezone.now() - timedelta(days=2))
    check = check_request(requested().pk)
    return check.matches.order_by("-score").first()


def test_only_co_owners_and_system_owners_may_label(client, match, member, co_owner):
    url = reverse("guard:label_list")
    client.force_login(member)
    assert client.get(url).status_code == 403
    assert client.post(reverse("guard:label_match", args=[match.pk])).status_code == 403
    client.force_login(co_owner)
    assert client.get(url).status_code == 200


def test_the_page_lists_matches_through_the_presenters(client, match, co_owner):
    client.force_login(co_owner)
    response = client.get(reverse("guard:label_list"))
    assert stock_rows_in(response.context) == []
    views = response.context["match_views"]
    assert [view.kind for view in views] == ["stock", "open"]
    stock_view = views[0].item
    # The co-owner sees where restricted stock is (design §2 visibility matrix).
    assert stock_view.redacted is False and stock_view.location_path.endswith("Box 4")
    body = response.content.decode()
    assert "Same thing" in body and "T1/stock" in body


def test_labelling_with_htmx_returns_the_row(client, match, co_owner):
    client.force_login(co_owner)
    url = reverse("guard:label_match", args=[match.pk])
    response = client.post(url, {"label": "true_dup"}, headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "Labelled: <strong>Same thing: a duplicate</strong>" in response.content.decode()
    assert stock_rows_in(response.context) == []
    match.refresh_from_db()
    assert (match.label, match.labelled_by) == ("true_dup", co_owner)
    # Without htmx the form posts and comes back to the list, where it is gone now.
    other = DuplicateMatch.objects.exclude(pk=match.pk).get()
    response = client.post(reverse("guard:label_match", args=[other.pk]), {"label": "false"})
    assert response.status_code == 302
    assert client.get(reverse("guard:label_list")).context["match_views"] == []


def test_a_bad_label_is_refused(client, match, co_owner):
    client.force_login(co_owner)
    response = client.post(reverse("guard:label_match", args=[match.pk]), {"label": "maybe"})
    assert response.status_code == 400


def test_another_labs_match_is_not_found(client, match, other_lab):
    from apps.core.models import User

    other_owner = User.objects.create_user(email="o@other.test", full_name="Other Owner")
    Membership.objects.create(user=other_owner, lab=other_lab, role=Membership.Role.CO_OWNER)
    client.force_login(other_owner)
    url = reverse("guard:label_match", args=[match.pk])
    assert client.post(url, {"label": "true_dup"}).status_code == 404
