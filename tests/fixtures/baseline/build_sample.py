"""Writes the synthetic Quartzy export that the baseline tests and notebook run on.

    uv run --no-sync python -m tests.fixtures.baseline.build_sample

export-sample/ has the shape of ``scripts/quartzy_probe.py export`` output; ui-requests.csv
stands in for a Quartzy UI "All Time" requests export with status dates (its real column
names aren't known yet). Every person, id and price is made up. tests/guard/test_baseline.py
checks that the committed files match this script: edit the tables, then re-run it.
"""

import csv
import io
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from apps.quartzy.money import infer_money_unit

HERE = Path(__file__).resolve().parent
EXPORT_DIR = HERE / "export-sample"
UI_CSV = HERE / "ui-requests.csv"
EXPORTED_AT = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
_NAMESPACE = uuid.UUID("6b1e6f0e-5c1f-4a5e-9a51-3f0d6c9b7a10")

PEOPLE = {
    "priya": ("Priya", "Patel"),
    "raj": ("Raj", "Kumar"),
    "lee": ("Lee", "Chen"),
    "sam": ("Sam", "Ortiz"),
    "dana": ("Dana", "Fox"),
    "alex": ("Alex", "Kim"),
    "morgan": ("Morgan", "Hale"),  # the lab manager: only ever created_by, for someone else
}
LABS = {"main": "Smith Lab", "teaching": "Smith Lab Teaching"}
TYPES = {
    "ab": "Antibodies",
    "cc": "Cell Culture",
    "mb": "Molecular Biology",
    "ch": "Chemicals",
    "kit": "Kits",
    "sup": "Lab Supplies",
    "oli": "Oligos",
    "svc": "Services",
}

