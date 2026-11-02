"""The Phase 0 baseline (apps.guard.baseline) on the synthetic export in tests/fixtures/baseline/.

build_sample.py says what each numbered request is there to show; N maps ids back to numbers.
"""

import csv
import io
import json
import re
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from apps.catalog.normalization import normalize_catalog
from apps.guard import baseline
from tests.fixtures.baseline import build_sample as sample

N = {sample.request_id(row[0]): row[0] for row in sample.REQUESTS}
NAME_WORDS = sorted({word for name in sample.PEOPLE.values() for word in name})
NOTEBOOK = Path(__file__).resolve().parents[2] / "notebooks" / "baseline_duplicates.ipynb"


@pytest.fixture(scope="module")
def records():
    return baseline.load_requests(sample.EXPORT_DIR)


@pytest.fixture(scope="module")
def generic(records):
    return baseline.generic_catalog_candidates(records)


@pytest.fixture(scope="module")
def result(records, generic):
    return baseline.find_clusters(records, generic_keys=generic)


def by_case(pairs):
    return {(N[pair.earlier.id], N[pair.later.id]): pair for pair in pairs}


def named_in(text):
    """The sample's first and last names found in ``text`` as whole words, in any case."""
    return [word for word in NAME_WORDS if re.search(rf"\b{word}\b", text, re.IGNORECASE)]


def test_committed_sample_matches_its_builder():
    rebuild = "out of date: run python -m tests.fixtures.baseline.build_sample"
    for name, text in sample.export_files().items():
        assert (sample.EXPORT_DIR / name).read_text(encoding="utf-8") == text, f"{name} {rebuild}"
    assert sample.UI_CSV.read_text(encoding="utf-8") == sample.ui_csv_text(), rebuild


def test_load_requests_reads_people_labs_types_and_vendors(records):
    by_n = {N[r.id]: r for r in records}
    assert len(records) == 52
    first = by_n[1]
    assert (first.requester, first.requester_email) == ("Priya Patel", "priya.patel@example.edu")
    assert first.requester_key == sample.uid("user", "priya")
    assert (first.vendor_family, first.catalog.key, first.catalog.base) == ("cst", "2118S", "2118")
    assert (first.lab_name, first.type_name, first.status) == ("Smith Lab", "Antibodies", "ORDERED")
    assert first.requested_at == datetime(2026, 3, 2, 9, 7, tzinfo=UTC)
    assert (first.price, first.currency, first.price_basis) == (Decimal("289.00"), "USD", "minor")
    # requested_by is null, so created_by stands in; with neither there is no requester.
    assert by_n[41].requester == "Raj Kumar" and by_n[41].requester_key == sample.uid("user", "raj")
    assert by_n[43].requester_key is None and by_n[43].requester == ""
    # Both people's names, and their full names, are kept for the anonymized summary to remove.
    assert first.person_names == ("Priya", "Patel", "Priya Patel")
    assert by_n[50].requester == "Priya Patel"
    assert by_n[50].person_names == (
        "Priya",
        "Patel",
        "Priya Patel",
        "Morgan",
        "Hale",
        "Morgan Hale",
    )
    assert by_n[43].person_names == ()
    # Unlisted vendors group under their normalized name, legal suffix dropped.
    assert by_n[41].vendor_family == by_n[42].vendor_family == "?acme reagents"
    # Blank and placeholder vendors ("N/A", "", "Unknown", "TBD") share the unknown family.
    assert {by_n[n].vendor_family for n in (47, 48, 49, 50, 51)} == {"unknown"}
    # Names come from labs.json and types.json when a request carries only ids.
    assert (by_n[30].lab_name, by_n[30].type_name) == ("Smith Lab", "Kits")


OUR_LABS = [sample.uid("lab", key) for key in sample.LABS]
JONES = {"id": sample.uid("lab", "jones"), "name": "Jones Lab (collaborator)"}


