# Research behind these rules (checked 2026-10-27). A rule is encoded only where the same
# product was found in several pack sizes under one stem.
#
# Thermo Fisher (Gibco/Invitrogen: 5-digit product + 3-digit pack code; Fisher US writes
# the same number 2-3-3, e.g. 11-965-092 = Gibco 11965092):
# - https://www.fishersci.com/shop/products/dmem/11965084 (11965092 500 mL, 11965084 1000 mL)
# - https://thermofisher.com/order/catalog/product/jp/ja/11965118 (11965118 10 x 500 mL)
# - https://www.fishersci.com/shop/products/gibco-dmem-high-glucose-41/11965092
# - https://www.thermofisher.com/order/catalog/product/15140148 (Pen-Strep 15140148/122/163)
# - https://thermofisher.com/order/catalog/product/25200072 (Trypsin-EDTA 25200056/072/114)
# - https://www.thermofisher.com/order/catalog/product/11668019 (Lipofectamine 2000 019/027/030)
# - https://thermofisher.cn/order/catalog/product/cn/en/15596026CN (China SKUs add "CN")
# Thermo A-series (A + 5-digit product + 2-digit pack) and Lipofectamine 3000:
# - https://commerce.thermofisher.com/order/catalog/product/A3160401 (50 mL; A3160402 10 x 50)
# - https://www.thermofisher.com/order/catalog/product/A1098202 (AlgiMatrix 1 plate / 4 plates)
# - https://www.fishersci.com/shop/products/lipofectamine-3000-transfection-reagent/L3000001
#   (L3000001 0.1 mL, L3000008 0.75 mL, L3000015 1.5 mL, L3000075 5 x 1.5 mL, L3000150 15 mL)
# eBioscience (conjugate-specificity-size; the first segment is the conjugate, not the pack):
# - https://www.thermofisher.com/order/catalog/product/16-0037-81 (50 ug; 16-0037-85 500 ug)
# - https://www.thermofisher.com/order/catalog/product/12-0041-82 (11-/12-/14-0041-82 conjugates)
# Thermo Scientific Chemicals (ex Alfa Aesar: 6-character product + "." + 2-character pack):
# - https://www.thermofisher.com/order/catalog/product/031324.36 (031324.22 100 g, .36 500 g)
# - https://thermofisher.com/order/catalog/product/014142.57 ("also known as 014142-57")
# PeproTech (Thermo Fisher since 2021-12-30):
# - https://www.thermofisher.cn/order/catalog/product/AF-100-15-1MG (also -100UG, -500UG)
# - https://www.drugdiscoverytrends.com/thermo-fisher-scientific-completes-1-85b-peprotech-acquisition/
#
# MilliporeSigma / Merck KGaA (product number + "-" + package code with a unit):
# - https://scientificlabs.com/en/product/antibodies/A2220-2X25ML (A2220 1, 5, 10, 25 mL,
#   2 x 25 and 4 x 25 mL; https://www.sigmaaldrich.com/US/en/product/sigma/a2220)
# - https://scientificlabs.com/fr/product/antibodies/F1804-200UG (F1804-50UG/-200UG/-1MG/-5MG)
# - https://www.capitolscientific.com/Sigma-1805-1VL-Phosphate-Buffer-Solution-for-In-Vitro-Diagnostic-Use-Serum-Vial
# - https://www.capitolscientific.com/Sigma-1391-5X1ML-Sigma-Sil-A-for-TMS-Derivative-Preparation-Case-of-5-x-1mL-Vials
# - https://www.sigmaaldrich.com/product/sigma/a5441 (A5441 100 uL, 0.2 mL, 0.5 mL)
# - https://www.merckmillipore.com/JP/en/product/mm/05636i (05-636 and 05-636-I are separate
#   listings, so "-I" is not stripped); "-AF555" style suffixes are conjugates:
#   https://merckmillipore.com/AT/en/product/Anti-Actin-Antibody-clone-C4-Alexa-Fluor-555-Conjugate,MM_NF-MAB1501-AF555
# - https://www.merckmillipore.com/AR/es/product/Amicon-Ultra-15-Centrifugal-Filter-Unit,MM_NF-UFC901024
#   (UFC901008 8 pk, UFC901024 24 pk, UFC901096 96 pk; the "D" codes are IVD versions)
# - https://sigmaaldrich.com/product/mm/100983 (EMSURE 1.00983.1000 1 L, .2500 2.5 L, .5000 5 L)
#
# New England Biolabs (letter + 4 digits + size/concentration letter):
# - https://www.neb.com/en-us/products/r0101-ecori (R0101S/T/L/M)
# - https://www.neb.com/products/m0202-t4-dna-ligase (M0202S/T/L/M)
# - https://www.neb.com/en/e2621 (E2621S/L/X); https://www.neb.com/en-ca/products/r0138-sali
# - https://www.neb.com/en-us/nebnow/johns-hopkins-university?pdf=true (C2987H, C2987I)
# Cell Signaling Technology (number + size letter; carrier-free versions get new numbers):
# - https://www.cellsignal.cn/products/2118 (2118T 20 uL, 2118S 100 uL, 2118L 300 uL)
# - https://rrid.site/resolver/RRID:AB_2223172 (4970S, 4970P, 4970L)
# - https://b2b-cn.vwr.com/store/product/45901650/anti-rabbit-igg-goat-antibody-hrp (7074S/P2/V)
# - https://cellsignal.com/products/primary-antibodies/gapdh-14c10-rabbit-mab-bsa-and-azide-free/85925
# Takara Bio (product code + package letter; Takara Bio USA 6-digit numbers have no suffix):
# - https://www.takarabio.com/documents/Certificate%20of%20Analysis/RR036A/RR036A.pdf (200 rxn;
#   RR036B 800 rxn, RR036Q sample size: https://takara.co.kr/file/manual/pdf/RR036A_e.v2202Da.pdf)
# - https://www.takara.co.kr/file/manual/pdf/RR820L_e.v2112Da.pdf (RR820L, RR820W = RR820A)
# - https://takarabio.com/assets/a/112472 (639650 100 rxn vs 638933 500 rxn: separate numbers)
# Bio-Techne (R&D Systems, Tocris, Novus; unified under the R&D Systems brand in 2026):
# - https://www.rndsystems.com/Products/236-eg (236-EG-200, 236-EG-01M)
# - https://www.rndsystems.com/product_results.aspx?k=350-NS (-010/CF, -050/CF; CF = no BSA,
#   a different formulation, so "/CF" stays in the base)
# - https://www.fishersci.com/shop/products/mouse-cd28-antibody-r-d-systems-3/MAB4832SP (SP 25 ug)
# - https://www.fishersci.com/shop/products/human-sparc-antibody-r-2/MAB941100 (MAB941-100/-500)
# - https://rndsystems.com/products/dy206 (DY206 15 plates, DY206-05 5 plates)
# - Tocris 1254/1, 1254/10 and 1254/50 (supplier numbers on Fisher's listing):
#   https://www.fishersci.com/shop/products/y-27632-dihydrochloride-tocris-3/125410
# - https://resources.rndsystems.com/bio-techne-assets/docs/documents/rndsystems-brand-notification-apr-2026.pdf
# Fisher Scientific own brands (Fisher BioReagents, Fisher Chemical: product + "-" + size):
# - https://www.fishersci.com/shop/products/p/BP1521 (BP152-500, -1, -5, -10, -25)
# - https://www.fishersci.com/shop/products/acetone-certified-acs-fisher-chemical-12/A18500
#   (A18-500, -1, -4, -20; A18P-4 and A18S-4 are other containers and keep their letter)
# Bio-Rad ("BUN" = bundle of the same item):
# - https://www.bio-rad.com/en-us/sku/1708890-iscript-cdna-synthesis-kit-25-x-20-ul-rxns
#   (1708890 25 rxn, 1708891 100 rxn, 1708891BUN 5 x 1708891)
# Cytiva (HyClone ".NN" pack codes; legacy GE "17-0756-01" style codes):
# - https://www.geneseesci.com/product/hyclone-fetal-bovine-serum-us-characterized/
#   (SH30071.02 100 mL, SH30071.03 500 mL; "HI" heat-inactivated variants stay in the base)
# - https://www.govsci.com/product-detail/Cytiva/17075601/EA (17-0756-01 10 mL, -05 100 mL,
#   -04 300 mL; https://perfqws.sigmaaldrich.com/US/en/product/sigma/ge17075605)
# Cayman, Zymo, Selleck, MedChemExpress (item number + "-" + amount):
# - https://www.thomassci.com/p/y-27632-hydrochloride (Cayman 10005583-1, -5, -10, -50 mg)
# - https://www.thomassci.com/p/dna/rna-shield (Zymo R1100-50 50 mL, R1100-250 250 mL)
# - https://labshake.com/product/eb5c1251-5b3e-4a29-9431-2cc99559a979/-Y-27632-2HCl (S1049-2mg)
# - https://test.insightbio.com/productinfo/HY-10583-10mg/MedChemexpress%20LLC (HY-10583-10mg;
#   HY-10583G is the GMP grade and HY-10583R a standard: letters stay in the base)
#
# No pack rule: each size has its own number, or suffixes name other products.
# - Santa Cruz: https://www.scbt.com/p/gapdh-antibody-0411 (sc-47724 AC/HRP/FITC conjugates);
#   https://labroots.com/companies/santa-cruz-biotechnology/products/sc-29297-V (-V, -SH)
# - Abcam: https://www.abcam.com/en-us/products/primary-antibodies/gapdh-antibody-6c5-loading-control-ab8245
# - Qiagen: https://www.govsci.com/product-detail/Qiagen/74106/PK (74104 50 / 74106 250 preps)
# - Promega: https://promega.com/products/part?id=G7570 (G7570/G7571/G7572/G7573)
# - Bio-Rad iScript 1708890/1708891 above; BioLegend 100201/100202 (purified) vs 100203/100204
#   (FITC): https://punchout.biolegend.com/de-de/products/purified-anti-mouse-cd3-antibody-48
# - BD: https://www.fishersci.com/shop/products/bd-annexin-v-pe-4/BDB556421 (556421/556422/560930)
# - Miltenyi: https://mediray.co.nz/laboratory/molecular-and-microbiology-reagents/cell-biology/cell-separation/cd14-microbeads-human
# - STEMCELL: https://www.stemcell.com/products/mtesr1.html (85850, 85857, 85870, 85875)
# - IDT: https://sg.idtdna.com/page/products/crispr-genome-editing/alt-r-crispr-enzymes/cas9-protein
# - Eppendorf: https://www.govsci.com/product-detail/Eppendorf/022363204/EA (vs 0030120086)
# - Roche: https://www.sigmaaldrich.com/JP/en/product/roche/coro (11697498001 vs 11836145001)
# - Corning: https://ecatalog.corning.com/life-sciences/b2b/US/en/p/3516 (3516 vs 3506)
# - Jackson ImmunoResearch: https://antibodyregistry.org/AB_2313567 (111-035-003; -045 differs)
# - Proteintech: https://www.ptglab.com/products/10494-1-AP (20 uL and 150 uL, one number)
# - Illumina: https://www.illumina.com/products/nextera_xt_dna_library_prep_kit.html
# - Agilent: https://www.agilent.com/store/en_US/Prod-5067-4626/5067-4626
# - Twist: https://www.govsci.com/product-detail/Twist--Bioscience/104207/EA
# - GenScript: https://www.genscript.com/gsfiles/catalog/Recombinant-Proteins.pdf
# - Lonza: https://caltagmedsystems.co.uk/information/?p=6565 (BE12-604F vs BE12-604F/U1)
# - Dharmacon: https://www.funakoshi.co.jp/domestic/news/231101pdf/231101_p09.pdf ("-0005" is
#   5 nmol; other size codes were not confirmed)
# - ATCC: one size of everything, but a dot between digits names a derivative line, so its rule
#   keeps the dot (DOTTED_RULES): https://www.atcc.org/products/all/CCL-2.2.aspx (HeLa S3) vs
#   https://www.atcc.org/products/ccl-22 (MDBK)
#
# Brand -> company mapping used by data/vendors.yaml:
# - Bio-Techne brands: https://bio-techne.com/brands
# - Avantor bought VWR (J.T.Baker, Macron): https://www.biospace.com/avantor-completes-acquisition-of-vwr
# - Abcam bought BioVision (2021), Danaher bought Abcam (2023):
#   https://www.sec.gov/Archives/edgar/data/1492074/000119312521308633/d169156dex991.htm
#   https://www.biospace.com/danaher-completes-acquisition-of-abcam
# - BioLegend is part of Revvity (ex PerkinElmer): https://en.wikipedia.org/wiki/BioLegend
# - Thermo Fisher bought Olink (2024): https://www.businesswire.com/news/home/20240710235089/en
# - Falcon plates labelled "Corning (Formerly BD)":
#   https://bcf.technion.ac.il/wp-content/uploads/2024/04/IncuCyte_Vessel_Support_2016B_Release_I4t4SDT.pdf
# - Quartzy sells as a vendor itself (Quartzy Shop): https://quartzy.com/tour/quartzy-shop
# - USA Scientific distributes Eppendorf, it is not owned by it:
#   https://app.scientist.com/providers/usa-scientific-inc
# - Excel drops leading zeros and rounds long numbers into scientific notation:
#   https://support.microsoft.com/en-us/office/keep-leading-zeros-and-large-numbers-1bf7b935-36e1-4985-842f-5dfa51f85fe7
"""Per-vendor-family pack rules for catalog numbers.

A rule takes a cleaned catalog number (separators still in place, any case) and returns
``(base, pack_code)`` when it recognises a pack suffix, or None. The caller normalizes
``base`` exactly like the catalog key, so separators left in it don't matter. Under the rules
in ``DOTTED_RULES`` a dot between two digits is part of the number, in the key as well.
"""

