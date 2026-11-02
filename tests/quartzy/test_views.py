"""The staff-only sync health panel."""

from datetime import timedelta

import pytest
from allauth.mfa.models import Authenticator
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from apps.catalog.models import Product
from apps.core.models import Lab, PersonAlias, User
from apps.quartzy import sync, views
from apps.quartzy.models import OrderRequest, QuartzyLab, SyncRun

pytestmark = pytest.mark.django_db

URL = "/quartzy/health/"


@pytest.fixture
def staff_client(client, staff, clear_cache):
    client.force_login(staff)
    return client


def test_the_url_is_mounted():
    assert reverse("quartzy:health") == URL


def test_the_panel_is_staff_only(client, member, clear_cache, no_quartzy):
    response = client.get(URL)
    assert response.status_code == 302 and reverse("account_login") in response["Location"]
    client.force_login(member)
    assert client.get(URL).status_code == 403


def test_a_warm_cache_means_no_quartzy_call(staff_client, quartzy_lab, no_quartzy):
    cache.set(views.ACCESS_CHECK_CACHE_KEY, True, 600)
    response = staff_client.get(URL)
    assert response.status_code == 200
    assert b"token OK" in response.content


def test_the_token_is_checked_once_per_ten_minutes(staff_client, fake, quartzy_lab, use_fake):
    assert b"token OK" in staff_client.get(URL).content
    assert fake.calls["/user"] == 1 and use_fake[0].calls == 1
    staff_client.get(URL)
    staff_client.get(URL)
    assert fake.calls["/user"] == 1  # cached


@pytest.mark.parametrize("failure", [401, 503, "timeout"])
def test_a_failed_check_shows_as_failed(staff_client, fake, quartzy_lab, use_fake, failure):
    fake.fail_next(failure, path="/user", times=5)
    content = staff_client.get(URL).content
    assert b"token failed" in content and b"token OK" not in content
    assert fake.calls["/user"] == 1  # one attempt, no retries


def test_no_successful_head_sync_shows_the_banner(staff_client, quartzy_lab, no_quartzy):
    cache.set(views.ACCESS_CHECK_CACHE_KEY, True, 600)
    assert b"No head sync has succeeded yet" in staff_client.get(URL).content

    long_ago = timezone.now() - timedelta(minutes=45)
    SyncRun.objects.create(
        kind="head", quartzy_lab=quartzy_lab, status="ok", started_at=long_ago, finished_at=long_ago
    )
    content = staff_client.get(URL).content.decode()
    assert "The last successful head sync was" in content and "more than 30 minutes" in content

    now = timezone.now()
    SyncRun.objects.create(
        kind="head", quartzy_lab=quartzy_lab, status="ok", started_at=now, finished_at=now
    )
    content = staff_client.get(URL).content.decode()
    assert "head sync was" not in content and "New requests are not reaching" not in content


def test_the_panel_counts(staff_client, quartzy_lab, no_quartzy):
    cache.set(views.ACCESS_CHECK_CACHE_KEY, False, 600)
    now = timezone.now()
    product = Product.objects.create(name="Provisional thing", provisional=True)
    stranger = PersonAlias.objects.create(kind="quartzy_user_id", value="u-1", display_name="X")
    rows = [
        ("ORDERED", "exact", product, stranger),
        ("CREATED", "no_catalog", None, None),
        ("RECEIVED", "generic", None, None),
        ("CREATED", "new", product, None),
    ]
    for n, (status, resolution, prod, alias) in enumerate(rows):
        OrderRequest.objects.create(
            lab=quartzy_lab.lab,
            quartzy_lab=quartzy_lab,
            quartzy_id=f"r{n}",
            status=status,
            resolution_status=resolution,
            product=prod,
            requested_by_alias=alias,
        )
    SyncRun.objects.create(kind="sweep", quartzy_lab=quartzy_lab, calls=7, started_at=now)
    SyncRun.objects.create(
        kind="sweep", quartzy_lab=quartzy_lab, calls=50, started_at=now - timedelta(hours=2)
    )
    response = staff_client.get(URL)
    panel = response.context["panels"][0]
    assert (panel["open"], panel["requests"]) == (3, 4)
    assert (panel["uncheckable"], panel["uncheckable_share"]) == (2, 50)
    assert panel["unresolved_people"] == 1 and panel["provisional_products"] == 1
    assert panel["calls_last_hour"] == 7
    content = response.content.decode()
    assert "token failed" in content and "50% (2)" in content


