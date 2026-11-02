"""Table tests for catalog-number, lot and vendor-name normalization.

Pure Python: no database. The catalog numbers are real ones; the sources for each vendor's
numbering scheme are listed at the top of apps/catalog/vendor_rules.py.
"""

import ast
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
import yaml

from apps.catalog import normalization
from apps.catalog.normalization import (
    NormalizedCatalog,
    build_vendor_table,
    clean_catalog,
    cross_family_key_ok,
    is_excel_mangled,
    is_placeholder_catalog,
    load_vendor_table,
    normalize_catalog,
    normalize_lot,
    normalize_vendor_name,
    vendor_family_for,
)
from apps.catalog.vendor_rules import RULES

NBSP = "\N{NO-BREAK SPACE}"
EN_DASH = "\N{EN DASH}"
# What people paste into Quartzy's catalog # field instead of a catalog number.
PASTED_URL = (
    "https://www.amazon.com/Fisherbrand-Disposable-Borosilicate-Glass-Pasteur/dp/B07Q3XGZ8M/"
    "ref=sr_1_3?crid=2X9QZ1&keywords=pasteur+pipettes+9+inch&qid=1700000000&sr=8-3"
)
PASTED_LIST = ", ".join(f"R0{number}S" for number in range(101, 121))  # 20 NEB numbers


def fullwidth(text):
    return "".join(chr(ord(char) + 0xFEE0) for char in text)


def utf16_read_as_utf8(text):
    """UTF-16 text decoded a byte at a time: a NUL after every ASCII character."""
    return "".join(f"{char}\x00" for char in text)


CLEAN_CASES = [
    ("11965092", "11965092"),
    ("  11965-092  ", "11965-092"),
    ("Cat# 11965-092", "11965-092"),
    ("Cat.#11965092", "11965092"),
    ("Cat. No. 2118S", "2118S"),
    ("cat no: R0101S", "R0101S"),
    ("Catalog #: A2220-1ML", "A2220-1ML"),
    ("Catalogue No. D8418-100ML", "D8418-100ML"),
    ("Catalog number 15140-122", "15140-122"),
    ("#2118S", "2118S"),
    ("#  2118   S", "2118 S"),
    ("No. 4970S", "4970S"),
    ("REF 0030120086", "0030120086"),
    ("P/N 5067-4626", "5067-4626"),
    ("Item No. 10005583-50", "10005583-50"),
    ("Art.-Nr. 0030120086", "0030120086"),
    # Vendor names in front of the number.
    ("ATCC CCL-2", "CCL-2"),
    ("ATCC\N{REGISTERED SIGN} CCL-2\N{TRADE MARK SIGN}", "CCL-2"),
    ("Addgene #12260", "12260"),
    ("CST #2118S", "2118S"),
    ("Cell Signaling Technology #2118S", "2118S"),
    ("NEB R0101S", "R0101S"),
    ("NEB Cat# R0101S", "R0101S"),
    ("Thermo Fisher Scientific 11965092", "11965092"),
    ("Thermo Scientific Nunc 156499", "156499"),
    ("Gibco - 11965092", "11965092"),
    ("Sigma COEDTAF-RO", "COEDTAF-RO"),
    # Left alone.
    ("sc-47724 HRP", "sc-47724 HRP"),
    ("ab8245", "ab8245"),
    ("CAT123", "CAT123"),  # "cat" glued to digits is part of the number
    ("A5441-.2ML", "A5441-.2ML"),
    ("N0447S", "N0447S"),  # NEB "N" class, not "No."
    # Size notes after the number are commentary, not part of it.
    ("11965-092 (500 mL)", "11965-092"),
    ("11965092, 1 x 500 mL", "11965092"),
    ("2118S (100 \N{MICRO SIGN}l)", "2118S"),
    ("sc-47724 (HRP)", "sc-47724 (HRP)"),  # a conjugate is not a size
    # Unicode and spreadsheet damage.
    (fullwidth("11965092"), "11965092"),
    (f"11965{EN_DASH}092", "11965-092"),
    (f"11965{NBSP}092", "11965 092"),
    ("11965\N{ZERO WIDTH SPACE}092", "11965092"),
    ("11965092.0", "11965092"),
    ("2118.00", "2118"),
    ("11,965,092", "11965092"),
    ("11,965,092.00", "11965092"),
    ("1,000", "1000"),  # a thousand, not 1.000
    ("10,000", "10000"),
    # Where the decimal mark is a comma.
    ("11965092,0", "11965092"),
    ("11.965.092", "11965092"),
    ("1.000", "1000"),
    ("1.000,00", "1000"),
    ("1.1965092E+07", "11965092"),
    ("1.1965E+07", "1.1965E+07"),  # kept for people to read; it gets no key
    ("'11965092", "11965092"),
    ('="0030120086"', "0030120086"),
    ("2118\x00S", "2118S"),  # Postgres text can't hold NUL
    (utf16_read_as_utf8("11965092"), "11965092"),
    (11965092, "11965092"),
    (11965092.0, "11965092"),
    (float("nan"), None),
    ("Cat#", None),
    ("", None),
    (None, None),
]


