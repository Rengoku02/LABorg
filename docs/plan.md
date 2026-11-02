# LabStock — lab inventory + duplicate-order guard (implementation plan)

## 1. Context
- ~30-person, wet-lab-first academic lab with 2 bioinformaticians. The user builds and maintains the app;
  Claude Code writes most of the code.
- PI's complaint: inventory isn't tracked; many members keep personal stock (cell lines, reagents,
  antibodies, kits); two people often buy the same product at the same time → wasted money.
- Purchasing runs through Quartzy, and **everyone orders on their own (no approver)**.
- Goals:
  1. Lab-wide visibility of what exists (including personal stock), what's on order, and what was bought before.
  2. Catch duplicates *before* checkout, or rescue them right after.
  3. Make physical inventory a by-product of purchasing so it stays current.
  4. Grow into a full lab-resource app: freezer maps, cell lines, antibodies, primers/plasmids, chemicals, AI helpers.
- Greenfield: `/Users/Raghuram/claude/lab` is empty. Local tooling: Docker, uv, Python 3.12, Node 26.
- Basis:
  - A 9-agent web-research workflow: commercial and open-source tools, Quartzy's API checked against its
    official OpenAPI spec (77 claims confirmed / 6 refuted), domain needs, adoption, and tech.
  - An adversarial critique of that research, and a two-lens review of this plan (engineering; lab outcome).
  - Raw research (temporary location): `/private/tmp/claude-502/-Users-Raghuram-claude-lab/ef48670f-587c-4600-91d1-402bf967286e/tasks/wuvhwvrt7.output`.
    It is copied to `docs/research/` as the first build step.

## 2. Decisions (locked)
| Topic | Decision |
|---|---|
| Stack | Django 6.1 now; move to 6.2 LTS after 6.2.1 ships (mid-2027; budget 4–8 h). htmx 2.0.x plus small vanilla ES modules, **no Alpine in MVP-A** (it conflicts with a strict CSP). PostgreSQL 18, Django-Q2 (ORM broker, no Redis), Docker Compose. If a dependency won't run on 6.1, replace the dependency, not Django. |
| Build vs adopt | Build from scratch. Borrow InvenTree's Product / SupplierProduct / StockItem split; don't fork it. |
| Quartzy | Stays the purchasing system. The app mirrors it **read-only**: its HTTP client refuses anything but GET. Write-back no earlier than v2, and only after sandbox tests and Quartzy's OK. |
| Physical stock | **The app is the system of record.** During the switch, Quartzy inventory is kept as a raw mirror, linked to app stock and with a cutover date per lab. After MVP-B: export a backup, then, with PI sign-off, clear Quartzy inventory so its lookup stops showing stale stock. |
| Ordering | Self-ordering, no approver. The "turn on Quartzy approvals" lever is parked: revisit only after 6 months of metrics, with your sign-off. |
| Email | Email only, sent through an ESP's HTTPS API (django-anymail) from a lab-controlled domain with SPF/DKIM/DMARC. Two senders: `alerts@` for action-needed mail, `news@` for digests and asks. One-action signed links: the GET shows a confirm page, the POST acts. |
| Hostname | Phase 0 fixes a **permanent DNS name** that you control. It is the base for email links and the sender domain, and later the QR base. MVP-A may run on a temporary host behind it; MVP-B needs it reachable from phones on lab Wi-Fi. |
| Item types | A generic core covers every type from day 1. **Tier 1** = durable, pricey items kept in personal boxes: antibodies (primary and secondary), kits, enzymes, bioactive small molecules/inhibitors, cytokines/growth factors/recombinant proteins, siRNA/sgRNA, FBS/serum lots. **Tier 2** cell lines. **Tier 3** oligos. **Tier 4** plasmids/strains. **Last:** bulk/hazardous chemicals (EHS). Also a "Lab-made / shared stock" type with a lab-pool custodian and a named steward. Equipment stays out of scope for now. |

## 3. Design-shaping facts (verified)
- **No tool covers the gap.** None checks a new request against personal stock, open requests and past
  receipts together.
  - Quartzy's warning fires only on the same catalog # among open requests in the same Quartzy Lab.
  - Its Add Request lookup can be skipped, and Quartzy Connect (Chrome) creates requests with no
    documented duplicate check.
- **Quartzy API** (spec: `https://docs.quartzy.com/redocusaurus/public-api.yaml`):
  - Base `https://api.quartzy.com`; per-user `Access-Token` with no scopes.
  - Reads: `GET /user`, `/healthz`, `/labs`, `/types`, `/order-requests[/{id}]`, `/inventory-items` (with `instances[]`).
  - Lists filter only by `lab_id`/`page` and return bare arrays. Page base, size, sort order and rate limits
    are undocumented. There is no `updated_since`.
  - `updated_at` and `requested_by` are nullable. The Money unit is undocumented.
  - Missing: no comments, no users/roles API, no inventory create, no DELETE.
  - No webhooks for approved/cancelled/updated, so **polling is primary**.
