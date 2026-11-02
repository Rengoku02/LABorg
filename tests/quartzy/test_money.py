from decimal import Decimal

import pytest

from apps.quartzy.money import infer_money_unit, money_amount, parse_quantity, to_decimal
from apps.quartzy.schemas import Money
from tests.quartzy.fake_quartzy import FakeQuartzy


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12.50", Decimal("12.50")),
        ("1,234.00", Decimal("1234.00")),
        ("$5", Decimal(5)),
        (" 7 ", Decimal(7)),
        ("-3.5", Decimal("-3.5")),
        ("USD 12.50", Decimal("12.50")),
        ("12.50 USD", Decimal("12.50")),
        ("€9.99", Decimal("9.99")),
        (".5", Decimal("0.5")),
        (12, Decimal(12)),
        (2.5, Decimal("2.5")),
        (Decimal("1.10"), Decimal("1.10")),
        ("", None),
        ("   ", None),
        (None, None),
        ("abc", None),
        ("12,50", None),  # a decimal comma is ambiguous: refuse rather than read 1250
        ("1,2345", None),
        ("12.5.1", None),
        ("$", None),
        ("NaN", None),
        ("1e3", None),
        (float("nan"), None),
        (Decimal("Infinity"), None),
        (True, None),
        ({"amount": "1"}, None),
    ],
)
def test_to_decimal(raw, expected):
    assert to_decimal(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2", Decimal(2)),
        ("1.5", Decimal("1.5")),
        ("1,000", Decimal(1000)),
        (" 3 ", Decimal(3)),
        ("0", Decimal(0)),
        (4, Decimal(4)),
        ("", None),
        (None, None),
        ("-1", None),
        (-2, None),
        ("2 boxes", None),
        ("$2", None),
    ],
)
def test_parse_quantity(raw, expected):
    assert parse_quantity(raw) == expected


def test_explicit_units():
    assert money_amount({"amount": "1250", "currency": "usd"}, "minor") == (
        Decimal("12.50"),
        "USD",
        "minor",
    )
    assert money_amount({"amount": "1250", "currency": "USD"}, "major") == (
        Decimal(1250),
        "USD",
        "major",
    )
    assert money_amount({"amount": "1500", "currency": "JPY"}, "minor")[0] == Decimal(1500)
    assert money_amount({"amount": "1500", "currency": "KWD"}, "minor")[0] == Decimal("1.500")
    assert money_amount({"amount": "", "currency": "USD"}, "minor") == (None, "USD", "unknown")
    with pytest.raises(ValueError):
        money_amount({"amount": "1"}, "cents")


USD = "USD"


@pytest.mark.parametrize(
    ("amount", "unit_price", "quantity", "expected"),
    [
        ("2500", "12.50", "2", (Decimal("25.00"), USD, "minor")),
        ("25", "12.50", "2", (Decimal(25), USD, "major")),
        ("2450", "25.00", "1", (Decimal("24.50"), USD, "minor")),  # within 2%
        ("11", "0.10", "1", (Decimal("0.11"), USD, "minor")),  # within one cent
        ("12.50", None, None, (Decimal("12.50"), USD, "major")),  # cents are whole numbers
        ("1250", None, None, (None, USD, "unknown")),  # nothing to compare with
        ("1250", "abc", "1", (None, USD, "unknown")),
        ("9999", "12.50", "1", (None, USD, "unknown")),  # fits neither (shipping added?)
        ("0", "0.00", "1", (None, USD, "unknown")),  # fits both
    ],
)
def test_auto_decides_only_when_the_numbers_do(amount, unit_price, quantity, expected):
    money = {"amount": amount, "currency": USD}
    assert money_amount(money, "auto", unit_price, quantity) == expected


def test_auto_edge_cases():
    assert money_amount(None, "auto", "12.50", "1") == (None, None, "unknown")
    assert money_amount("1250", "minor") == (None, None, "unknown")  # not a Money at all
    parsed = Money.model_validate({"amount": 2500, "currency": "USD"})
    assert money_amount(parsed, "auto", "12.50", "2") == (Decimal("25.00"), "USD", "minor")
    assert money_amount({"currency": "USD"}, "auto", "12.50", "1") == (None, "USD", "unknown")
    jpy = money_amount({"amount": "1500", "currency": "JPY"}, "auto")
    assert jpy == (Decimal(1500), "JPY", "major")  # no minor unit, so nothing to decide


def test_infer_money_unit_counts_each_record():
    records = [
        {
            "total_price": {"amount": "2500", "currency": "USD"},
            "unit_price": "12.50",
            "quantity": 2,
        },
        {
            "total_price": {"amount": "1000", "currency": "USD"},
            "unit_price": "10.00",
            "quantity": "1",
        },
        {
            "total_price": {"amount": "25", "currency": "USD"},
            "unit_price": "12.50",
            "quantity": "2",
        },
        {"total_price": None, "unit_price": "1.00", "quantity": "1"},
        {},
    ]
    assert infer_money_unit(records) == {"minor": 2, "major": 1, "unknown": 2}


@pytest.mark.parametrize("unit", ["minor", "major"])
def test_infer_money_unit_reads_the_fakes_totals(unit):
    fake = FakeQuartzy(money_unit=unit)
    for price, quantity in [("12.50", "2"), ("289.00", "1"), ("7.25", "4")]:
        fake.add_request(unit_price=price, quantity=quantity)
    counts = infer_money_unit(fake.order_requests.values())
    assert counts == {"minor": 0, "major": 0, "unknown": 0, unit: 3}
