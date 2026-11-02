import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import httpx
import pytest
import yaml
from openapi_core.exceptions import OpenAPIError
from pydantic import ValidationError

from apps.quartzy.client import (
    QuartzyAuthError,
    QuartzyClient,
    QuartzyError,
    QuartzyWriteBlocked,
    ReadOnlyTransport,
)
from apps.quartzy.schemas import (
    InventoryInstance,
    InventoryItem,
    Lab,
    Location,
    Money,
    OrderRequest,
    Organization,
    QType,
    SpendTrackingCode,
    TypeWithLab,
    User,
    unknown_fields,
)
from tests.quartzy.fake_quartzy import SPEC_PATH, FakeQuartzy, check_against_spec


@pytest.fixture
def fake():
    return FakeQuartzy()


@pytest.fixture
def qlab(fake):
    return fake.add_lab("Smith Lab")


def ids(records):
    return [record["id"] for record in records]


# The read-only guarantee ------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_writes_are_refused_before_anything_is_sent(fake, method):
    client = fake.client()
    with pytest.raises(QuartzyWriteBlocked):
        client._http.request(method, "/order-requests/abc", json={"status": "CANCELLED"})
    with httpx.Client(transport=ReadOnlyTransport(fake.transport())) as http:
        with pytest.raises(QuartzyWriteBlocked):
            http.request(method, "https://api.quartzy.com/order-requests")
        assert fake.log == [] and client.calls == 0
        assert http.get("https://api.quartzy.com/healthz").status_code == 200


# Retries ---------------------------------------------------------------------------------------


def test_retries_server_errors_with_backoff(fake, qlab):
    fake.add_request(qlab)
    fake.fail_next(503, times=2)
    client = fake.client()
    assert len(client.order_requests(page=1)) == 1
    assert client.calls == 3 == fake.total_calls
    first, second = fake.slept
    assert 1 <= first <= 2 and 2 <= second <= 3  # exponential, plus up to 1 s of jitter


def test_retry_after_is_honoured(fake):
    fake.fail_next(429, retry_after=7)
    client = fake.client()
    assert client.labs() == []
    assert fake.slept == [7.0] and client.calls == 2


def test_a_long_retry_after_ends_the_attempt_without_waiting(fake):
    fake.fail_next(429, retry_after=3600)
    client = fake.client()
    with pytest.raises(QuartzyError) as caught:
        client.labs()
    assert (caught.value.status, caught.value.retry_after) == (429, 3600)
    assert client.calls == 1 and fake.slept == []


def test_gives_up_after_max_attempts(fake):
    fake.fail_next(502, times=10)
    client = fake.client(max_attempts=3)
    with pytest.raises(QuartzyError) as caught:
        client.order_requests()
    assert caught.value.status == 502
    assert client.calls == 3 and len(fake.slept) == 2


def test_timeouts_are_retried_then_reported(fake):
    fake.fail_next("timeout")
    client = fake.client()
    assert client.labs() == [] and client.calls == 2
    fake.fail_next("timeout", times=10)
    with pytest.raises(QuartzyError) as caught:
        client.labs()
    assert caught.value.status is None
    assert isinstance(caught.value.__cause__, httpx.TimeoutException)


def test_client_errors_are_not_retried(fake):
    fake.fail_next(400)
    client = fake.client()
    with pytest.raises(QuartzyError) as caught:
        client.labs()
    assert caught.value.status == 400 and client.calls == 1 and fake.slept == []


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors_raise_at_once(fake, status):
    fake.fail_next(status)
    client = fake.client()
    with pytest.raises(QuartzyAuthError) as caught:
        client.me()
    assert caught.value.status == status
    assert client.calls == 1 and fake.slept == []


def test_a_wrong_token_gets_401_without_retrying(fake):
    client = fake.client(token="not-the-token")
    with pytest.raises(QuartzyAuthError):
        client.order_requests()
    assert fake.total_calls == 1


# Reads -----------------------------------------------------------------------------------------


def test_reads_return_the_json_as_sent(fake, qlab):
    request = fake.add_request(qlab)
    item = fake.add_inventory_item(qlab)
    client = fake.client()
    assert client.order_request(request["id"]) == request
    assert client.inventory_item(item["id"]) == item
    assert client.lab(qlab["id"]) == qlab
    assert client.me() == fake.me
    assert client.health() == {}


def test_missing_ids_return_none_except_for_labs(fake, qlab):
    client = fake.client()
    assert client.order_request("1d7b0a52-0000-4000-8000-000000000000") is None
    assert client.inventory_item("1d7b0a52-0000-4000-8000-000000000000") is None
    with pytest.raises(QuartzyError) as caught:
        client.lab("1d7b0a52-0000-4000-8000-000000000000")
    assert caught.value.status == 404


