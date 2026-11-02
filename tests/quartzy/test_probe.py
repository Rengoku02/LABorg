import hashlib
import json
import re

import httpx
import pytest

from apps.quartzy.client import QuartzyError
from scripts import quartzy_probe as probe
from tests.quartzy.fake_quartzy import FakeQuartzy, check_against_spec

TOKEN = "tok-PROBE-9f8e7d6c5b4a3210"


@pytest.fixture(autouse=True)
def token_env(monkeypatch):
    monkeypatch.setenv("QUARTZY_ACCESS_TOKEN", TOKEN)
    monkeypatch.delenv("LABSTOCK_FIXTURE_SALT", raising=False)
    monkeypatch.delenv("QUARTZY_LAB_IDS", raising=False)


def run(fake, command, data_dir, *extra):
    argv = [command, "--data-dir", str(data_dir), "--min-interval", "0", *extra]
    return probe.main(argv, transport=fake.transport(), sleep=fake.slept.append)


def stocked(count=47, **options):
    fake = FakeQuartzy(TOKEN, **{"page_size": 10, **options})
    lab = fake.add_lab("Smith Lab")
    fake.add_type(lab, "Antibodies")
    for _ in range(count):
        fake.add_request(lab)
    fake.add_inventory_item(lab)
    return fake, lab


def with_a_collaborators_lab(fake):
    """Another group's Quartzy lab that the token's owner also belongs to."""
    theirs = fake.add_lab("Jones Lab")
    fake.add_type(theirs, "Secret targets")
    jones = fake.add_user("Zed", "Outsider")
    for _ in range(3):
        fake.add_request(
            theirs, requested_by=jones, item_name="Jones lab confidential target antibody"
        )
    fake.add_inventory_item(theirs, name="Jones lab freezer box")
    return theirs


def report(data_dir):
    [path] = (data_dir / "probe").glob("*/report.json")
    return json.loads(path.read_text())


def order_request_pages(fake):
    return [params.get("page") for _, path, params in fake.log if path == "/order-requests"]


# measure ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("base", [0, 1])
@pytest.mark.parametrize(
    ("order", "lands"), [("newest_first", "first_page"), ("oldest_first", "last_page")]
)
def test_measure_infers_page_base_and_sort(tmp_path, capsys, base, order, lands):
    fake, lab = stocked(page_base=base, order=order)
    assert run(fake, "measure", tmp_path) == 0
    measured = report(tmp_path)
    paging = measured["order_requests"]
    assert paging["page_base"] == base and paging["omitted_page_is_base"] is True
    assert (paging["page_size"], paging["page_size_exact"]) == (10, True)
    assert (paging["last_page"], paging["estimated_total"]) == (base + 4, 47)
    assert paging["first_page_stable"] is True
    assert (paging["sort"]["key"], paging["sort"]["new_items_at"]) == ("requested_at", lands)
    assert paging["sample"]["records"] == 47 and paging["sample"]["statuses"] == {"CREATED": 47}
    assert paging["sample"]["money"] == {
        "counts": {"minor": 47, "major": 0, "unknown": 0},
        "recommended": "minor",
    }
    [lab_facts] = measured["labs"]
    assert (lab_facts["id"], lab_facts["types"]) == (lab["id"], 1)
    assert lab_facts["inventory_items"]["estimate"] == 1
    assert measured["calls"] == fake.total_calls and measured["complete"] is True
    printed = capsys.readouterr().out
    assert (
        f"QUARTZY_PAGE_BASE={base}\nQUARTZY_NEW_ITEMS_AT={lands}\nQUARTZY_MONEY_UNIT=minor\n"
        in (printed)
    )