@pytest.mark.parametrize(("raw", "cleaned"), CLEAN_CASES)
def test_clean_catalog(raw, cleaned):
    assert clean_catalog(raw) == cleaned
    # Cleaning is idempotent, so stored cleaned values normalize like the raw ones.
    assert cleaned is None or clean_catalog(cleaned) == cleaned


PLACEHOLDERS = [
    None,
    "",
    "   ",
    "N/A",
    "n/a",
    "NA",
    "n.a.",
    "-",
    "--",
    "?",
    "??",
    "...",
    "none",
    "None",
    "TBD",
    "tba",
    "unknown",
    "Unknown",
    "custom",
    "Custom order",
    "Custom-made",
    "custom oligo",
    "various",
    "misc",
    "see notes",
    "See Note",
    "see below",
    "see attached quote",
    "not applicable",
    "Not Applicable",
    "0",
    "00",
    "000",
    "0.0",
    "x",
    "xx",
    "xxx",
    "XXXX",
    "nan",
    "NULL",
    "#N/A",
    "#VALUE!",
    "#DIV/0!",
    "Cat# N/A",
    "N/A - custom",
    "please see attached quote",
    "Sigma-Aldrich",  # a vendor name typed into the catalog-number field
    "Thermo Fisher (Gibco)",
]


@pytest.mark.parametrize("raw", PLACEHOLDERS)
def test_placeholders_have_no_key(raw):
    assert is_placeholder_catalog(raw)
    result = normalize_catalog(raw, "thermo")
    assert result.is_placeholder and result.no_key_reason == "placeholder"
    assert (result.key, result.base, result.pack_code, result.rule) == (None, None, None, None)


@pytest.mark.parametrize("raw", [PASTED_URL, PASTED_LIST], ids=["url", "list"])
def test_a_key_too_long_for_a_catalog_number_means_there_is_none(raw):
    result = normalize_catalog(raw, "neb")
    assert (result.key, result.base, result.pack_code, result.rule) == (None, None, None, None)
    assert result.is_placeholder is False and result.no_key_reason == "too_long"
    assert not is_placeholder_catalog(raw) and not is_excel_mangled(raw)


def test_max_key_length():
    longest = "A" + "1" * (normalization.MAX_KEY_LENGTH - 1)
    assert normalize_catalog(longest).key == longest
    assert normalize_catalog(f"{longest}1").key is None
    assert normalize_lot(longest) == longest
    assert normalize_lot(f"{longest}1") is None


def test_a_pack_code_longer_than_any_real_one_is_not_split_off():
    # The merck rule reads any amount with a unit; this one is a 47-character "pack size".
    result = normalize_catalog("A2220-" + "1" * 45 + "ML", "merck")
    assert (result.base, result.pack_code, result.rule) == (result.key, None, None)


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("11965092", None),
        (None, "placeholder"),
        ("N/A", "placeholder"),
        ("1.1965E+07", "excel_mangled"),
        (PASTED_URL, "too_long"),
    ],
)
def test_no_key_reason_says_why_there_is_no_key(raw, reason):
    result = normalize_catalog(raw, "thermo")
    assert result.no_key_reason == reason
    assert (result.key is None) is (reason is not None)


@pytest.mark.parametrize("raw", ["COEDTAF-RO", "TRIZOL", "N0447S", "X100", "0030120086", "NA1"])
def test_codes_that_only_look_odd_are_not_placeholders(raw):
    assert not is_placeholder_catalog(raw)
    assert normalize_catalog(raw).key