def with_a_collaborators_lab(folder, lab_ids):
    """The sample export plus three requests from another group's Quartzy lab that the token
    also sees, with a manifest that lists ``lab_ids`` (None: no list, as before the probe
    recorded one)."""
    sample.write_export(folder)
    zed = {"id": sample.uid("user", "zed"), "first_name": "Zed", "last_name": "Outsider"}
    yan = {"id": sample.uid("user", "yan"), "first_name": "Yan", "last_name": "Visitor"}
    theirs = [
        (101, "2026-04-02", zed, "MAB1234-SP", "Jones lab confidential target antibody", "999.00"),
        (102, "2026-04-20", yan, "MAB1234-SP", "Jones lab confidential target antibody", "999.00"),
        (103, "2025-11-15", zed, "ab8245", "Anti-GAPDH [6C5] (Jones)", "395.00"),
    ]
    with (folder / "order_requests.jsonl").open("a", encoding="utf-8") as handle:
        for n, day, who, catalog, item, price in theirs:
            row = (n, day, None, "ab", "R&D Systems", catalog, item, price, "RECEIVED")
            raw = {**sample.order_request(row, "minor"), "lab": JONES}
            handle.write(json.dumps({**raw, "requested_by": who, "created_by": who}) + "\n")
    labs = json.loads((folder / "labs.json").read_text(encoding="utf-8"))
    (folder / "labs.json").write_text(json.dumps([*labs, JONES]), encoding="utf-8")
    manifest = {k: v for k, v in baseline.read_manifest(folder).items() if k != "lab_ids"}
    if lab_ids is not None:
        manifest["lab_ids"] = lab_ids
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return folder


def test_a_collaborators_lab_stays_out_of_the_baseline(result, tmp_path):
    folder = with_a_collaborators_lab(tmp_path / "export", OUR_LABS)
    records = baseline.load_requests(folder)
    assert len(records) == 52 and JONES["id"] not in {r.lab_id for r in records}
    manifest = baseline.read_manifest(folder)
    candidates = baseline.generic_catalog_candidates(records)
    summary = baseline.summarize(
        baseline.find_clusters(records, generic_keys=candidates), manifest=manifest
    )
    assert summary == baseline.summarize(result, manifest=manifest)
    assert summary["upper_bound"]["amount"] == Decimal("3103.50")
    shown = json.dumps(summary, default=str)
    assert "Jones" not in shown and "confidential" not in shown
    labs = "Quartzy labs included: Smith Lab and Smith Lab Teaching."
    assert labs in " ".join(summary["assumptions"])


def test_an_export_that_does_not_say_which_labs_are_ours_is_refused(tmp_path):
    folder = with_a_collaborators_lab(tmp_path / "export", None)
    with pytest.raises(ValueError, match=r"3 Quartzy labs.*Jones Lab \(collaborator\)"):
        baseline.load_requests(folder)
    ours = [lab_id.upper() for lab_id in OUR_LABS]  # ids in any case
    assert len(baseline.load_requests(folder, lab_ids=ours)) == 52
    assert len(baseline.load_requests(folder, lab_ids=[JONES["id"]])) == 3
    with pytest.raises(ValueError, match="no Quartzy lab not-a-lab"):
        baseline.load_requests(folder, lab_ids=["not-a-lab", ours[0]])
    # As QUARTZY_LAB_IDS writes them, comma-separated; a blank id is no id.
    assert len(baseline.load_requests(folder, lab_ids=f" {ours[0]}, {ours[1]} ,")) == 52
    assert len(baseline.load_requests(folder, lab_ids=[*ours, "", " "])) == 52
    with pytest.raises(ValueError, match="3 Quartzy labs"):
        baseline.load_requests(folder, lab_ids=["", " "])
    # With one lab there is nothing to choose.
    alone = sample.write_export(tmp_path / "alone", rows=sample.REQUESTS[:2])
    (alone / "manifest.json").write_text(json.dumps({"money_unit": "minor"}), encoding="utf-8")
    assert len(baseline.load_requests(alone)) == 2