- **Roles in Quartzy:** only Lab Admins can Mark Ordered; Members can cancel only their own *New* requests;
  both can mark Received. **Statuses are unreliable in a self-ordering lab.** "Probably bought" is
  therefore inferred from age.
- **ToS §3.2:** no account sharing, no unreasonable load, no working around limits.
- **Bench reality:** BSL-2 glove rules vs phones; iOS BarcodeDetector is disabled; iOS home-screen apps
  don't share Safari's login. So: Safari-first URL-QR, lab-owned stations, and no free text at the bench.

## 4. Roles (Phase 0 exit criterion: co-owner and champions named)
- **PI:**
  - rule scope: in-scope types and a $ threshold, e.g. $250
  - default visibility *ask-first*, with a "registered ≠ shared" guarantee
  - amnesty dates and the unclaimed-items policy
  - privacy commitments, hardware budget, launch talks
- **Wet-lab co-owner** (lab manager or senior tech, ~1 h/week for 10 weeks):
  - map Quartzy Types → ItemTypes; set the durable list and look-back windows
  - review the top ~40 baseline clusters
  - label shadow matches (~15 min/week)
  - curate vendor families and equivalences
  - walk the lab once (~1 h) to produce the coarse location CSV
- **Quartzy Lab Admin:**
  - audit each member's role and seat (no shared logins)
  - stale-request cleanup
  - test the native warning on web, mobile and Connect
- **You (system owner):** probe, exports, baseline notebook, IT/ESP/DNS emails, build, imports.
- **Deputy** (the other bioinformatician): code review, runbook, access to secrets.
- **3–4 bench champions** (~1 h/week): pilot users, freezer walks, friction log.

## 5. Roadmap
Estimates are focused developer hours. Calendar time goes mostly to real-data testing and rollout.

### Phase 0 — prep, measure, decide (weeks 0–2; ~25–40 h of yours)
Standalone code only (no Django yet): build steps 0a–0d in §10.
1. **People:** roles per §4. Confirm that all 30 people request in ONE Quartzy Lab, each with their own seat.
2. **Quartzy probe** (your token). Measure each item below and pin it with a fixture:
   - page base (0 or 1, and what omitting it does)
   - page size
   - sort key, and whether order is stable between calls
   - where new requests land, and whether editing an old request moves it
   - whether a deleted request returns 404 (sandbox lab)
   - the Money unit: `total_price.amount` vs `unit_price` × qty vs the UI
   - `requested_by` vs `created_by`
   - what QBot requests look like
   - whether `GET /labs` lists every Lab in use, and `uses_instances`

   Then export the full API history plus the UI "All Time" requests export (for status dates) to a
   git-ignored `data/` folder. Email support@quartzy.com: OK for a read-only mirror? OAuth client possible?
3. **Status paths & timing:**
   - % of requests that ever reach ORDERED/RECEIVED; % stuck in CREATED for more than 30 days
   - median Submitted→Ordered gap, and the gap between paired duplicates
   - ask 5 frequent orderers how their last 3 orders actually happened
   - ask the PI's admin to estimate spend outside Quartzy from one quarter of statements
4. **Baseline notebook:**
   - Report an upper bound (all clusters) and a reviewed figure (the co-owner labels the top ~40 by $).
   - Sort each duplicate by why Quartzy missed it: same # in the same Lab while open / different Labs /
     formatting or pack-size variant / received months earlier / another vendor's equivalent / unknown.
   - Seed the generic-catalog stop-list: catalog keys that carry many different item names.
   - Present an anonymized summary to the PI and the lab.
5. **Quartzy hygiene** (Lab Admin):
   - Cancel or receive stale open requests: CREATED/APPROVED older than 30 days; ORDERED/BACKORDERED
     older than 60 days.
   - Test the native warning with a deliberately repeated catalog #.
   - Optional 4-week trial: a required org-level request field "Checked LabStock? (yes / need my own / no)".
     It needs an Org Admin, can't be read by the API, and disables QBot.
6. **Name & mail:**
   - Get a lab domain, or a permanent university subdomain you control.
   - Set up an ESP account via anymail, with SPF + DKIM + DMARC `p=none`.
   - Ask IT to allowlist the sender and, if possible, exempt it from Safe Links.
   - Seed-test inbox placement in Gmail and the university mailbox, on desktop and phone.
7. **Data in:**
   - Collect lab-wide registers first: LN2/cell-line log, primer/plasmid/antibody lists, the shared enzyme box.
   - Then personal sheets, using one template.
   - The co-owner's walk produces the coarse location CSV (room → unit → shelf/rack, owners).
8. **Bench checks:** the biosafety officer's rule on phones; Wi-Fi in freezer rooms; ask for the MVP-B
   hardware budget (label printer, tablet, scanner).

**Exit criteria:**
- co-owner and champions named
- probe facts pinned
- baseline reviewed with the PI
- sender passes the seed test