NORMALIZE_CASES = [
    # raw, family, key, base, pack code, rule
    # Thermo Fisher: Gibco/Invitrogen 5-digit product + 3-digit pack.
    ("11965092", "thermo", "11965092", "11965", "092", "thermo"),
    ("11965084", "thermo", "11965084", "11965", "084", "thermo"),
    ("11965-118", "thermo", "11965118", "11965", "118", "thermo"),
    ("11-965-092", "thermo", "11965092", "11965", "092", "thermo"),
    ("15140122", "thermo", "15140122", "15140", "122", "thermo"),
    ("15140163", "thermo", "15140163", "15140", "163", "thermo"),
    ("11668019", "thermo", "11668019", "11668", "019", "thermo"),
    ("25200072", "thermo", "25200072", "25200", "072", "thermo"),
    ("15596026CN", "thermo", "15596026CN", "15596", "026CN", "thermo"),
    ("A3160401", "thermo", "A3160401", "A31604", "01", "thermo"),
    ("A3160402", "thermo", "A3160402", "A31604", "02", "thermo"),
    ("L3000015", "thermo", "L3000015", "L3000", "015", "thermo"),
    ("16-0037-81", "thermo", "16003781", "160037", "81", "thermo"),
    ("16-0037-85", "thermo", "16003785", "160037", "85", "thermo"),
    ("031324.22", "thermo", "03132422", "031324", "22", "thermo"),
    ("014104.A1", "thermo", "014104A1", "014104", "A1", "thermo"),
    ("AF-100-15-100UG", "thermo", "AF10015100UG", "AF10015", "100UG", "thermo"),
    ("AF-100-15-1MG", "thermo", "AF100151MG", "AF10015", "1MG", "thermo"),
    ("A11001", "thermo", "A11001", "A11001", None, None),
    ("4367659", "thermo", "4367659", "4367659", None, None),
    # MilliporeSigma: product + "-" + package code with a unit.
    ("A2220-1ML", "merck", "A22201ML", "A2220", "1ML", "merck"),
    ("A2220-4X25ML", "merck", "A22204X25ML", "A2220", "4X25ML", "merck"),
    ("A2220-1VL", "merck", "A22201VL", "A2220", "1VL", "merck"),
    ("A2220", "merck", "A2220", "A2220", None, None),
    ("a2220-5ml", "merck", "A22205ML", "A2220", "5ML", "merck"),
    ("D8418-100ML", "merck", "D8418100ML", "D8418", "100ML", "merck"),
    ("D8418 100 mL", "merck", "D8418100ML", "D8418", "100ML", "merck"),
    ("F1804-200UG", "merck", "F1804200UG", "F1804", "200UG", "merck"),
    ("A5441-.2ML", "merck", "A54412ML", "A5441", ".2ML", "merck"),
    ("05-636-25UG", "merck", "0563625UG", "05636", "25UG", "merck"),
    ("05-636", "merck", "05636", "05636", None, None),
    ("05-636-I", "merck", "05636I", "05636I", None, None),
    ("MAB1501-AF555", "merck", "MAB1501AF555", "MAB1501AF555", None, None),
    ("UFC901024", "merck", "UFC901024", "UFC9010", "24", "merck"),
    ("1.00983.2500", "merck", "1009832500", "100983", "2500", "merck"),
    # New England Biolabs: size/concentration letter.
    ("R0101S", "neb", "R0101S", "R0101", "S", "neb"),
    ("R0101L", "neb", "R0101L", "R0101", "L", "neb"),
    ("M0202T", "neb", "M0202T", "M0202", "T", "neb"),
    ("E2621X", "neb", "E2621X", "E2621", "X", "neb"),
    # Cell Signaling Technology.
    ("2118S", "cst", "2118S", "2118", "S", "cst"),
    ("2118L", "cst", "2118L", "2118", "L", "cst"),
    ("7074P2", "cst", "7074P2", "7074", "P2", "cst"),
    ("85925SF", "cst", "85925SF", "85925", "SF", "cst"),
    # Takara Bio.
    ("RR036A", "takara", "RR036A", "RR036", "A", "takara"),
    ("639650", "takara", "639650", "639650", None, None),
    # Bio-Techne: R&D proteins, antibodies, DuoSets; Tocris.
    ("236-EG-200", "biotechne", "236EG200", "236EG", "200", "biotechne"),
    ("236-EG-01M", "biotechne", "236EG01M", "236EG", "01M", "biotechne"),
    ("350-NS-010/CF", "biotechne", "350NS010CF", "350NSCF", "010", "biotechne"),
    ("AF1050-SP", "biotechne", "AF1050SP", "AF1050", "SP", "biotechne"),
    ("AF1050", "biotechne", "AF1050", "AF1050", None, None),
    ("MAB4832SP", "biotechne", "MAB4832SP", "MAB4832", "SP", "biotechne"),
    ("DY206-05", "biotechne", "DY20605", "DY206", "05", "biotechne"),
    ("1254/10", "biotechne", "125410", "1254", "10", "biotechne"),
    # Fisher Scientific: own-brand product + size; Thermo numbers keep base == key.
    ("BP152-1", "fisher", "BP1521", "BP152", "1", "fisher"),
    ("BP152-500", "fisher", "BP152500", "BP152", "500", "fisher"),
    ("A18P-4", "fisher", "A18P4", "A18P", "4", "fisher"),
    ("11-965-092", "fisher", "11965092", "11965092", None, None),
    # Bio-Rad, Cytiva, Cayman, Zymo, Selleck, MedChemExpress.
    ("1708891BUN", "biorad", "1708891BUN", "1708891", "BUN", "biorad"),
    ("170-8891", "biorad", "1708891", "1708891", None, None),
    ("SH30071.03", "cytiva", "SH3007103", "SH30071", "03", "cytiva"),
    ("SH30071.03HI", "cytiva", "SH3007103HI", "SH30071HI", "03", "cytiva"),
    ("17-0756-01", "cytiva", "17075601", "170756", "01", "cytiva"),
    ("10005583-50", "cayman", "1000558350", "10005583", "50", "cayman"),
    ("R1100-250", "zymo", "R1100250", "R1100", "250", "zymo"),
    ("S1049-2mg", "selleck", "S10492MG", "S1049", "2MG", "selleck"),
    ("HY-10583-10mg", "mce", "HY1058310MG", "HY10583", "10MG", "mce"),
    # No pack rule: base == key.
    ("sc-47724", "scbt", "SC47724", "SC47724", None, None),
    ("sc-47724 HRP", "scbt", "SC47724HRP", "SC47724HRP", None, None),
    ("ab8245", "abcam", "AB8245", "AB8245", None, None),
    ("74104", "qiagen", "74104", "74104", None, None),
    ("0030 120.086", "eppendorf", "0030120086", "0030120086", None, None),
    # ATCC: no pack sizes, and a dot between digits names a derivative line, so it stays:
    # CCL-2.2 is HeLa S3, CCL-22 is MDBK.
    ("CCL-2", "atcc", "CCL2", "CCL2", None, None),
    ("CCL-2.2", "atcc", "CCL2.2", "CCL2.2", None, None),
    ("ATCC CCL-2.2", "atcc", "CCL2.2", "CCL2.2", None, None),
    ("CCL 2.2", "atcc", "CCL2.2", "CCL2.2", None, None),
    ("ccl-2.2", "atcc", "CCL2.2", "CCL2.2", None, None),
    ("CCL-22", "atcc", "CCL22", "CCL22", None, None),
    ("CCL.2", "atcc", "CCL2", "CCL2", None, None),  # not between two digits
    ("CCL-2.2", "ATCC", "CCL2.2", "CCL2.2", None, None),
    # Everywhere else a dot is a separator like any other.
    ("CCL-2.2", None, "CCL22", "CCL22", None, None),
    ("CCL-2.2", "lonza", "CCL22", "CCL22", None, None),
    ("Hs00174097_m1", "thermo", "HS00174097M1", "HS00174097M1", None, None),
    # Rules apply only within their family, or when the family is unknown not at all.
    ("R0101S", None, "R0101S", "R0101S", None, None),
    ("R0101S", "no-such-family", "R0101S", "R0101S", None, None),
    ("11965092", "illumina", "11965092", "11965092", None, None),
    # A vendor name works in place of the family slug.
    ("R0101S", "New England Biolabs", "R0101S", "R0101", "S", "neb"),
    ("11965092", "Gibco", "11965092", "11965", "092", "thermo"),
]


