"""import_stock (design §3): lab registers and personal sheets into the ledger."""

import csv
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import CommandError, call_command

from apps.catalog.models import Product, SupplierProduct
from apps.core.models import Membership, User
from apps.inventory.importers import StockImportFailed, import_stock
from apps.inventory.models import Location, StockEvent, StockItem
from apps.quartzy.models import OrderRequest
from tests.catalog.conftest import item_types  # noqa: F401  (fixture)

pytestmark = pytest.mark.django_db
TEMPLATE = Path(__file__).resolve().parents[2] / "docs" / "templates" / "stock-sheet.csv"
HEADER = (
    "owner_email,item_name,vendor,catalog_number,lot,type,containers,size,location,"
    "temperature,position,share,source,date,expiry,opened,mta,notes\n"
)


@pytest.fixture
def owners(lab, make_member):
    return {
        "priya": make_member("priya@example.edu", full_name="Priya Patel"),
        "raj": make_member("raj@example.edu", full_name="Raj Rao"),
    }


@pytest.fixture
def sheet(tmp_path):
    def _sheet(*rows):
        path = tmp_path / "stock.csv"
        path.write_text(HEADER + "".join(row + "\n" for row in rows), encoding="utf-8")
        return path

    return _sheet


def counts():
    return (
        StockItem.objects.count(),
        Product.objects.count(),
        SupplierProduct.objects.count(),
        Location.objects.count(),
    )


def test_the_committed_template_imports_cleanly(lab, owners, item_types, location_types):  # noqa: F811
    report = import_stock(TEMPLATE, lab=lab, commit=True, create_locations=True)
    assert [row.action for row in report.rows] == ["create", "create", "create"]
    assert report.items == 5  # 1 antibody, 3 HEK293T vials, 1 polymerase
    antibody = StockItem.objects.get(product__name="GAPDH (14C10) Rabbit mAb")
    assert antibody.location.path_text() == "Room 210 / -20 B / Shelf 2 / Box 4"
    assert (antibody.position_label, antibody.lifecycle) == ("A1", "unverified")
    assert antibody.lot.lot_raw == "0012" and antibody.opened_at is not None
    hek = StockItem.objects.filter(product__name="HEK293T")
    assert hek.count() == 3 and {item.visibility for item in hek} == {"restricted"}  # MTA
    assert {item.location.is_unsorted for item in hek} == {True}
    q5 = StockItem.objects.get(product__name="Q5 High-Fidelity DNA Polymerase")
    assert (q5.visibility, str(q5.expiry_date), q5.expiry_precision) == (
        "shared",
        "2026-11-01",
        "month",
    )
    event = StockEvent.objects.filter(stock_item=antibody).get()
    assert (event.source, event.reason) == ("import", "stock-sheet.csv line 2")


def test_a_dry_run_saves_nothing(lab, owners, item_types, location_types):  # noqa: F811
    before = counts()
    report = import_stock(TEMPLATE, lab=lab, create_locations=True)
    assert report.items == 5 and not report.committed
    assert counts() == before


def test_reimporting_the_same_file_creates_nothing(lab, owners, item_types, sheet):  # noqa: F811
    path = sheet("priya@example.edu,EcoRI,NEB,R0101S,,enzyme,2,,,-20C,,,,,,,,")
    first = import_stock(path, lab=lab, commit=True)
    again = import_stock(path, lab=lab, commit=True)
    assert [row.action for row in first.rows] == ["create"]
    assert [row.action for row in again.rows] == ["exists"]
    assert StockItem.objects.count() == 2 and first.rows[0].short_ids == again.rows[0].short_ids


def test_many_containers_become_one_aggregate(lab, owners, item_types, sheet):  # noqa: F811
    path = sheet("raj@example.edu,Pipette tips,Corning,4488,,consumable,30,,,RT,,shared,,,,,,")
    (row,) = import_stock(path, lab=lab, commit=True).rows
    assert (row.items, row.aggregate) == (1, True)
    item = StockItem.objects.get()
    assert (item.is_aggregate, item.container_count) == (True, 30)