def test_the_panel_shows_what_the_last_catalog_re_resolution_changed(
    staff_client, quartzy_lab, no_quartzy
):
    cache.set(views.ACCESS_CHECK_CACHE_KEY, True, 600)
    content = staff_client.get(URL).content.decode()
    assert "Catalog re-resolution" in content  # listed before any has run
    now = timezone.now()
    earlier = now - timedelta(hours=1)
    for started, seen, changed in ((earlier, 200, 150), (now, 37, 12)):
        SyncRun.objects.create(
            kind="resolve",
            quartzy_lab=quartzy_lab,
            status="ok",
            started_at=started,
            finished_at=started,
            seen=seen,
            changed=changed,
        )
    response = staff_client.get(URL)
    [row] = [row for row in response.context["panels"][0]["kinds"] if row["kind"] == "resolve"]
    assert (row["label"], row["last"].changed) == ("Catalog re-resolution", 12)
    content = response.content.decode()
    assert "12 of 37 requests resolved differently" in content


def test_the_page_has_no_inline_script_or_style(staff_client, quartzy_lab, no_quartzy):
    cache.set(views.ACCESS_CHECK_CACHE_KEY, True, 600)
    response = staff_client.get(URL)
    content = response.content.decode()
    assert "<style" not in content and "style=" not in content
    assert content.count("<script") == 1  # only base.html's htmx, by nonce
    assert "script-src 'self' 'nonce-" in response["Content-Security-Policy"]


def test_the_mirror_admin_pages_render(client, fake, fake_lab, quartzy_lab, api, run):
    fake.add_inventory_item(fake_lab)
    sync.sync_inventory(api, quartzy_lab, run)
    sync.upsert_order_request(
        quartzy_lab, api.order_request(fake.add_request(fake_lab)["id"]), via="poll", run=run
    )
    sync.record_error(run, "parse", "id: blank", payload={"id": " "})
    run.save()
    admin = User.objects.create_superuser(email="root@lab.test", full_name="Root")
    Authenticator.objects.create(user=admin, type=Authenticator.Type.TOTP, data={})
    client.force_login(admin)
    for model in (
        "quartzylab",
        "quartzytype",
        "orderrequest",
        "syncrun",
        "syncerror",
        "quartzyinventoryitem",
        "specsnapshot",
    ):
        assert client.get(f"/admin/quartzy/{model}/").status_code == 200, model
    request = OrderRequest.objects.get()
    page = client.get(f"/admin/quartzy/orderrequest/{request.pk}/change/")
    assert page.status_code == 200 and b"Payload as Quartzy sent it" in page.content
    item = quartzy_lab.inventory_items.get()
    assert client.get(f"/admin/quartzy/quartzyinventoryitem/{item.pk}/change/").status_code == 200
    assert client.get(f"/admin/quartzy/syncrun/{run.pk}/change/").status_code == 200


def test_remapping_a_quartzy_lab_in_the_admin_moves_its_requests(
    admin_user_client, fake, fake_lab, quartzy_lab, api, run
):
    for _ in range(3):
        sync.upsert_order_request(
            quartzy_lab, api.order_request(fake.add_request(fake_lab)["id"]), via="poll", run=run
        )
    jones = Lab.objects.create(name="Jones Lab", slug="jones")
    blank = {
        f"{name}_{part}": ""
        for name in ("alerts_armed_at", "cutover_at", "backoff_until")
        for part in (0, 1)
    }
    response = admin_user_client.post(
        f"/admin/quartzy/quartzylab/{quartzy_lab.pk}/change/",
        {"lab": jones.pk, "is_active": "on", **blank, "_save": "Save"},
    )
    assert response.status_code == 302, response.content[:2000]
    assert set(OrderRequest.objects.values_list("lab", flat=True)) == {jones.pk}
    assert OrderRequest.objects.for_lab(jones).count() == 3
    assert not OrderRequest.objects.for_lab(quartzy_lab.lab).exists()


def test_the_panel_lists_the_quartzy_labs_it_does_not_mirror(
    staff_client, lab, quartzy_lab, no_quartzy
):
    cache.set(views.ACCESS_CHECK_CACHE_KEY, True, 600)
    QuartzyLab.objects.create(
        lab=lab, quartzy_id="jones-q", name="Jones Lab (collaborator)", is_active=False
    )
    response = staff_client.get(URL)
    content = response.content.decode()
    assert "Not mirrored" in content and "Jones Lab (collaborator)" in content
    assert "jones-q" in content and "quartzy_purge_lab" in content
    assert [panel["qlab"] for panel in response.context["panels"]] == [quartzy_lab]