@pytest.mark.parametrize(("raw", "family", "key", "base", "pack", "rule"), NORMALIZE_CASES)
def test_normalize_catalog(raw, family, key, base, pack, rule):
    result = normalize_catalog(raw, family)
    assert (result.key, result.base, result.pack_code, result.rule) == (key, base, pack, rule)
    assert result.is_placeholder is False


SAME_BASE = {
    "Gibco DMEM 500 mL, 1 L, 10 x 500 mL": ["11965092", "11965084", "11965118"],
    "Gibco Pen-Strep 20 mL, 100 mL, 20 x 100 mL": ["15140148", "15140122", "15140163"],
    "Lipofectamine 2000 legacy and current forms": ["11668-019", "11668027", "11668500"],
    "Gibco FBS One Shot 50 mL, 10 x 50 mL": ["A3160401", "A3160402"],
    "Lipofectamine 3000 sizes": ["L3000001", "L3000015", "L3000150"],
    "eBioscience OKT3 50 ug, 500 ug": ["16-0037-81", "16-0037-85"],
    "PeproTech EGF 100 ug, 1 mg": ["AF-100-15-100UG", "AF-100-15-1MG"],
}
SAME_BASE_OTHER = {
    "NEB EcoRI": ("neb", ["R0101S", "R0101L", "R0101T", "R0101M"]),
    "NEB T4 DNA ligase": ("neb", ["M0202S", "M0202L", "M0202T", "M0202M"]),
    "CST GAPDH 14C10": ("cst", ["2118S", "2118L", "2118T"]),
    "Sigma ANTI-FLAG M2 gel": ("merck", ["A2220", "A2220-1VL", "A2220-1ML", "A2220-4X25ML"]),
    "Sigma ANTI-FLAG M2 antibody": ("merck", ["F1804-50UG", "F1804-1MG", "F1804-5MG"]),
    "Amicon Ultra-15 10 kDa": ("merck", ["UFC901008", "UFC901024", "UFC901096"]),
    "R&D EGF": ("biotechne", ["236-EG-200", "236-EG-01M"]),
    "Tocris Y-27632": ("biotechne", ["1254/1", "1254/10", "1254/50"]),
    "Takara PrimeScript RT master mix": ("takara", ["RR036A", "RR036B", "RR036Q"]),
    "Fisher BioReagents Tris": ("fisher", ["BP152-500", "BP152-1", "BP152-5"]),
    "HyClone FBS": ("cytiva", ["SH30071.02", "SH30071.03"]),
    "Zymo DNA/RNA Shield": ("zymo", ["R1100-50", "R1100-250"]),
}
SAME_BASE_ALL = {
    **{name: ("thermo", numbers) for name, numbers in SAME_BASE.items()},
    **SAME_BASE_OTHER,
}