def test_prices_follow_the_manifest_unit_and_never_guess(records, tmp_path):
    by_n = {N[r.id]: r for r in records}
    assert by_n[2].price == Decimal("304.00")  # the total, shipping included
    assert (by_n[42].price, by_n[42].price_basis) == (Decimal("120.00"), "unit_price*quantity")
    assert (by_n[44].price, by_n[44].price_basis) == (None, "none")

    dollars = {N[r.id]: r for r in baseline.load_requests(sample.EXPORT_DIR, money_unit="major")}
    assert dollars[1].price == Decimal(28900)  # as told, however implausible

    auto = {N[r.id]: r for r in baseline.load_requests(sample.EXPORT_DIR, money_unit="auto")}
    assert (auto[1].price, auto[1].price_basis) == (Decimal("289.00"), "auto:minor")
    # 30400 fits neither 289.00 nor 28900, so the total is set aside rather than guessed.
    assert (auto[2].price, auto[2].price_basis) == (Decimal("289.00"), "unit_price*quantity")

    major = sample.write_export(tmp_path / "major", money_unit="major")
    in_dollars = {N[r.id]: r for r in baseline.load_requests(major)}
    assert (in_dollars[1].price, in_dollars[1].price_basis) == (Decimal("289.00"), "major")
    assert in_dollars[2].price == Decimal("304.00")
    pinned = {"money_unit": "auto", "lab_ids": OUR_LABS}
    (major / "manifest.json").write_text(json.dumps(pinned), encoding="utf-8")
    assert {N[r.id]: r for r in baseline.load_requests(major)}[1].price_basis == "auto:major"
    with pytest.raises(ValueError, match="money_unit"):
        baseline.load_requests(major, money_unit="cents")


def test_load_requests_refuses_broken_lines_and_warns_about_repeated_ids(tmp_path):
    folder = sample.write_export(tmp_path / "export")
    jsonl = folder / "order_requests.jsonl"
    first = jsonl.read_text(encoding="utf-8").splitlines()[0]
    jsonl.write_text(f"{first}\n\n{first}\n", encoding="utf-8")
    with pytest.warns(UserWarning, match="more than once"):
        assert len(baseline.load_requests(folder)) == 1
    jsonl.write_text(f"{first}\n{{not json\n", encoding="utf-8")
    with pytest.raises(ValueError, match=":2: not JSON"):
        baseline.load_requests(folder)


def test_generic_candidates_are_keys_with_many_item_names(records, generic):
    assert [(c.vendor_family, c.catalog_key, c.n_names) for c in generic] == [
        ("idt", "DNAOLIGO25NM", 5)
    ]
    assert "KRAS sgRNA top strand" in generic[0].examples
    assert baseline.generic_catalog_candidates(records, min_distinct_names=6) == []
    # Set the bar too low and a popular antibody typed three ways shows up: review by eye.
    loose = baseline.generic_catalog_candidates(records, min_distinct_names=3)
    assert ("abcam", "AB8245") in {(c.vendor_family, c.catalog_key) for c in loose}


def test_each_pair_gets_the_first_bucket_that_explains_the_miss(result):
    pairs = by_case(result.pairs)
    assert {case: (pair.tier, pair.bucket) for case, pair in pairs.items()} == {
        (1, 2): ("T1", "same_number_same_lab_open"),
        (3, 4): ("T1", "unknown_open_state"),
        (5, 6): ("T1", "different_quartzy_labs"),
        (6, 7): ("T1", "same_number_same_lab_open"),
        (8, 9): ("T1", "format_variant"),
        (10, 11): ("T2", "pack_variant"),
        (12, 13): ("T1", "received_earlier"),
        (13, 44): ("T2", "pack_variant"),
        (14, 15): ("T1x", "other_vendor_equivalent"),
        (20, 21): ("T1", "unknown_open_state"),
        (41, 42): ("T1", "format_variant"),
        (50, 51): ("T1x", "other_vendor_equivalent"),
    }
    assert (pairs[1, 2].earlier_state, pairs[1, 2].state_evidence) == ("open", "still open today")
    assert (pairs[12, 13].gap_days, pairs[12, 13].state_evidence) == (81.0, "age heuristic")
    assert pairs[1, 2].amount == Decimal("304.00") and pairs[13, 44].amount is None