# fmt: off
REQUESTS = [
    # n, requested, requester, type, vendor, catalog #, item, unit price, status
    # same_number_same_lab_open: #1 is still ORDERED (#2's total includes $15 shipping).
    (1, "2026-03-02", "priya", "ab", "CST", "2118S", "GAPDH (14C10) Rabbit mAb", "289.00",
     "ORDERED"),
    (2, "2026-03-20", "raj", "ab", "CST", "2118S", "GAPDH (14C10) Rabbit mAb #2118", "289.00",
     "RECEIVED"),
    # unknown_open_state without status dates; the UI export shows #3 arrived after #4.
    (3, "2026-01-05", "priya", "mb", "NEB", "R0101S", "EcoRI - 10,000 units", "79.00", "RECEIVED"),
    (4, "2026-01-15", "sam", "mb", "New England Biolabs", "R0101S", "EcoRI", "79.00", "RECEIVED"),
    # different_quartzy_labs (#5 is in the teaching Lab), then #7 pairs with the open #6.
    (5, "2025-11-03", "lee", "ab", "Abcam", "ab8245", "Anti-GAPDH antibody [6C5]", "395.00",
     "RECEIVED"),
    (6, "2025-11-10", "dana", "ab", "abcam", "ab8245", "GAPDH antibody 6C5", "395.00", "ORDERED"),
    (7, "2025-11-20", "sam", "ab", "Abcam", "ab8245", "Anti-GAPDH [6C5]", "395.00", "CREATED"),
    # format_variant: one key typed two ways.
    (8, "2026-02-02", "sam", "cc", "Gibco", "11965-092", "DMEM, high glucose", "34.50", "CREATED"),
    (9, "2026-02-10", "raj", "cc", "Thermo Fisher", "11965092", "DMEM high glucose 500 mL", "34.50",
     "RECEIVED"),
    # pack_variant: Thermo 15140 in two pack sizes.
    (10, "2026-04-01", "priya", "cc", "Gibco", "15140-122", "Penicillin-Streptomycin, 100 mL",
     "28.00", "RECEIVED"),
    (11, "2026-04-15", "lee", "cc", "Thermo Fisher", "15140148", "Penicillin-Streptomycin, 20 mL",
     "61.00", "CREATED"),
    # received_earlier: #12 is RECEIVED and 81 days older than #13.
    (12, "2025-12-01", "dana", "ab", "Sigma", "A2220-5ML", "ANTI-FLAG M2 Affinity Gel", "612.00",
     "RECEIVED"),
    (13, "2026-02-20", "priya", "ab", "Sigma-Aldrich", "A2220-5ML", "Anti-FLAG M2 affinity gel",
     "612.00", "ORDERED"),
    # pack_variant with no price: #44 has neither a unit price nor a total.
    (44, "2026-03-10", "lee", "ab", "Sigma", "A2220-10ML", "ANTI-FLAG M2 Affinity Gel, 10 mL", None,
     "CREATED"),
    # other_vendor_equivalent: the Gibco number bought through Fisher.
    (14, "2026-05-04", "sam", "cc", "Gibco", "25200-056", "Trypsin-EDTA (0.25%), phenol red",
     "22.00", "CREATED"),
    (15, "2026-05-20", "dana", "cc", "Fisher Scientific", "25-200-056", "Gibco Trypsin-EDTA 0.25%",
     "23.10", "RECEIVED"),
    # Not T1x: a key under 7 characters, then one whose types and names don't agree.
    (16, "2026-06-02", "raj", "ab", "CST", "9101S", "Phospho-p44/42 MAPK (Erk1/2) Antibody",
     "330.00", "RECEIVED"),
    (17, "2026-06-09", "lee", "ab", "Santa Cruz", "9101S", "MEK-1 antibody (H-8)", "290.00",
     "RECEIVED"),
    (18, "2025-09-10", "alex", "kit", "Bio-Rad", "1708891", "iScript cDNA Synthesis Kit, 100 rxn",
     "410.00", "RECEIVED"),
    (19, "2025-09-24", "sam", "sup", "Sarstedt", "1708891", "Serological pipettes 10 mL, case",
     "96.00", "RECEIVED"),
    # unknown_open_state without status dates; unknown with them (#20 arrived 7 days before #21).
    (20, "2026-06-01", "lee", "mb", "Takara", "RR036A", "PrimeScript RT Master Mix", "255.00",
     "RECEIVED"),
    (21, "2026-06-12", "raj", "mb", "Takara Bio", "RR036A",
     "PrimeScript RT Master Mix (Perfect Real Time)", "255.00", "CREATED"),
    # Cancelled requests never pair: first the earlier one, then the later one.
    (22, "2026-07-01", "priya", "ab", "CST", "4970S", "beta-Actin (13E5) Rabbit mAb", "325.00",
     "CANCELLED"),
    (23, "2026-07-05", "raj", "ab", "CST", "4970S", "beta-Actin (13E5) Rabbit mAb", "325.00",
     "RECEIVED"),
    (24, "2026-07-10", "sam", "ab", "Abcam", "ab150077",
     "Goat Anti-Rabbit IgG H&L (Alexa Fluor 488)", "410.00", "RECEIVED"),
    (25, "2026-07-12", "dana", "ab", "Abcam", "ab150077", "Goat anti-rabbit AF488", "410.00",
     "CANCELLED"),
    # Same-requester repeats under three Thermo brand names.
    (26, "2026-01-10", "priya", "mb", "Invitrogen", "AM9937",
     "Nuclease-Free Water (not DEPC-Treated)", "57.00", "RECEIVED"),
    (27, "2026-02-15", "priya", "mb", "Thermo Fisher", "AM9937", "Nuclease-free water", "57.00",
     "RECEIVED"),
    (28, "2026-03-25", "priya", "mb", "Ambion", "AM9937", "Nuclease free water", "57.00",
     "CREATED"),
    # Outside the 90-day window.
    (29, "2025-06-02", "sam", "kit", "Promega", "G7570",
     "CellTiter-Glo Luminescent Cell Viability Assay", "486.00", "RECEIVED"),
    (30, "2025-12-01", "lee", "kit", "Promega", "G7570", "CellTiter-Glo", "486.00", "RECEIVED"),
    # Uncheckable: placeholders, an Excel-rounded number and a missing catalog #.
    (31, "2026-03-03", "raj", "sup", "VWR", "N/A", "Kimwipes", "45.00", "RECEIVED"),
    (32, "2026-03-04", "dana", "sup", "VWR", "N/A", "Kimwipes", "45.00", "RECEIVED"),
    (33, "2026-08-11", "lee", "kit", "QIAGEN", "TBD", "RNeasy Mini Kit (50)", "590.00", "APPROVED"),
    (34, "2026-08-20", "alex", "sup", "Corning", "3.5207E+05", "Falcon 50 mL conical tubes",
     "152.00", "RECEIVED"),
    (35, "2026-09-01", "sam", "svc", "Genewiz", None, "Sanger sequencing (96 samples)", "240.00",
     "RECEIVED"),
    # A generic key: custom oligos that share one "catalog number".
    (36, "2026-08-03", "priya", "oli", "IDT", "DNA-OLIGO-25NM", "GAPDH qPCR primer F", "6.50",
     "RECEIVED"),
    (37, "2026-08-05", "raj", "oli", "IDT", "DNA-OLIGO-25NM", "GAPDH qPCR primer R", "6.50",
     "RECEIVED"),
    (38, "2026-08-19", "lee", "oli", "IDT", "DNA-OLIGO-25NM", "KRAS sgRNA top strand", "7.20",
     "RECEIVED"),
    (39, "2026-09-02", "sam", "oli", "IDT", "DNA-OLIGO-25NM", "ACTB qPCR primer F", "6.50",
     "RECEIVED"),
    (40, "2026-09-30", "priya", "oli", "IDT", "DNA-OLIGO-25NM", "TP53 sequencing primer", "6.80",
     "BACKORDERED"),
    # An unlisted vendor; #41 has only created_by, #42 no total, #43 no requester at all.
    (41, "2026-09-08", "raj", "ch", "Acme Reagents", "AC-1001", "Acme lysis buffer", "120.00",
     "CREATED"),
    (42, "2026-09-15", "sam", "ch", "Acme Reagents Inc.", "AC1001", "Lysis buffer (Acme)", "120.00",
     "CREATED"),
    (43, "2026-09-20", None, "ch", "Acme Reagents", "AC-1001", "Acme lysis buffer", "120.00",
     "CREATED"),
    # No vendor named: blank and placeholder vendors share the "unknown" family, where a
    # shared key only counts like T1x. 2118S is too short for that, so these three are
    # uncheckable and make no pairs.
    (47, "2026-07-06", "lee", "ab", "N/A", "2118S", "GAPDH (14C10) Rabbit mAb", "289.00",
     "RECEIVED"),
    (48, "2026-07-13", "dana", "ab", "", "2118S", "GAPDH (14C10) Rabbit mAb", "289.00", "ORDERED"),
    (49, "2026-07-17", "alex", "ab", "Unknown", "2118S", "GAPDH (14C10) Rabbit mAb", "289.00",
     "CREATED"),
    # ...a key long enough for T1x pairs, with the same Quartzy type. Morgan entered #50 for
    # Priya and named her in it: the anonymized summary must not show that.
    (50, "2026-05-11", "priya", "cc", "N/A", "10270-106",
     "Fetal Bovine Serum, qualified - for Priya's organoids", "560.00", "RECEIVED"),
    (51, "2026-05-25", "dana", "cc", "TBD", "10270106", "FBS, qualified, 500 mL", "560.00",
     "CREATED"),
    # A pasted link is too long to be a catalog #.
    (52, "2026-08-26", "sam", "sup", "Amazon",
     "https://www.amazon.com/dp/B07Q3XGZ8M/ref=sr_1_3?crid=2X9QZ1&keywords=pasteur+pipettes+9+inch",
     "Pasteur pipettes, 9 in, 1000/pk", "38.00", "RECEIVED"),
    # Two identical requests on one day: the UI export can't tell them apart.
    (45, "2026-03-04", "alex", "sup", "VWR", "N/A", "Kimwipes", "45.00", "RECEIVED"),
    (46, "2026-03-04", "alex", "sup", "VWR", "N/A", "Kimwipes", "45.00", "RECEIVED"),
]
# fmt: on

