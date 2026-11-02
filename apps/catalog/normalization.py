"""Catalog-number and vendor-name normalization. Pure Python: no Django or ORM imports.

The Phase 0 baseline notebook, the catalog app and the duplicate guard all key products
through these functions, so changing them changes what counts as "the same product".

A catalog number becomes:
- ``cleaned``: NFKC, trimmed, prefixes such as "Cat#", "Cat. No.", "#" or a vendor name
  ("ATCC CCL-2", "CST #2118S") removed, a trailing size note ("(500 mL)") removed,
  whitespace collapsed and lossless Excel damage repaired.
- ``key``: ``cleaned`` uppercased with every non-alphanumeric removed; None when there is
  nothing to match on, or when it is longer than ``MAX_KEY_LENGTH`` (a pasted URL or a
  list of numbers is not a catalog number). Under a rule in ``vendor_rules.DOTTED_RULES``
  a dot between two digits stays: ATCC CCL-2.2 (HeLa S3) is "CCL2.2", CCL-22 (MDBK) "CCL22".
- ``base``: the vendor family's pack rule (``vendor_rules.RULES``) runs on ``cleaned``
  while the separators it reads are still there (11965-092 is product 11965, pack 092),
  and the result is normalized like ``key``. Equals ``key`` when no rule applies.
- ``is_placeholder``: True for missing values (None, empty) and fillers (N/A, TBD,
  custom, see notes, 0, x, a vendor name typed into the catalog field...).
- ``no_key_reason``: why ``key`` is None: "placeholder", "excel_mangled" or "too_long".

Excel damage, and what happens to it:
- "11965092.0", "2118.00", "11,965,092": digits only, so repaired to "11965092" and
  "2118". Thousands separators are read first, so "1,000" is 1000, not 1. Where the decimal
  mark is a comma, "11965092,0" and "11.965.092" are repaired the same way.
- "1.1965092E+07": every digit survived, so it is expanded to "11965092".
- "1.1965E+07": digits were rounded away (it could be any 11965xxx), so the key is None
  rather than a guess that would match unrelated products. ``is_excel_mangled`` flags it.
- '="0030120086"' and a leading apostrophe (Excel's ways of forcing text) are unwrapped.
- "#N/A", "#VALUE!" and friends are placeholders.
- Lost leading zeros (Eppendorf 0030120086 read back as 30120086) cannot be detected.
- NBSP and other Unicode spaces become plain spaces; en-dashes and other dashes become "-".
- NUL characters (UTF-16 read as UTF-8, text pasted from PDFs) are dropped, here and in lot
  numbers and vendor names: Postgres text can't hold them.

Known limitation: stripping separators can merge numbers that differ only by punctuation.
Where that is known to happen, the family's rule keeps the dot (ATCC, above).
"""

import math
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from types import MappingProxyType

import yaml

from .vendor_rules import DOTTED_RULES, RULES

VENDORS_YAML = Path(__file__).resolve().parent / "data" / "vendors.yaml"
# A longer key is a pasted URL or a list of numbers, not a catalog or lot number, so it gets
# no key. Well under the 100 characters the catalog's key columns hold.
MAX_KEY_LENGTH = 60
# Pack codes are short ("026CN", "4X25ML"); a longer one is a rule misreading the number.
_MAX_PACK_LENGTH = 20
# The vendor family of requests that name no vendor (blank, "N/A", "unknown"...). It isn't in
# vendors.yaml: catalog.services keeps it as the provisional "Unknown vendor"'s family.
UNKNOWN_FAMILY = "unknown"


@dataclass(frozen=True)
class NormalizedCatalog:
    """One catalog number, ready for matching. ``key`` and ``base`` are None together, when
    there is nothing to match on; ``no_key_reason`` then says why: "placeholder",
    "excel_mangled" (digits lost to Excel) or "too_long" (longer than MAX_KEY_LENGTH)."""

    raw: str | None
    cleaned: str | None
    key: str | None
    base: str | None
    pack_code: str | None
    is_placeholder: bool
    rule: str | None
    no_key_reason: str | None = None


