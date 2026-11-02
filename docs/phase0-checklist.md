# Phase 0 checklist

Phase 0 measures the problem with real data, names the people, and gets email delivery working.
Nobody gets an alert until all four exit criteria at the bottom are met. The details and reasons
are in plan §5.

## 1. Measure Quartzy (you, ~1 h)

Create a token in Quartzy: Profile Settings → Access Tokens. A Lab Member token is enough, because
LabStock only reads. Keep it in the password manager. Never paste it into chat, Slack or a commit.

```bash
read -rs QUARTZY_ACCESS_TOKEN && export QUARTZY_ACCESS_TOKEN   # typed, not saved in shell history
uv run python -m scripts.quartzy_probe measure    # ~1–2 min, one call every 0.5 s
export QUARTZY_LAB_IDS=...                        # only if the token sees several labs (below)
uv run python -m scripts.quartzy_probe export     # read-only copy of the lab's labs into data/
uv run jupyter lab notebooks/baseline_duplicates.ipynb
```

- `measure` prints four lines to pin in `.env`: `QUARTZY_PAGE_BASE`, `QUARTZY_NEW_ITEMS_AT`,
  `QUARTZY_MONEY_UNIT` and `QUARTZY_LAB_IDS`. Before you pin the money unit, open one request in
  the Quartzy web page and check that its total matches the report.
- **Which Quartzy labs are the lab's.** The token sees every Quartzy lab its owner belongs to, a
  collaborator's included, and their purchases must not count as ours. If it sees only one,
  `measure` pins that one. If it sees several, or `QUARTZY_LAB_IDS` names one it doesn't see,
  `measure` lists them (id and name) instead: put the lab's own ids in `QUARTZY_LAB_IDS`
  (comma-separated), in `.env` and in the shell as above. `export` refuses to guess. It reads
  only those labs (or the ones given with `--lab-id`) and records them in its `manifest.json`,
  and the notebook counts only their requests.