def test_errors_name_their_line_and_nothing_is_saved(lab, owners, item_types, sheet, box):  # noqa: F811
    path = sheet(
        "priya@example.edu,Good row,CST,2118S,,antibody,1,,,-20C,,,,,,,,",
        f"priya@example.edu,Bad box,CST,2118S,,antibody,1,,{box.path_text()},,J1,,,,,,,",
        "nobody@example.edu,Who,CST,2118S,,antibody,1,,,-20C,,,,,,,,",
        "priya@example.edu,Odd,CST,2118S,,potion,x,,,hot,,maybe,stolen,2025-13-01,soon,perhaps,,",
    )
    before = counts()
    with pytest.raises(StockImportFailed) as failed:
        import_stock(path, lab=lab, commit=True)
    errors = failed.value.errors
    assert any(e.startswith("line 3:") and "J1" in e for e in errors)
    assert any(e.startswith("line 4:") and "--placeholders" in e for e in errors)
    line5 = [e for e in errors if e.startswith("line 5:")]
    assert len(line5) >= 7  # type, containers, temperature, share, source, date, expiry, opened
    assert counts() == before
    assert [row.action for row in failed.value.report.rows] == ["create", "error", "error", "error"]


def test_former_members_become_placeholders(lab, owners, item_types, sheet):  # noqa: F811
    path = sheet("lee.chen@example.edu,Old antibody,Abcam,ab8245,,antibody,1,,,-80C,,,,,,,,")
    report = import_stock(path, lab=lab, commit=True, placeholders=True)
    assert report.placeholders == ["lee.chen@example.edu"]
    lee = User.objects.get(email="lee.chen@example.edu")
    assert (lee.kind, lee.is_active, lee.full_name) == ("placeholder", False, "Lee Chen")
    assert Membership.objects.get(user=lee, lab=lab).status == "leaver"
    assert StockItem.objects.get().custodian == lee


def test_an_outside_purchase_is_recorded_for_the_guard(lab, owners, item_types, sheet):  # noqa: F811
    path = sheet(
        "raj@example.edu,Anti-CD3,BioLegend,300302,,antibody,1,,,4C,,,outside_purchase,"
        "2026-05-01,,,,"
    )
    import_stock(path, lab=lab, commit=True)
    import_stock(path, lab=lab, commit=True)  # again: no second purchase
    purchase = OrderRequest.objects.get()
    assert (purchase.source, purchase.status, purchase.requested_by.email) == (
        "outside",
        "RECEIVED",
        "raj@example.edu",
    )
    assert StockItem.objects.get().source_order_request == purchase


def test_verified_and_opened(lab, owners, item_types, sheet):  # noqa: F811
    path = sheet(
        "priya@example.edu,Opened kit,Qiagen,74104,,kit,1,,,RT,,,,2026-01-10,,yes,,",
        "priya@example.edu,Sealed kit,Qiagen,74106,,kit,1,250 preps,,RT,,,,,,no,,",
    )
    import_stock(path, lab=lab, commit=True, verified=True)
    opened = StockItem.objects.get(product__name="Opened kit")
    sealed = StockItem.objects.get(product__name="Sealed kit")
    assert opened.lifecycle == "opened" and opened.opened_at.date().isoformat() == "2026-01-10"
    assert (sealed.lifecycle, sealed.quantity, sealed.unit) == ("available", 250, "preps")


def test_the_command_writes_a_review_csv(lab, owners, item_types, sheet):  # noqa: F811
    path = sheet("priya@example.edu,EcoRI,NEB,R0101S,,enzyme,1,,,-20C,,,,,,,,")
    out = StringIO()
    call_command("import_stock", str(path), lab="test-lab", stdout=out)
    assert "Dry run: 1 item(s) from 1 row(s)" in out.getvalue()
    review = path.with_suffix(".review.csv")
    rows = list(csv.DictReader(review.open(encoding="utf-8")))
    assert (rows[0]["action"], rows[0]["resolution"], rows[0]["items"]) == ("create", "new", "1")
    assert StockItem.objects.count() == 0
    bad = path.parent / "bad.csv"
    bad.write_text(HEADER + "ghost@example.edu,X,,,,,,,,,,,,,,,,\n", encoding="utf-8")
    with pytest.raises(CommandError, match="nothing saved"):
        call_command("import_stock", str(bad), lab="test-lab", stdout=StringIO(), stderr=out)
    assert bad.with_suffix(".review.csv").exists()