def test_the_strict_reading_needs_status_dates_to_call_a_request_open(records, generic):
    strict = baseline.find_clusters(
        records, generic_keys=generic, infer_open_from_current_status=False
    )
    pairs = by_case(strict.pairs)
    assert pairs[1, 2].bucket == pairs[6, 7].bucket == "unknown_open_state"
    assert pairs[12, 13].bucket == "received_earlier"  # the age heuristic still applies
    assert "still open today" not in " ".join(baseline.summarize(strict)["assumptions"])


def test_a_later_request_pairs_with_the_request_quartzy_should_have_caught(result):
    pairs = by_case(result.pairs)
    # #7 also repeats #5, but #6 was open in the same Quartzy Lab under the same number.
    assert (5, 7) not in pairs and pairs[6, 7].bucket == "same_number_same_lab_open"
    assert pairs[5, 6].bucket == "different_quartzy_labs"


def test_cancelled_requests_never_pair(result):
    paired = {n for case in by_case(result.pairs) for n in case}
    assert not paired & {22, 23, 24, 25}
    assert result.excluded == {"cancelled": 2, "undated": 0, "no_requester": 1}


def test_same_requester_repeats_are_reported_separately(result):
    assert set(by_case(result.repeats)) == {(26, 27), (27, 28)}
    assert all(pair.same_requester and pair.bucket is None for pair in result.repeats)


def test_window_and_cross_family_limits(records, generic):
    pairs = by_case(baseline.find_clusters(records, generic_keys=generic).pairs)
    assert (29, 30) not in pairs  # 182 days apart
    assert (16, 17) not in pairs  # "9101S" is too short to compare across vendor families
    assert (18, 19) not in pairs  # "1708891": a cDNA kit and serological pipettes
    wide = by_case(baseline.find_clusters(records, generic_keys=generic, window_days=200).pairs)
    assert wide[29, 30].gap_days == 182.0


def test_requests_that_name_no_vendor_match_only_as_t1x_does(records, generic, result):
    by_n = {N[r.id]: r for r in records}
    # Three people asked for 2118S within two weeks, naming the vendor "N/A", "" and
    # "Unknown". Unnamed vendors may differ, and 2118S is too short to count across vendors.
    paired = {n for case in by_case(result.pairs) for n in case}
    assert not paired & {47, 48, 49}
    assert sorted(N[r.id] for r in result.uncheckable["no_vendor"]) == [47, 48, 49]
    # A key long enough for T1x pairs, with the bucket of a cross-family match...
    pair = by_case(result.pairs)[50, 51]
    assert (pair.tier, pair.bucket, pair.reasons) == (
        "T1x",
        "other_vendor_equivalent",
        ("other_vendor_equivalent",),
    )
    # ...unless an earlier bucket applies: the same number as typed, still open, one Lab.
    open_50 = replace(by_n[50], status="ORDERED")
    same_51 = replace(by_n[51], catalog=normalize_catalog("10270-106", "unknown"))
    swapped = [{50: open_50, 51: same_51}.get(N[r.id], r) for r in records]
    pair = by_case(baseline.find_clusters(swapped, generic_keys=generic).pairs)[50, 51]
    assert (pair.tier, pair.bucket) == ("T1x", "same_number_same_lab_open")
    # Like T1x, it needs the same Quartzy type or similar item names.
    filter_51 = replace(by_n[51], type_name="Lab Supplies", item_name="Bottle-top filter 0.22 um")
    swapped = [filter_51 if N[r.id] == 51 else r for r in records]
    assert (50, 51) not in by_case(baseline.find_clusters(swapped, generic_keys=generic).pairs)


def test_uncheckable_requests_are_counted_not_compared(result, records):
    uncheckable = {
        reason: sorted(N[r.id] for r in found) for reason, found in result.uncheckable.items()
    }
    assert uncheckable == {
        "placeholder": [31, 32, 33, 35, 45, 46],
        "excel_mangled": [34],
        "too_long": [52],  # a pasted link, not an Excel-damaged number
        "generic": [36, 37, 38, 39, 40],
        "no_vendor": [47, 48, 49],
    }
    assert result.compared == 33
    # Without the generic key, five custom oligos make four "duplicates".
    plain = baseline.find_clusters(records)
    assert sum(p.later.catalog.key == "DNAOLIGO25NM" for p in plain.pairs) == 4
    assert baseline.summarize(plain)["upper_bound"]["amount"] == Decimal("3130.50")
    for keys in ([("idt", "dna-oligo-25nm")], ["DNAOLIGO25NM"]):
        assert len(baseline.find_clusters(records, generic_keys=keys).uncheckable["generic"]) == 5


