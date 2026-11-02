# LabStock

A lab-wide inventory and duplicate-order guard that works next to Quartzy.

- **Problem:** inventory isn't tracked, personal stock is invisible, and two people buy the same
  thing in the same week.
- **Approach:** Quartzy stays the purchasing tool. LabStock keeps a read-only mirror of it, checks
  every new request against what the lab already has, is buying, or bought before, and emails the
  requester before (or right after) checkout. It also becomes the lab's record of physical stock.

The approved plan is in [`docs/plan.md`](docs/plan.md), and the research behind it is in
[`docs/research/`](docs/research/README.md).

## Status

MVP-A is built: the read-only Quartzy mirror, the duplicate guard (silent shadow mode, replay,
labelling, then alerts tier by tier), claims and intake, search before you buy, asks, the
in-flight board, nudges, digests and the lab-totals dashboard. Going live waits on Phase 0
(below): the permanent hostname and email sender, the Quartzy token, and the people. The
module contracts are in `docs/design/mvp-a.md`; members get `docs/cheat-sheet.md`.

## Development

Requirements: Docker, [uv](https://docs.astral.sh/uv/). On Apple Silicon, make sure the venv
uses an arm64 Python (`uv venv --python cpython-3.13-macos-aarch64-none`).

```bash
docker compose -f compose.dev.yaml up -d   # Postgres 18 on :54329, Mailpit on :8025
uv sync --all-groups                       # app + dev + notebook dependencies
uv run python manage.py migrate
uv run python manage.py createcachetable
uv run python manage.py seed_catalog       # vendors and item types; safe to re-run
uv run python manage.py seed_inventory     # location types (rooms, freezers, boxes…); safe to re-run
uv run python manage.py bootstrap_lab --name "Our Lab" --slug our-lab --time-zone America/New_York
uv run python manage.py provision_members roster.csv --lab our-lab           # dry run
uv run python manage.py provision_members roster.csv --lab our-lab --commit
uv run python manage.py import_locations locations.csv --lab our-lab         # dry run
uv run python manage.py import_locations locations.csv --lab our-lab --commit
uv run python manage.py import_stock stock-sheet.csv --lab our-lab            # dry run + review CSV
uv run python manage.py import_stock stock-sheet.csv --lab our-lab --commit
uv run python manage.py link_quartzy_inventory --lab our-lab                  # dry run
uv run python manage.py createsuperuser
uv run python manage.py runserver
```

- Signup is closed. Members sign in with an emailed code; in dev you'll find it in Mailpit at
  <http://localhost:8025>.
- Roster CSV columns: `email, full_name, role, status, start_date, end_date, quartzy_lab_admin,
  champion` (`docs/templates/roster.csv`).
  - `role` is one of `member`, `co_owner`, `pi`, `admin`.
  - `status` is one of `active`, `rotator`, `leaver`.
- Locations CSV columns (`docs/templates/locations.csv`, explained in
  `docs/phase0-checklist.md` §7): `path, kind, temperature, owner_email, code, notes`. Re-running
  the import creates nothing new; run it after the roster, because owners must be members.

Tests and lint:

```bash
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
```

Browser tests (Playwright, phone-sized, failing on any CSP violation):

```bash
uv run --group e2e playwright install chromium   # once
uv run --group e2e pytest -m e2e
```

A demo lab to click through (synthetic people, freezers, stock and the sample Quartzy history,
replayed by the guard): run it on a development database only.

```bash
uv run python manage.py seed_demo --password demo-only-password
uv run python manage.py runserver   # sign in as priya.patel@example.edu, or morgan.hale@example.edu (co-owner)
```

## Phase 0: measure Quartzy and the baseline

The full to-do list, with draft emails to IT and to Quartzy and the spreadsheet templates, is in
[`docs/phase0-checklist.md`](docs/phase0-checklist.md).

Create a Quartzy access token (Profile Settings → Access Tokens). A Lab Member token is enough,
because LabStock only reads.

```bash
export QUARTZY_ACCESS_TOKEN=...            # never commit it
uv run python -m scripts.quartzy_probe measure   # page base, sort order, money unit, labs → env lines
export QUARTZY_LAB_IDS=...                 # if the token sees several Quartzy labs: the lab's own
uv run python -m scripts.quartzy_probe export    # read-only copy of those labs into data/ (git-ignored)
uv run jupyter lab notebooks/baseline_duplicates.ipynb
```

The token sees every Quartzy lab its owner belongs to, a collaborator's included. If it sees more
than one, `export` refuses until you name the lab's own in `QUARTZY_LAB_IDS` (or with `--lab-id`,
once per lab). The export, and so the baseline, then cover only those labs.

The notebook produces the "before" duplicate figure. It writes a review CSV that the lab's
wet-lab co-owner labels; the reviewed figure is the one to show the PI.

## Production

`compose.yaml` runs these services:

- Caddy (TLS)
- Gunicorn
- a Django-Q2 worker
- PostgreSQL 18

```bash
cp .env.example .env   # fill in secrets, the permanent hostname, the ESP key, the Quartzy token
docker compose up -d --build   # web runs migrate, createcachetable, seed_catalog, setup_schedules
docker compose exec web python manage.py bootstrap_lab --name "Our Lab" --slug our-lab
docker compose exec web python manage.py quartzy_backfill --lab our-lab   # needs seed_catalog
```

`seed_catalog` runs on every web start: it is idempotent and keeps what people changed. The
Quartzy backfill refuses to start without its item types, which decide how requests group.

If the token can see more than one Quartzy lab (a collaborator's, say), the backfill mirrors
none of them until you list yours in `QUARTZY_LAB_IDS` in `.env`, or switch them on in the
admin. It prints which labs it left off. After an update that changes catalog-number rules,
re-key and re-resolve as `docs/runbook.md` describes (`refresh_catalog_keys`, then
`quartzy_resolve --all`).

The duplicate guard starts in silent shadow mode: every new request gets a check, nothing is
emailed. Replay the history and label matches while the shadow weeks run:

```bash
docker compose exec web python manage.py guard_replay --lab our-lab --months 12
docker compose exec web python manage.py guard_report --lab our-lab   # per-tier gates
```

The co-owner labels matches at `/guard/label/`.

Backups run from the host: `scripts/backup.sh` does `pg_dump` plus media, sent to restic.
Monitor them with an external dead-man's switch. Operations are covered in `docs/runbook.md`.

## Layout

| Path | What |
|---|---|
| `apps/core` | users, lab memberships, policies, signed one-action email links, email queue |
| `apps/catalog` | global product identity: vendors, catalog-number normalization, products, lots |
| `apps/quartzy` | read-only Quartzy client, mirror models and sync |
| `apps/guard` | duplicate matcher, shadow checks, replay, labelling (`/guard/label/`), Phase-0 baseline analysis |
| `apps/inventory` | locations, stock items and their ledger (`apply_event`), the presenters that decide who sees what, CSV imports, claims and intake |
| `apps/bench` | mobile pages: home, search before you buy (`/s`, address-bar keyword, bookmarklet), ask the lab, the in-flight board, item and box pages, my stock, claim your history (and the email's scoped link), add what I have, register a box |
| `apps/reports` | the lab-totals dashboard (`/reports/`), nudges, the personal digest, the PI's weekly lab summary, the email budget |
| `scripts/quartzy_probe.py` | Phase-0 measurements and export (standalone) |
| `notebooks/` | baseline duplicate analysis |