### MVP-A — visibility + duplicate guard (~150–300 h; the shadow weeks overlap the UI build)
Bench staff get search, "bought before" (24 months) and claims from day 1. The PI gets the guard and the numbers.
- **Quartzy mirror**, running 24/7 from build step 5:
  - head probe + rolling sweep + hot set (§8)
  - status observations; health panel; alerts that survive an outage
- **Product identity** (global catalog):
  - normalization: vendor pack rules run on the raw string first, then NFKC, uppercase, strip separators
  - placeholders (N/A, TBD, custom…) → NULL key
  - `identity = identifying | generic`; generic covers oligos, services, fees
  - provisional products and vendors created at ingest, then reviewed in a queue
  - merge service with redirects
  - manufacturer key for distributor SKUs
- **Minimal inventory:**
  - coarse Location tree seeded from the CSV
  - per-person "unsorted" containers per temperature
  - inline box creation
  - StockItem with custodian + visibility; every change goes through `apply_event` (§7)
  - plain human-readable short-ID box labels, browser-printed; QR codes come in MVP-B
- **Getting stock in, without typing at the bench:**
  - **Claim your history:**
    - New items surface weekly: 7 days after receipt, or 21 days after the request.
    - The list is sorted by price, shows progress, and allows "done for now".
    - Outcomes: *Still have it* (pick or create a box, or *place later*) / *Used up* / *Gave to X* /
      *In the shared stock* / *Not mine* / *Did this arrive?*
    - Champions also work a lab-pool list of departed members' durable items.
  - **Add what I have** (desk form):
    - typeahead over products from Quartzy history
    - source: previous lab / collaborator / lab-made / gift / outside purchase
    - an outside purchase also creates an `OrderRequest(source=outside)`, so the guard sees it
    - collaborator + MTA material defaults to *restricted*
  - **Register a box:** owner, location, type tags, lid photo. The box is searchable before it's itemized.
  - **`manage.py import_stock`** for registers and legacy sheets: canonical CSV → services, dry-run by
    rollback, review CSV. Time-boxed to ~20 h, durable types first.
  - **Quartzy inventory:**
    - raw mirror + `ExternalRef`
    - a whole-number quantity ≤20 splits into containers; anything else becomes an aggregate with "split later"
    - claims offer "This is the same bottle"
- **Check before you buy:**
  - `/s?q=` takes a name, a raw catalog # or a **pasted vendor URL**. Server-side URL patterns cover
    Thermo, Fisher, Sigma, CST, Abcam, NEB, ATCC, IDT, Qiagen and VWR; v1's extension reuses them.
  - One-click setup of a **browser address-bar keyword** (`ls 2118S`) and a **bookmarklet** (selected text
    or page URL). Both are on the cheat sheet and in every alert footer.
  - Result groups: *In the lab* (shared stock first) / *On order* / *Bought before* / *Probably — ask X*
    (unverified). Restricted items show that they exist and whom to ask, never where they are.
  - **Ask owner** sends a personal email.
  - **Ask the lab** sends per-recipient emails with signed *I have it / Maybe / No* buttons and Reply-To
    set to the asker. No inbound mail parsing. At most 1 ask per person per day, batched twice daily
    unless urgent.
- **Duplicate guard** (§9):
  - T1 against open requests goes live after 1 week of shadow, if the historical replay passes.
  - The other tiers go live after 2–4 weeks of shadow.
  - Alerts branch on the person's answer to "already ordered?"
  - For in-scope requests during the first 8 weeks, a "No match — clear to order" receipt.
- **In-flight board & nudges:**
  - Open requests grouped by product; "requested, not marked ordered"; "probably arrived" after 60 days.
  - Nudge 21 days after `requested_at` if not RECEIVED/CANCELLED: *It arrived / Still waiting /
    Never ordered — cancel (opens Quartzy)*.
- **Email budget:**
  - a personal Monday digest, sent only when it has content
  - a weekly lab-level PI summary (no names)
  - the dashboard (§11)
  - at most ~3 non-alert emails per person per week
- **Fast-follow** (~2–3 days): primer finder. Import IDT spec sheets and lab primer lists; search by name,
  gene or sequence (exact / reverse-complement / substring).

### MVP-B — physical stock the app owns (~100–150 h)
- **Gate:** the permanent hostname is reachable from lab Wi-Fi.
- **Locations:** QR stickers carrying the same short IDs; freezer templates; box types with grid or
  linear positions; position swaps done as two-step service moves.
- **Receiving → put-away:**
  - Trigger: RECEIVED appears in the sync, someone taps "it arrived (n of m)", or a claim.
  - Whoever opens the box does put-away at the station, and the requester is told where it went.
  - Perishables are flagged by type and temperature.
  - In Quartzy, receivers choose "Do Not Add To Inventory"; the app raises a drift flag if Quartzy items
    appear anyway.
- **Bench pages** `/i/<id>` and `/l/<code>`:
  - buttons: *Opened · Running low · Empty · Moved · Discard (reason)*
  - *Empty* re-runs the guard, then links to Quartzy "Request Again"
