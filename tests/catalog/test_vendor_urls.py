"""Pasted vendor URLs (design §10.1). The URLs are the ones vendor_rules.py cites."""

import pytest

from apps.catalog.vendor_urls import find_url, parse_vendor_url


@pytest.mark.parametrize(
    ("url", "family", "catalog"),
    [
        ("https://www.thermofisher.com/order/catalog/product/15140148", "thermo", "15140148"),
        ("https://thermofisher.com/order/catalog/product/jp/ja/11965118", "thermo", "11965118"),
        ("https://www.thermofisher.com/order/catalog/product/16-0037-81", "thermo", "16-0037-81"),
        ("https://thermofisher.com/order/catalog/product/014142.57", "thermo", "014142.57"),
        ("https://thermofisher.cn/order/catalog/product/cn/en/15596026CN", "thermo", "15596026CN"),
        (
            "https://www.fishersci.com/shop/products/gibco-dmem-high-glucose-41/11965092",
            "fisher",
            "11965092",
        ),
        ("https://www.fishersci.com/shop/products/p/BP1521", "fisher", "BP1521"),
        ("https://www.sigmaaldrich.com/US/en/product/sigma/a2220", "merck", "a2220"),
        ("https://www.sigmaaldrich.com/product/sigma/a5441", "merck", "a5441"),
        ("https://www.cellsignal.cn/products/2118", "cst", "2118"),
        (
            "https://cellsignal.com/products/primary-antibodies/gapdh-14c10-rabbit-mab-bsa-and-azide-free/85925",
            "cst",
            "85925",
        ),
        (
            "https://www.abcam.com/en-us/products/primary-antibodies/gapdh-antibody-6c5-loading-control-ab8245",
            "abcam",
            "ab8245",
        ),
        ("https://www.neb.com/en-us/products/r0101-ecori", "neb", "R0101"),
        ("https://www.neb.com/products/m0202-t4-dna-ligase", "neb", "M0202"),
        ("https://www.neb.com/en/e2621", "neb", "E2621"),
        ("https://www.atcc.org/products/all/CCL-2.2.aspx", "atcc", "CCL-2.2"),
        ("https://www.atcc.org/products/crl-3216", "atcc", "CRL-3216"),
        ("https://promega.com/products/part?id=G7570", "promega", "G7570"),
        ("https://www.qiagen.com/us/products/rneasy-mini-kit?catno=74104", "qiagen", "74104"),
        (
            "https://www.bio-rad.com/en-us/sku/1708890-iscript-cdna-synthesis-kit-25-x-20-ul-rxns",
            "biorad",
            "1708890",
        ),
        ("https://www.rndsystems.com/Products/236-eg", "biotechne", "236-eg"),
    ],
)
def test_catalog_numbers_from_product_pages(url, family, catalog):
    found = parse_vendor_url(url)
    assert (found.family, found.catalog_raw) == (family, catalog)


def test_a_page_without_a_number_falls_back_to_words():
    found = parse_vendor_url(
        "https://sg.idtdna.com/page/products/crispr-genome-editing/alt-r-crispr-enzymes/cas9-protein"
    )
    assert found.family == "idt" and found.catalog_raw is None
    assert found.words == "cas9 protein"


def test_words_leave_out_the_catalog_number():
    found = parse_vendor_url("https://www.neb.com/en-us/products/r0101-ecori")
    assert found.words == "ecori"


def test_non_vendor_text():
    assert parse_vendor_url("GAPDH antibody") is None
    assert parse_vendor_url("https://example.com/product/123") is None
    assert find_url("see (https://neb.com/en/e2621).") == "https://neb.com/en/e2621"