- **Where new requests land.** The probe can't create anything, so this check is manual:
  1. Run `measure` (with `QUARTZY_LAB_IDS` set, if the token sees several labs, so that it reads
     the lab's own requests).
  2. Ask the Lab Admin to create (or edit) one test request.
  3. Run `measure` again and compare `first_page_ids` in the two reports.
  4. Cancel the test request.
- Also export Quartzy's own "All Time" requests table from the web UI into `data/`. It has the
  approved/ordered/received dates, which the API doesn't. Then set `STATUS_COLUMNS` in the
  notebook to its column names.

## 2. Baseline for the PI (you + co-owner)

- The notebook gives two numbers:
  - an **upper bound**: every possible duplicate pair, counted once;
  - a **review CSV** of the biggest pairs.
- The co-owner labels the top ~40 pairs by $ (`true_dup` / `related_not_dup` / `false`). Re-run
  the notebook to get the **reviewed figure**, which is the one to show the PI.
- The review CSV contains names. Don't share it; share only the anonymized summary: notebook
  section 6, plus the totals in section 8. Nothing else from the notebook goes on a slide.

## 3. People (PI decides, ideally at the next lab meeting)

| Role | Who | Time |
|---|---|---|
| Wet-lab co-owner (lab manager or senior tech) — **exit criterion** | | ~1 h/week for 10 weeks |
| 3–4 bench champions | | ~1 h/week |
| Deputy (the other bioinformatician): code review, runbook, secrets | | occasional |
| Quartzy Lab Admin | | ~2 h in Phase 0 |

PI decisions:
- which item types the guard covers, and the $ threshold (suggested: $250)
- default visibility of personal stock (suggested: ask-first; "registered ≠ shared")
- amnesty dates and the unclaimed-items rule
- the privacy commitments in `docs/privacy.md`

## 4. Quartzy hygiene (Lab Admin)

- Check that everyone has their own seat (no shared logins) and requests in one Quartzy Lab.
- Clean up stale requests: cancel or receive anything CREATED/APPROVED for more than 30 days, or
  ORDERED/BACKORDERED for more than 60 days.
- Test Quartzy's own duplicate warning: request the same catalog # twice, on the web, on mobile
  and through Quartzy Connect. Also try it once with different capitalisation or dashes. Then
  cancel the test requests.

## 5. Permanent name and email (IT — start now, this is the slowest part)

You need:
- a hostname you will keep (it is printed in every email link, and later on QR stickers);
- an email provider supported by django-anymail (e.g. Postmark, Mailgun, Amazon SES, Brevo);
- `alerts@` and `news@` senders on that domain, set up with SPF, DKIM and DMARC `p=none`.

**Seed test:** send test emails to a Gmail address and to a university mailbox. Check that they
reach the inbox on both a desktop and a phone.

Draft for IT (edit before sending):

> Subject: Hostname and email sender for a small lab web app
>
> Hi, our lab (~30 people) is setting up LabStock, a small internal web app that tracks our
> reagent and freezer stock and warns people before they order something the lab already has.
> Could you help with:
> 1. A permanent hostname, e.g. `labstock.<dept>.<university>.edu`, for a server we will run (or a
>    campus VM if one is available: Docker, 2 vCPU, 4 GB RAM, 40 GB disk, outbound HTTPS).
> 2. Sending email as `alerts@` and `news@` that domain through <email provider>. They will send
>    the SPF include, DKIM records and a DMARC `p=none` record.
> 3. Allow-listing that sender in the university mail filter and, if possible, exempting it from
>    link rewriting. The emails contain one-time links that open a confirmation page.
>
> Volume is low: a few emails per person per week.

## 6. Quartzy support (optional — only if you and the PI agree)

> Subject: Read-only API integration for our lab
>
> Hi, our lab uses Quartzy for all ordering. We are building an internal tool that reads our
> order requests and inventory through the public API (GET only, one token, ~50–70 calls per hour)
> to warn people about duplicate orders. Could you tell us:
> 1. Is this use within your terms of service?
> 2. Could you register an OAuth client for us, so the tool doesn't depend on one person's token?
> 3. What are the page size and sort order of `GET /order-requests`, and is there a rate limit?
> 4. Is there any way to read when a request was approved, ordered or received?

## 7. Data in (co-owner + champions, in parallel)

Templates are in `docs/templates/`:

| File | Who fills it | What |
|---|---|---|
| `roster.csv` | you + PI | everyone in the lab; loaded with `provision_members` |
| `locations.csv` | co-owner, during a ~1 h walk | rooms → freezers/fridges/dewars → shelves/racks, with owners; loaded with `import_locations` |
| `stock-sheet.csv` | each person (personal stock) | one row per item; columns explained below |

- Collect the lab-wide registers first: the LN2/cell-line log, the primer, plasmid and antibody
  lists, and the shared enzyme box. Personal sheets come second.
- Any spreadsheet program works. Keep the header row exactly as it is; leave unknown cells blank.
- In Excel, format the `catalog_number` and `lot` columns as **Text** before typing. Otherwise
  Excel turns numbers like `11965-092` into dates and drops leading zeros.
- Save as **CSV UTF-8 (Comma delimited)**, not plain "CSV". The imports refuse a file that isn't
  UTF-8 and name the first line they can't read.

`locations.csv` columns. Load the file with
`manage.py import_locations locations.csv --lab <slug>`, after the roster. On its own, the command
is a dry run that lists what it would create; add `--commit` to save. Rows may come in any
order. Running it again creates nothing new: paths LabStock already has are left as they are,
and the report lists anything the file says differently. If any row has an error, the command
names the line and saves nothing.

| Column | Required | Values |
|---|---|---|
| `path` | yes | the full path, parts separated by ` / `, e.g. `Room 210 / -80 A / Shelf 1`. A part the file doesn't list (`Room 214` above `Room 214 / LN2 dewar`) is created, with its kind taken from its first word (`Room`, `Freezer`, `Shelf`…). A part named by its temperature (`-80 A`, `-20 B`, `4C door`) becomes a freezer or fridge at that temperature; a top-level part is always a room. |
| `kind` | yes | `room`, `freezer`, `fridge`, `ln2_dewar`, `cabinet`, `bench`, `shelf`, `rack`, `drawer`, `box` (9 × 9), `box_10x10`, `bag` |
| `temperature` | no | `RT`, `4C`, `-20C`, `-80C`, `LN2`; blank = the same as the location it is in |
| `owner_email` | no | for a rack or box that belongs to one person; they must be on the roster |
| `code` | no | the short code on its label: 1–12 letters, digits or `-`, unique across LabStock. Not six letters and digits, because stock item IDs look like that. Blank = generated (5 characters) |
| `notes` | no | anything that helps find it |

`stock-sheet.csv` columns:

| Column | Required | Values |
|---|---|---|
| `owner_email` | yes | whose stock it is |
| `item_name` | yes | as on the label |
| `vendor`, `catalog_number`, `lot` | if known | as on the label |
| `type` | no | `antibody`, `kit`, `enzyme`, `small_molecule`, `recombinant_protein`, `sirna_sgrna`, `serum`, `cell_line`, `lab_made_shared`, `oligo`, `plasmid`, `strain`, `chemical`, `media_buffer`, `consumable`, `other`. Blank is guessed, then reviewed |
| `containers` | no | how many tubes/vials/bottles (default 1) |
| `size` | no | per container, e.g. `100 uL`, `500 mL`, `1 vial` |
| `location` | no | a path like `Room 210 / -20 B / Shelf 2 / Box 4`; blank = "unsorted" for that owner |
| `temperature` | if no location | `RT`, `4C`, `-20C`, `-80C`, `LN2` |
| `position` | no | in the box, e.g. `A1`. A position the box doesn't have (`J1` or `A10` in a 9 × 9 box) is an error on its line, so check the box's kind in `locations.csv`; anything else, such as `top shelf, left`, is kept as written |
| `share` | no | `shared`, `ask-first` (default) or `restricted` (others see only that it exists and whom to ask) |
| `source` | no | `purchased` (default), `previous_lab`, `collaborator`, `lab_made`, `gift`, `outside_purchase` |
| `date`, `expiry` | no | `YYYY-MM-DD` (expiry may be `YYYY-MM`) |
| `opened` | no | `yes` / `no` |
| `mta` | no | `yes` if received under a material transfer agreement (forces `restricted`) |
| `notes` | no | passage, concentration, dilution… |

## 8. Bench checks

- Ask the biosafety officer whether phones are allowed in the BSL-2 rooms.
- Check Wi-Fi in the freezer rooms.
- Ask the PI about a hardware budget for MVP-B: label printer, tablet, scanner.

## Exit criteria

- [ ] Co-owner and champions named
- [ ] Probe facts pinned in `.env`
- [ ] Baseline reviewed with the PI
- [ ] Sender passes the seed test