def test_ids_stay_inside_one_path_segment(fake):
    client = fake.client()
    assert client.order_request("../labs") is None
    assert fake.log[-1][1] == "/order-requests/..%2Flabs"
    for bad in ("", " ", "..", None):
        with pytest.raises(ValueError):
            client.inventory_item(bad)


def test_query_params_and_call_counting(fake, qlab):
    client = fake.client()
    client.order_requests()
    client.order_requests(lab_id=qlab["id"], page=0)
    client.types(lab_id=qlab["id"], name="Antibodies")
    client.labs(page=2, organization_id="org-1")
    client.inventory_items(page=1)
    assert [params for _, _, params in fake.log] == [
        {},
        {"lab_id": qlab["id"], "page": "0"},
        {"lab_id": qlab["id"], "name": "Antibodies"},
        {"page": "2", "organization_id": "org-1"},
        {"page": "1"},
    ]
    assert client.calls == 5 == fake.total_calls
    assert fake.calls["/order-requests"] == 2


def test_min_interval_spaces_calls_out(fake):
    client = fake.client(min_interval=0.5)
    client.labs()
    assert fake.slept == []
    client.labs()
    [wait] = fake.slept
    assert 0.4 < wait <= 0.5


def test_unexpected_bodies_raise():
    def handler(request):
        if request.url.path == "/labs":
            return httpx.Response(200, json={"data": [], "next": None})
        return httpx.Response(200, text="<html>maintenance</html>")

    client = QuartzyClient("t0ken", transport=httpx.MockTransport(handler))
    with pytest.raises(QuartzyError, match="expected a JSON array"):
        client.labs()
    with pytest.raises(QuartzyError, match="not JSON") as caught:
        client.me()
    assert "maintenance" in caught.value.body


def test_rate_limit_headers_are_kept_for_the_probe():
    def handler(request):
        return httpx.Response(200, json=[], headers={"X-RateLimit-Remaining": "99", "X-Other": "1"})

    client = QuartzyClient("t0ken", transport=httpx.MockTransport(handler))
    client.labs()
    assert client.rate_limit_headers == {"x-ratelimit-remaining": "99"}


def test_close_and_context_manager(fake):
    with fake.client() as client:
        client.labs()
    assert client._http.is_closed


# The token stays secret ------------------------------------------------------------------------


def test_token_is_sent_but_never_shown(caplog):
    token = "tok-0123456789abcdef"
    fake = FakeQuartzy(token)
    client = fake.client(token=f"  {token}\n")  # stray whitespace from a token file is fine
    assert client.me()["id"] == fake.me["id"]
    assert token not in repr(client) and token not in str(client)
    fake.fail_next(500, times=10, body={"message": f"token {token} is not valid here"})
    with caplog.at_level(logging.WARNING, logger="apps.quartzy.client"):
        with pytest.raises(QuartzyError) as caught:
            client.labs()
    assert "retrying" in caplog.text and token not in caplog.text
    assert token not in str(caught.value) and token not in caught.value.body
    assert "[redacted]" in caught.value.body


@pytest.mark.parametrize("bad", ["", "   ", "abc\ndef", "café-token"])
def test_unusable_tokens_are_refused_without_being_echoed(bad):
    with pytest.raises(ValueError) as caught:
        QuartzyClient(bad)
    assert not bad.strip() or bad.strip() not in str(caught.value)


# Schemas ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "schema"),
    [
        (User, "User"),
        (Organization, "Organization"),
        (Lab, "Lab"),
        (QType, "Type"),
        (Money, "Money"),
        (SpendTrackingCode, "SpendTrackingCode"),
        (Location, "Location"),
        (InventoryInstance, "InventorySubitem"),
        (InventoryItem, "InventoryItem"),
        (OrderRequest, "OrderRequest"),
    ],
)
def test_models_cover_every_field_in_the_spec(model, schema):
    spec = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    assert set(spec["components"]["schemas"][schema]["properties"]) <= set(model.model_fields)
    assert {"id", "name", "lab"} <= set(TypeWithLab.model_fields)


def test_schemas_parse_what_the_fake_serves(fake, qlab):
    fake.add_type(qlab, "Antibodies")
    request = fake.add_request(qlab, qtype="Antibodies", requested_at="2026-03-01T10:00:00Z")
    item = fake.add_inventory_item(qlab)
    client = fake.client()
    parsed = OrderRequest.model_validate(client.order_request(request["id"]))
    assert parsed.requested_at == datetime(2026, 3, 1, 10, tzinfo=UTC)
    assert parsed.type.name == "Antibodies" and parsed.lab.organization.name == "Example University"
    assert parsed.total_price.amount == "1000" and parsed.requested_by.id == fake.me["id"]
    assert unknown_fields(parsed) == set()
    stock = InventoryItem.model_validate(client.inventory_item(item["id"]))
    assert stock.instances[0].location.pathNames == ["Room 101", "Freezer -80 A"]
    assert unknown_fields(stock) == set()
    assert [TypeWithLab.model_validate(t).lab.id for t in client.types()] == [qlab["id"]]
    assert Lab.model_validate(client.lab(qlab["id"])).uses_instances is True
    assert User.model_validate(client.me()).email == fake.me["email"]