@pytest.mark.parametrize(("family", "numbers"), SAME_BASE_ALL.values(), ids=SAME_BASE_ALL.keys())
def test_pack_sizes_share_a_base_but_not_a_key(family, numbers):
    results = [normalize_catalog(number, family) for number in numbers]
    assert len({result.base for result in results}) == 1
    assert len({result.key for result in results}) == len(numbers)


DIFFERENT_BASE = {
    "Santa Cruz conjugate": (("sc-47724", "scbt"), ("sc-47724 HRP", "scbt")),
    "Santa Cruz agarose conjugate": (("sc-47724", "scbt"), ("sc-47724AC", "scbt")),
    "MCE GMP grade": (("HY-10583-10mg", "mce"), ("HY-10583G-10mg", "mce")),
    "R&D carrier-free": (("350-NS-010", "biotechne"), ("350-NS-010/CF", "biotechne")),
    "HyClone heat-inactivated": (("SH30071.03", "cytiva"), ("SH30071.03HI", "cytiva")),
    "Millipore -I listing": (("05-636", "merck"), ("05-636-I", "merck")),
    "Millipore conjugate": (("MAB1501", "merck"), ("MAB1501-AF555", "merck")),
    "eBioscience clones": (("12-0041-82", "thermo"), ("12-0042-82", "thermo")),
    "eBioscience conjugates": (("12-0041-82", "thermo"), ("14-0041-82", "thermo")),
    "Jackson cross-adsorbed": (
        ("111-035-003", "jackson-immunoresearch"),
        ("111-035-045", "jackson-immunoresearch"),
    ),
    "BioLegend purified vs FITC": (("100201", "biolegend"), ("100203", "biolegend")),
    "Fisher container letter": (("A18-4", "fisher"), ("A18P-4", "fisher")),
    "ATCC HeLa S3 vs MDBK": (("ATCC CCL-2.2", "atcc"), ("CCL-22", "atcc")),
}


@pytest.mark.parametrize(("first", "second"), DIFFERENT_BASE.values(), ids=DIFFERENT_BASE.keys())
def test_different_products_keep_different_bases(first, second):
    assert normalize_catalog(*first).base != normalize_catalog(*second).base


def test_fisher_sku_has_the_gibco_key_and_can_match_across_families():
    fisher = normalize_catalog("11-965-092", "fisher")
    gibco = normalize_catalog("11965092", "thermo")
    assert fisher.key == gibco.key == "11965092"
    assert cross_family_key_ok(fisher.key)


EXCEL_CASES = [
    # raw, key under the thermo family, what happened
    ("1.1965E+07", None, "digits were rounded away; any 11965xxx would fit, so no key"),
    ("1.1965092E+07", "11965092", "scientific notation that kept every digit is expanded"),
    ("1E+07", None, "a lone digit can't be a real catalog number"),
    ("11965092.0", "11965092", "float read back with a trailing .0"),
    ("2118.00", "2118", "float shown with two decimals"),
    ("11965092,0", "11965092", "float where the decimal mark is a comma"),
    ("11,965,092", "11965092", "thousands separators"),
    ("1,000", "1000", "a thousands separator, not a float's .000"),
    ("10,000", "10000", "a thousands separator, so not the catalog # 10"),
    ("1.000", "1000", "a thousands separator where the decimal mark is a comma"),
    ("  11965092\t", "11965092", "leading and trailing whitespace"),
    (f"{NBSP}11965092{NBSP}", "11965092", "NBSP becomes a space and is trimmed"),
    (f"11965{EN_DASH}092", "11965092", "en-dash becomes a hyphen"),
    ("11965\N{MINUS SIGN}092", "11965092", "minus sign becomes a hyphen"),
    ("'11965092", "11965092", "Excel's leading apostrophe"),
    ('="0030120086"', "0030120086", 'Excel\'s ="..." text formula'),
    ("30120086", "30120086", "lost leading zeros can't be detected"),
    ("#N/A", None, "spreadsheet error is a placeholder"),
    (11965092.0, "11965092", "a float straight from pandas"),
]