@pytest.mark.parametrize(
    ("past_end", "status", "records", "repeats"),
    [("empty", 200, 0, False), ("error", 404, 0, False), ("clamp", 200, 7, True)],
)
def test_measure_records_what_the_page_after_the_last_returns(
    tmp_path, capsys, past_end, status, records, repeats
):
    fake, _ = stocked(past_end=past_end)  # 47 requests: the last page, 5, holds 7
    assert run(fake, "measure", tmp_path) == 0
    paging = report(tmp_path)["order_requests"]
    after = paging["after_last_page"]
    assert after["page"] == paging["last_page"] + 1
    assert (after["status"], after["records"], after["repeats_last_page"]) == (
        status,
        records,
        repeats,
    )
    assert "page after the last" in capsys.readouterr().out


@pytest.mark.parametrize("below_base", ["empty", "clamp", "error"])
def test_measure_finds_base_one_whatever_page_zero_does(tmp_path, below_base):
    fake, _ = stocked(page_base=1, page_below_base=below_base)
    run(fake, "measure", tmp_path)
    assert report(tmp_path)["order_requests"]["page_base"] == 1


def test_measure_notices_an_omitted_page_that_is_not_the_base(tmp_path):
    fake, _ = stocked(page_base=0, omitted_page=1)
    run(fake, "measure", tmp_path)
    paging = report(tmp_path)["order_requests"]
    assert paging["page_base"] == 0 and paging["omitted_page_is_base"] is False


def test_measure_flags_an_unstable_order(tmp_path, capsys):
    fake, _ = stocked(order="unstable")
    run(fake, "measure", tmp_path)
    paging = report(tmp_path)["order_requests"]
    assert paging["page_base"] == 1 and paging["first_page_stable"] is False
    assert paging["sort"]["new_items_at"] == "unknown"
    assert "QUARTZY_NEW_ITEMS_AT=unknown\n" in capsys.readouterr().out


def test_measure_sees_an_updated_at_sort(tmp_path):
    fake, _ = stocked(sort_key="updated_at")
    for request_id in list(fake.order_requests)[:5]:  # edit the five oldest requests
        fake.update_request(request_id, status="ORDERED")
    run(fake, "measure", tmp_path)
    sort = report(tmp_path)["order_requests"]["sort"]
    assert sort["requested_at"]["first_page"] == "none"
    assert (sort["key"], sort["new_items_at"]) == ("updated_at", "first_page")


def test_measure_counts_nulls_and_requester_mismatches(tmp_path):
    fake = FakeQuartzy(TOKEN, page_size=10)
    lab = fake.add_lab()
    helper = fake.add_user("Lab", "Manager", email=None)
    for n in range(20):
        fake.add_request(
            lab,
            created_by=helper if n % 4 == 0 else fake.me,
            requested_by=None if n == 0 else fake.me,
            updated_at=None if n % 2 else fake.now,
        )
    run(fake, "measure", tmp_path)
    sample = report(tmp_path)["order_requests"]["sample"]
    assert sample["requested_by_vs_created_by"] == {"compared": 19, "different": 4}
    assert sample["null_pct"]["updated_at"] == 50.0
    assert sample["null_pct"]["requested_by"] == 5.0
    assert sample["null_pct"]["created_by_email"] == 25.0


def test_measure_with_no_requests_pins_nothing_it_did_not_measure(tmp_path, capsys):
    fake = FakeQuartzy(TOKEN)
    fake.add_lab()
    assert run(fake, "measure", tmp_path) == 0
    assert report(tmp_path)["pinned"]["QUARTZY_PAGE_BASE"] is None
    printed = capsys.readouterr().out
    assert "# QUARTZY_PAGE_BASE=?" in printed and "QUARTZY_MONEY_UNIT=auto\n" in printed