# ---------------------------------------------------------------------------------------
# Text clean-up shared by catalog numbers, lots and vendor names

# Trademark signs go before NFKC, which would otherwise turn "CCL-2(TM)" into "CCL-2TM".
# NUL goes too: Postgres text can't hold it.
_DROP = dict.fromkeys(
    map(ord, "\x00\u00ad\u200b\u200c\u200d\u2060\ufeff\u2122\u2120\u00ae\u00a9"), None
)
_REPLACE = str.maketrans(
    {
        **dict.fromkeys("\u2010\u2011\u2012\u2013\u2014\u2015\u2212", "-"),
        "\u03bc": "u",  # NFKC turns the micro sign into Greek mu; units are written "uL"
        "\u00d7": "x",
    }
)
_QUOTES = "\"'`\u2018\u2019\u201c\u201d\u00ab\u00bb"


def _as_text(raw):
    """Accept what spreadsheets hand back: str, int, float (NaN means empty), Decimal."""
    if raw is None or isinstance(raw, str):
        return raw
    if isinstance(raw, float):
        if math.isnan(raw):
            return None
        if raw.is_integer() and abs(raw) < 1e15:
            return str(int(raw))
    return str(raw)


def _tidy(text):
    text = unicodedata.normalize("NFKC", text.translate(_DROP)).translate(_REPLACE)
    text = re.sub(r"\s+", " ", text).strip()
    # Excel's ways of forcing text: ="0030120086" and a leading apostrophe.
    text = re.sub(r'^="(.*)"$', r"\1", text)
    return text.strip(_QUOTES + " ").rstrip(".,;: ")


_NOT_KEY = re.compile(r"[^0-9A-Z]")
# The same, except a dot between two digits (kept under vendor_rules.DOTTED_RULES).
_NOT_DOTTED_KEY = re.compile(r"(?!(?<=\d)\.\d)[^0-9A-Z]")


def _key(text, *, keep_dots=False):
    return (_NOT_DOTTED_KEY if keep_dots else _NOT_KEY).sub("", text.upper())


# ---------------------------------------------------------------------------------------
# Catalog numbers

_LABEL = re.compile(
    r"""^(?:
        (?:cat(?:alog(?:ue)?)?|item|prod(?:uct)?|part|ref|sku|art(?:ikel|icle)?)
            (?=[\s.\#:]|no\b|nr\b|num)\.?\s*(?:-\s*)?(?:(?:no|nr|num(?:ber)?)\b\.?)?\s*[\#:]*
        | p/n\s*[\#:]*
        | pn\s*[\#:]+
        | (?:no|nr)(?:\.|\s*[\#:])[\#:]*
        | \#+
    )\s*""",
    re.IGNORECASE | re.VERBOSE,
)
_VENDOR_BOUNDARY = re.compile(r"\s*[#:]\s*|\s+-\s+|\s+")
_SIZE = (
    r"(?:\d+(?:[.,]\d+)?\s*[x*]\s*)?\d+(?:[.,]\d+)?\s*"
    r"(?:ul|ml|l|ug|mg|g|kg|ng|nmol|umol|mmol|units?|u|ku|rxns?|reactions?|preps?|tests?"
    r"|assays?|samples?|pk|packs?|pcs?|pieces?|ea|each|vials?|tubes?|plates?|kits?"
    r"|bottles?|tablets?|cs|cases?)\.?"
)
_TRAILING_SIZE = re.compile(
    rf"\s*(?:,|;|\(|\[)\s*(?:{_SIZE}|(?:pack|pk|case|box)\s+of\s+\d+"
    r"|\d+\s*/\s*(?:pk|pack|cs|case|bx|box))\s*[)\]]?$",
    re.IGNORECASE,
)
_SCIENTIFIC = re.compile(r"(\d)(?:[.,](\d+))?E([+-])(\d{2,3})", re.IGNORECASE)
# Numbers as Excel formats them: thousands separators, then a float's one or two zero
# decimals ("11,965,092", "11965092.0", "2118.00"), or the same where the decimal mark is a
# comma ("11.965.092", "11965092,0"). Separators are read first: "1,000" is a thousand.
_EXCEL_NUMBER = re.compile(
    r"(?P<point>\d{1,3}(?:,\d{3})+|\d+)(?:\.0{1,2})?"
    r"|(?P<comma>\d{1,3}(?:\.\d{3})+|\d+)(?:,0{1,2})?"
)