@pytest.mark.parametrize(("raw", "key", "why"), EXCEL_CASES)
def test_excel_damage(raw, key, why):
    assert normalize_catalog(raw, "thermo").key == key, why


def test_a_thousand_does_not_match_a_short_catalog_number():
    assert normalize_catalog("10,000").key != normalize_catalog("10").key


def test_unrecoverable_excel_number_is_flagged_not_a_placeholder():
    result = normalize_catalog("1.1965E+07", "thermo")
    assert result == NormalizedCatalog(
        "1.1965E+07", "1.1965E+07", None, None, None, False, None, "excel_mangled"
    )
    assert is_excel_mangled("1.1965E+07")
    assert not is_excel_mangled("1.1965092E+07")
    assert not is_excel_mangled("11965092")
    assert not is_excel_mangled(None)


def test_dash_variants_still_reach_the_pack_rule():
    result = normalize_catalog(f"{NBSP}11965{EN_DASH}092{NBSP}", "thermo")
    assert (result.cleaned, result.base, result.pack_code) == ("11965-092", "11965", "092")


def test_raw_is_kept_as_given():
    assert normalize_catalog(" Cat# 2118S ", "cst").raw == " Cat# 2118S "
    assert normalize_catalog(11965092.0, "thermo").raw == "11965092"


def test_rule_can_be_named_directly():
    assert normalize_catalog("R0101S", rule="neb").base == "R0101"
    with pytest.raises(ValueError, match="Unknown catalog rule"):
        normalize_catalog("R0101S", rule="nope")


def test_result_is_frozen():
    result = normalize_catalog("2118S", "cst")
    with pytest.raises(FrozenInstanceError):
        result.key = "X"


LOT_CASES = [
    ("SLBX1234", "SLBX1234"),
    ("Lot# SLBX1234", "SLBX1234"),
    ("LOT: 10238675", "10238675"),
    ("Lot10238675", "10238675"),
    ("lot 1234-567", "1234567"),
    ("Batch No. 05427ES-021", "05427ES021"),
    ("  2118s-12 ", "2118S12"),
    (f"A1{NBSP}B2", "A1B2"),
    ("123456.0", "123456"),
    (123456, "123456"),
    ("Lot\x001234", "1234"),
    (utf16_read_as_utf8("SLBX1234"), "SLBX1234"),
    (PASTED_URL, None),  # too long to be a lot number
    ("1.2346E+09", None),
    ("N/A", None),
    ("unknown", None),
    ("see box", None),
    ("0", None),
    ("", None),
    (None, None),
]


@pytest.mark.parametrize(("raw", "lot"), LOT_CASES)
def test_normalize_lot(raw, lot):
    assert normalize_lot(raw) == lot


@pytest.mark.parametrize(
    ("key", "min_length", "ok"),
    [
        ("11965092", 7, True),
        ("1234567", 7, True),
        ("SC47724", 7, True),
        ("123456", 7, False),
        ("AB8245", 7, False),
        ("2118S", 7, False),
        ("2118S", 5, True),
        ("COEDTAFRO", 7, False),  # no digit
        ("", 7, False),
        (None, 7, False),
    ],
)
def test_cross_family_key_ok(key, min_length, ok):
    assert cross_family_key_ok(key, min_length=min_length) is ok


VENDOR_NAME_CASES = [
    ("Thermo Fisher Scientific", "thermo fisher scientific"),
    ("Thermo Fisher Scientific, Inc.", "thermo fisher scientific"),
    ("THERMO FISHER SCIENTIFIC INC", "thermo fisher scientific"),
    ("Thermo Scientific\N{TRADE MARK SIGN}", "thermo scientific"),
    ("Cell Signaling Technology\N{REGISTERED SIGN}", "cell signaling technology"),
    ("  The Jackson Laboratory ", "jackson laboratory"),
    ("R&D Systems", "r and d systems"),
    ("R & D Systems, Inc.", "r and d systems"),
    ("Bio-Rad Laboratories, Inc.", "bio rad laboratories"),
    ("Miltenyi Biotec GmbH", "miltenyi biotec"),
    ("Merck KGaA", "merck"),
    ("Sigma-Aldrich Co. LLC", "sigma aldrich"),
    ("Becton, Dickinson and Company", "becton dickinson"),
    ("Abcam plc", "abcam"),
    ("Eppendorf SE", "eppendorf"),
    ("J.T.Baker", "j t baker"),
    ("Fisher\N{RIGHT SINGLE QUOTATION MARK}s", "fishers"),
    ("fishersci.com", "fishersci"),
    ("https://www.neb.com/en-us", "neb"),
    ("Thermo\x00 Fisher", "thermo fisher"),
    (utf16_read_as_utf8("Gibco"), "gibco"),
    ("Co.", "co"),  # never strips the whole name
    ("The", "the"),
    ("", None),
    ("   ", None),
    (None, None),
]