@pytest.mark.parametrize("cap", [3, 5, 7, 9, 12, 15, 19])
def test_measure_cut_short_still_writes_what_it_learned(tmp_path, capsys, cap):
    fake, _ = stocked()
    assert run(fake, "measure", tmp_path, "--max-calls", str(cap)) == 1
    measured = report(tmp_path)
    assert measured["complete"] is False and "max-calls" in measured["stopped_because"]
    assert fake.total_calls == measured["calls"] == cap
    if cap >= 7:  # page 0 and page 1 were read
        assert measured["order_requests"]["page_base"] == 1
    assert "INCOMPLETE" in capsys.readouterr().out


def test_measure_records_a_failing_quartzy_and_keeps_going_to_the_report(tmp_path):
    fake, _ = stocked()
    fake.fail_next(503, times=100, path="/inventory-items")
    assert run(fake, "measure", tmp_path) == 1
    measured = report(tmp_path)
    assert "/inventory-items returned 503" in measured["stopped_because"]
    assert measured["order_requests"]["sort"]["new_items_at"] == "first_page"


def test_measure_prints_the_lab_ids_to_pin(tmp_path, capsys):
    fake, lab = stocked()
    run(fake, "measure", tmp_path)
    assert report(tmp_path)["pinned"]["QUARTZY_LAB_IDS"] == lab["id"]
    assert f"QUARTZY_MONEY_UNIT=minor\nQUARTZY_LAB_IDS={lab['id']}\n" in capsys.readouterr().out


def test_measure_lists_several_labs_and_leaves_the_choice(tmp_path, capsys, monkeypatch):
    fake = FakeQuartzy(TOKEN, page_size=10)
    theirs = with_a_collaborators_lab(fake)  # listed first
    ours = fake.add_lab("Smith Lab")
    for _ in range(12):
        fake.add_request(ours)
    run(fake, "measure", tmp_path / "first")
    measured = report(tmp_path / "first")
    assert measured["pinned"]["QUARTZY_LAB_IDS"] is None
    assert measured["probed_lab_id"] == theirs["id"]
    printed = capsys.readouterr().out
    assert "# QUARTZY_LAB_IDS=?" in printed and "QUARTZY_LAB_IDS=" + theirs["id"] not in printed
    assert f"#   {theirs['id']}  Jones Lab\n#   {ours['id']}  Smith Lab\n" in printed
    # Once QUARTZY_LAB_IDS says which lab is the lab's, measure reads that one and pins it.
    monkeypatch.setenv("QUARTZY_LAB_IDS", ours["id"])
    run(fake, "measure", tmp_path / "second")
    measured = report(tmp_path / "second")
    assert measured["probed_lab_id"] == ours["id"]
    assert measured["order_requests"]["estimated_total"] == 12
    assert measured["pinned"]["QUARTZY_LAB_IDS"] == ours["id"]
    assert f"QUARTZY_LAB_IDS={ours['id']}\n" in capsys.readouterr().out


def test_measure_says_which_lab_ids_the_token_does_not_see(tmp_path, capsys, monkeypatch):
    fake = FakeQuartzy(TOKEN, page_size=10)
    theirs = with_a_collaborators_lab(fake)  # listed first
    ours = fake.add_lab("Smith Lab")
    for _ in range(12):
        fake.add_request(ours)
    monkeypatch.setenv("QUARTZY_LAB_IDS", f"{ours['id']},not-a-lab")  # one typo
    assert run(fake, "measure", tmp_path) == 0
    measured = report(tmp_path)
    assert measured["pinned"]["QUARTZY_LAB_IDS"] is None
    assert measured["probed_lab_id"] == ours["id"]  # the one named that it sees, not the first
    printed = capsys.readouterr().out
    assert "# QUARTZY_LAB_IDS=?  (QUARTZY_LAB_IDS names not-a-lab, which the token " in printed
    assert f"#   {theirs['id']}  Jones Lab\n#   {ours['id']}  Smith Lab\n" in printed


def env_block(printed):
    return printed.split("Pin these in the environment (.env):\n")[1].splitlines()


