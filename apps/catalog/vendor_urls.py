"""Product pages → (vendor family, catalog #): "paste the vendor URL" in search (plan §5),
and later the browser extension. Pure Python.

``parse_vendor_url(text)`` returns a VendorURL, or None when ``text`` isn't a URL of a vendor
listed here. ``catalog_raw`` is None when the page names no number (IDT's product pages); the
search then falls back to ``words``, the page's slug as words.
"""

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlsplit


@dataclass(frozen=True)
class VendorURL:
    family: str
    catalog_raw: str | None
    words: str


_CAT = r"[A-Za-z0-9][A-Za-z0-9.\-]*"

# (host suffix, family, path patterns tried in order; each has a "cat" group)
_RULES = (
    (
        "thermofisher.com",
        "thermo",
        (rf"/order/catalog/product/(?:[a-z]{{2}}/[a-z]{{2}}/)?(?P<cat>{_CAT})",),
    ),
    (
        "thermofisher.cn",
        "thermo",
        (rf"/order/catalog/product/(?:[a-z]{{2}}/[a-z]{{2}}/)?(?P<cat>{_CAT})",),
    ),
    ("fishersci.com", "fisher", (rf"/shop/products/(?:p/)?(?:[^/]+/)?(?P<cat>{_CAT})/?$",)),
    ("sigmaaldrich.com", "merck", (rf"/product/[a-z]+/(?P<cat>{_CAT})",)),
    ("merckmillipore.com", "merck", (rf"/product/[a-z]+/(?P<cat>{_CAT})",)),
    ("cellsignal.com", "cst", (r"/products/(?:[^/]+/)*?(?P<cat>\d{3,6})/?$",)),
    ("cellsignal.cn", "cst", (r"/products/(?:[^/]+/)*?(?P<cat>\d{3,6})/?$",)),
    (
        "abcam.com",
        "abcam",
        (r"-(?P<cat>ab\d{2,7})(?:\.html)?/?$", r"/(?P<cat>ab\d{2,7})(?:\.html)?/?$"),
    ),
    (
        "neb.com",
        "neb",
        (r"/products/(?P<cat>[a-z]\d{4})(?:-|/|$)", r"/(?:en|en-[a-z]{2})/(?P<cat>[a-z]\d{4})/?$"),
    ),
    ("atcc.org", "atcc", (rf"/products/(?:all/)?(?P<cat>{_CAT}?)(?:\.aspx)?/?$",)),
    ("idtdna.com", "idt", ()),
    ("qiagen.com", "qiagen", ()),
    ("vwr.com", "vwr", ()),
    ("promega.com", "promega", ()),
    ("bio-rad.com", "biorad", (r"/sku/(?P<cat>\d{5,9})(?:-|/|$)",)),
    ("rndsystems.com", "biotechne", (rf"/products/(?P<cat>{_CAT})/?$",)),
)
# Query parameters that carry a catalog # on vendors' search or product pages.
_PARAMS = ("catno", "catalog_number", "catalognumber", "catalogNumber", "sku", "id", "k")
_URL = re.compile(r"https?://\S+", re.IGNORECASE)


def find_url(text):
    """The first http(s) URL in ``text``, or None."""
    match = _URL.search(text or "")
    return match[0].rstrip(").,;'\"") if match else None


def parse_vendor_url(text):
    url = find_url(text)
    if url is None:
        return None
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = unquote(parts.path or "")
    query = parse_qs(parts.query)
    for suffix, family, patterns in _RULES:
        if host != suffix and not host.endswith("." + suffix):
            continue
        catalog = None
        for pattern in patterns:
            match = re.search(pattern, path, re.IGNORECASE)
            if match:
                catalog = match["cat"]
                break
        if catalog is None:
            catalog = _from_query(query)
        if catalog is not None and family in ("neb", "atcc", "abcam"):
            catalog = catalog.upper() if family != "abcam" else catalog.lower()
        return VendorURL(family=family, catalog_raw=catalog, words=_words(path, catalog))
    return None


def _from_query(query):
    for name in _PARAMS:
        values = query.get(name)
        if values and re.fullmatch(_CAT, values[0]):
            return values[0]
    return None


def _words(path, catalog):
    """The page's slug as words, without the catalog # and the shop's own path words."""
    segments = [s for s in path.split("/") if s and re.search(r"[A-Za-z]{3}", s)]
    if catalog:
        segments = [s for s in segments if s.casefold() != catalog.casefold()] or segments
    slug = segments[-1] if segments else ""  # the product's own slug comes last
    words = re.sub(r"[-_+]+", " ", re.sub(r"\.(html?|aspx)$", "", slug))
    if catalog:
        words = re.sub(re.escape(catalog), " ", words, flags=re.IGNORECASE)
    return " ".join(word for word in words.split() if not word.isdigit())[:120]