# Compared casefolded with every non-alphanumeric removed ("N/A", "n.a." and "NA" all
# become "na"). Strings of only zeros or only x's, and strings with no letters or digits,
# are placeholders too.
_PLACEHOLDERS = frozenset(
    {
        "na", "nan", "none", "null", "nil", "tbd", "tba", "tbc", "unknown", "unk", "custom",
        "customorder", "custommade", "customoligo", "customprimer", "customprimers",
        "various", "varies", "misc", "miscellaneous", "multiple", "assorted", "mixed",
        "generic", "other", "seenotes", "seenote", "seebelow", "seeabove", "seecomments",
        "seecomment", "seedescription", "seedetails", "seequote", "seeattached", "seelink",
        "seeurl", "seewebsite", "seelabel", "seebox", "seevial", "onlabel", "onbox",
        "notapplicable", "notavailable", "notknown", "notlisted", "nocatalognumber",
        "nocatalogno", "nocat", "nocatno", "nonumber", "pending", "quote", "same",
        "sameasabove", "asabove", "ditto", "blank", "empty", "missing", "tobedetermined",
        "tobeconfirmed", "notsure", "unsure", "nk", "nr",
        # Spreadsheet errors: #N/A is covered by "na" once the "#" is gone.
        "value", "ref", "name", "div0", "num", "error",
    }
)  # fmt: skip
# Words that, in a string with no digits at all, mean "no catalog number here".
_PLACEHOLDER_WORDS = _PLACEHOLDERS | {"see", "n/a"}


def _strip_vendor_prefix(text):
    """Drop a leading vendor name: "ATCC CCL-2" -> "CCL-2", "CST #2118S" -> "2118S"."""
    first_digit = next((i for i, char in enumerate(text) if char.isdigit()), len(text))
    has_digits = first_digit < len(text)
    if not has_digits and vendor_family_for(text):
        return text  # a vendor name typed as the catalog number; a placeholder
    table = load_vendor_table()
    # Longest label first, so "Thermo Fisher Scientific 11965092" loses all three words.
    for boundary in reversed(list(_VENDOR_BOUNDARY.finditer(text, 0, first_digit))):
        label, rest = text[: boundary.start()], text[boundary.end() :].lstrip("-#: ")
        if not label or not rest or (not has_digits and " " in rest):
            continue
        if table.lookup(normalize_vendor_name(label)):
            return rest
    return text


def _repair_excel_number(text):
    """Return (text, mangled). ``mangled`` means digits were lost and can't be recovered."""
    match = _EXCEL_NUMBER.fullmatch(text)
    if match:
        return re.sub(r"\D", "", match["point"] or match["comma"]), False
    match = _SCIENTIFIC.fullmatch(text)
    if match:
        lead, fraction, sign, exponent = match.groups()
        digits = lead + (fraction or "")
        if sign == "+" and len(digits) == int(exponent) + 1:
            return digits, False
        return text, True
    return text, False


def _clean(raw):
    """Return (cleaned or None, mangled)."""
    text = _as_text(raw)
    if text is None:
        return None, False
    text = _tidy(text)
    for _ in range(4):
        before = text
        text = _strip_vendor_prefix(_LABEL.sub("", text, count=1))
        if text == before:
            break
    text = _TRAILING_SIZE.sub("", text).strip()
    text, mangled = _repair_excel_number(text)
    return text or None, mangled