def test_lab_names_print_on_one_line_so_they_cannot_add_an_env_line(tmp_path, capsys):
    fake, _ = stocked()
    theirs = fake.add_lab("Jones Lab\nQUARTZY_LAB_IDS=not-ours\r\n")
    run(fake, "measure", tmp_path)
    block = env_block(capsys.readouterr().out)
    assert [line for line in block if not line.startswith("#")] == [
        "QUARTZY_PAGE_BASE=1",
        "QUARTZY_NEW_ITEMS_AT=first_page",
        "QUARTZY_MONEY_UNIT=minor",
    ]
    assert f"#   {theirs['id']}  Jones Lab QUARTZY_LAB_IDS=not-ours" in block
    assert run(fake, "export", tmp_path, "--page-base", "1") == 2
    printed = capsys.readouterr().out
    assert f"  {theirs['id']}  Jones Lab QUARTZY_LAB_IDS=not-ours\n" in printed


def test_a_missing_or_rejected_token_stops_cleanly(tmp_path, capsys, monkeypatch):
    fake, _ = stocked()
    monkeypatch.delenv("QUARTZY_ACCESS_TOKEN")
    assert run(fake, "measure", tmp_path) == 2
    assert "QUARTZY_ACCESS_TOKEN" in capsys.readouterr().out
    monkeypatch.setenv("QUARTZY_ACCESS_TOKEN", "not-the-token")
    assert run(fake, "measure", tmp_path) == 2
    assert "refused the token" in capsys.readouterr().out
    assert not (tmp_path / "probe").exists()


# The pieces ------------------------------------------------------------------------------------


@pytest.mark.parametrize("last", [0, 1, 2, 3, 7, 8, 100, 4095])
def test_last_page_search_is_logarithmic(last):
    probed = []

    def nonempty(page):
        probed.append(page)
        return page <= last

    assert probe.find_last_page(nonempty, 0) == (last, False)
    assert len(probed) <= 2 * (last + 1).bit_length() + 2
    assert probe.find_last_page(lambda page: page <= last + 1, 1) == (last + 1, False)


def test_last_page_search_edges():
    assert probe.find_last_page(lambda page: False, 1) == (None, False)
    assert probe.find_last_page(lambda page: True, 1, max_offset=64) == (65, True)


def test_page_base_inference():
    infer = probe.infer_page_base
    a, b = [{"id": "a"}], [{"id": "b"}]
    refused = QuartzyError("GET /order-requests returned 400", status=400)
    assert infer([], b)[0] == 1
    assert infer(refused, b)[0] == 1
    assert infer(b, b)[0] == 1  # page 0 served as page 1
    assert infer(a, [])[0] == 0
    assert infer(a, b)[0] == 0
    assert infer(a, refused)[0] == 0
    assert infer([], [])[0] is None
    assert infer([{"id": "a"}, {"id": "b"}], [{"id": "b"}, {"id": "c"}])[0] is None


# export ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("base", [0, 1])
def test_export_dedupes_ids_and_stops_at_the_empty_page(tmp_path, capsys, base):
    fake, lab = stocked(count=12, page_size=5, page_base=base)
    # A request created while the export sits between pages pushes every later page down one
    # slot, so the second page starts with the last request of the first.
    fake.after_page(lambda: fake.add_request(lab), page=base)
    assert run(fake, "export", tmp_path, "--page-base", str(base)) == 0
    [folder] = tmp_path.glob("export-*")
    lines = (folder / "order_requests.jsonl").read_text().splitlines()
    exported = [json.loads(line)["id"] for line in lines]
    assert len(exported) == len(set(exported)) == 12
    assert order_request_pages(fake) == [str(base + n) for n in range(4)]  # page 4 was empty
    manifest = json.loads((folder / "manifest.json").read_text())
    stats = manifest["per_lab"][lab["id"]]["order_requests"]
    assert stats == {"pages": 4, "records": 12, "duplicates": 1, "stopped": "empty_page"}
    assert manifest["complete"] is True and manifest["calls"] == fake.total_calls
    assert manifest["counts"] == {"labs": 1, "types": 1, "order_requests": 12, "inventory_items": 1}
    assert manifest["spec_sha256"] == hashlib.sha256(probe.SPEC.read_bytes()).hexdigest()
    facts = manifest["probe_facts"]
    assert (facts["page_base"], facts["page_base_source"]) == (base, "--page-base")
    assert facts["money_unit"] == "auto" and facts["money_unit_counts"]["minor"] == 12
    assert len(json.loads((folder / "labs.json").read_text())) == 1
    assert len((folder / "inventory_items.jsonl").read_text().splitlines()) == 1
    assert "Exported" in capsys.readouterr().out