def test_generic_keys_are_keyed_the_way_requests_are(records):
    by_n = {N[r.id]: r for r in records}
    # ATCC's rule keeps the dot (CCL-2.2 is HeLa S3), so a candidate's own key must match...
    hela = replace(by_n[1], vendor_family="atcc", catalog=normalize_catalog("CCL-2.2", "atcc"))
    for keys in ([("atcc", "CCL2.2")], [("atcc", "ATCC CCL-2.2")]):
        found = baseline.find_clusters([hela], generic_keys=keys).uncheckable["generic"]
        assert found == (hela,), keys
    # ...and a key typed with its vendor prefix is the key requests get.
    found = baseline.find_clusters(records, generic_keys=[("cst", "CST #2118S")])
    assert sorted(N[r.id] for r in found.uncheckable["generic"]) == [1, 2]


def test_status_dates_from_the_ui_export_settle_the_open_state(records, generic):
    joined, join = baseline.attach_status_dates(records, sample.UI_CSV, sample.UI_COLUMN_MAP)
    assert join == baseline.StatusJoin(
        rows=53,
        by_id=25,
        by_fields=25,
        ambiguous=2,
        unmatched=1,
        bad_dates=0,
        date_order="month/day",
    )
    assert join.attached == 50
    pairs = by_case(baseline.find_clusters(joined, generic_keys=generic).pairs)
    assert pairs[3, 4].bucket == "same_number_same_lab_open"  # #3 arrived after #4 was made
    assert pairs[20, 21].bucket == "unknown"  # received only 7 days before
    assert pairs[12, 13].bucket == "received_earlier"
    assert pairs[12, 13].state_evidence == "status dates"
    # Two identical requests on one day can't be told apart, so neither gets dates.
    assert not any(r.has_status_dates for r in joined if N[r.id] in (45, 46))


def test_status_join_reads_day_first_dates_and_semicolons(records, tmp_path):
    def day_first(value):
        month, day, year = value.split("/")
        return f"{day}/{month}/{year}"

    path = tmp_path / "ui.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, sample.UI_COLUMNS, delimiter=";")
        writer.writeheader()
        for row in csv.DictReader(io.StringIO(sample.ui_csv_text())):
            writer.writerow(
                {k: day_first(v) if k.startswith("Date") and v else v for k, v in row.items()}
            )
    joined, join = baseline.attach_status_dates(records, path, sample.UI_COLUMN_MAP)
    assert (join.date_order, join.attached) == ("day/month", 50)
    assert {N[r.id]: r for r in joined}[3].received_at == datetime(2026, 1, 19, tzinfo=UTC)


def test_status_join_checks_its_column_mapping(records):
    with pytest.raises(ValueError, match="Unknown status columns"):
        baseline.attach_status_dates(records, sample.UI_CSV, {"recieved_at": "Date Received"})
    with pytest.raises(ValueError, match="no column"):
        baseline.attach_status_dates(records, sample.UI_CSV, {"received_at": "Received On"})
    with pytest.raises(ValueError, match="at least one"):
        baseline.attach_status_dates(records, sample.UI_CSV, {"id": "Link"})


