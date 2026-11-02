"""Each pack rule against real catalog numbers, including the look-alikes it must leave alone.

Sources for every scheme are listed at the top of apps/catalog/vendor_rules.py.
"""

import pytest

from apps.catalog import vendor_rules
from apps.catalog.vendor_rules import RULES

RULE_CASES = [
    # rule, cleaned catalog number, (base, pack) or None
    ("thermo", "11965-092", ("11965", "092")),
    ("thermo", "11965092", ("11965", "092")),
    ("thermo", "11965 092", ("11965", "092")),
    ("thermo", "11-965-092", ("11-965", "092")),
    ("thermo", "15596026CN", ("15596", "026CN")),
    ("thermo", "a3160402", ("A31604", "02")),
    ("thermo", "A1098201", ("A10982", "01")),
    ("thermo", "L3000075", ("L3000", "075")),
    ("thermo", "16-0037-85", ("16-0037", "85")),
    ("thermo", "363-0041-82", ("363-0041", "82")),
    ("thermo", "014142-57", ("014142", "57")),
    ("thermo", "H50094.03", ("H50094", "03")),
    ("thermo", "030760.K7", ("030760", "K7")),
    ("thermo", "GMP100-15-500UG", ("GMP100-15", "500UG")),
    ("thermo", "A11001", None),
    ("thermo", "A-11001", None),
    ("thermo", "1196509", None),
    ("thermo", "119650921", None),
    ("thermo", "PA5-12345", None),
    ("thermo", "4304437", None),
    ("merck", "A2220-2X25ML", ("A2220", "2X25ML")),
    ("merck", "A2220-1 mL", ("A2220", "1ML")),
    ("merck", "A2220 - 10ML", ("A2220", "10ML")),
    ("merck", "1805-1VL", ("1805", "1VL")),
    ("merck", "1391-5X1ML", ("1391", "5X1ML")),
    ("merck", "S40887-100MG", ("S40887", "100MG")),
    ("merck", "F1804-5MG", ("F1804", "5MG")),
    ("merck", "A5441-100UL", ("A5441", "100UL")),
    ("merck", "UFC901008", ("UFC9010", "08")),
    ("merck", "1009835000", ("100983", "5000")),
    ("merck", "1.00983.9025", ("1.00983", "9025")),
    ("merck", "05-636", None),
    ("merck", "05-636-I", None),
    ("merck", "MAB1501-AF555", None),
    ("merck", "COEDTAF-RO", None),
    ("merck", "UFC901024D", None),
    ("merck", "11836170001", None),
    ("neb", "M0202M", ("M0202", "M")),
    ("neb", "e2621x", ("E2621", "X")),
    ("neb", "C2987I", ("C2987", "I")),
    ("neb", "R0138-L", ("R0138", "L")),
    ("neb", "R0101", None),
    ("neb", "2118S", None),
    ("cst", "2118T", ("2118", "T")),
    ("cst", "4970P", ("4970", "P")),
    ("cst", "7074V", ("7074", "V")),
    ("cst", "85925SF", ("85925", "SF")),
    ("cst", "2118", None),
    ("cst", "2118X", None),
    ("takara", "RR036Q", ("RR036", "Q")),
    ("takara", "RR820W", ("RR820", "W")),
    ("takara", "RR82LR", None),
    ("takara", "638916", None),
    ("biotechne", "236-GMP-01M", ("236-GMP", "01M")),
    ("biotechne", "350-NS-050/CF", ("350-NS/CF", "050")),
    ("biotechne", "MAB941-500", ("MAB941", "500")),
    ("biotechne", "FAB3439P-100", ("FAB3439P", "100")),
    ("biotechne", "DY206-05", ("DY206", "05")),
    ("biotechne", "1254/1", ("1254", "1")),
    ("biotechne", "236-EG", None),
    ("biotechne", "DY008B", None),
    ("biotechne", "NB100-56708", None),
    ("fisher", "BP152-25", ("BP152", "25")),
    ("fisher", "A18-20", ("A18", "20")),
    ("fisher", "A18S-4", ("A18S", "4")),
    ("fisher", "11-965-092", None),
    ("fisher", "BP1521", None),
    ("fisher", "MT10-013-CV", None),
    ("biorad", "1708891BUN", ("1708891", "BUN")),
    ("biorad", "170-8891BUN", ("170-8891", "BUN")),
    ("biorad", "1708891", None),
    ("cytiva", "SH30071.02", ("SH30071", "02")),
    ("cytiva", "SH30071.03HI", ("SH30071HI", "03")),
    ("cytiva", "17-0756-05", ("17-0756", "05")),
    ("cytiva", "GE17-0756-04", ("17-0756", "04")),
    ("cytiva", "17075605", None),
    ("cayman", "10005583-10", ("10005583", "10")),
    ("cayman", "CAY10005583-50 mg", ("10005583", "50MG")),
    ("cayman", "10005583", None),
    ("zymo", "R1100-50", ("R1100", "50")),
    ("zymo", "D4003-1", None),
    ("zymo", "D4013", None),
    ("selleck", "S1049-10mg", ("S1049", "10MG")),
    ("selleck", "S1049", None),
    ("mce", "HY-10583-10mg", ("HY-10583", "10MG")),
    ("mce", "HY-P0001-1mg", ("HY-P0001", "1MG")),
    ("mce", "HY-10583G", None),
    # ATCC sells one size of each item: its rule only keeps the dot (DOTTED_RULES).
    ("atcc", "CCL-2.2", None),
    ("atcc", "CRL-1573", None),
]


@pytest.mark.parametrize(("rule", "cleaned", "expected"), RULE_CASES)
def test_rule(rule, cleaned, expected):
    assert RULES[rule](cleaned) == expected


def test_dotted_rules_are_rules():
    assert vendor_rules.DOTTED_RULES == {"atcc"}
    assert vendor_rules.DOTTED_RULES <= set(RULES)


@pytest.mark.parametrize("rule", sorted(RULES))
@pytest.mark.parametrize("cleaned", ["", "N/A", "not a catalog number", "sc-47724 HRP"])
def test_rules_ignore_what_they_do_not_know(rule, cleaned):
    assert RULES[rule](cleaned) is None