def test_export_takes_its_facts_from_the_latest_probe_report(tmp_path):
    fake, _ = stocked(page_base=0)
    run(fake, "measure", tmp_path)
    fake.reset_calls()
    assert run(fake, "export", tmp_path) == 0
    [folder] = tmp_path.glob("export-*")
    facts = json.loads((folder / "manifest.json").read_text())["probe_facts"]
    assert (facts["page_base"], facts["page_base_source"]) == (0, "probe report")
    assert (facts["money_unit"], facts["new_items_at"]) == ("minor", "first_page")
    assert order_request_pages(fake)[0] == "0"


def test_export_measures_the_page_base_without_a_report(tmp_path):
    fake, _ = stocked(page_base=0)
    assert run(fake, "export", tmp_path) == 0
    [folder] = tmp_path.glob("export-*")
    facts = json.loads((folder / "manifest.json").read_text())["probe_facts"]
    assert facts["page_base"] == 0 and facts["page_base_source"].startswith("measured")


def test_export_stops_at_the_call_cap_and_says_so(tmp_path, capsys):
    fake, lab = stocked()
    assert run(fake, "export", tmp_path, "--page-base", "1", "--max-calls", "4") == 1
    assert fake.total_calls == 4
    [folder] = tmp_path.glob("export-*")
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["complete"] is False
    assert manifest["per_lab"][lab["id"]]["order_requests"]["stopped"] == "call_budget"
    assert "INCOMPLETE" in capsys.readouterr().out


def test_export_keeps_what_it_read_when_quartzy_fails(tmp_path, capsys):
    fake, lab = stocked()
    fake.fail_next(503, times=100, path="/order-requests")
    assert run(fake, "export", tmp_path, "--page-base", "1") == 1
    [folder] = tmp_path.glob("export-*")
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["complete"] is False
    assert manifest["counts"]["types"] == 1 and manifest["counts"]["order_requests"] == 0
    stopped = manifest["per_lab"][lab["id"]]["order_requests"]["stopped"]
    assert stopped.startswith("error: GET /order-requests returned 503")
    assert "inventory_items" not in manifest["per_lab"][lab["id"]]  # not attempted
    assert len(json.loads((folder / "types.json").read_text())) == 1
    assert "INCOMPLETE" in capsys.readouterr().out


def test_a_second_export_on_the_same_day_gets_its_own_folder(tmp_path):
    fake, _ = stocked(count=3)
    run(fake, "export", tmp_path, "--page-base", "1")
    run(fake, "export", tmp_path, "--page-base", "1")
    assert len(list(tmp_path.glob("export-*"))) == 2


def data_files(folder):
    """The text of an export's files but the manifest (which names the labs left out)."""
    return "".join(path.read_text() for path in folder.iterdir() if path.name != "manifest.json")


def test_export_never_guesses_which_of_several_labs_is_ours(tmp_path, capsys):
    fake, ours = stocked()
    theirs = with_a_collaborators_lab(fake)
    assert run(fake, "export", tmp_path, "--page-base", "1") == 2
    printed = capsys.readouterr().out
    assert "--lab-id" in printed and "QUARTZY_LAB_IDS" in printed
    assert f"{ours['id']}  Smith Lab" in printed and f"{theirs['id']}  Jones Lab" in printed
    assert not list(tmp_path.glob("export-*"))
    assert fake.calls["/order-requests"] == fake.calls["/inventory-items"] == 0