EXTRAS = {
    2: {"shipping_cents": 1500},
    5: {"lab": "teaching"},
    8: {"quantity": "6"},
    9: {"quantity": "6"},
    10: {"quantity": "4"},
    14: {"quantity": "5"},
    15: {"quantity": "5"},
    26: {"quantity": "2"},
    27: {"quantity": "2"},
    28: {"quantity": "2"},
    30: {"bare_references": True},  # lab and type names come from labs.json / types.json
    31: {"quantity": "4"},
    32: {"quantity": "4"},
    41: {"requested_by": None},
    42: {"total_price": None},
    43: {"requested_by": None, "created_by": None},
    45: {"no_link": True},
    46: {"no_link": True},
    50: {"created_by": "morgan"},
}
# Status-change dates in the UI export where the defaults (ordered the next day, received
# after 9 days, cancelled after 2) would not show the case.
UI_DATES = {
    3: {"Date Received": "2026-01-19"},
    12: {"Date Ordered": "2025-12-02", "Date Received": "2025-12-12"},
    20: {"Date Received": "2026-06-05"},
}
UI_COLUMNS = (
    "Item Name",
    "Vendor",
    "Catalog #",
    "Requested By",
    "Lab",
    "Price",
    "Date Requested",
    "Date Approved",
    "Date Ordered",
    "Date Received",
    "Date Cancelled",
    "Link",
)
UI_COLUMN_MAP = {
    "id": "Link",
    "requested_at": "Date Requested",
    "approved_at": "Date Approved",
    "ordered_at": "Date Ordered",
    "received_at": "Date Received",
    "cancelled_at": "Date Cancelled",
    "lab": "Lab",
    "item_name": "Item Name",
    "catalog_number": "Catalog #",
    "vendor_name": "Vendor",
    "requester": "Requested By",
    "price": "Price",
}
# A request deleted from Quartzy since: in the UI export only.
UI_UNMATCHED = {
    "Item Name": "Centrifuge rotor service",
    "Vendor": "Beckman Coulter",
    "Catalog #": "SVC-ROTOR",
    "Requested By": "Pat Owner",
    "Lab": "Smith Lab",
    "Price": "350.00",
    "Date Requested": "02/01/2024",
    "Date Received": "02/15/2024",
}