def test_summary_reports_the_upper_bound_breakdowns_and_assumptions(result):
    summary = baseline.summarize(result, manifest=baseline.read_manifest(sample.EXPORT_DIR))
    assert summary["upper_bound"] == {
        "amount": Decimal("3103.50"),
        "pairs": 12,
        "unpriced_pairs": 1,
        "currency": "USD",
    }
    assert {name: row["pairs"] for name, row in summary["by_bucket"].items()} == {
        "same_number_same_lab_open": 2,
        "unknown_open_state": 2,
        "different_quartzy_labs": 1,
        "format_variant": 2,
        "pack_variant": 2,
        "received_earlier": 1,
        "other_vendor_equivalent": 2,
        "unknown": 0,
    }
    assert sum(row["amount"] for row in summary["by_bucket"].values()) == Decimal("3103.50")
    assert next(iter(summary["by_type"])) == "Antibodies"
    assert summary["by_type"]["Antibodies"] == {"pairs": 5, "amount": Decimal("1706.00")}
    assert list(summary["by_month"]) == sorted(summary["by_month"])
    assert summary["by_month"]["2025-11"] == {"pairs": 2, "amount": Decimal("790.00")}
    assert sum(row["pairs"] for row in summary["by_gap"].values()) == 12
    top = summary["top_products"][0]
    assert (top["catalog"], top["requests"], top["amount"]) == ("ab8245", 3, Decimal("790.00"))
    assert (top["people"], top["first"], top["last"]) == (3, "2025-11", "2025-11")
    assert summary["uncheckable"]["share"] == round(16 / 50, 3)
    assert summary["uncheckable"]["by_reason"] == {
        "placeholder": 6,
        "excel_mangled": 1,
        "too_long": 1,
        "generic": 5,
        "no_vendor": 3,
    }
    assert summary["same_requester_repeats"]["pairs"] == 2
    assert summary["same_requester_repeats"]["amount"] == Decimal("228.00")
    assumptions = " ".join(summary["assumptions"])
    for expected in (
        "complete",
        "within 90 days",
        "2 cancelled",
        "or on two requests that name no vendor",
        "read as minor units (cents), as configured: 50 requests",
        "1 request priced as unit_price x quantity",
        "1 request with a link or list pasted as the catalog #",
        "3 requests with no vendor and a catalog # too short",
        "idt DNAOLIGO25NM",
        "2 format_variant pairs (327.00)",
        "28 days earlier",
    ):
        assert expected in assumptions


def test_the_anonymized_summary_names_nobody(result, records):
    summary = baseline.summarize(result, manifest=baseline.read_manifest(sample.EXPORT_DIR))
    shown = json.dumps(summary, default=str)
    # Not a first or last name in any case, though #50 names Priya in its item name.
    assert named_in(shown) == []
    # No email address, id or link either, and not the day of any request: months only.
    assert "@" not in shown and "quartzy.com" not in shown
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-", shown)
    days = {f"{record.requested_at:%Y-%m-%d}" for record in records}
    assert [day for day in sorted(days) if day in shown] == []
    named = json.dumps(baseline.summarize(result, anonymize=False), default=str)
    assert "Dana Fox" in named and "for Priya's organoids" in named and "2025-11-03" in named


def test_no_person_label_links_two_clusters(result, tmp_path):
    # A label that carried over from one cluster to the next would let anyone count
    # duplicates per person.
    summary = baseline.summarize(result)
    clusters = {cluster.cluster_id: cluster for cluster in result.clusters}
    for row in summary["top_products"]:
        records = clusters[row["cluster"]].records
        assert row["people"] == len({r.requester_key for r in records})  # a count, not labels
    assert "Person" not in json.dumps(summary, default=str)
    assert baseline.summarize(result, anonymize=False)["top_products"][0]["people"] == [
        "Dana Fox",
        "Lee Chen",
        "Sam Ortiz",
    ]

    path = baseline.write_review_csv(result.pairs, tmp_path / "review.csv", anonymize=True)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = {row["pair_id"]: row for row in csv.DictReader(handle)}
    seen_in = defaultdict(set)
    for row in rows.values():
        for side in ("earlier", "later"):
            seen_in[row[f"{side}_requester"]].add(row["cluster"])
    assert seen_in and all(len(found) == 1 for found in seen_in.values())
    # Inside a cluster a label still follows one person: #6 is in two of C001's pairs.
    pairs = by_case(result.pairs)
    first, second = rows[pairs[5, 6].pair_id], rows[pairs[6, 7].pair_id]
    assert (first["earlier_requester"], first["later_requester"]) == (
        "C001 person A",
        "C001 person B",
    )
    assert (second["earlier_requester"], second["later_requester"]) == (
        "C001 person B",
        "C001 person C",
    )