def test_schemas_bend_rather_than_break():
    parsed = OrderRequest.model_validate(
        {
            "id": 7,
            "status": "SOMETHING_NEW",
            "quantity": 2,
            "unit_price": 12.5,
            "requested_at": "2026-03-01T10:00:00",
            "updated_at": "",
            "requested_by": None,
            "type": {"name": "Antibodies"},
            "lab": {"id": "lab-1", "uses_instances": "true"},
            "total_price": {"amount": 1250, "currency": "USD", "precision": 2},
            "brand_new": {"x": 1},
        }
    )
    assert (parsed.id, parsed.quantity, parsed.unit_price) == ("7", "2", "12.5")
    assert parsed.requested_at == datetime(2026, 3, 1, 10, tzinfo=UTC)  # naive taken as UTC
    assert parsed.updated_at is None and parsed.requested_by is None and parsed.type.id is None
    assert parsed.total_price.amount == "1250" and parsed.lab.uses_instances is True
    assert unknown_fields(parsed) == {"brand_new", "total_price.precision"}
    item = InventoryItem.model_validate(
        {"id": "i-1", "instances": [{"id": "x", "barcode": "123"}], "added_by": {"id": "u", "x": 1}}
    )
    assert unknown_fields(item) == {"instances.barcode", "added_by.x"}
    with pytest.raises(ValidationError):
        OrderRequest.model_validate({"status": "CREATED"})  # no id: can't be stored


# The fake itself -------------------------------------------------------------------------------


@pytest.mark.parametrize("base", [0, 1])
def test_fake_pages_from_its_base(base):
    fake = FakeQuartzy(page_size=2, page_base=base)
    made = [fake.add_request()["id"] for _ in range(5)]
    client = fake.client()
    pages = [ids(client.order_requests(page=base + n)) for n in range(4)]
    assert pages == [[made[4], made[3]], [made[2], made[1]], [made[0]], []]
    assert ids(client.order_requests()) == pages[0]  # an omitted page means the base page
    assert client.order_requests(page=base - 1) == []


def test_fake_page_options(fake):
    made = [fake.add_request()["id"] for _ in range(30)]
    client = fake.client()
    fake.page_below_base = "clamp"
    assert ids(client.order_requests(page=0)) == ids(client.order_requests(page=1))
    fake.page_below_base = "error"
    with pytest.raises(QuartzyError) as caught:
        client.order_requests(page=0)
    assert caught.value.status == 400
    fake.omitted_page = 2
    assert ids(client.order_requests()) == made[4::-1]
    fake.order = "oldest_first"
    assert ids(client.order_requests(page=1)) == made[:25]


def test_fake_past_end_options(fake):
    fake.page_size = 2
    made = [fake.add_request()["id"] for _ in range(3)]
    client = fake.client()
    assert client.order_requests(page=3) == []  # "empty", the default
    fake.past_end = "error"
    with pytest.raises(QuartzyError) as caught:
        client.order_requests(page=3)
    assert caught.value.status == 404 and ids(client.order_requests(page=2)) == [made[0]]
    fake.past_end = "clamp"
    assert ids(client.order_requests(page=3)) == ids(client.order_requests(page=9)) == [made[0]]
    assert client.inventory_items(page=1) == client.inventory_items(page=2) == []  # none at all
    with pytest.raises(ValueError, match="past_end"):
        FakeQuartzy(past_end="wrap")


def test_fake_unstable_order_reshuffles_every_read(fake):
    made = {fake.add_request()["id"] for _ in range(10)}
    fake.order = "unstable"
    client = fake.client()
    first, second = ids(client.order_requests()), ids(client.order_requests())
    assert first != second and set(first) == set(second) == made


def test_fake_updated_at_sort_moves_edited_requests():
    fake = FakeQuartzy(sort_key="updated_at")
    old, new = fake.add_request(), fake.add_request()
    client = fake.client()
    assert ids(client.order_requests()) == [new["id"], old["id"]]
    edited = fake.update_request(old["id"], status="ORDERED")
    assert edited["updated_at"] > edited["requested_at"]
    assert ids(client.order_requests()) == [old["id"], new["id"]]
    fake.update_request(new["id"], bump_updated_at=False, status="CANCELLED")
    assert ids(client.order_requests()) == [old["id"], new["id"]]