def test_export_reads_only_the_labs_it_is_given(tmp_path, capsys, monkeypatch):
    fake, ours = stocked()
    theirs = with_a_collaborators_lab(fake)
    monkeypatch.setenv("QUARTZY_LAB_IDS", f" {ours['id'].upper()} ,")  # as typed into .env
    assert run(fake, "export", tmp_path / "ours", "--page-base", "1") == 0
    [folder] = (tmp_path / "ours").glob("export-*")
    manifest = json.loads((folder / "manifest.json").read_text())
    assert (manifest["lab_ids"], manifest["lab_ids_source"]) == ([ours["id"]], "QUARTZY_LAB_IDS")
    assert manifest["labs_left_out"] == [{"id": theirs["id"], "name": "Jones Lab"}]
    assert manifest["counts"] == {"labs": 1, "types": 1, "order_requests": 47, "inventory_items": 1}
    assert [lab["id"] for lab in json.loads((folder / "labs.json").read_text())] == [ours["id"]]
    data = data_files(folder)
    assert "Jones" not in data and theirs["id"] not in data
    asked = {params.get("lab_id") for _, path, params in fake.log if path != "/labs"} - {None}
    assert asked == {ours["id"]}
    assert "Quartzy labs: Smith Lab" in capsys.readouterr().out
    # --lab-id, once per lab, wins over QUARTZY_LAB_IDS.
    argv = ["--page-base", "1", "--lab-id", theirs["id"], "--lab-id", ours["id"]]
    assert run(fake, "export", tmp_path / "both", *argv) == 0
    [folder] = (tmp_path / "both").glob("export-*")
    manifest = json.loads((folder / "manifest.json").read_text())
    assert (manifest["lab_ids"], manifest["lab_ids_source"]) == (
        [ours["id"], theirs["id"]],
        "--lab-id",
    )
    assert manifest["counts"]["order_requests"] == 50 and manifest["labs_left_out"] == []


def test_export_takes_lab_ids_comma_separated_as_in_env(tmp_path):
    fake, ours = stocked()
    theirs = with_a_collaborators_lab(fake)
    named = f"{theirs['id']}, {ours['id']}"  # the value of a pinned QUARTZY_LAB_IDS line
    assert run(fake, "export", tmp_path, "--page-base", "1", "--lab-id", named) == 0
    [folder] = tmp_path.glob("export-*")
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["lab_ids"] == [ours["id"], theirs["id"]]


@pytest.mark.parametrize("named", ["not-a-lab", "{ours},not-a-lab"])
def test_export_refuses_a_lab_the_token_does_not_see(tmp_path, capsys, monkeypatch, named):
    fake, ours = stocked()
    monkeypatch.setenv("QUARTZY_LAB_IDS", named.format(ours=ours["id"]))
    assert run(fake, "export", tmp_path, "--page-base", "1") == 2
    printed = capsys.readouterr().out
    assert "not-a-lab" in printed and f"{ours['id']}  Smith Lab" in printed
    assert not list(tmp_path.glob("export-*"))


def test_export_leaves_out_what_quartzy_sends_from_another_lab(tmp_path, capsys, monkeypatch):
    fake, ours = stocked()
    theirs = with_a_collaborators_lab(fake)

    def ignoring_the_lab_filter(request):  # an API that answers for every lab
        url = request.url.copy_remove_param("lab_id")
        return fake(httpx.Request(request.method, url, headers=request.headers))

    monkeypatch.setenv("QUARTZY_LAB_IDS", ours["id"])
    argv = ["export", "--data-dir", str(tmp_path), "--min-interval", "0", "--page-base", "1"]
    transport = httpx.MockTransport(ignoring_the_lab_filter)
    assert probe.main(argv, transport=transport, sleep=fake.slept.append) == 0
    [folder] = tmp_path.glob("export-*")
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["counts"]["order_requests"] == 47
    assert manifest["from_other_labs"] == {"types": 1, "order_requests": 3, "inventory_items": 1}
    data = data_files(folder)
    assert "Jones" not in data and theirs["id"] not in data
    assert "Left out 5 records" in capsys.readouterr().out