def uid(kind, name):
    return str(uuid.uuid5(_NAMESPACE, f"{kind}/{name}"))


def request_id(n):
    return uid("request", n)


def requested_at(n, day):
    # Spread over the working day so that no two requests share a timestamp.
    return datetime.fromisoformat(day).replace(hour=8 + n % 9, minute=7 * n % 60, tzinfo=UTC)


def person(name):
    if name is None:
        return None
    first, last = PEOPLE[name]
    email = f"{first}.{last}@example.edu".lower()
    return {"id": uid("user", name), "first_name": first, "last_name": last, "email": email}


def display_name(name):
    return " ".join(PEOPLE[name]) if name else ""


def lab(key):
    organization = {"id": uid("organization", "example"), "name": "Example University"}
    return {
        "id": uid("lab", key),
        "name": LABS[key],
        "uses_instances": True,
        "organization": organization,
    }


def type_ref(key, lab_key):
    return {"id": uid("type", f"{lab_key}/{key}"), "name": TYPES[key]}


def money(unit_price, quantity, money_unit, shipping_cents=0):
    """A Quartzy Money as the fake writes it: integer cents, or dollars with 2 decimals."""
    if unit_price is None:
        return None
    cents = (Decimal(unit_price) * Decimal(quantity) * 100).quantize(Decimal(1)) + shipping_cents
    amount = str(cents) if money_unit == "minor" else str((cents / 100).quantize(Decimal("0.01")))
    return {"amount": amount, "currency": "USD"}


def order_request(row, money_unit):
    n, day, who, type_key, vendor, catalog, item, unit_price, status = row
    extra = EXTRAS.get(n, {})
    lab_key = extra.get("lab", "main")
    at = requested_at(n, day)
    quantity = extra.get("quantity", "1")
    the_lab, the_type = lab(lab_key), type_ref(type_key, lab_key)
    if extra.get("bare_references"):
        the_lab, the_type = {"id": the_lab["id"]}, {"id": the_type["id"]}
    total = extra.get(
        "total_price", money(unit_price, quantity, money_unit, extra.get("shipping_cents", 0))
    )
    return {
        "id": request_id(n),
        "app_url": f"https://app.quartzy.com/groups/{the_lab['id']}/requests?id={request_id(n)}",
        "status": status,
        "vendor_name": vendor,
        "catalog_number": catalog,
        "quantity": quantity,
        "unit_size": "1 ea",
        "unit_price": unit_price,
        "total_price": total,
        "requested_at": at.isoformat(),
        "updated_at": (at if status == "CREATED" else at + timedelta(days=2)).isoformat(),
        "requested_by": person(extra.get("requested_by", who)),
        "created_by": person(extra.get("created_by", who)),
        "notes": None,
        "item_name": item,
        "invoice_number": None,
        "requisition_number": None,
        "confirmation_number": None,
        "tracking_number": None,
        "purchase_order_number": None,
        "shipping_and_handling": None,
        "details": None,
        "lab": the_lab,
        "type": the_type,
        "spend_tracking_code": None,
        "backordered_expected_at": None,
        "is_urgent": False,
    }