@pytest.mark.parametrize(("raw", "key"), VENDOR_NAME_CASES)
def test_normalize_vendor_name(raw, key):
    assert normalize_vendor_name(raw) == key


VENDOR_FAMILY_CASES = [
    # Thermo Fisher brands.
    ("Thermo Fisher Scientific", "thermo"),
    ("Thermo Fisher", "thermo"),
    ("ThermoFisher", "thermo"),
    ("Thermo", "thermo"),
    ("thermo scientific", "thermo"),
    ("Life Technologies", "thermo"),
    ("Life Technologies Corporation", "thermo"),
    ("Gibco", "thermo"),
    ("GIBCO BRL", "thermo"),
    ("Gibco by Life Technologies", "thermo"),
    ("Invitrogen", "thermo"),
    ("Invitrogen by Thermo Fisher Scientific", "thermo"),
    ("Applied Biosystems", "thermo"),
    ("Ambion", "thermo"),
    ("Pierce", "thermo"),
    ("Thermo Scientific Pierce", "thermo"),
    ("Molecular Probes", "thermo"),
    ("Nunc", "thermo"),
    ("eBioscience", "thermo"),
    ("PeproTech", "thermo"),
    ("Alfa Aesar", "thermo"),
    ("Thermo Fisher Scientific (Gibco)", "thermo"),
    ("Thermo Fisher Scientific Inc. - Invitrogen division", "thermo"),
    # Fisher Scientific is a distributor family of its own.
    ("Fisher Scientific", "fisher"),
    ("Fisher", "fisher"),
    ("Fisher Sci", "fisher"),
    ("fishersci.com", "fisher"),
    ("Fisherbrand", "fisher"),
    ("Fisher BioReagents", "fisher"),
    ("Fisher Chemical", "fisher"),
    # MilliporeSigma / Merck.
    ("Sigma Aldrich", "merck"),
    ("Sigma-Aldrich", "merck"),
    ("SigmaAldrich", "merck"),
    ("Sigma", "merck"),
    ("MilliporeSigma", "merck"),
    ("Millipore Sigma", "merck"),
    ("Merck Millipore", "merck"),
    ("EMD Millipore", "merck"),
    ("Merck KGaA", "merck"),
    ("Supelco", "merck"),
    ("Calbiochem", "merck"),
    ("MilliporeSigma (Merck KGaA)", "merck"),
    # Others from the plan's vendor list.
    ("VWR International", "vwr"),
    ("Avantor", "vwr"),
    ("NEB", "neb"),
    ("New England BioLabs", "neb"),
    ("New England Biolabs, Inc.", "neb"),
    ("New England Biolabs (NEB)", "neb"),
    ("CST", "cst"),
    ("Cell Signaling", "cst"),
    ("Cell Signaling Technology, Inc.", "cst"),
    ("Abcam", "abcam"),
    ("Bio-Rad", "biorad"),
    ("BioRad", "biorad"),
    ("Bio Rad Laboratories", "biorad"),
    ("QIAGEN", "qiagen"),
    ("Promega Corporation", "promega"),
    ("Corning", "corning"),
    ("Falcon", "corning"),
    ("BD Falcon", "corning"),
    ("Corning Costar", "corning"),
    ("Axygen", "corning"),
    ("Eppendorf", "eppendorf"),
    ("IDT", "idt"),
    ("Integrated DNA Technologies", "idt"),
    ("Takara", "takara"),
    ("Clontech", "takara"),
    ("R&D Systems", "biotechne"),
    ("R and D Systems", "biotechne"),
    ("Tocris", "biotechne"),
    ("Novus Biologicals", "biotechne"),
    ("R&D Systems, a Bio-Techne brand", "biotechne"),
    ("Santa Cruz Biotechnology", "scbt"),
    ("Santa Cruz", "scbt"),
    ("BioLegend", "biolegend"),
    ("BD Biosciences", "bd"),
    ("BD Pharmingen", "bd"),
    ("Becton, Dickinson and Company", "bd"),
    ("Miltenyi Biotec", "miltenyi"),
    ("STEMCELL Technologies", "stemcell"),
    ("Stem Cell Technologies", "stemcell"),
    ("ATCC", "atcc"),
    ("American Type Culture Collection", "atcc"),
    ("Addgene", "addgene"),
    ("GenScript", "genscript"),
    ("Twist Bioscience", "twist"),
    ("Roche", "roche"),
    ("Roche Diagnostics GmbH", "roche"),
    ("Lonza", "lonza"),
    ("Selleck Chemicals", "selleck"),
    ("Selleckchem", "selleck"),
    ("MedChemExpress", "mce"),
    ("Cayman Chemical", "cayman"),
    ("Jackson ImmunoResearch", "jackson-immunoresearch"),
    ("Proteintech", "proteintech"),
    ("Agilent Technologies", "agilent"),
    ("Illumina", "illumina"),
    ("Zymo Research", "zymo"),
    ("Cytiva", "cytiva"),
    ("GE Healthcare", "cytiva"),
    ("HyClone", "cytiva"),
    ("Dharmacon", "revvity"),
    ("GE Dharmacon", "revvity"),
    ("The Jackson Laboratory", "jax"),
    ("Quartzy", "quartzy"),
    ("Amazon.com", "amazon"),
    (utf16_read_as_utf8("Gibco"), "thermo"),
    # No vendor named: the shared "unknown" family, as in catalog.services.resolve_vendor.
    ("", "unknown"),
    ("   ", "unknown"),
    ("-", "unknown"),
    (None, "unknown"),
    ("N/A", "unknown"),
    ("n/a", "unknown"),
    ("Unknown", "unknown"),
    ("Unknown vendor", "unknown"),
    ("TBD", "unknown"),
    ("Other", "unknown"),
    ("various", "unknown"),
    # Unknown, ambiguous or mixed: left for the review queue.
    ("Acme Reagents", None),
    ("Jackson", None),
    ("Fisher Scientific (Thermo)", None),
    ("Gibco via Fisher", None),
    ("Vendor X Labs", None),
]