# The token never leaks -------------------------------------------------------------------------


def test_the_token_never_reaches_stdout_or_files(tmp_path, capsys, monkeypatch):
    fake, lab = stocked()
    # Belt and braces: even a token pasted into Quartzy data is scrubbed from what we write.
    fake.add_request(lab, notes=f"pasted {TOKEN} here by mistake")
    token_file = tmp_path / "secret" / "token.txt"
    token_file.parent.mkdir()
    token_file.write_text(f"{TOKEN}\n")
    monkeypatch.setenv("QUARTZY_ACCESS_TOKEN", "a-stale-token")  # --token-file wins
    data = tmp_path / "data"
    assert run(fake, "measure", data, "--token-file", str(token_file)) == 0
    assert run(fake, "export", data, "--token-file", str(token_file)) == 0
    [export_dir] = data.glob("export-*")
    assert probe.main(["fixtures", str(export_dir), "--out", str(tmp_path / "fixtures")]) == 0
    printed = capsys.readouterr()
    assert TOKEN not in printed.out + printed.err
    written = [path for path in tmp_path.rglob("*") if path.is_file() and path != token_file]
    assert len(written) >= 11
    for path in written:
        assert TOKEN not in path.read_text(), path
    assert "[redacted]" in (export_dir / "order_requests.jsonl").read_text()


# fixtures --------------------------------------------------------------------------------------


@pytest.fixture
def personal_export(tmp_path):
    fake = FakeQuartzy(TOKEN, page_size=10)
    lab = fake.add_lab("Patel Lab")
    priya = fake.add_user("Priya", "Patel", "priya.patel@uni.edu")
    raj = fake.add_user("Raj", "Kumar", "RAJ.KUMAR@uni.edu")
    for n in range(30):
        fake.add_request(
            lab,
            requested_by=priya if n % 2 else raj,
            created_by=raj,
            unit_price="289.00",
            item_name="Antibody for Priya's project" if n == 0 else f"Antibody {n}",
            notes="ask Priya, ext. 5551",
            details="for Raj only",
            spend_tracking_code={"name": "R01-GM12345"},
            purchase_order_number="PO-778899",
        )
    fake.add_inventory_item(
        lab,
        added_by=priya,
        technical_details="Priya's protocol",
        instances=[{"location": {"name": "Priya's box", "pathNames": ["Room 4", "Kumar shelf"]}}],
    )
    assert run(fake, "export", tmp_path / "data", "--page-base", "1") == 0
    [folder] = (tmp_path / "data").glob("export-*")
    return fake, folder