import re
from collections.abc import Callable

Rule = Callable[[str], tuple[str, str | None] | None]


def _rule(*patterns):
    """Build a rule from regexes with ``base`` and ``pack`` groups.

    An optional ``variant`` group (e.g. R&D's "/CF") names a different formulation: it is
    appended to the base so it never merges with the plain product.
    """
    compiled = [re.compile(pattern) for pattern in patterns]

    def rule(cleaned):
        text = cleaned.strip().upper()
        for pattern in compiled:
            match = pattern.fullmatch(text)
            if match:
                base = match["base"] + (match.groupdict().get("variant") or "")
                return base, re.sub(r"\s+", "", match["pack"])
        return None

    return rule


def _amount(units):
    """An amount with a unit, optionally multi-pack: 100ML, .2ML, 2.5KG, 4X25ML, 1 ML."""
    return rf"(?:\d+X)?(?:\d+(?:\.\d+)?|\.\d+)\s?(?:{units})"


_SIGMA_UNITS = (
    "KG|MG|UG|NG|G|ML|UL|L|EA|VL|PAK|PK|CS|KT|KIT|RXN|UN|KU|MU|TST|TEST|SET|BOX|RL|PC|NMOL|UMOL"
)
_MASS_VOLUME = "KG|MG|UG|G|ML|UL"