def test_names_in_request_text_never_reach_the_anonymized_summary(result, records, generic):
    by_n = {N[r.id]: r for r in records}
    top = baseline.summarize(result)["top_products"]
    fbs = next(row for row in top if row["catalog"] == "10270-106")
    assert fbs["product"] == "Fetal Bovine Serum, qualified - for [name]'s organoids"
    # Any requested_by or created_by name goes (Morgan only ever created #50), in any case
    # and in any text the summary shows, and so do email addresses and links.
    texts = {
        50: replace(
            by_n[50],
            item_name=f"FBS for MORGAN HALE's organoids, ask {by_n[1].requester_email}",
            vendor_name="Hale & Patel",
            type_name="Morgan's media",
        ),
        51: replace(by_n[51], type_name="Morgan's media"),
        27: replace(by_n[27], item_name=f"Nuclease-free water for priya ({by_n[1].app_url})"),
    }
    edited = [texts.get(N[r.id], r) for r in records]
    keys = [*generic, ("idt", "Priya")]  # a generic key that is someone's name
    summary = baseline.summarize(baseline.find_clusters(edited, generic_keys=keys))
    shown = json.dumps(summary, default=str)
    assert named_in(shown) == []
    assert "@" not in shown and not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-", shown)
    fbs = next(row for row in summary["top_products"] if row["catalog"] == "10270-106")
    assert (fbs["product"], fbs["vendor"], fbs["type"]) == (
        "FBS for [name] [name]'s organoids, ask [email]",
        "[name] & [name]",
        "[name]'s media",
    )
    assert "[name]'s media" in summary["by_type"]
    repeats = summary["same_requester_repeats"]["top_products"]
    assert repeats[0]["product"].startswith("Nuclease-free water for [name] (")


def test_names_go_whatever_the_accents_or_the_script(tmp_path):
    # #1's requester has no accent in Quartzy, but the item name has one. The person who
    # entered #50 has a Chinese name, which the item name writes family name first.
    folder = sample.write_export(tmp_path / "export")
    jsonl = folder / "order_requests.jsonl"
    westerns = "GAPDH (14C10) Rabbit mAb - for José's westerns"
    lines = []
    for line in jsonl.read_text(encoding="utf-8").splitlines():
        raw = json.loads(line)
        if N[raw["id"]] in (1, 2):
            raw["item_name"] = westerns
        if N[raw["id"]] == 1:
            jose = {**raw["requested_by"], "first_name": "Jose", "last_name": "Garcia"}
            raw["requested_by"] = raw["created_by"] = jose
        if N[raw["id"]] == 50:
            raw["item_name"] = "Fetal Bovine Serum, qualified - 王伟订购"
            raw["created_by"] = {**raw["created_by"], "first_name": "伟", "last_name": "王"}
        lines.append(json.dumps(raw, ensure_ascii=False))
    jsonl.write_text("\n".join(lines) + "\n", encoding="utf-8")
    records = baseline.load_requests(folder)
    candidates = baseline.generic_catalog_candidates(records)
    summary = baseline.summarize(baseline.find_clusters(records, generic_keys=candidates))
    products = {row["catalog"]: row["product"] for row in summary["top_products"]}
    assert products["2118S"] == "GAPDH (14C10) Rabbit mAb - for [name]'s westerns"
    assert products["10270-106"] == "Fetal Bovine Serum, qualified - [name]订购"
    # "王伟" comes from the full name of whoever entered #50, so that is kept too.
    by_n = {N[r.id]: r for r in records}
    assert by_n[50].person_names == ("Priya", "Patel", "Priya Patel", "伟", "王", "伟 王")


