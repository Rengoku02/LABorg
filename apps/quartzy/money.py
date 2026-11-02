"""Quartzy prices and quantities as Decimals (plan §7). Pure Python.

Quartzy sends prices and quantities as strings (``unit_price: "12.50"``, ``quantity: "2"``)
and ``total_price`` as Money ``{amount, currency}``, whose amount is "a string with format
integer" in an undocumented unit: cents or dollars. scripts/quartzy_probe.py measures the
unit and settings pin it as QUARTZY_MONEY_UNIT. Until then "auto" decides record by record
and answers "unknown" rather than guess.
"""

import math
import re
from decimal import Decimal

MONEY_UNITS = ("minor", "major", "auto")
# ISO 4217 currencies whose minor unit isn't a hundredth; all others are taken as 2 digits.
_MINOR_DIGITS = {
    "BHD": 3,
    "CLP": 0,
    "ISK": 0,
    "JOD": 3,
    "JPY": 0,
    "KRW": 0,
    "KWD": 3,
    "OMR": 3,
    "TND": 3,
    "VND": 0,
}
_TOLERANCE = Decimal("0.02")
# Thousands separators only in groups of three, so a decimal comma ("12,50") is rejected
# instead of being read as 1250.
_DIGITS = r"\d{1,3}(?:,\d{3})+(?:\.\d*)?|\d+(?:\.\d*)?|\.\d+"
_MONEY = re.compile(
    rf"(?P<sign>[-+]?)\s*(?:[A-Z]{{3}}\s*)?[$€£¥]?\s*(?P<number>{_DIGITS})"
    r"\s*(?:[A-Z]{3})?"
)
_QUANTITY = re.compile(_DIGITS)


def to_decimal(value):
    """``"12.50"``, ``"1,234.00"``, ``"$5"``, ``"12.50 USD"``, 7 -> Decimal.

    None, ``""``, ``"abc"``, ``"12,5"``, NaN and booleans -> None.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value)) if math.isfinite(value) else None
    if not isinstance(value, str):
        return None
    match = _MONEY.fullmatch(value.strip())
    if match is None:
        return None
    return Decimal(match["sign"] + match["number"].replace(",", ""))


def parse_quantity(value):
    """``"2"``, ``"1.5"``, ``"1,000"``, 3 -> Decimal.

    None, ``""``, negative numbers and ``"2 boxes"`` -> None.
    """
    if isinstance(value, str):
        match = _QUANTITY.fullmatch(value.strip())
        return Decimal(match[0].replace(",", "")) if match else None
    number = to_decimal(value)
    return number if number is not None and number >= 0 else None


def money_amount(money, unit, unit_price=None, quantity=None):
    """Return ``(amount in major units, currency, unit used)`` for a Quartzy Money.

    ``money`` is the raw dict or a ``schemas.Money``. ``unit`` is "minor" (the amount counts
    cents), "major" (it counts dollars) or "auto". "auto" compares the amount with
    unit_price x quantity (read as major units) and with 100 times that, within about 2% or
    one cent; when exactly one fits, that is the unit. A fractional amount can only be major.
    Anything else gives ``(None, currency, "unknown")``.
    """
    if unit not in MONEY_UNITS:
        raise ValueError(f"unit must be one of {MONEY_UNITS}, not {unit!r}")
    if money is None:
        return None, None, "unknown"
    if isinstance(money, dict):
        raw_amount, raw_currency = money.get("amount"), money.get("currency")
    else:
        raw_amount, raw_currency = getattr(money, "amount", None), getattr(money, "currency", None)
    currency = str(raw_currency or "").strip().upper() or None
    amount = to_decimal(raw_amount)
    if amount is None:
        return None, currency, "unknown"
    digits = _MINOR_DIGITS.get(currency, 2)
    if unit == "auto":
        unit = _infer_unit(amount, digits, unit_price, quantity)
        if unit == "unknown":
            return None, currency, "unknown"
    if unit == "minor":
        return amount.scaleb(-digits), currency, "minor"
    return amount, currency, "major"


def _infer_unit(amount, digits, unit_price, quantity):
    if digits == 0:
        return "major"  # minor and major are the same unit (e.g. JPY)
    if amount != amount.to_integral_value():
        return "major"  # cents are whole numbers
    price, count = to_decimal(unit_price), parse_quantity(quantity)
    if price is None or count is None:
        return "unknown"
    expected = price * count
    scale = Decimal(10) ** digits
    fits_major = _close(amount, expected, one_cent=1 / scale)
    fits_minor = _close(amount, expected * scale, one_cent=Decimal(1))
    if fits_major == fits_minor:
        return "unknown"
    return "major" if fits_major else "minor"


def _close(actual, expected, *, one_cent):
    return abs(actual - expected) <= max(abs(expected) * _TOLERANCE, one_cent)


def infer_money_unit(records):
    """How "auto" reads each raw order request: ``{"minor": n, "major": n, "unknown": n}``."""
    counts = {"minor": 0, "major": 0, "unknown": 0}
    for record in records:
        _, _, used = money_amount(
            record.get("total_price"), "auto", record.get("unit_price"), record.get("quantity")
        )
        counts[used] += 1
    return counts