- **Labels:**
  - Box-level by default. Item labels only for antibodies, kits, cell-line boxes and items ≥$X.
  - HTML/CSS print sheets with an inline SVG QR (segno); optional Brother QL.
  - WeasyPrint for PDF door sheets.
  - Run a 1-week adhesion test at −80 °C and in LN2 before bulk printing.
- **Freezer station mode:** a lab tablet; tap name + PIN; auto-logout after 2 minutes.
- **Leavers:** the Membership end date triggers an "estate sale" list 30 days ahead; unclaimed items go to
  the lab pool.
- **Disaster prep:** a weekly door-sheet PDF per freezer; an emergency "move freezer X into Y".
- **Freshness:** a custodian self-check each month (confirm 1–2 of your own boxes) plus a full recount at
  the annual clean-out.
- **Quartzy inventory end state:** export a backup, then clear it (PI sign-off).

### v1 — depth (pick by demand; ~200–300 h)
- **Decision rule:** if `alert_in_time` stays under ~40% and search-before-buy stays low after 6 live
  weeks, a minimal URL-pattern Chrome/Edge extension comes first.
- **Freezer UX:** box-grid UI and box-map import; aliquots with lineage and a freeze–thaw count;
  Holds + borrow/lend.
- **Scanning:** barcode census first; then barcode-detector + self-hosted zxing-wasm, keyboard-wedge
  scanners, and GS1 parsing via biip. GTINs are learned at first receipt.
- **Stock levels:** expiry and use-first ordering; low-stock thresholds counted in pack-equivalents.
- **Typed schemas:**
  - antibody: RRID, host/clone/conjugate, dilution table, new-lot re-validation alert
  - cell line: Cellosaurus lookup, ICLAC misidentified-line warning, vials with passage and freeze date
  - the full oligo registry
- **Enrichment:** Cellosaurus, PubChem, Antibody Registry, UniProt, NCBI, Addgene (its token takes about
  5 days to issue). The matcher's T3/T4 tiers and scoped substitution links go live by measured precision.
- **Platform:** django-ninja API; SSO + passkeys; an email-in mailbox for outside purchases.
- **AI photo intake** (behind a flag; read the claude-api skill when building it): labels, packing slips
  and box lids become a prefilled form with per-field confidence, and a person always confirms.

### v2 / later
- **Cell banking:** master/working banks; passage/PDL ledger; mycoplasma/STR schedules (CLASTR).
- **Molecular biology:** plasmid registry (GenBank/SnapGene via Biopython, map viewer); strain lineage;
  sequence-level duplicate checks on IDT/Twist pages.
- **Chemicals:** GHS autofill (PubChem); SDS store with AI extraction; peroxide-former timers;
  regulated-material flags; EHS export.
- **AI and integrations:** natural-language Q&A over read-only tools; a read-only MCP server.
- **Lab economics:** surplus marketplace and neighbouring labs; group buys; spend analytics; RRID /
  methods-section export.
- **Later:** equipment booking (eLabFTW/NEMO), rack scanners, freezer monitors, forecasting.

## 6. Architecture & stack
- **Compose services:**
  - `caddy`: TLS (DNS-01 if no inbound 80/443); scrubs tokens from its logs
  - `web`: Gunicorn
  - `worker`: `qcluster`
  - `db`: postgres:18, with the volume mounted at **`/var/lib/postgresql`** (the v18 layout)
  - dev only: `mailpit`
- **Backups** run from a host timer or a supercronic sidecar, **not from Django-Q2**: `pg_dump -Fc` plus
  media → restic, off-site. The monthly restore drill includes `down && up` and a row-count check.
- **Monitoring:** an external dead-man's switch (healthchecks.io) watches:
  - a worker heartbeat every 5 min
  - each lab sync
  - each backup

  It alerts both the owner and the deputy. The app shows a banner when the last sync is more than 30 min
  old. Error tracking runs with token scrubbing.