def test_review_round_trip_gives_the_reviewed_figure(result, tmp_path):
    path = baseline.write_review_csv(result.pairs, tmp_path / "review.csv")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["amount"] for row in rows[:3]] == ["612.00", "560.00", "395.00"]
    assert rows[-1]["amount"] == ""  # no price: last
    assert {row["label"] for row in rows} == {""}
    picked = ["true_dup", "related not dup", "FALSE"]
    labels = {row["pair_id"]: label for row, label in zip(rows, picked, strict=False)}
    note = "v\N{LATIN SMALL LETTER E WITH ACUTE}rifi\N{LATIN SMALL LETTER E WITH ACUTE}"

    # Saved back the way Excel does in many European locales: semicolons, Windows-1252.
    labelled = tmp_path / "labelled.csv"
    with labelled.open("w", encoding="cp1252", newline="") as handle:
        writer = csv.DictWriter(handle, baseline.REVIEW_COLUMNS, delimiter=";")
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "label": labels.get(row["pair_id"], ""), "notes": note})
    reviews = baseline.read_review_csv(labelled)
    assert reviews[rows[1]["pair_id"]] == baseline.Review("related_not_dup", note)

    summary = baseline.reviewed_summary(result.pairs, reviews)
    assert summary["upper_bound"] == {"pairs": 12, "amount": Decimal("3103.50")}
    assert summary["reviewed"] == {"pairs": 1, "amount": Decimal("612.00")}
    assert summary["labelled"]["pairs"] == 3 and summary["unlabelled"]["pairs"] == 9
    assert summary["by_label"]["false"] == {"pairs": 1, "amount": Decimal("395.00")}
    assert summary["precision"] == round(1 / 3, 3)
    assert summary["by_bucket"]["received_earlier"]["amount"] == Decimal("612.00")
    assert summary["stale_labels"] == 0


def test_write_review_csv_never_replaces_labelled_work(result, tmp_path):
    path = baseline.write_review_csv(result.pairs, tmp_path / "review.csv")
    baseline.write_review_csv(result.pairs, path)  # nothing labelled yet: fine to rewrite
    text = path.read_text(encoding="utf-8-sig").replace("\n1,,,", "\n1,true_dup,,", 1)
    path.write_text(text, encoding="utf-8")
    with pytest.raises(FileExistsError):
        baseline.write_review_csv(result.pairs, path)
    baseline.write_review_csv(result.pairs, path, overwrite=True)
    assert not any(review.label for review in baseline.read_review_csv(path).values())


def test_read_review_csv_rejects_labels_it_does_not_know(result, tmp_path):
    path = baseline.write_review_csv(result.pairs, tmp_path / "review.csv")
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    lines[1] = lines[1].replace("1,,,", "1,yes,,", 1)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line 2: 'yes'"):
        baseline.read_review_csv(path)
    with pytest.raises(ValueError, match="no pair_id"):
        baseline.read_review_csv(sample.UI_CSV)


def test_review_csv_can_hide_names_and_defuses_formulas(result, tmp_path):
    first, *rest = result.pairs
    risky = replace(first, later=replace(first.later, item_name='=HYPERLINK("http://x")'))
    path = baseline.write_review_csv([risky, *rest], tmp_path / "review.csv", anonymize=True)
    text = path.read_text(encoding="utf-8-sig")
    assert named_in(text) == []  # #50's item name names Priya
    assert "'=HYPERLINK" in text and "C001 person A" in text and "for [name]'s organoids" in text
    # The co-owner's own file keeps every name.
    named = baseline.write_review_csv(result.pairs, tmp_path / "named.csv").read_text("utf-8-sig")
    assert "Dana Fox" in named and "for Priya's organoids" in named


def test_status_paths_measure_how_far_requests_get(records):
    paths = baseline.status_paths(records, as_of="2026-10-20T08:00:00Z")
    assert paths["requests"] == 52 and paths["by_status"]["RECEIVED"] == 32
    assert paths["reached_ordered_or_received"] == {"count": 37, "share": round(37 / 52, 3)}
    assert paths["stuck_in_created"]["count"] == 11  # #43 is 29 days old
    assert paths["median_days_to_received"] is None  # needs the UI export's dates


def test_trigram_similarity_matches_pg_trgm():
    assert baseline.trigram_similarity("word", "two words") == pytest.approx(4 / 11)
    assert baseline.trigram_similarity("", "anything") == 0.0


def test_the_notebook_is_saved_without_outputs():
    notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    code = [cell for cell in notebook["cells"] if cell["cell_type"] == "code"]
    assert code and all(not c["outputs"] and c["execution_count"] is None for c in code)
    assert "BASELINE_EXPORT_DIR" in "".join("".join(cell["source"]) for cell in code)