def clean_catalog(raw):
    """The human-readable cleaned catalog number, or None if nothing is left."""
    return _clean(raw)[0]


def _is_placeholder_text(cleaned):
    token = re.sub(r"[^0-9a-z]", "", cleaned.casefold())
    if not token or token in _PLACEHOLDERS or set(token) <= {"0"} or set(token) <= {"x"}:
        return True
    if any(char.isdigit() for char in cleaned):
        return False
    # No digits: free text ("please see attached quote"), a placeholder phrase
    # ("N/A - custom") or a vendor name typed into the wrong field.
    if len(cleaned.split()) >= 3 or vendor_family_for(cleaned):
        return True
    pieces = re.split(r"[\s,;()\[\]-]+", cleaned.casefold())
    return any(re.sub(r"[^0-9a-z/]", "", piece) in _PLACEHOLDER_WORDS for piece in pieces)


def is_placeholder_catalog(raw):
    """True for a missing or filler catalog number (N/A, TBD, custom, see notes, 0, x...)."""
    cleaned = clean_catalog(raw)
    return cleaned is None or _is_placeholder_text(cleaned)


def is_excel_mangled(raw):
    """True for Excel scientific notation that rounded digits away, e.g. "1.1965E+07"."""
    cleaned, mangled = _clean(raw)
    return mangled and not _is_placeholder_text(cleaned)


def _family_rule(vendor_family):
    if not vendor_family:
        return None
    families = load_vendor_table().families
    info = families.get(vendor_family) or families.get(vendor_family_for(vendor_family) or "")
    return info.rule if info else None


def normalize_catalog(raw, vendor_family=None, *, rule=None):
    """Normalize a catalog number for matching.

    ``vendor_family`` is a family slug from vendors.yaml (a raw vendor name also works);
    its ``catalog_rule`` picks the pack rule. ``rule`` names a rule directly instead.
    """
    if rule is not None and rule not in RULES:
        raise ValueError(f"Unknown catalog rule {rule!r}; see apps.catalog.vendor_rules.RULES")
    text = _as_text(raw)
    cleaned, mangled = _clean(text)
    if cleaned is None or _is_placeholder_text(cleaned):
        return NormalizedCatalog(text, cleaned, None, None, None, True, None, "placeholder")
    if mangled:
        return NormalizedCatalog(text, cleaned, None, None, None, False, None, "excel_mangled")
    rule_name = rule if rule is not None else _family_rule(vendor_family)
    keep_dots = rule_name in DOTTED_RULES
    key = _key(cleaned, keep_dots=keep_dots)
    if len(key) > MAX_KEY_LENGTH:
        return NormalizedCatalog(text, cleaned, None, None, None, False, None, "too_long")
    found = RULES[rule_name](cleaned) if rule_name else None
    base_key = _key(found[0], keep_dots=keep_dots) if found else ""
    if not base_key or len(found[1] or "") > _MAX_PACK_LENGTH:
        return NormalizedCatalog(text, cleaned, key, key, None, False, None)
    return NormalizedCatalog(text, cleaned, key, base_key, found[1], False, rule_name)


def cross_family_key_ok(key, min_length=7):
    """Is a bare key distinctive enough to compare across vendor families (guard tier T1x)?"""
    return bool(key) and len(key) >= min_length and any(char.isdigit() for char in key)


# ---------------------------------------------------------------------------------------
# Lot numbers

# "Lot# 1234", "LOT: 1234", "Batch No. 1234", "Lot1234". Lot numbers themselves never
# start with these words, so a digit straight after the label still counts as a label.
_LOT_LABEL = re.compile(
    r"""^(?:
        (?:lot|batch|charge)(?=[\s.\#:\d]|no\b|nr\b|num)\.?\s*(?:(?:no|nr|num(?:ber)?)\b\.?)?
            \s*[\#:]*
        | \#+
    )\s*""",
    re.IGNORECASE | re.VERBOSE,
)