@pytest.mark.parametrize(("raw", "family"), VENDOR_FAMILY_CASES)
def test_vendor_family_for(raw, family):
    assert vendor_family_for(raw) == family


@pytest.mark.parametrize(
    ("raw", "placeholder"),
    [
        (None, True),
        ("", True),
        ("N/A", True),
        ("Unknown vendor", True),
        ("other", True),
        ("xxx", True),
        ("Thermo Fisher", False),
        ("Acme Reagents", False),
        ("Vendor X Labs", False),
    ],
)
def test_is_placeholder_vendor(raw, placeholder):
    assert normalization.is_placeholder_vendor(raw) is placeholder


def test_the_unknown_family_is_not_a_vendors_yaml_family():
    assert normalization.UNKNOWN_FAMILY == "unknown"
    assert normalization.UNKNOWN_FAMILY not in load_vendor_table().families
    # Its catalog numbers follow no vendor's rule.
    assert normalize_catalog("11965092", normalization.UNKNOWN_FAMILY).base == "11965092"


def _yaml_families():
    return yaml.safe_load(normalization.VENDORS_YAML.read_text(encoding="utf-8"))["families"]


def test_every_vendor_name_and_alias_resolves_to_its_own_family():
    table = load_vendor_table()
    for family in _yaml_families():
        for vendor in family["vendors"]:
            for alias in [vendor["name"], *(vendor["aliases"] or [])]:
                assert vendor_family_for(alias) == family["slug"], alias
                key = normalize_vendor_name(alias)
                assert table.alias_to_vendor_name[key] == vendor["name"], alias


def test_family_table():
    families = load_vendor_table().families
    assert families["fisher"].distributor and families["vwr"].distributor
    assert not families["thermo"].distributor and not families["merck"].distributor
    assert families["thermo"].rule == "thermo" and families["abcam"].rule is None
    assert families["atcc"].rule == "atcc"
    assert "Gibco" in families["thermo"].vendors
    # Every rule is wired to a family, and every family's rule exists.
    assert {info.rule for info in families.values() if info.rule} == set(RULES)
    assert load_vendor_table() is load_vendor_table()


@pytest.mark.parametrize(
    ("families", "error"),
    [
        (
            [
                {"slug": "a", "name": "A", "vendors": [{"name": "Acme", "aliases": ["Big Lab"]}]},
                {"slug": "b", "name": "B", "vendors": [{"name": "Beta", "aliases": ["BigLab"]}]},
            ],
            "already used",
        ),
        ([{"slug": "Bad Slug", "name": "X", "vendors": []}], "slug"),
        ([{"slug": "x", "name": "X", "catalog_rule": "nope", "vendors": []}], "unknown"),
        ([{"slug": "x", "name": "X", "vendors": [{"name": "X", "aliases": ["!!"]}]}], "nothing"),
        ([{"slug": "unknown", "name": "Unknown", "vendors": []}], "reserved"),
    ],
)
def test_bad_vendor_tables_are_rejected(families, error):
    with pytest.raises(ValueError, match=error):
        build_vendor_table({"families": families})


@pytest.mark.parametrize("module", ["normalization.py", "vendor_rules.py"])
def test_modules_import_no_django(module):
    tree = ast.parse((Path(normalization.__file__).parent / module).read_text(encoding="utf-8"))
    imported = {
        name.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for name in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module
    }
    assert not imported & {"django", "apps"}
