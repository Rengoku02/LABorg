"""The bench pages: what each viewer sees (through the presenters), the claims link's scoped
session, add what I have, register a box."""

import re
import uuid
from datetime import timedelta

import pytest
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from apps.core.models import Membership, Notification
from apps.inventory.claims import queue_claims_emails, surface_claims
from apps.inventory.models import HistoryClaim, Location, StockItem
from apps.quartzy.models import OrderRequest
from tests.inventory.test_templates import stock_rows_in

pytestmark = pytest.mark.django_db
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
def login(client):
    def _login(user):
        client.force_login(user)
        return client

    return _login


def assert_clean(response):
    assert response.status_code in (200, 302), response.status_code
    if response.status_code == 200:
        assert stock_rows_in(response.context) == []
        body = response.content.decode()
        assert ' style="' not in body  # strict CSP: no inline styles
        assert body.count("<script") <= 1  # htmx only


# ---------------------------------------------------------------------------------------
# Items and locations


def test_the_custodian_sees_everything_and_may_change_sharing(login, make_item, member):
    item = make_item(visibility="restricted", notes="for the westerns")
    client = login(member)
    response = client.get(reverse("bench:item", args=[item.short_id]))
    assert_clean(response)
    body = response.content.decode()
    assert "Box 4" in body and "for the westerns" in body and "Who may see where it is?" in body
    response = client.post(
        reverse("bench:item_share", args=[item.short_id]),
        {"share": "shared", "version": item.version},
    )
    assert response.status_code == 302
    item.refresh_from_db()
    assert item.visibility == "shared"


def test_others_see_a_restricted_item_without_where_it_is(login, make_item, raj):
    item = make_item(visibility="restricted", notes="for the westerns")
    response = login(raj).get(reverse("bench:item", args=[item.short_id]))
    assert_clean(response)
    body = response.content.decode()
    assert "Not shown to you" in body and "Ask Priya Patel" in body
    assert "Box 4" not in body and "westerns" not in body and "Who may see" not in body
    response = login(raj).post(
        reverse("bench:item_share", args=[item.short_id]), {"share": "shared", "version": 1}
    )
    assert response.status_code == 403


def test_outsiders_and_leavers_get_nothing(login, make_item, outsider, leaver):
    item = make_item()
    # Another lab's member looks in their own lab: the item isn't there (not even "forbidden").
    assert login(outsider).get(reverse("bench:item", args=[item.short_id])).status_code == 404
    assert login(leaver).get(reverse("bench:item", args=[item.short_id])).status_code == 403


def test_a_short_id_typed_loosely_redirects(login, make_item, member):
    item = make_item()
    loose = item.short_id.lower()
    response = login(member).get(reverse("bench:item", args=[loose]))
    assert response.status_code == 302 and response.url.endswith(f"/i/{item.short_id}")


def test_a_location_lists_only_what_the_viewer_may_know_is_there(login, make_item, box, raj):
    make_item(visibility="restricted", name="Secret antibody", catalog="9999S")
    make_item(name="Open antibody", catalog="1111S")
    response = login(raj).get(reverse("bench:location", args=[box.code]))
    assert_clean(response)
    body = response.content.decode()
    assert "Open antibody" in body and "Secret antibody" not in body


def test_a_lid_photo_only_for_those_who_may_see_the_box(login, lab, shelf, member, raj, outsider):
    from apps.inventory.intake import register_box

    photo = SimpleUploadedFile("lid.png", PNG, content_type="image/png")
    secret = register_box(
        lab=lab, owner=member, parent=shelf, name="Box 9", user=member, photo=photo,
        visibility="restricted",
    )  # fmt: skip
    url = reverse("bench:location_photo", args=[secret.code])
    response = login(member).get(url)
    assert response.status_code == 200 and response["Cache-Control"].startswith("private")
    assert b"".join(response.streaming_content) == PNG
    assert login(raj).get(url).status_code == 404  # restricted box: not Raj's
    assert login(outsider).get(url).status_code == 404  # not in their lab


def test_my_stock(login, make_item, member):
    make_item(name="Mine", catalog="1234S")
    response = login(member).get(reverse("bench:my_stock"))
    assert_clean(response)
    assert "Mine" in response.content.decode()


def test_home_shows_next_steps(login, member, make_member):
    co_owner = make_member("co@lab.test", role=Membership.Role.CO_OWNER)
    response = login(member).get(reverse("core:home"))
    assert_clean(response)
    assert "Claim your history" in response.content.decode()
    assert "Label duplicate matches" not in response.content.decode()
    assert "Label duplicate matches" in login(co_owner).get(reverse("core:home")).content.decode()


# ---------------------------------------------------------------------------------------
# Claims, and the email's scoped link


def test_answering_a_claim_with_htmx(login, lab, bought, member, box):
    row = bought()
    surface_claims(lab)
    claim = HistoryClaim.objects.get(order_request=row)
    client = login(member)
    response = client.get(reverse("bench:claims"))
    assert_clean(response)
    assert "GAPDH (14C10) Rabbit mAb" in response.content.decode()
    response = client.post(
        reverse("bench:claim_decide", args=[claim.pk]),
        {"outcome": "still_have", "count": "1", "place": box.code, "temp": ""},
        headers={"HX-Request": "true"},
    )
    assert_clean(response)
    assert "Answered: <strong>Still have it</strong>" in response.content.decode()
    assert StockItem.objects.get().location == box


def test_someone_elses_claim_is_refused(login, lab, bought, raj):
    row = bought()
    surface_claims(lab)
    claim = HistoryClaim.objects.get(order_request=row)
    url = reverse("bench:claim_decide", args=[claim.pk])
    response = login(raj).post(url, {"outcome": "used_up"})
    assert response.status_code == 403