def normalize_lot(raw):
    """A lot key: NFKC, uppercase, separators and spaces stripped; placeholders and keys
    longer than MAX_KEY_LENGTH -> None."""
    text = _as_text(raw)
    if text is None:
        return None
    text = _LOT_LABEL.sub("", _tidy(text), count=1)
    text, mangled = _repair_excel_number(text.strip())
    if not text or mangled or _is_placeholder_text(text):
        return None
    key = _key(text)
    return key if key and len(key) <= MAX_KEY_LENGTH else None


# ---------------------------------------------------------------------------------------
# Vendor names

_LEGAL_SUFFIXES = frozenset(
    {
        "inc", "incorporated", "ltd", "limited", "llc", "gmbh", "co", "corp", "corporation",
        "company", "plc", "ag", "sa", "bv", "kg", "kgaa", "nv", "se", "srl", "spa", "pty",
        "lp", "llp", "ab",
    }
)  # fmt: skip
# Words that may sit between brand names: "Gibco by Life Technologies", "Thermo Fisher
# Scientific Inc. (Invitrogen division)", "R&D Systems, a Bio-Techne brand".
_FILLER_WORDS = _LEGAL_SUFFIXES | {
    "a", "an", "and", "by", "from", "via", "of", "brand", "division", "part", "now",
}  # fmt: skip
_DOMAIN = re.compile(
    r"^(?:https?://)?(?:www\.)?([^\s/]+?)\.(?:com|net|org|co\.uk|de|cn|jp)(?:/\S*)?$"
)
# Vendor "names" that mean the vendor isn't known, compared after normalize_vendor_name with
# the spaces removed ("N/A" -> "na"). They are all UNKNOWN_FAMILY.
_PLACEHOLDER_VENDORS = frozenset(
    {
        "na", "nan", "none", "null", "nil", "unknown", "unk", "unknownvendor", "novendor",
        "vendor", "tbd", "tba", "tbc", "other", "others", "various", "varies", "misc",
        "miscellaneous", "multiple", "notapplicable", "notavailable", "notsure", "seenotes",
        "seebelow", "same", "sameasabove", "x", "xx", "xxx", "0",
    }
)  # fmt: skip


def normalize_vendor_name(raw):
    """The alias key for a vendor name: "Thermo Fisher Scientific, Inc." -> "thermo fisher
    scientific"; "R&D Systems" -> "r and d systems"; "fishersci.com" -> "fishersci"."""
    if raw is None:
        return None
    text = unicodedata.normalize("NFKC", str(raw).translate(_DROP)).casefold().strip()
    text = _DOMAIN.sub(r"\1", text)
    text = text.replace("&", " and ")
    text = re.sub(r"['\u2019`]", "", text)
    tokens = re.sub(r"[\W_]+", " ", text).split()
    if len(tokens) > 1 and tokens[0] == "the":
        tokens = tokens[1:]
    while len(tokens) > 1 and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
        # "Becton, Dickinson and Company" leaves a dangling "and".
        if len(tokens) > 1 and tokens[-1] == "and":
            tokens.pop()
    return " ".join(tokens) or None


def is_placeholder_vendor(raw_vendor_name):
    """True for a blank or filler vendor name: "N/A", "unknown", "TBD", "other"..."""
    key = normalize_vendor_name(raw_vendor_name)
    return not key or key.replace(" ", "") in _PLACEHOLDER_VENDORS


@dataclass(frozen=True)
class VendorFamilyInfo:
    slug: str
    name: str
    distributor: bool
    rule: str | None
    vendors: tuple[str, ...]