RULES: dict[str, Rule] = {
    "thermo": _rule(
        # Gibco/Invitrogen 11965-092 or 11965092 (China SKUs add "CN").
        r"(?P<base>\d{5})[-\s]?(?P<pack>\d{3}(?:CN)?)",
        # The same number as Fisher US writes it: 11-965-092.
        r"(?P<base>\d{2}-\d{3})-(?P<pack>\d{3})",
        r"(?P<base>A\d{5})-?(?P<pack>\d{2})",  # A3160401
        r"(?P<base>L3000)(?P<pack>\d{3})",  # Lipofectamine 3000
        r"(?P<base>\d{2,3}-\d{4})-(?P<pack>\d{2})",  # eBioscience 16-0037-81
        r"(?P<base>[A-Z0-9]\d{5})[.-](?P<pack>[A-Z0-9]{2})",  # ex Alfa Aesar 031324.22
        rf"(?P<base>(?:AF-|GMP-?)?\d{{3}}-\d{{2,3}}[A-Z]?)-(?P<pack>{_amount('UG|MG')})",
    ),
    "merck": _rule(
        rf"(?P<base>[A-Z0-9]+(?:-[A-Z0-9]+)*?)(?:\s*-\s*|\s+)(?P<pack>{_amount(_SIGMA_UNITS)})",
        r"(?P<base>UFC\d{4})(?P<pack>08|24|96)",  # Amicon Ultra
        r"(?P<base>1\.?\d{5})\.?(?P<pack>\d{4})",  # Merck chemicals 1.00983.2500
    ),
    "neb": _rule(r"(?P<base>[A-Z]\d{4})[-\s]?(?P<pack>[A-Z])"),
    "cst": _rule(r"(?P<base>\d{3,6})[-\s]?(?P<pack>SF|S|L|T|P\d?|V)"),
    "takara": _rule(r"(?P<base>[A-Z]{1,2}\d{3,4})(?P<pack>[A-Z])"),
    "biotechne": _rule(
        r"(?P<base>\d{2,5}-[A-Z]{1,4}\d?)-(?P<pack>\d{3}|\d{2}M)(?P<variant>/CF)?",  # 236-EG-200
        # Antibodies: AF1050-SP, MAB4832SP, MAB941-100. Digit packs need the hyphen.
        r"(?P<base>(?:AF|BAF|MAB|BAM|FAB|IC)\d{3,5}[A-Z]?)-?(?P<pack>SP)",
        r"(?P<base>(?:AF|BAF|MAB|BAM|FAB|IC)\d{3,5}[A-Z]?)-(?P<pack>\d{3}|\d{2,3}UG)",
        r"(?P<base>DY\d{3,5}[A-Z]?)-(?P<pack>\d{2})",  # DuoSet DY206-05
        r"(?P<base>\d{3,5})/(?P<pack>\d{1,3})",  # Tocris 1254/10
    ),
    "fisher": _rule(r"(?P<base>[A-Z]{1,3}\d{1,5}[A-Z]{0,2})-(?P<pack>\d{1,4})"),
    "biorad": _rule(r"(?P<base>\d{3}-?\d{4})(?P<pack>BUN)"),
    "cytiva": _rule(
        r"(?P<base>SH\d{5})\.(?P<pack>\d{2})(?P<variant>HI)?",
        r"(?:GE)?(?P<base>\d{2}-\d{4})-(?P<pack>\d{2})",
    ),
    "cayman": _rule(rf"(?:CAY)?(?P<base>\d{{4,8}})-(?P<pack>\d{{1,4}}(?:\s?(?:{_MASS_VOLUME}))?)"),
    "zymo": _rule(r"(?P<base>[A-Z]\d{4})-(?P<pack>\d{2,4})"),
    "selleck": _rule(rf"(?P<base>S\d{{4}})-(?P<pack>{_amount(_MASS_VOLUME)})"),
    "mce": _rule(rf"(?P<base>HY-[A-Z]?\d+[A-Z]?)-(?P<pack>{_amount(_MASS_VOLUME)})"),
    "atcc": _rule(),  # no pack sizes; the rule is there for DOTTED_RULES
}

# Rules under which a dot between two digits stays in the key and base, because it tells
# products apart: ATCC CCL-2.2 (HeLa S3) is "CCL2.2", CCL-22 (MDBK) is "CCL22".
DOTTED_RULES = frozenset({"atcc"})