- **Pins:**
  - Django `>=6.1,<6.2`
  - django-allauth `>=65.21`
  - django-q2 `>=1.11`
  - django-simple-history `>=3.12`
  - django-treebeard `>=7.0.3` (don't subclass TreeAdmin)
  - django-anymail `>=15.1`
  - httpx, tenacity, pydantic v2, RapidFuzz
  - `requires-python >=3.12,<3.14`, with the same minor version in the Dockerfile
  - MVP-B adds segno, WeasyPrint (+ Pango/HarfBuzz) and brother_ql_next
  - v1 adds django-ninja and biip
  - Not used: django-import-export (it bypasses the stock ledger) and `rules`.
- **Config:**
  - `Q_CLUSTER = {orm: 'default', workers: 2, timeout: 600, retry: 900, max_attempts: 2, ack_failures: True}`.
    Task arguments are ids only.
  - Email uses Django 6.1 **`MAILERS`** (`EMAIL_*` is deprecated): the anymail ESP in prod, Mailpit in dev.
  - `DatabaseCache` backs allauth's rate limits.
  - Proxy and HTTPS: `ALLAUTH_TRUSTED_PROXY_COUNT=1`, `SECURE_PROXY_SSL_HEADER`, `CSRF_TRUSTED_ORIGINS`.
  - `SECURE_CSP` with a nonce-based `script-src 'self'`, enforced in dev and tests too.
  - htmx-config: `{allowEval:false, includeIndicatorStyles:false, historyCacheSize:0}`; CSRF goes through
    `hx-headers` on `<body>`.
- **Frontend:** server-rendered templates with vendored htmx and no JS build step. Django admin is the
  steward's back office; stock state fields are read-only there.

## 7. Data model
- **Tenancy:**
  - The catalog is **global**: VendorFamily, Vendor, VendorAlias, Product, SupplierProduct, ProductCode,
    Lot, ItemType. A `LabProduct` overlay holds min stock, synonyms, notes and steward.
  - Tenant data carries a `lab` FK: Location, StockItem, StockEvent, QuartzyLab (and through it
    OrderRequest), Notification, DuplicateCheck, PolicySetting, HistoryClaim, AskThread.
  - Scoping is enforced by `for_lab()` managers, a request.lab middleware, and service checks
    (`item.lab == location.lab`).
- **People:**
  - Custom `User`.
  - `Membership` (user, lab, role, status active/rotator/leaver, start/end).
  - `PersonAlias` resolves in order: Quartzy user id → lowercased email → manual queue. If `requested_by`
    is null, use `created_by`.
  - Former members become inactive placeholder users. Each lab has a "Lab stock" pool user.
  - `requested_for` exists only in the app.
- **Catalog:**
  - `Product`: `provisional`, `merged_into` (self-FK), `attributes` JSONB, identifiers.
  - `SupplierProduct`:
    - vendor, `catalog_raw`, nullable `catalog_key`, `catalog_base`
    - `mfr_family` + `mfr_catalog_key`
    - `identity`
    - `containers_per_pack`, `amount_per_container` + unit, `unit_size_raw`, last price
    - unique on (vendor_family, catalog_key) where the key is not null
  - `ProductCode`: GTIN; pack-level, not unique.
  - `Lot`: (product, `lot_key`) unique, plus `lot_raw` and expiry + precision.
  - `ItemType`: key, schemas, tracking_mode, `identity_rule` (including `none`), durable, look-back
    months, in-scope. QuartzyType has an FK to ItemType.
- **Inventory:**
  - `LocationType`: structural, position scheme grid/linear/none, rows/cols, prefix.
  - `Location`: treebeard node with a code; its default custodian is used only at put-away. Moves write a
    `LocationEvent`.
  - `StockItem`:
    - `short_id`; product, supplier_product?, lot?, `parent`
    - custodian; `visibility` = shared / ask-first / restricted
    - location + position label/index
    - `lifecycle` = unverified / available / opened / empty / discarded / lost
    - `level` = ok / low
    - optional qty + unit, opened_at, expiry, `attributes` JSONB
    - `source`, `source_order_request`, `is_aggregate`, `version`
  - `ContainerCode`: unique per container.
  - `ExternalRef` (stock_item, system, external_id): one Quartzy instance can map to many StockItems.
  - `StockEvent`: stock_item, `seq` unique per item, type, qty_delta, from/to location & custodian,
    reason, user, `occurred_at` (can be backdated) vs `recorded_at`, `client_uuid` unique when not null,
    source.
  - `Hold` arrives in v1.
- **`apply_event(item_id, event, expected_version)`** is the only writer:
  - In one transaction: `select_for_update`, reject a stale version, append the event with the next seq,
    update cached fields, bump the version.
  - `StockItem.save()` raises if a state field changes outside it.
  - Retention means pseudonymizing users on events, never deleting events.
- **Quartzy mirror:**
  - `QuartzyLab`: `alerts_armed_at`, sweep cursor, `cutover_at`.
  - `QuartzyType`.
  - `OrderRequest`:
    - `quartzy_id`; source quartzy/outside/gift; status; raw fields
    - `payload_sha256`, `raw_payload`
    - resolved supplier_product/product + `resolution_status`
    - prices and quantities as Decimal
    - requested_by/created_by aliases, `requested_for`, `requested_at`, `quartzy_updated_at`, `app_url`
    - `first_seen_at`, `deleted_in_quartzy_at`
  - `OrderStatusObservation`: from/to, observed_at, quartzy_updated_at, via.
  - `QuartzyInventoryItem` / `QuartzyInventoryInstance` (raw), `SyncRun`, `SyncError`.
- **Guard & comms:**
  - `DuplicateCheck`: subject, matcher_version, shadow, `quartzy_native_would_fire` (raw and normalized),
    `searched_before`, `alert_in_time`.
  - `DuplicateMatch`:
    - tier, score, `candidate_snapshot`
    - `candidate_stock_item` or `candidate_order_request` FK, with a check constraint that exactly one is set
    - `suppressed`
    - label true_dup / related_not_dup / false, with labelled_by and labelled_at
  - `Outcome`: kind, reason pick-list, decided_by, `verified_at`, `est_value_avoided`.
  - `PairSuppression`: subject, candidate, scope requester|lab, `expires_at`.
  - `HistoryClaim`: order_request, person, outcome, count_kept, target_person, stock_items, round.
    Unique on (order_request, person).
  - `AskThread` / `AskReply`.
  - `Notification`: kind, subject, recipient, nonce, sent_at, esp_message_id, acted_at. Unique on
    (kind, subject, recipient).
  - `SearchLog` (30-day retention), `PolicySetting`, `FrictionReport`.
- **Availability** is counted in pack-equivalents only. Quartzy purchase units are never added to volumes.

## 8. Quartzy sync (read-only)
- **Client:**
  - The httpx transport allows GET only, and a unit test checks it.
  - Permissive pydantic models (`extra='allow'`) that log unknown fields.
  - Per-record errors go to `SyncError`, so one bad row never aborts a page.
  - Decimal parsing; the Money unit comes from the probe.
- **Cycle** (per QuartzyLab, under a single-flight Postgres advisory lock):
  - **Head probe:** start from the measured insertion end and keep reading while pages contain unknown ids
    (≤5 pages). Every 2 min from 07:00 to 20:00, every 10 min otherwise.
  - **Rolling sweep:** 2–4 more pages from the saved cursor each cycle, so all of history is re-read every
    few hours (≈50–70 calls/h at ~4,000 requests).
  - **Hot set:** refresh by id the requests with a pending alert, outcome or cancel verification.
  - **Nightly:** inventory and types. **Weekly:** diff `public-api.yaml`.
- **Change detection:** a sha256 of the canonical payload; `updated_at` is only a hint. A status change
  writes an observation.
- **Deletion:** only after `GET /order-requests/{id}` returns 404, which sets `deleted_in_quartzy_at`.
  More than 5% 404s in one run → abort and alert, because that means a token or permission problem.
- **Long work:** split into self-re-enqueuing chunks that stay well under the 600 s timeout. The history
  backfill is a management command.
- **Contingency:** if the API has no usable time order, use the `order-request.created` webhook as a hint
  (needs public HTTPS) and keep polling as reconciliation.

## 9. Duplicate guard & alerts
- **Candidates:**
  - active StockItems (unverified ones show as "probably — ask X")
  - open requests, i.e. any status other than RECEIVED/CANCELLED, with their age; after 60 days they show
    as "probably arrived"
  - "probably bought" requests (not CANCELLED, older than 14 days) inside each type's look-back window:
    24 months for tier 1, none for consumables
- **Tiers:**
  - **T1:** exact `catalog_key` (or manufacturer key) within a vendor family.
  - **T1x:** exact key across vendor families. Only when the key has ≥7 characters *and* the item type
    matches or name trigram similarity is ≥0.2. Measured separately.
  - **T2:** same `catalog_base`, different pack size.
  - **T3:** shared identifiers (v1).
  - **T4:** fuzzy names, search-only until its precision is measured.
  - Generic SupplierProducts never match on catalog #. The dashboard shows the % of requests the guard
    can't check.
- **Go-live, per tier:**
  - Requires a labelled replay of the last 6–12 months plus live shadow data, with ≥30 labelled matches
    and ≥85–90% precision. Below that, the tier stays digest-only.
  - T1 against open requests may go live after 1 week of shadow.
- **Safety:**
  - Alert only when `requested_at ≥ alerts_armed_at`.
  - Late detections (first seen more than 6 h after `requested_at`) get post-order wording in the digest.
  - **Burst hold:** more than 8 new requests from one creator within 15 min are held for the owner to review.
  - **Circuit breaker:** more than 10 alerts in one cycle → hold them all and email the owner a one-tap release.
  - Global and per-tier kill switches, checked at send time.
  - Sends are queued via `on_commit` and are idempotent.
- **Recipients:**
  - the requester, always
  - for an open-request match, also the earlier requester (they can share the order)
  - stock owners only when the requester taps *Borrow*
  - for a "bought before" match: *Ask Lee if she still has it*
- **The alert** (one email per request, sent within one sync cycle):
  - **Subject:** "Before you order: Priya has CST 2118S (GAPDH 14C10) in −20 B".
  - **Body:** the request line, then the best match (owner, location via the presenter, date, why it
    matched), then **"Have you placed the vendor order yet?"**
    - **Not yet:** *Borrow from Priya* / *Share Raj's order* / *Cancel my Quartzy request* (opens Quartzy,
      and names the Lab Admin when the requester can't cancel).
    - **Yes:** *I'll cancel with the vendor* (shows the vendor's contact and the confirmation #) /
      *Keep it, offer the spare*.
    - **Always:** *Not the same thing* / *Need my own* (lot / application / urgent / other).
  - **Footer:** "Only you receive this; the PI sees lab totals only", plus the search-keyword tip.
  - No chaser emails; unresolved alerts appear in the personal digest.
- **Suppression:**
  - recurring consumables bought N or more times a year, unless two are open at once
  - excluded Types
  - `PairSuppression`: scoped to the requester by default, made lab-wide only by the co-owner, and it expires
- **$ avoided** counts only *verified* cancel/borrow/join outcomes: CANCELLED is seen in the sync, the vendor
  cancellation is confirmed, or the borrow is recorded. It must also have no equivalent request from the
  same person within 30 days, and it excludes what Quartzy's own rule would have caught.

## 10. Repo layout & build order
```
/Users/Raghuram/claude/lab/
  pyproject.toml uv.lock Dockerfile compose.yaml compose.override.yaml Caddyfile .env.example
  labstock/settings/{base,dev,prod}.py  urls.py  wsgi.py
  apps/core/       User, Membership, PersonAlias, Lab, PolicySetting, permissions.py, signed actions, mail
  apps/catalog/    normalization.py (pure Python, no ORM imports), vendor rules, products, merge/resolution services
  apps/quartzy/    client.py, mirror models, sync.py, tasks.py, health view, quartzy_sync/quartzy_backfill
  apps/inventory/  locations, stock, services.py (apply_event), presenters.py, imports, claims, asks
  apps/guard/      matcher.py, suppression.py, checks/outcomes, alerts, shadow labelling, replay, baseline.py
  apps/bench/      mobile views: search, in-flight board, claims, add/register, confirm pages
  apps/reports/    dashboard, digests
  scripts/quartzy_probe.py  notebooks/baseline_duplicates.ipynb  data/ (git-ignored)
  docs/            research/, runbook.md, privacy.md, cheat-sheet.md
  tests/           pytest-django, fake_quartzy.py (stateful), fixtures/
```
| Step | Work | h |
|---|---|---|
| 0a | Copy the research JSON and a summary into `docs/research/`; `git init` | 1 |
| 0b | `scripts/quartzy_probe.py` (httpx only): raw export + the probe measurements | 4–8 |
| 0c | `apps/catalog/normalization.py` + table tests built from real exported strings | 4–8 |
| 0d | Baseline notebook, importing 0c | 6–12 |
| 1–2 | Scaffold: uv, Django 6.1, split settings, custom User, compose (db, mailpit), pytest, ruff, pre-commit, CSP and htmx config in `base.html`. Core: Membership + roster command, closed signup, staff MFA, admin login through allauth, MAILERS, signed actions, permissions, presenter | 10–15 |
| 3 | Catalog: models, provisional products, merge service, stop-list, vendor seed, ItemType seed + QuartzyType map, review queue | 15–25 |
| 4 | Quartzy sync: client, mirror, locks, Q2 schedules, backfill, health panel, fake-Quartzy tests | 20–35 |
| 5 | Deploy the minimal prod stack (Caddy, web, worker, db, backups, dead-man's switch, ESP + DNS) → **24/7 mirror starts**. ★ Re-estimate checkpoint | 10–20 |
| 6 | Guard in silent shadow + labelling page + historical replay | 25–40 |
| 7 | During the shadow weeks: locations seed, unsorted containers, `apply_event`, claims, add/register, `import_stock`, Quartzy-inventory link | 25–40 |
| 8 | Search (URL patterns, keyword, bookmarklet), asks, in-flight board, nudges, digests, dashboard; alerts go live tier by tier | 20–35 |
| 9 | Pilot fixes | 15–30 |
- Imports run as a separate track (≤20 h).
- Non-developer time: co-owner ~1 h/week; champions ~1 h/week; Lab Admin ~2 h in Phase 0.

## 11. Rollout & metrics
- **Announcements are tied to tools that have shipped:**
  - **Phase 0 meeting:** the project, the anonymized baseline, the asks (registers, spreadsheets), Quartzy hygiene.
  - **MVP-A launch:** a 10-min live demo that searches for items the audience names. Then the rules that
    now have tools: search first for in-scope items, give a reason when there's a strong match, log
    outside purchases and gifts.
  - **Amnesty fortnight, 2 weeks after launch:** a freezer-by-freezer walk; each box gets an owner sticker
    and a registration (~30 s/box). Unregistered boxes found at the next clean-out sit on an "unclaimed"
    shelf for 4 weeks, then go to the lab pool.
  - **Put-away rule** at MVP-B launch.
  - **Follow-up at 3–4 weeks:** the top 3 friction fixes.
  - **Ongoing:** a 2-minute monthly PI slot; onboarding for new members and rotators.
- **Dashboard** (lab level only):
  - reviewed avoidable-duplicate $ per quarter, vs the same quarter last year
  - `alert_in_time` rate
  - search-before-buy rate for in-scope requests
  - coverage: % of in-scope requests from the last 24 months claimed or resolved; boxes with an owner,
    per freezer
  - asks that ended in a borrow
  - precision per tier
  - % of requests the guard can't check

  The usability targets ("who has X" <10 s; ≤2 taps per update; put-away <30 s) are pilot tests, not
  dashboard metrics.
- **Privacy** (`docs/privacy.md` + the launch talk):
  - The PI sees lab totals and custodianship, never individual behaviour (searches, outcomes, reasons).
  - No per-person duplicate counts.
  - Search logs are deleted after 30 days.
  - Override reasons are a pick-list used for tuning only.
  - Lenders are named only if they opt in.
  - Check works-council/GDPR rules if the lab is in the EU.

## 12. Security
- **Closed signup:** accounts are provisioned from a roster; login-by-code only for active members;
  90-day sessions.
- **Admin:** login goes through allauth (rate limits); TOTP MFA is required for staff from MVP-A;
  optionally restrict `/admin` to campus/VPN in Caddy.
- **Signed links:**
  - `TimestampSigner(salt)` over {notification, recipient, nonce}; only the nonce is stored, and rotating
    it revokes the link; 14-day max age.
  - Single-use POST under `select_for_update`.
  - The confirm page is a plain form ("Recording as X — not you?") with no htmx or JS triggers.
  - A claim-list link opens a 7-day session scoped to that person's claim list.
- **Visibility:** `presenters.present_item(item, viewer, channel)` is the only renderer for pages, emails,
  digests, CSVs and partials. Permissions are plain predicates in `apps/core/permissions.py`.
- **Secrets:** env vars, plus a password manager the deputy can open (Quartzy token, ESP key, restic
  password). The runbook records the token owner's Quartzy role.

## 13. Verification
- **Unit tests:**
  - normalization table: Gibco vs Fisher forms, NEB pack suffixes, Excel-mangled numbers, placeholders → NULL
  - matcher tiers, including generic SKUs and T1x
  - suppression scopes
  - **redaction matrix:** a restricted fixture × every renderer × every viewer role must never show a
    location, plus a grep for direct `item.location` in templates
  - signed links: expiry, tampering, single use, GET/HEAD never mutate, no `hx-trigger` on the confirm page
  - ledger:
    - folding events by seq equals the cached state
    - a stale version is rejected
    - `client_uuid` is idempotent
    - the admin form has no state fields
    - the `save()` guard holds
- **Sync tests,** all against a **stateful fake Quartzy** (respx over an in-memory list with configurable
  page size, sort order, inserts mid-scan, deletes, 404s, 429/5xx and null fields):
  - double and concurrent runs of the sync and send tasks → identical observations, exactly one email
  - time-machine tests for arming, late alerts, burst hold, circuit breaker, nudges and digests
  - fixtures validated with openapi-core (additionalProperties relaxed) plus an unknown-field report
- **Other automated checks:**
  - a TransactionTestCase with two connections writing to one StockItem
  - `Q_CLUSTER sync=True` in tests
  - `pytest -W error::DeprecationWarning` for first-party code
  - CSP enforced in tests
- **Integration:**
  - `docker compose up`, then `manage.py quartzy_sync --dry-run` with the live token
  - counts reconcile with the Quartzy UI
  - shadow precision report
  - email seed test
- **E2E** (Playwright, mobile viewport, CSP on):
  - search by pasted URL → ask owner
  - claim history
  - alert → confirm outcome
  - (MVP-B) receive → put-away → scan → empty
  - Manual check: a real iPhone and Android phone in the freezer room (camera → Safari, session persists).
- **Ops:**
  - restic restore drill into a fresh stack
  - `check --deploy` passes
  - the dead-man's switch fires when the worker is stopped

## 14. Top risks → mitigations
| Risk | Mitigation |
|---|---|
| The alert lands after checkout | Pre-order hooks (pasted-URL search, keyword, bookmarklet), 2-min polling, branching alert, post-order rescue, extension rule |
| Alert fatigue or an alert storm | Shadow mode, tier gates, arming, burst hold, circuit breaker, kill switches, digests |
| Mail goes to junk | Lab domain, ESP, SPF/DKIM/DMARC, IT allowlist, seed tests, bounce tracking |
| Hidden stock stays hidden | Claims, add-what-I-have, register-a-box, amnesty walk, unclaimed-shelf policy |
| Quartzy API change or ToS issue | Read-only GET-only client, spec diff, polite polling, written OK from Quartzy |
| Developer becomes the lab's data clerk | Wet-lab co-owner (Phase 0 exit), champions, RACI |
| Bus factor of one | Deputy, runbook, tests, external heartbeat, shared password manager |

## 15. Open items (don't block MVP-A)
- Hosting provider; SSO type (v1).
- Hardware budget (MVP-B).
- Biosafety rule on phones; Wi-Fi in freezer rooms.
- Country / privacy law; regulated materials; does campus EHS already have a chemical system?
- Is AI use OK under the data policy (v1+)?