@dataclass(frozen=True)
class VendorTable:
    """vendors.yaml, indexed. Every alias is stored under its normalized key and under the
    same key without spaces, so "Bio-Rad", "Bio Rad" and "BioRad" all find one family."""

    families: Mapping[str, VendorFamilyInfo]
    alias_to_family: Mapping[str, str]
    alias_to_vendor_name: Mapping[str, str]

    def lookup(self, alias_key):
        if not alias_key:
            return None
        return self.alias_to_family.get(alias_key) or self.alias_to_family.get(
            alias_key.replace(" ", "")
        )


def build_vendor_table(data):
    """Index parsed vendors.yaml data; raises ValueError on bad slugs, rules or aliases."""
    families, alias_to_family, alias_to_vendor = {}, {}, {}
    for family in data["families"]:
        slug = family["slug"]
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) or slug in families:
            raise ValueError(f"Bad or duplicate vendor family slug {slug!r}")
        if slug == UNKNOWN_FAMILY:
            raise ValueError(f"Vendor family slug {slug!r} is reserved for unnamed vendors")
        rule = family.get("catalog_rule")
        if rule is not None and rule not in RULES:
            raise ValueError(f"Family {slug!r} names unknown catalog rule {rule!r}")
        vendor_names = []
        for vendor in family["vendors"]:
            name = vendor["name"]
            vendor_names.append(name)
            for alias in [name, *(vendor.get("aliases") or [])]:
                key = normalize_vendor_name(alias)
                if not key:
                    raise ValueError(f"Alias {alias!r} of {name!r} normalizes to nothing")
                for variant in {key, key.replace(" ", "")}:
                    seen = (alias_to_family.get(variant), alias_to_vendor.get(variant))
                    if seen[0] is not None and seen != (slug, name):
                        raise ValueError(
                            f"Alias {alias!r} ({variant!r}) of {slug}/{name} is already "
                            f"used by {seen[0]}/{seen[1]}"
                        )
                    alias_to_family[variant], alias_to_vendor[variant] = slug, name
        families[slug] = VendorFamilyInfo(
            slug=slug,
            name=family["name"],
            distributor=bool(family.get("distributor", False)),
            rule=rule,
            vendors=tuple(vendor_names),
        )
    return VendorTable(
        families=MappingProxyType(families),
        alias_to_family=MappingProxyType(alias_to_family),
        alias_to_vendor_name=MappingProxyType(alias_to_vendor),
    )


@cache
def load_vendor_table():
    with VENDORS_YAML.open(encoding="utf-8") as handle:
        return build_vendor_table(yaml.safe_load(handle))


@cache
def _family_by_segments(alias_key):
    """Resolve names that join several aliases of one family: "Thermo Fisher Scientific
    (Gibco)", "Corning Costar", "Gibco by Life Technologies". Mixed families (a distributor
    plus a brand, "Fisher Scientific (Thermo)") stay unresolved."""
    table = load_vendor_table()
    tokens = alias_key.split()
    # reach[i]: families that cover tokens[:i]; None while only filler words were seen.
    reach = {0: {None}}
    for start in range(len(tokens)):
        for family in reach.get(start, ()):
            if tokens[start] in _FILLER_WORDS:
                reach.setdefault(start + 1, set()).add(family)
            for end in range(start + 1, min(len(tokens), start + 6) + 1):
                found = table.lookup(" ".join(tokens[start:end]))
                if found and family in (None, found):
                    reach.setdefault(end, set()).add(found)
    matches = reach.get(len(tokens), set()) - {None}
    return matches.pop() if len(matches) == 1 else None


def vendor_family_for(raw_vendor_name):
    """The vendor family slug for a vendor name as typed into Quartzy: ``UNKNOWN_FAMILY`` for
    a blank or placeholder name ("N/A", "unknown"), None for a name vendors.yaml doesn't know.
    """
    if is_placeholder_vendor(raw_vendor_name):
        return UNKNOWN_FAMILY
    key = normalize_vendor_name(raw_vendor_name)
    return load_vendor_table().lookup(key) or _family_by_segments(key)