def test_fake_insert_between_pages_shifts_later_pages():
    fake = FakeQuartzy(page_size=2)
    made = [fake.add_request()["id"] for _ in range(4)]
    inserted = []
    fake.after_page(lambda: inserted.append(fake.add_request()["id"]), page=1)
    client = fake.client()
    assert ids(client.order_requests(page=1)) == [made[3], made[2]]
    assert ids(client.order_requests(page=2)) == [made[2], made[1]]  # pushed down by the insert
    assert ids(client.order_requests(page=1)) == [inserted[0], made[3]]
    assert len(inserted) == 1  # times=1


def test_fake_deletes_edits_and_money(fake, qlab):
    request = fake.add_request(qlab, unit_price="12.50", quantity="2")
    assert request["total_price"] == {"amount": "2500", "currency": "USD"}
    fake.update_request(request["id"], quantity="3")
    assert request["total_price"]["amount"] == "3750"
    fake.money_unit = "major"
    assert fake.add_request(qlab, unit_price="12.50", quantity="2")["total_price"]["amount"] == (
        "25.00"
    )
    client = fake.client()
    fake.delete_request(request["id"])
    assert client.order_request(request["id"]) is None
    assert request["id"] not in ids(client.order_requests())


def test_fake_null_toggles_leave_stored_data_alone():
    fake = FakeQuartzy(null_updated_at=True, null_requested_by=True, null_emails=True)
    request = fake.add_request()
    client = fake.client()
    [row] = client.order_requests()
    assert row["updated_at"] is None and row["requested_by"] is None
    assert row["created_by"]["email"] is None and client.me()["email"] is None
    assert fake.order_requests[request["id"]]["updated_at"] == request["requested_at"]


def test_fake_filters_by_lab_type_name_and_organization(fake):
    first, second = fake.add_lab("First"), fake.add_lab("Second", organization=None)
    fake.add_type(first, "Antibodies")
    fake.add_type(second, "Antibodies")
    fake.add_type(second, "Kits")
    mine = fake.add_request(second, qtype="Kits")
    fake.add_request(first)
    client = fake.client()
    assert ids(client.order_requests(lab_id=second["id"])) == [mine["id"]]
    assert mine["type"]["name"] == "Kits"
    assert len(client.types(lab_id=second["id"])) == 2
    assert len(client.types(name="antibodies")) == 2
    assert ids(client.labs(organization_id=fake.organization["id"])) == [first["id"]]
    assert "organization" not in client.lab(second["id"])


def test_fake_checks_the_token_except_for_healthz(fake):
    with httpx.Client(transport=fake.transport(), base_url="https://api.quartzy.test") as http:
        assert http.get("/order-requests").status_code == 401
        assert http.get("/healthz").status_code == 200
        ok = http.get("/order-requests", headers={"Access-Token": fake.token})
        assert ok.status_code == 200 and ok.json() == []
        assert http.get("/nowhere", headers={"Access-Token": fake.token}).status_code == 404
    assert fake.calls == {"/order-requests": 2, "/healthz": 1, "/nowhere": 1}
    fake.healthy = False
    with pytest.raises(QuartzyError) as caught:
        fake.client(max_attempts=1).health()
    assert caught.value.status == 500


def test_fake_serves_concurrent_clients(fake):
    made = sorted(fake.add_request()["id"] for _ in range(60))

    def read_everything(_):
        client = fake.client()
        return sorted(r["id"] for page in (1, 2, 3) for r in client.order_requests(page=page))

    with ThreadPoolExecutor(4) as pool:
        assert list(pool.map(read_everything, range(8))) == [made] * 8
    assert fake.total_calls == 24


def test_fake_responses_match_the_spec(fake, qlab):
    fake.add_type(qlab, "Antibodies")
    request = fake.add_request(qlab, spend_tracking_code={"name": "R01"}, is_urgent=None)
    item = fake.add_inventory_item(qlab, added_by=None, location={"name": "Shelf"})
    fake.add_lab("Legacy", uses_instances=None)
    client = fake.client()
    served = {
        "/healthz": client.health(),
        "/user": client.me(),
        "/labs": client.labs(),
        "/labs/{id}": client.lab(qlab["id"]),
        "/types": client.types(),
        "/order-requests": client.order_requests(),
        "/order-requests/{id}": client.order_request(request["id"]),
        "/inventory-items": client.inventory_items(),
        "/inventory-items/{id}": client.inventory_item(item["id"]),
    }
    for route, body in served.items():
        check_against_spec(route, body)
    fake.null_updated_at = fake.null_requested_by = fake.null_emails = True
    check_against_spec("/order-requests", client.order_requests())
    with pytest.raises(OpenAPIError):
        check_against_spec("/order-requests", [{"id": request["id"]}])  # required fields missing
    drifted = [{**item, "barcode": "123"}]
    with pytest.raises(OpenAPIError):
        check_against_spec("/inventory-items", drifted)
    check_against_spec("/inventory-items", drifted, allow_unknown_fields=True)