@pytest.mark.django_db(transaction=True)
def test_the_email_link_opens_only_the_claims_list(client, lab, bought, member, make_item):
    bought()
    surface_claims(lab)
    queue_claims_emails(lab)
    (message,) = mail.outbox
    link = re.search(r"https://labstock\.test(/claims/t/[^\s]+/)", message.body)[1]
    response = client.get(link)
    assert response.status_code == 302 and response.url == reverse("bench:claims")
    page = client.get(reverse("bench:claims"))
    assert page.status_code == 200 and "from your email link" in page.content.decode()
    claim = HistoryClaim.objects.get()
    response = client.post(reverse("bench:claim_decide", args=[claim.pk]), {"outcome": "used_up"})
    assert response.status_code == 302
    assert HistoryClaim.objects.get().decided_by == member
    # Nothing else: not the member's stock, not an item, not the admin.
    item = make_item()
    for url in (
        reverse("bench:my_stock"),
        reverse("bench:item", args=[item.short_id]),
        reverse("core:home"),
    ):
        assert client.get(url).status_code == 302  # to sign in
    assert client.get("/admin/").status_code == 302


@pytest.mark.django_db(transaction=True)
def test_a_revoked_or_tampered_link_opens_nothing(client, lab, bought):
    bought()
    surface_claims(lab)
    queue_claims_emails(lab)
    link = re.search(r"https://labstock\.test(/claims/t/[^\s]+/)", mail.outbox[0].body)[1]
    assert client.get(link.replace("/t/", "/t/x")).status_code == 400
    Notification.objects.get().rotate_nonce()
    assert client.get(link).status_code == 400
    assert client.get(reverse("bench:claims")).status_code == 302


def test_an_expired_scope_opens_nothing(client, lab, member):
    session = client.session
    session["claims_scope"] = {
        "person": member.pk,
        "lab": lab.pk,
        "until": (timezone.now() - timedelta(minutes=1)).isoformat(),
    }
    session.save()
    assert client.get(reverse("bench:claims")).status_code == 302


def test_champions_see_the_lab_pool_list(login, lab, bought, member, raj, make_member):
    bought(by=raj)
    surface_claims(lab)
    Membership.objects.filter(user=raj).update(status=Membership.Status.LEAVER)
    champion = make_member("champ@lab.test", is_champion=True)
    assert login(member).get(reverse("bench:champions")).status_code == 403
    response = login(champion).get(reverse("bench:champions"))
    assert_clean(response)
    assert "GAPDH" in response.content.decode()
    claim = HistoryClaim.objects.get()
    response = login(champion).post(
        reverse("bench:claim_decide", args=[claim.pk]), {"outcome": "shared_stock", "temp": "4C"}
    )
    assert response.status_code == 302
    assert StockItem.objects.get().custodian == lab.pool_user


# ---------------------------------------------------------------------------------------
# Add what I have, register a box


def add_data(**extra):
    return {
        "name": "HEK293T",
        "vendor": "ATCC",
        "catalog_number": "CRL-3216",
        "item_type": "cell_line",
        "source": "collaborator",
        "count": "2",
        "place": "",
        "temp": "LN2",
        "share": "",
        "mta": "on",
        "remarks": "passage 12",
        "client_uuid": str(uuid.uuid4()),
        **extra,
    }


def test_add_what_i_have(login, member, item_types):
    client = login(member)
    assert_clean(client.get(reverse("bench:add")))
    data = add_data()
    response = client.post(reverse("bench:add"), data)
    assert response.status_code == 302
    items = StockItem.objects.all()
    assert items.count() == 2 and {i.visibility for i in items} == {"restricted"}  # MTA
    assert {i.source for i in items} == {"collaborator"}
    client.post(reverse("bench:add"), data)  # sent twice
    assert StockItem.objects.count() == 2


def test_an_outside_purchase_is_recorded_as_bought(login, member, item_types):
    data = add_data(source="outside_purchase", mta="", name="Anti-CD3", catalog_number="300302")
    login(member).post(reverse("bench:add"), data)
    purchase = OrderRequest.objects.get()
    assert (purchase.source, purchase.status, purchase.requested_by) == (
        "outside",
        "RECEIVED",
        member,
    )


def test_the_typeahead_offers_what_the_lab_bought(login, bought, member):
    bought()
    response = login(member).get(reverse("bench:add_products"), {"q": "2118S"})
    assert response.status_code == 200
    assert "GAPDH (14C10) Rabbit mAb" in response.content.decode()
    assert 'name="product"' in response.content.decode()


def test_register_a_box_with_a_lid_photo(login, member, shelf):
    client = login(member)
    assert_clean(client.get(reverse("bench:box_new")))
    response = client.post(
        reverse("bench:box_new"),
        {
            "name": "Priya's antibodies",
            "parent": shelf.code,
            "kind": "box",
            "owner": str(member.pk),
            "share": "",
            "photo": SimpleUploadedFile("lid.png", PNG, content_type="image/png"),
        },
    )
    assert response.status_code == 302
    box = Location.objects.get(name="Priya's antibodies")
    assert box.default_custodian == member and box.photo
    bad = client.post(
        reverse("bench:box_new"),
        {
            "name": "Not a photo",
            "parent": shelf.code,
            "kind": "box",
            "owner": str(member.pk),
            "photo": SimpleUploadedFile("lid.png", b"MZ\x90\x00", content_type="image/png"),
        },
    )
    assert bad.status_code == 200 and "a photo LabStock can keep" in bad.content.decode()
    assert not Location.objects.filter(name="Not a photo").exists()
