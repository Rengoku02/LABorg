"""Real Quartzy payloads (plan §13): the anonymized sample that ``python -m scripts.quartzy_probe
fixtures`` writes to tests/fixtures/quartzy/ from a real export.

Each file is checked against the spec with its new fields allowed (and reported), its requests
are mirrored with no SyncError, and they load into the baseline. These tests are skipped while
the folder is empty; the last test runs the same checks on a sample made from FakeQuartzy, so
they keep working until real fixtures arrive.
"""

import json
import warnings
from pathlib import Path

import pytest

from apps.guard import baseline
from apps.quartzy import schemas, sync
from apps.quartzy.models import OrderRequest, QuartzyLab, SyncError, SyncRun
from scripts import quartzy_probe as probe
from tests.quartzy.fake_quartzy import FakeQuartzy, check_against_spec

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "quartzy"
# What a fixture file holds, by the start of its name: the route it was read from, and the
# schema its records parse with.
KINDS = {
    "order_requests": ("/order-requests", schemas.OrderRequest),
    "inventory_items": ("/inventory-items", schemas.InventoryItem),
    "labs": ("/labs", schemas.Lab),
    "types": ("/types", schemas.TypeWithLab),
}


def kind(path):
    return next((name for name in KINDS if path.stem.startswith(name)), None)


def fixture_files(*kinds):
    wanted = kinds or tuple(KINDS)
    found = [path for path in sorted(FIXTURES.glob("*.json")) if kind(path) in wanted]
    if found:
        return [pytest.param(path, id=path.name) for path in found]
    reason = f"no real fixtures in {FIXTURES} yet (python -m scripts.quartzy_probe fixtures)"
    return [pytest.param(None, marks=pytest.mark.skip(reason=reason))]


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def check_spec(path):
    """Raise unless the spec allows every record (new fields aside); warn about new fields."""
    route, schema = KINDS[kind(path)]
    records = read(path)
    check_against_spec(route, records, allow_unknown_fields=True)
    found = set()
    for record in records:
        found |= schemas.unknown_fields(schema.model_validate(record))
    if found:
        warnings.warn(
            f"{path.name}: fields the spec doesn't document: {', '.join(sorted(found))}",
            stacklevel=2,
        )
    return found


def check_mirror(path, lab):
    records = read(path)
    lab_ids = {(record.get("lab") or {}).get("id") for record in records} - {None}
    qlab = QuartzyLab.objects.create(lab=lab, quartzy_id=min(lab_ids, default="fixtures"))
    run = SyncRun.objects.create(kind=SyncRun.Kind.BACKFILL, quartzy_lab=qlab)
    for payload in records:
        sync.upsert_order_request(qlab, payload, via="backfill", run=run)
    errors = SyncError.objects.values_list("kind", "quartzy_id", "message")
    assert list(errors) == []
    assert OrderRequest.objects.count() == len({record["id"] for record in records})


def check_baseline(path, folder):
    """Load the requests the way the baseline notebook reads an export."""
    export = folder / "export"
    export.mkdir()
    lines = [json.dumps(record, ensure_ascii=False) for record in read(path)]
    (export / "order_requests.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for name in ("labs.json", "types.json", "manifest.json"):
        if (path.parent / name).exists():
            (export / name).write_text((path.parent / name).read_text(encoding="utf-8"))
    records = baseline.load_requests(export)
    assert len(records) == len({record["id"] for record in read(path)})
    baseline.find_clusters(records)


@pytest.mark.parametrize("path", fixture_files())
def test_a_real_fixture_is_what_the_spec_allows(path):
    check_spec(path)


@pytest.mark.django_db
@pytest.mark.parametrize("path", fixture_files("order_requests"))
def test_real_requests_mirror_without_a_sync_error(path, lab, item_types):
    check_mirror(path, lab)


@pytest.mark.parametrize("path", fixture_files("order_requests"))
def test_real_requests_load_into_the_baseline(path, tmp_path):
    check_baseline(path, tmp_path)


@pytest.mark.django_db
def test_the_checks_run_on_a_sample_the_probe_makes(tmp_path, lab, item_types, monkeypatch):
    token = "tok-fixture-check"
    monkeypatch.setenv("QUARTZY_ACCESS_TOKEN", token)
    fake = FakeQuartzy(token, page_size=10)
    fake_lab = fake.add_lab("Smith Lab")
    fake.add_type(fake_lab, "Antibodies")
    for n in range(12):
        fake.add_request(fake_lab, item_name=f"Antibody {n}", qbot_source="chat")
    fake.add_inventory_item(fake_lab)
    data, out = tmp_path / "data", tmp_path / "fixtures"
    argv = ["export", "--data-dir", str(data), "--min-interval", "0", "--page-base", "1"]
    assert probe.main(argv, transport=fake.transport(), sleep=fake.slept.append) == 0
    [export] = data.glob("export-*")
    assert probe.main(["fixtures", str(export), "--out", str(out)]) == 0
    files = {kind(path): path for path in out.glob("*.json") if kind(path)}
    assert files.keys() == KINDS.keys()
    with pytest.warns(UserWarning, match="qbot_source"):
        check_spec(files["order_requests"])
    for name in ("inventory_items", "labs", "types"):
        assert check_spec(files[name]) == set()
    check_mirror(files["order_requests"], lab)
    check_baseline(files["order_requests"], tmp_path)