def test_fixtures_remove_names_emails_ids_and_free_text(tmp_path, personal_export, monkeypatch):
    fake, export_dir = personal_export
    monkeypatch.setenv("LABSTOCK_FIXTURE_SALT", "a-secret-only-the-owner-knows")
    out = tmp_path / "fixtures"
    argv = ["fixtures", str(export_dir), "--out", str(out), "--requests", "10"]
    assert probe.main(argv) == 0
    text = "\n".join(path.read_text() for path in sorted(out.glob("*.json")))
    secrets = ["priya", "patel", "raj", "kumar", "uni.edu", "R01-GM12345", "778899", "5551"]
    secrets += [*fake.order_requests, *fake.users, *fake.labs, *fake.inventory_items]
    for secret in secrets:
        assert secret.lower() not in text.lower(), secret
    assert "a-secret-only-the-owner-knows" not in text

    requests = json.loads((out / "order_requests.json").read_text())
    assert len(requests) == 10
    assert {r["unit_price"] for r in requests} == {"289.00"}  # prices kept
    assert {r["total_price"]["amount"] for r in requests} == {"28900"}
    assert {(r["notes"], r["details"]) for r in requests} == {("", "")}
    assert requests[0]["item_name"].startswith("Antibody for Person-")
    # One person keeps one pseudonym, and ids agree across records and files.
    assert len({json.dumps(r["created_by"], sort_keys=True) for r in requests}) == 1
    assert len({r["requested_by"]["email"] for r in requests}) == 2
    [lab] = json.loads((out / "labs.json").read_text())
    assert {r["lab"]["id"] for r in requests} == {lab["id"]}
    assert all(lab["id"] in r["app_url"] and r["id"] in r["app_url"] for r in requests)
    check_against_spec("/order-requests", requests)
    [item] = json.loads((out / "inventory_items.json").read_text())
    assert item["technical_details"] == "" and item["instances"][0]["location"]["name"] != "Box"
    check_against_spec("/inventory-items", [item])
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["counts"]["order_requests"] == 10
    assert manifest["pseudonyms"] == "keyed by LABSTOCK_FIXTURE_SALT"
    assert manifest["lab_ids"] == [lab["id"]]  # the baseline reads that lab's requests

    # The same key gives the same pseudonyms on the next run.
    again = tmp_path / "again"
    assert probe.main(["fixtures", str(export_dir), "--out", str(again), "--requests", "10"]) == 0
    assert (again / "order_requests.json").read_text() == (out / "order_requests.json").read_text()


def test_fixtures_find_names_whatever_the_accents_the_length_or_the_script(tmp_path):
    # Profiles without the accents that item names have, a two-letter name, and a Chinese
    # name, which item names write family name first and run into the words around it.
    fake = FakeQuartzy(TOKEN, page_size=10)
    lab = fake.add_lab("Smith Lab")
    jose, mei = fake.add_user("Jose", "Garcia"), fake.add_user("Mei", "Li")
    wei = fake.add_user("伟", "王")
    for person, item in [
        (jose, "GAPDH mAb - for José García's westerns"),
        (jose, "Gels for JOSE and Jose"),
        (mei, "Trypsin for Li's passaging"),
        (wei, "王伟的抗体"),
    ]:
        fake.add_request(lab, requested_by=person, created_by=person, item_name=item)
    assert run(fake, "export", tmp_path / "data", "--page-base", "1") == 0
    [folder] = (tmp_path / "data").glob("export-*")
    assert probe.main(["fixtures", str(folder), "--out", str(tmp_path / "out")]) == 0
    requests = json.loads((tmp_path / "out" / "order_requests.json").read_text())
    items = [request["item_name"] for request in requests]
    pseudonym = re.compile(r"Person-[0-9A-F]{6}")
    assert [pseudonym.sub("Person", item) for item in items] == [
        "GAPDH mAb - for Person Person's westerns",
        "Gels for Person and Person",
        "Trypsin for Person's passaging",
        "Person的抗体",
    ]
    # Every spelling of a name gets the same pseudonym.
    assert len({pseudonym.findall(items[0])[0], *pseudonym.findall(items[1])}) == 1


def test_fixtures_without_an_export_fail_cleanly(tmp_path, capsys):
    assert probe.main(["fixtures", str(tmp_path / "missing")]) == 2
    assert "run export first" in capsys.readouterr().out


def test_spread_picks_evenly():
    assert probe.spread(list(range(10)), 3) == [0, 4, 9]  # round(4.5) == 4
    assert probe.spread(list(range(3)), 5) == [0, 1, 2]
    assert probe.spread(list(range(5)), 1) == [0]
    assert probe.spread(list(range(5)), 0) == []