def ui_row(row):
    n, day, who, _, vendor, catalog, item, unit_price, status = row
    extra = EXTRAS.get(n, {})
    at = requested_at(n, day)
    dates = {}
    if status in {"ORDERED", "BACKORDERED", "RECEIVED"}:
        dates["Date Ordered"] = at + timedelta(days=1)
    if status == "RECEIVED":
        dates["Date Received"] = at + timedelta(days=9)
    if status == "CANCELLED":
        dates["Date Cancelled"] = at + timedelta(days=2)
    dates = {name: f"{moment:%m/%d/%Y}" for name, moment in dates.items()}
    for name, iso_day in UI_DATES.get(n, {}).items():
        dates[name] = f"{datetime.fromisoformat(iso_day):%m/%d/%Y}"
    requester = extra.get("requested_by", who) or extra.get("created_by", who)
    link = "" if n % 2 == 0 or extra.get("no_link") else order_request(row, "minor")["app_url"]
    return {
        "Item Name": item,
        "Vendor": vendor,
        "Catalog #": catalog or "",
        "Requested By": display_name(requester),
        "Lab": LABS[extra.get("lab", "main")],
        "Price": unit_price or "",
        "Date Requested": f"{at:%m/%d/%Y}",
        **dates,
        "Link": link,
    }


def export_files(money_unit="minor", rows=REQUESTS):
    """{file name: text} of an export directory."""
    requests = [order_request(row, money_unit) for row in rows]
    labs = [lab(key) for key in LABS]
    used = sorted({(EXTRAS.get(row[0], {}).get("lab", "main"), row[3]) for row in rows})
    types = [{**type_ref(key, lab_key), "lab": lab(lab_key)} for lab_key, key in used]
    manifest = {
        "synthetic": "tests/fixtures/baseline/build_sample.py",
        "started_at": EXPORTED_AT.isoformat().replace("+00:00", "Z"),
        "finished_at": (EXPORTED_AT + timedelta(minutes=3)).isoformat().replace("+00:00", "Z"),
        "base_url": "https://api.quartzy.com",
        "complete": True,
        # Both are the lab's own Quartzy labs; the baseline reads only the labs listed here.
        "lab_ids": [lab["id"] for lab in labs],
        "lab_ids_source": "QUARTZY_LAB_IDS",
        "counts": {
            "labs": len(labs),
            "types": len(types),
            "order_requests": len(requests),
            "inventory_items": 0,
        },
        "probe_facts": {
            "page_base": 1,
            "page_base_source": "probe report",
            "money_unit": money_unit,
            "money_unit_source": "probe report",
            "new_items_at": "first_page",
            "money_unit_counts": infer_money_unit(requests),
        },
    }

    def as_json(data):
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"

    return {
        "order_requests.jsonl": "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in requests),
        "inventory_items.jsonl": "",
        "labs.json": as_json(labs),
        "types.json": as_json(types),
        "manifest.json": as_json(manifest),
    }


def ui_csv_text():
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, UI_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows([*(ui_row(row) for row in REQUESTS), UI_UNMATCHED])
    return buffer.getvalue()


def write_export(folder, money_unit="minor", rows=REQUESTS):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in export_files(money_unit, rows).items():
        (folder / name).write_text(text, encoding="utf-8")
    return folder


def main():
    write_export(EXPORT_DIR)
    UI_CSV.write_text(ui_csv_text(), encoding="utf-8")
    print(f"Wrote {EXPORT_DIR} and {UI_CSV}")


if __name__ == "__main__":
    main()
