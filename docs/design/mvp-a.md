# MVP-A design: inventory, claims and the duplicate guard

This file turns plan §5 (MVP-A), §7 (data model), §9 (guard), §11 (privacy) and §12 (security) into
module contracts, so the pieces can be built in parallel and still fit together. Where this file
and the plan disagree, the plan's intent wins; fix this file.

Build order: **inventory core** → (**guard core** ∥ **imports** ∥ **bench intake**) → **guard alerts**.
Step 8 (search, asks, in-flight board, nudges, digests, dashboard) comes after.

## 0. Conventions that apply everywhere

- Tenant data carries `lab`. Querysets get a `for_lab(lab)` manager method, and services check
  that related rows share the lab (`item.lab == location.lab`). Views use `request.lab`
  (set by `CurrentLabMiddleware`) and `apps.core.permissions` predicates.
- **Only `apps.inventory.services.apply_event` changes stock state** (§2). Only
  `apps.inventory.presenters` renders stock for people: pages, emails, digests, CSVs, partials.
  Templates never read `item.location`, `item.position_*` or `location.*` of a stock item
  directly; a test greps for it.
  - **Presenter naming convention:** a template reads stock only from presenter views
    (`ItemView`, `LocationView`, or a later app's own view dataclass), bound to a name that
    ends in `_view` (`_views` for a list): `{{ item_view.location_path }}`,
    `{% for box_view in box_views %}`, `{% with match_view=... %}`. A `_view` name is bound
    only to a view or part of one (`match_view.item`, `item_views.0`), never to a model row.
  - A later app's own view (a claim row, a duplicate match) is a frozen dataclass built in
    Python. It takes stock fields only from `present_item` / `present_items` output (embed
    the `ItemView`), never from the model, so the visibility rules stay in one place.
  - A page that lists what is in one place (a box's page, a freezer's with everything inside
    it, a shelf's boxes) gets its views with `listed_in=` that place (§2). It then leaves out
    what the viewer may not know is there, since the page itself is that place.
  - `tests/inventory/test_templates.py` scans every template under `templates/` and
    `apps/*/templates/` (hidden files such as `.DS_Store` aside), whatever the object (an
    OrderRequest's quantity, a HistoryClaim's notes, a form field named `location`). It fails
    on:
    - a read, from a name that doesn't end in `_view`, of an attribute that names a place
      (`location`, `location_id`, `to_location`, `location_path`…), walks to one or gives its
      path or temperature (`path_text`, `display_name`, `ancestors`, `get_parent`,
      `get_children`…, `temperature`, `effective_temperature`), or is something the
      presenter redacts: a position, `lot`, `notes`, `expiry_date`, `expiry_precision`,
      `opened_at`, `quantity`, `unit`, `qty_delta`, `container_count`. Stock rows listed
      without the presenter (`stock_items`, `stock_held`, `aliquots`) fail too.
    - a `{% with %}`, `{% for %}`, `{% include … with %}` or `{% regroup … as %}` that binds
      a `_view` name to anything but a view.
    - Template comments and string literals are ignored.
  - View tests also check what reached the template: `stock_rows_in(response.context) == []`
    (from the same test file) finds any StockItem, Location or StockEvent, or a queryset of
    them, in the context.
- Emails go through `apps.core.mail.queue_notification` (idempotent, sent on commit) with
  templates `templates/emails/<kind>/{subject.txt,body.txt,body.html}` that extend
  `emails/base.*`. Signed one-action links use `apps.core.actions.register_action` +
  `action_url`; the GET shows a confirm page, the POST acts once.
- Policies: read with `apps.core.policies.get_policy(key, lab)`; every key has a default in
  `DEFAULTS`.
- Django-Q2 tasks take ids only, finish well under the 600 s timeout (chunk and re-enqueue),
  and are safe to run twice.
- Frontend: server-rendered templates extending `base.html`, vendored htmx 2, strict CSP (no
  inline scripts or styles, no `hx-on`, no Alpine). Mobile-first: one column, 44 px tap targets,
  no free-text typing needed at the bench (pick-lists and buttons). Each app keeps its CSS in
  `static/css/<app>.css`, loaded from its templates' `{% block head %}`.
- Privacy (plan §11): the PI and managers see lab totals and custodianship (who holds what),
  never individual behaviour (searches, alert answers, reasons). No per-person duplicate counts.
  PI-facing text built from requests (the weekly PI summary, the dashboard) passes item text
  through `apps.guard.anonymize.NameScrubber`, as the baseline summary does.
- Read the Quartzy mirror only through `OrderRequest.objects.for_lab(lab)` / `.live()` /
  `.open()` and `QuartzyInventoryItem.objects.for_lab(lab)` / `.live()`. They hide the rows of
  inactive Quartzy labs (a collaborator's lab the token happens to see). Only the admin and the
  sync's own internals read every row.
- Tests that upsert Quartzy payloads under time-machine and move the clock *backwards* must pass
  `fetched_at=` to `apps.quartzy.sync.upsert_order_request`; otherwise the second upsert is
  skipped as stale.
- Vendor names that are blank or placeholders resolve to the shared vendor family
  `apps.catalog.normalization.UNKNOWN_FAMILY` (`"unknown"`).

## 1. Inventory models (`apps/inventory/models.py`)

**LocationType** (seeded from `apps/inventory/data/location_types.yaml` by
`manage.py seed_inventory`, which runs on every web start and, like `seed_catalog`, keeps what
people changed): `key`, `label`, `structural`, `position_scheme` (`grid` / `linear` / `none`),
`rows`, `cols`, `prefix`, `sort_order`.
- Seed: room, freezer, fridge, ln2_dewar, cabinet, bench, shelf, rack, drawer (structural);
  box (grid 9×9), box_10x10 (grid 10×10), bag, unsorted (not structural).
- A grid is lettered A–Z by row: `position_index("B3")` is 12 in a 9×9 box and
  `position_label(12)` is "B3". A linear scheme labels positions `prefix` + number. A label
  that isn't written like a position ("top shelf, left") is kept as free text with no index,
  so old sheets import.
  - The services write a label the scheme knows its own way ("b-3" → "B3"). They refuse an
    index the location doesn't have (82 in a 9×9 box, any number on a shelf), a label written
    like a position it doesn't have ("J1", "A10" or "Z99" in a 9×9 box, "S9" in an 8-place
    strip: `looks_like_position(label)` is true but `position_index` is None), and a label
    and an index that disagree.
- A type the YAML knows but the database doesn't yet is created from the YAML on first use
  (`services.get_location_type`).

**Location** (treebeard `MP_Node`; one tree for all labs, each lab's rooms are roots):
- Add and move locations only through `services.create_location` / `move_location`, never
  with treebeard's methods or treebeard's admin.
- `lab`, `name`, `code`, `location_type`.
  - `name`: whitespace collapsed, no "/", unique (case-insensitively) among its active siblings.
  - `code`: printed on labels and unique across all labs. It is stored in upper case (a check
    constraint, `^[A-Z0-9-]{1,12}$`), so it is unique case-insensitively. A blank code gets 5
    generated Crockford characters, retried on a collision.
  - A hand-made code (CSV, admin) is 1–12 of A–Z, 0–9 and "-", and never reads as a stock
    item's short id: 6 Crockford characters once spaces and hyphens are dropped and I/L/O read
    as 1/1/0. So "SHELF1" and "OLDBOX" are refused; "UNIT12" and "BOX-12" are fine.
- `temperature`: `RT` / `4C` / `-20C` / `-80C` / `LN2` / blank (the same as the enclosing
  location). `models.parse_temperature` reads what people type ("-20 °C", "rt", "ln2").
- `default_custodian` (User, null): the box's owner. It is used at put-away, and it is the
  custodian of a restricted box (§2).
- `is_unsorted` + `unsorted_owner`: see "Unsorted containers" below.
- `visibility`: structural locations and unsorted containers are always shared (the services
  and the admin refuse anything else: a restricted freezer would hide everyone's stock in
  it). A box or bag starts at the lab default (`inventory.default_visibility`). A restricted
  location hides where everything inside it is (§2); "ask first" on a box applies to the box
  itself, not to the items in it.
- `tags` (ItemType keys, checked to exist), `photo`, `notes`, `active` (retire a location
  instead of deleting it), `created_at`, `created_by`.
  - A location is retired (in the admin) only once nothing active is left inside it. The
    admin locks its row before counting, and stock going in share-locks it (§2), so one waits
    for the other: the count sees the stock, or the stock is refused as no longer in use.
  - A retired location that stays retired isn't checked against its siblings' names or a
    retired parent, so a retired "Box 9" whose replacement took its name can still be edited.
    Bringing it back into use is checked.
- A structural location goes only inside a structural one (no shelf in a box), so a location
  that holds a structural one can't become a box or bag. Nothing but stock goes into an
  unsorted container, and no location becomes one.
- `path_text()` → "Room 210 / -80 A / Shelf 1 / Box 4". `effective_temperature()` → its own
  temperature, else the nearest enclosing one, else "". `display_name`. `custodian_id` (the
  unsorted owner, else the default custodian).
- **Unsorted containers** hold stock that has no place yet ("place later"):
  - One per (lab, person, temperature), enforced by a unique constraint and created on demand
    by `services.unsorted_location`. A blank temperature means not known (Quartzy-derived
    stock).
  - Each is a root of type `unsorted`, named "Unsorted", and displays as
    "Unsorted: Priya Patel, -20 °C" (or "Unsorted: Priya Patel").
  - They are always shared, never move, never hold another person's stock, and have no path
    for `selectors.location_by_path`.
- Moving a location writes a **LocationEvent**: `lab`, `location`, `from_parent`, `to_parent`
  (null = top level), `user`, `at`, `reason`.

**StockItem**:
- Identity: `short_id`, `lab`, `product` (catalog.Product, PROTECT; canonical at creation),
  `supplier_product` (null), `lot` (null), `parent` (self, null; aliquots in v1). `lab` and
  `short_id` never change.
  - `short_id`: 6 Crockford base32 characters (no I/L/O/U) from `secrets`, unique, stored in
    upper case (a check constraint), retried on a collision. Lookups read what people type (§2).
- **State fields** (`models.STATE_FIELDS`, changed only by `apply_event`):
  - `custodian` (User, PROTECT), `visibility` (`shared` / `ask_first` / `restricted`)
  - `location`: never null; unplaced stock is in an unsorted container
  - `position_label`, `position_index`
  - `lifecycle` (`unverified` / `available` / `opened` / `empty` / `discarded` / `lost`),
    `level` (`ok` / `low`)
  - `quantity` + `unit` (optional), `opened_at`, `container_count` (aggregates only)
  - A quantity has 4 decimal places. The services round it as Postgres does (half away from
    zero, `models.round_quantity`), so the cached value and the one its event records match.
- Other fields:
  - `expiry_date` + `expiry_precision` (`day` / `month` / `year`), `attributes` (JSON)
  - `source` (`purchased` / `previous_lab` / `collaborator` / `lab_made` / `gift` /
    `outside_purchase` / `quartzy_inventory` / `import`), `source_order_request`
    (quartzy.OrderRequest, null, SET_NULL)
  - `mta` (bool; a check constraint keeps MTA stock restricted)
  - `is_aggregate` ("N containers, split later"; a check constraint allows `container_count`
    only on aggregates)
  - `mta` and `is_aggregate` are fixed at creation (`models.FROZEN_FIELDS`, with `lab` and
    `short_id`): dropping the MTA flag would let MTA stock be shared, with no event saying
    so. Stock wrongly flagged MTA stays restricted.
  - `notes`, `version` (always the number of events), `created_at`, `updated_at`,
    `created_by`
- Active means lifecycle in (unverified, available, opened): `StockItem.objects.active()`.
- **Guards** (they raise `models.LedgerError`, a programming error):
  - `save()` raises if a state field, `version`, `lab`, `short_id`, `mta` or `is_aggregate`
    changed since the item was loaded (a snapshot taken in `from_db` and `refresh_from_db`).
    Creating an item with `save()` or `objects.create()` raises too: only `create_stock_item`
    creates stock, so every item has its "created" event.
  - Outside the ledger, `save()` writes only the other fields: it narrows `update_fields`. So a
    copy loaded before the last event can't write its old state back while saving, say, the
    notes.
  - `StockItem.objects`, which is also the base manager and the related managers' class,
    refuses state fields, `version`, `lab`, `short_id`, `mta` and `is_aggregate` in
    `update()` and `bulk_update()`, and refuses `bulk_create()` outright. Other fields stay writable: catalog merges repoint
    `product`, `supplier_product` and `lot` with `update()`.
  - The one internal path that writes state: `StockItem._save_from_ledger(**save_kwargs)`. Only
    `apply_event` and `create_stock_item` call it, inside their transaction.

**StockEvent** (append-only):
- `lab`, `stock_item` (PROTECT), `seq` (unique per item, 1 = created, always `item.version`
  after the event), `kind` (`created`, `verified`, `moved`, `opened`, `marked_low`,
  `marked_ok`, `emptied`, `discarded`, `lost`, `found`, `custody_changed`,
  `visibility_changed`, `quantity_adjusted`, `split`, `note`).
- `qty_delta`: the new quantity minus the old, when the event changes it (the starting
  quantity on "created").
- `from_location`, `to_location`, `from_custodian`, `to_custodian`: set only when the event
  changes them.
- `reason`, `user` (null), `occurred_at` (may be backdated), `recorded_at`, `client_uuid`
  (unique when not null), `source` (`web` / `bench` / `email` / `import` / `sync` / `admin` /
  `claim` / `system`).
- `data["state"]`: the new value of every state field the event changed, as
  `models.ledger_value` writes it (ids for people and places, strings for decimals and UTC
  datetimes). The "created" event holds all of them. The rest of `data` is the caller's
  (`apply_event(..., data={"into": [...]})`).
- **Guards:**
  - In the ORM: `save()` of an existing event, `delete()`, and the queryset's `update()`,
    `bulk_update()`, `delete()` and `bulk_create()` raise `LedgerError`, unless they touch only
    `user`, which retention pseudonymizes. Only the services insert events.
  - In the database: migration 0002 adds the trigger `inventory_stockevent_append_only`. It
    rejects DELETE, and any UPDATE that changes a column other than `user_id`, with SQLSTATE
    23001 (Django raises `IntegrityError`). TRUNCATE is not blocked (the test runner flushes
    with it).

**ContainerCode** (`code` unique, `stock_item`, `kind` barcode/qr/gs1; used from MVP-B, and
`resolve_code` already finds them).
**ExternalRef** (`stock_item`, `system` = `quartzy_item` / `quartzy_instance`, `external_id`;
unique on (stock_item, system, external_id), indexed on (system, external_id); one Quartzy
instance may map to several StockItems).

**HistoryClaim** (claims, §4): `lab`, `order_request` (CASCADE), `person` (User), `round`
(int), `outcome` (`pending` / `still_have` / `used_up` / `gave_away` / `shared_stock` /
`not_mine` / `not_arrived` / `skipped`), `count_kept`, `target_person` (null),
`stock_items` (M2M), `surfaced_at`, `decided_at`, `notes`. Unique on (order_request, person).
`HistoryClaim.objects.for_lab(lab)`. The model and its admin exist. The claims code writes
the rows and checks `order_request.lab == lab`.

## 2. Inventory services (`apps/inventory/services.py`, `selectors.py`, `presenters.py`)

```python
create_stock_item(*, lab, product, custodian, user, source, supplier_product=None, lot=None,
                  location=None, temperature=None, visibility=None, lifecycle="available",
                  position_label="", position_index=None, quantity=None, unit="",
                  expiry_date=None, expiry_precision="day", mta=False,
                  source_order_request=None, attributes=None, notes="", occurred_at=None,
                  client_uuid=None, event_source="web", is_aggregate=False,
                  container_count=None, opened_at=None, parent=None, reason="",
                  data=None) -> StockItem
apply_event(item, kind, *, user, expected_version=None, occurred_at=None, client_uuid=None,
            source="web", reason="", data=None, **changes) -> StockEvent
create_location(*, lab, parent, name, location_type, user, code="", temperature="",
                default_custodian=None, visibility=None, tags=(), notes="",
                photo=None) -> Location
move_location(location, new_parent, *, user, reason="") -> LocationEvent
unsorted_location(lab, owner, temperature) -> Location        # get or create
lock_location_tree(lab) -> None     # until the transaction ends; lab is a Lab or its pk
may_hold_stock(user, lab) -> bool; may_receive_stock(user, lab) -> bool
default_visibility(lab) -> Visibility
fold_events(events) -> dict; ledger_drift(item) -> dict        # {} when cache == ledger
# For forms that edit a location field by field (the admin): each raises InventoryError.
get_location_type(key_or_type) -> LocationType
clean_location_name(name) -> str; clean_location_code(code, *, location=None) -> str
clean_location_tags(tags) -> list[str]
clean_location_visibility(visibility, *, location_type, lab) -> Visibility
validate_placement(*, lab, parent, location_type, name, location=None, active=True) -> None
```

- **`create_stock_item`** (one transaction) writes the item and its "created" event (seq 1,
  version 1), and returns the item.
  - `product` is followed to its canonical product. `supplier_product` and `lot` must be of
    that product.
  - `location=None` puts the item in the custodian's unsorted container for `temperature`
    (None or "" = not known). `temperature` is ignored when a location is given.
  - `lifecycle` is unverified, available or opened. "Opened" sets `opened_at` (default
    `occurred_at`). An unverified item may carry an `opened_at`, so verifying it gives
    "opened".
  - `occurred_at` and `opened_at` (and `apply_event`'s `occurred_at`) take a datetime or a
    date. A date is midnight in the lab's time zone (`Lab.time_zone`), and a naive datetime
    is read in that zone: a sheet's "date" column can be passed as it is. Anything else is an
    `InventoryError`.
  - An aggregate needs `container_count` >= 1.
  - `reason` and `data` go on the "created" event, as for `apply_event`: where the stock
    came from ("stock-sheet.csv line 4", `{"claim": 12}`). `data["state"]` is the ledger's.
  - A repeated `client_uuid` returns the item it created the first time, also under a race.
    The same uuid used for another lab's stock, or for an event, raises
    `IdempotencyConflict`.
- **`apply_event`** runs in one transaction:
  - It locks the item row (`SELECT … FOR UPDATE` on the row alone: with joins, Postgres
    re-checks a row another writer just changed against stale joined rows, and drops it).
  - It returns the existing event if `client_uuid` was used before. The same uuid on another
    item or kind raises `IdempotencyConflict`. A replay wins over a stale `expected_version`:
    it already happened.
  - It raises `StaleVersion` (`.expected`, `.actual`) when `expected_version` isn't the item's
    version. Without `expected_version`, concurrent writers queue on the lock and all apply.
  - It checks the transition (below), appends the event with the next seq, writes the cached
    fields and sets `version = seq`.
  - `item` may be a StockItem (updated in place) or a pk. `changes` are state fields the kind
    allows; `location` and `custodian` are model instances.
  - A refused event leaves the item and its ledger exactly as they were.
- **Transitions** (`services.TRANSITIONS`, below). An event that would change nothing is
  refused, except a note or a split. "Position" means `position_label` and/or
  `position_index`: give either one, and the other is derived from the location type's scheme
  (§1). A position the location doesn't have is refused, as a label ("J1" in a 9×9 box) or as
  a number.

| Kind | When the item is | `**changes` | It also sets |
|---|---|---|---|
| `verified` | unverified | location, position | lifecycle → opened if `opened_at`, else available |
| `moved` | unverified, available, opened, empty | location, position | a new location without a position clears it |
| `opened` | unverified, available | — | lifecycle opened; `opened_at` = `occurred_at` if unset |
| `marked_low` / `marked_ok` | unverified, available, opened | — | level (it must change) |
| `emptied` | unverified, available, opened | — | lifecycle empty; a tracked quantity → 0 |
| `discarded` | anything but discarded | — | lifecycle discarded |
| `lost` | unverified, available, opened, empty | — | lifecycle lost |
| `found` | lost | location, position | lifecycle → what it was before the loss (never unverified again) |
| `custody_changed` | anything but discarded | `custodian` (required), location, position | stock in the old custodian's unsorted container moves to the new one's, same temperature |
| `visibility_changed` | anything but discarded | `visibility` (required) | MTA stock stays restricted |
| `quantity_adjusted` | unverified, available, opened | quantity, unit, container_count | `container_count` only for aggregates; 0 empties it (below) |
| `split` | unverified, available, opened | quantity, unit, container_count | may change nothing: the new items come from `create_stock_item(parent=...)`, listed in `data`; 0 empties it (below) |
| `note` | anything | — | nothing; `reason` is the note (required) |
| `created` | never | | only `create_stock_item` writes it |

- An aggregate whose `container_count` goes to 0 (every container split off into items of
  its own, or counted away) is empty in the same event: lifecycle `empty` and a tracked
  quantity 0, as if emptied. So it leaves the active stock, and the guard never matches a
  phantom. Its `containers_text` is "all split off".

- **Errors:** `InventoryError` (its message can be shown to people) and its subclasses
  `StaleVersion`, `InvalidTransition`, `CrossLab`, `InvalidCustodian`, `IdempotencyConflict`
  and `InvalidLocation`. A broken guard raises `models.LedgerError`. The services don't
  decide who may act; views check `apps.core.permissions`.
  - A refusal about another lab's row never names it: "That location belongs to another
    lab.", "That person isn't in Test Lab, so they can't hold its stock.". Rows of the
    caller's own lab are named ("Lee Left can't take stock of Test Lab: …").
- **Who may be a custodian:**
  - `may_hold_stock` is true for the lab's pool user, and for anyone with a Membership in the
    lab, current or not. That includes leavers, ended memberships and placeholders, who keep
    what they hold until it is handed over. `create_stock_item`, `unsorted_location` and a
    location's `default_custodian` need it.
  - `may_receive_stock` is true only for current members (`Membership.is_current`, so
    rotators too) and the pool user. `custody_changed` needs it: nobody hands stock *to*
    someone who left.
  - So `import_stock --placeholders` must give each placeholder user a Membership in the lab
    (status leaver).
- **Tenancy** (`CrossLab`): the location, `source_order_request` and `parent` must be of the
  item's lab, and so must a location's parent. Products, supplier products and lots are
  global. A location must be active, and an unsorted container takes only its owner's stock
  (`InvalidLocation`).
- **Visibility default:** `visibility=None` means `inventory.default_visibility` (a value that
  isn't a visibility is read as `ask_first`). `mta=True` forces `restricted`, and
  `visibility_changed` can't lift it. A collaborator's gift without an MTA follows the
  default.
- **Locations:**
  - `create_location` takes a LocationType or its key. `visibility=None` means shared for
    structural types and the lab default for boxes and bags. `tags` are ItemType keys (a
    list, or text split on commas). `photo` is an uploaded file (a box lid, "Register a box").
  - It refuses another lab's parent, an inactive or unsorted parent, a structural type inside
    a box or bag, a sibling with the same name, a type of "unsorted", a structural type that
    isn't shared, and an unknown tag, temperature or code.
  - `move_location` moves the whole subtree. The stock inside keeps its location (the box),
    so no stock events are written.
  - It refuses another lab's parent, a move into itself or below itself, moving or filling an
    unsorted container, and a move to where the location already is. It writes a
    LocationEvent and refreshes the caller's `location` and `new_parent`.
  - Race safety:
    - `create_location` and `move_location` first take the lab's **tree lock**
      (`lock_location_tree(lab)`, a Postgres advisory lock held to the end of the
      transaction). So a lab's tree changes one change at a time. A move rewrites its whole
      subtree in one UPDATE, which can't see a location still being added inside it: without
      the lock, that location would keep its old path (an orphan, and out from under a
      restricted box). Two people adding "Box 9" to one shelf at once make one box; the other
      gets `InvalidLocation`. Moving a shelf and a box out of it at once can't deadlock.
    - The tree lock blocks nothing else: not stock writes, and not the foreign keys to the
      lab row (no lab row is locked). A long import makes the lab's other location adds and
      moves wait for it.
    - Then `create_location` locks the parent row, and `move_location` the whole subtree and
      both parents, in id order.
    - Stock writers (`create_stock_item`, `apply_event`) share-lock (`FOR KEY SHARE`) the
      location the stock goes to and the one it leaves, in id order, before checking them.
      That is the lock their foreign keys take anyway, so stock writers never wait for each
      other. Retiring (the admin) and moving a location lock it `FOR UPDATE`, so each waits
      for the other, and nothing waits in a circle with a single stock write.
    - A transaction that writes stock and then adds locations
      (`import_stock --create-locations`) takes `lock_location_tree` before its first row.
      Otherwise it would hold share locks a move waits for while it waits for the move's
      tree lock. If Postgres still cancels a move to break a deadlock, `move_location`
      raises `InvalidLocation` ("Someone else is changing these locations; try again").
    - New roots (top-level locations, unsorted containers) retry when a concurrent insert
      took the tree path. A hand-made code another lab takes at the same moment is an
      `InvalidLocation`, never an `IntegrityError`.
- **Selectors** (`selectors.py`, each scoped to one lab; they return querysets with the usual
  `select_related`):
  - `active_stock(lab, *, products=None, supplier_products=None)` returns the active items.
    With `products` (Products or ids), only items of those products' canonical products, or
    of anything merged into them (`product_family_ids`). With `supplier_products`, only items
    of those listings. With both, items matching either. The guard uses only this.
  - `stock_held_by(person, *, lab, active_only=True)`
  - `location_contents(location, *, include_sublocations=False, active_only=True)`, in
    position order; `child_locations(location, *, include_inactive=False)`, in tree order,
    with what `present_locations` reads selected. Present them with `listed_in=location`
    (below).
  - `location_by_path(lab, path, *, include_inactive=False)` matches "Room 210 / -80 A"
    case- and space-insensitively (`split_path`); unsorted containers never match.
  - `resolve_code(code, *, lab)` → a StockItem, a Location or None, never another lab's.
    - Short ids as people type them ("7k3m-9q", O for 0, I or L for 1).
    - Location codes in any case, Crockford look-alikes for generated codes, and the code
      without its hyphens when that is unambiguous.
    - ContainerCodes, and the links `/i/<short_id>` and `/l/<code>`, bare or in a full URL.
  - `ancestor_chains(locations)` → `{pk: [outermost, …, location]}` in one query, and
    `chain_text(chain)`.
- **Presenters:**
  - `present_item(item, viewer, *, channel)` → an `ItemView`, or None when the viewer may see
    nothing. `present_items(items, viewer, *, channel, listed_in=None)` → the views the
    viewer may see, in order. Access, paths and the custodians' memberships are looked up
    once per call, so a list costs the same few queries however long it is (pass rows from
    the selectors).
  - `present_location(location, viewer, *, channel)` → a `LocationView` or None;
    `present_locations(locations, viewer, *, channel, listed_in=None)` → the views the viewer
    may see (pass rows from `child_locations`).
  - **Lists of one place:** a page that lists what is in one location passes `listed_in=`
    that location: `present_items(location_contents(box), viewer, channel="page",
    listed_in=box)`, the same for a freezer with `include_sublocations=True`, and
    `present_locations(child_locations(shelf), viewer, channel="page", listed_in=shelf)`.
    The rows that would be redacted are then left out, with no count either: the page is the
    place they must not be tied to. Otherwise Raj would read on Box wombat's page that
    Priya's restricted item is in it, and on Shelf pelican's page that Box quokka is on it.
    Lists that aren't about one place (search results, the guard's matches, what someone
    holds) leave `listed_in` out and show redacted rows: that stock exists, and whom to ask.
    A restricted box's own page (`present_location`) still shows its name, code and owner;
    its list of contents is empty for a viewer who sees it redacted.
  - `channel` is one of `page`, `email`, `digest`, `csv`, `partial`. It changes formatting
    only: `email`, `digest` and `csv` get absolute links (`SITE_URL`). Views are frozen
    dataclasses; `view.as_json()` gives a snapshot (e.g. `DuplicateMatch.candidate_snapshot`).
  - People's names are `str(user)` (`User.__str__`: the full name, else the email).
  - **Whom to ask:** the custodian. For lab stock (the pool user), and for stock of someone
    who has left (a leaver, an ended membership, a placeholder), the product's LabProduct
    steward, else "the lab's co-owner" (`ask_user_id` None: ask the co-owners).
  - `ItemView` fields:
    - identity: `short_id`, `url` ("/i/<short_id>"), `version` (for `expected_version` in a
      form), `product_id`, `product_name`, `item_type`, `vendor`, `catalog_number`
    - people: `custodian_id`, `custodian_name`, `custodian_is_pool`, `custodian_has_left`,
      `viewer_is_custodian`, `ask_user_id`, `ask_name`, `ask_text` ("Ask Priya Patel" when
      the viewer should ask: the item is ask-first, restricted or unverified)
    - status: `visibility` + `_label`, `redacted`, `lifecycle` + `_label`, `is_active`,
      `unverified`, `unverified_text` ("Probably: ask Priya Patel"; "Not checked yet" for
      the person asked), `level` + `_label`, `mta`, `source` + `_label`
    - where: `location_code`, `location_name`, `location_path`, `location_url`
      ("/l/<code>"), `unplaced`, `temperature` + `_label`, `position`, `position_index`
    - more: `lot`, `expiry_date`, `expiry_precision`, `expiry_text` ("2027-03" for month
      precision; the lot's expiry when the item has none), `quantity`, `unit`,
      `quantity_text` ("250 mL"), `opened_at`, `is_aggregate`, `container_count`,
      `containers_text` ("4 containers, not split yet"), `notes`, `created_at`, `channel`
    - Redacted, the "where" and "more" fields are blank or None (all but `is_aggregate`,
      `created_at` and `channel`).
  - `LocationView` fields: `code`, `url`, `name` (the display name), `kind`, `kind_label`,
    `structural`, `is_unsorted`, `custodian_id`, `custodian_name`, `custodian_has_left`,
    `viewer_is_custodian`, `ask_text`, `visibility` + `_label`, `redacted`, `path`,
    `parent_path`, `temperature` + `_label`, `tags`, `tag_labels`, `position_scheme`, `rows`,
    `cols`, `notes`, `has_photo`, `active`, `channel`.

Visibility matrix (the presenter is the only place it is decided):

| Viewer | shared / ask_first | restricted |
|---|---|---|
| custodian, co-owner, system admin | everything | everything |
| other current lab members (incl. PI and rotators) | everything, plus "ask X" for ask_first | that it exists, its product, its state and whom to ask; **no location, position, temperature, lot, expiry, quantity (container count included) or notes** |
| anyone else (leavers, ended memberships, other labs, anonymous) | nothing (`None`) | nothing |

- "Restricted" covers a restricted item, and anything inside a restricted location for a
  viewer who isn't that location's custodian. A restricted box is redacted the same way: no
  path, temperature, notes or photo. Its name, code, kind, tags and owner stay visible.
- "Custodian" means a current member: someone who left sees nothing, not even their own
  stock. Co-owners and system admins (`permissions.is_co_owner`) see everything.
- The redaction-matrix test (`tests/inventory/test_presenters.py`) covers a restricted item,
  a restricted aggregate, a shared item inside a restricted box and the restricted box
  itself, on every channel, for every viewer role. It checks that no secret appears in any
  field, even inside another string. Its listing cases (a box's page, a freezer's page with
  what is inside it, a restricted box's page, a shelf's boxes) check that no redacted row is
  listed at all.

## 3. Imports (`apps/inventory/importers.py`, management commands)

- **Shared pieces** in `importers.py`, for import_stock and link_quartzy_inventory too:
  - `read_rows(path, *, required)` → `[Row(line, values)]`: the non-blank rows, keyed by
    lower-case column name, values stripped. A missing column is an error on line 1; a file
    that isn't UTF-8 (Excel's plain "CSV") is an error naming the first bad line.
  - `ImportFailed(errors)`: one message per problem, each starting "line N:".
  - The dry run: `with import_run(commit=commit) as errors:` runs the block in one
    transaction. It rolls back unless `commit` and `errors` stayed empty, then raises
    `ImportFailed(errors)` if any were added. So a dry run reports exactly what a commit
    would do and creates nothing, not even location types.
  - `PathIndex(lab, *, user=None, created=None)`: the lab's real locations by path
    (case- and space-insensitively, inactive ones too), loaded once for a whole file. As in
    `location_by_path`, a location in use wins over a retired one with the same path (a box
    retired, replaced, then brought back).
    - `get(path)` → the Location or None (`path` is text or a tuple of parts)
    - `ensure(path, *, line=None)` → the Location, creating every part LabStock doesn't
      have yet, the last one too, each with its kind told from its name (below). That is
      `import_stock --create-locations`.
    - `parent_of(parts, *, line=None)` and `add(plan, parent)` are what import_locations
      builds on.
    - What it creates is appended to `created` (`PlannedLocation`s, `implied_by` = `line`).
      A kind it can't tell raises `services.InventoryError`.
  - Other helpers: `selectors.split_path` / `location_by_path`, `models.parse_temperature`,
    and `services.InventoryError` messages.
- `import_locations FILE.csv --lab SLUG [--commit]` (`importers.import_locations(path, *, lab,
  commit=False, user=None)` → `LocationReport(created, existing, committed)`):
  - The columns are those of `docs/templates/locations.csv`: `path, kind, temperature,
    owner_email, code, notes`. `docs/phase0-checklist.md` §7 explains them for the co-owner.
  - Rows may come in any order: parents are created first, sorted by depth.
  - A missing intermediate part (`Room 214` above `Room 214 / LN2 dewar`) is created. A
    top-level part is always a room, whatever its first word ("4 West", "Freezer room B",
    "Box room"). Below that, its kind is taken from its first word: a kind's key or the first
    word of its label ("Shelf", "Freezer", "LN2" → ln2_dewar…), and a temperature names a
    freezer or fridge and sets its temperature: "-80 A" and "-20 B" are freezers, "4C door" a
    fridge (that is how labs name them). Any other part that can't be told is an error asking
    for a row in locations.csv. The report says which line implied each one.
  - A path LabStock already has (case- and space-insensitively, inactive ones too) is left as
    it is, and the report lists what the file says differently. Re-running creates nothing.
  - Errors name the line, and nothing is saved if any row fails:
    - an unknown kind, temperature or owner (`owner_email` must be someone in the lab)
    - a malformed code, or one that reads like a short id
    - a code or path used twice in the file
    - a code already used elsewhere in LabStock
  - The committed template imports cleanly (tested).
- `import_stock FILE.csv --lab SLUG [--commit] [--verified] [--create-locations]
  [--placeholders]`: the columns of `docs/templates/stock-sheet.csv`, as documented in
  `docs/phase0-checklist.md` §7.
  - It is a dry run by default (everything runs, then rolls back) and writes a review CSV next to
    the input (row, action, product resolution, warnings).
  - It resolves products through `apps.catalog.services.resolve_supplier_product`.
  - Items are `unverified` unless `--verified`.
  - `--placeholders` creates inactive placeholder users for unknown owner emails (departed members),
    each with a leaver Membership in the lab (`may_hold_stock`, §2).
  - It creates stock with `create_stock_item(event_source="import", source=...,
    reason="<file> line N")`: `location` from a `PathIndex` (`ensure` with
    `--create-locations`, else `get`), else `location=None` + `temperature` for the owner's
    unsorted container; `opened=yes` on an unverified row → `opened_at` (the row's `date`,
    a `datetime.date`, is fine as it is); `mta=yes` → `mta=True`; an expiry of `YYYY-MM` →
    the first of the month with `expiry_precision="month"`.
  - With `--create-locations` it calls `services.lock_location_tree(lab)` inside
    `import_run`, before its first row (§2, race safety).
  - Its dry run is `import_run(commit=...)`, so a row that fails in the services (a
    position the box doesn't have, such as `J1` in a 9×9 box, or a retired location) is an
    error on its line too.
- As built (`importers.import_stock(path, *, lab, commit=False, verified=False,
  create_locations=False, placeholders=False, user=None) -> StockReport`, plus
  `write_review_csv(report, path)`):
  - The review CSV is `<file>.review.csv`, written on a dry run, a commit and a failed run
    alike (`StockImportFailed` carries the report): line, action (`create` / `exists` /
    `error`), owner, item, product, resolution, items, aggregate, location, short ids,
    warnings, errors.
  - Each item's `client_uuid` is a uuid5 of (lab, the file's sha256, line, container n), so
    re-importing the same file creates nothing (rows report `exists`).
  - Up to `inventory.quartzy_split_max` containers make that many items; more make one
    aggregate. `size` ("100 uL") becomes each item's quantity + unit.
  - A lot goes through `get_or_create_lot` (the expiry then goes on the lot); an
    `outside_purchase` row also records `OrderRequest(source=outside, status=RECEIVED)`, found
    again on a re-import by its import key in `raw_payload`.
  - Lab registers (LN2 log, primer, plasmid, antibody lists) are reshaped into this template.
- `link_quartzy_inventory --lab SLUG [--commit]` turns the raw Quartzy inventory mirror into
  StockItems with ExternalRefs:
  - Each instance (or each item when instances are off) gets one StockItem per container when
    the quantity is a whole number ≤ `inventory.quartzy_split_max` (20); otherwise one aggregate
    (`is_aggregate=True, container_count=N`).
  - Custodian: the person who added it, else the lab pool user. The location comes from the
    Quartzy location path (`QuartzyInventoryInstance.location_path`, " / "-joined) when
    `location_by_path` finds it, else the custodian's unsorted container with an unknown
    temperature (`location=None, temperature=""`).
  - Items are always `unverified`, with `event_source="sync"` and
    `source="quartzy_inventory"`.
  - Idempotent through ExternalRef.
  - As built (`apps/inventory/quartzy_link.py`): a quantity that isn't a whole number of
    containers (a volume, or none) gives one item with "Quartzy quantity: …" in its notes;
    each item's `client_uuid` comes from its instance, so concurrent runs can't double it.

## 4. Claims and intake (`apps/inventory/claims.py`, `intake.py`; views in `apps/bench`)

- **Surfacing:**
  - A durable-type request (ItemType.durable) becomes claimable for its requester (or
    `requested_for`) `claims.after_receipt_days` after RECEIVED was observed, or
    `claims.after_request_days` after `requested_at` if it never shows RECEIVED.
  - It must be within `claims.lookback_months` and not CANCELLED.
  - Weekly rounds; at most `claims.batch_size` per email, sorted by price.
- The weekly **claims** email (kind `claims`, sender `news@`) links to the claim list through a
  signed link that opens a **7-day session scoped to that person's claim list** (its own signer
  salt; it grants nothing else).
- **Outcomes:**
  - *Still have it*: pick a box, create one inline, or *place later* (unsorted). It creates
    `count_kept` StockItems and offers *This is the same bottle* when an unverified
    Quartzy-derived item of the same product exists.
  - *Used up*.
  - *Gave to X*: an unverified item in X's custody.
  - *In the shared stock*: custodian = lab pool user, visibility shared.
  - *Not mine*: goes to the champions' list.
  - *Did this arrive?*: sets the request's `hot_until` so the sync re-reads it.
- **Champions' list:** durable items of departed members (Membership status leaver, or ended)
  and *not mine* items, claimed on behalf of the lab pool.
- **Add what I have** (desk form):
  - A typeahead over the lab's products (Quartzy history first, then the catalog).
  - Source: previous lab / collaborator / lab-made / gift / outside purchase.
  - Count, location (or place later + temperature), visibility (collaborator + MTA → restricted),
    lot, expiry, notes.
  - An outside purchase also creates `OrderRequest(source="outside", status=RECEIVED)`, so the
    guard sees it as bought before.
- **Register a box:** owner, parent location, type tags, optional lid photo, label → a box
  Location (searchable before it's itemized). Photos are served only through a login-checked
  view, never as public media.
- **As built:**
  - `claims.py`: `claimable_requests`, `surface_claims` (new pending claims; a `skipped` or
    `not_arrived` one comes back pending a week on, up to `claims.max_rounds`),
    `pending_claims` (most valuable first), `decide(claim, outcome, *, by, count_kept,
    location, temperature, target_person, same_item, visibility)`, `champions_list`,
    `claim_views` (`ClaimView` embeds the `RequestView` and the `ItemView`s of "same bottle"
    candidates and kept stock), `queue_claims_emails` (kind `claims`, period = ISO week).
    `HistoryClaim.decided_by` records who answered (the person, or a champion for the pool).
    "Durable" is the Quartzy type's item type, else the product's; only `source=quartzy`
    requests are claimed.
  - The scoped session: `claim_link` signs `{n, r, k}` with salt `labstock.claims.v1` (14 days);
    `open_claim_session` cycles the session key and stores `claims_scope` (person, lab, 7 days);
    `@claims_access` (apps/bench/views.py) accepts it on the claims pages only. Nobody is
    logged in by it. Log filters and Caddy redact `/claims/t/<token>`.
  - `intake.py`: `add_what_i_have` (a repeated `client_uuid` adds nothing), `product_typeahead`,
    `register_box`, `check_photo` (size, then JPEG/PNG/WebP/HEIC magic bytes).
  - `apps/inventory/tasks.tick` (schedule `inventory.tick`, hourly) runs the claims round on
    Monday from 08:00 lab time.
  - Pages (`apps/bench`): home tiles, `/i/<short_id>` (the custodian may change its sharing:
    the only stock change in MVP-A's UI), `/l/<code>` (+ `/photo`), `/me/stock`, `/claims/`
    (htmx rows), `/claims/t/<token>/`, `/claims/<pk>/`, `/claims/champions/`, `/add/`
    (+ `/add/products` typeahead), `/boxes/new`. Form fields avoid names the template scan
    reserves for views (`place`, `temp`, `lot_number`, `remarks`). Another lab's member gets a
    404 (their own lab has no such item), not a 403.

## 5. Guard models (`apps/guard/models.py`)

As built: the foreign key to a check is called `duplicate_check` (a field called `check` would
hide Django's `Model.check()`), and its reverse names are `matches` and `outcomes`.

- **CheckRun** (added): `lab`, `via` (`head` / `sweep` / `hot` / `resolve` / `safety_net` /
  `replay` / `manual`), `started_at`, `finished_at`, `requests`, `checks_created`,
  `alerts_planned`, `held` + `hold_reason`. One batch of checks: the "cycle" the circuit breaker
  counts alerts in (§7.6).
- **DuplicateCheck:**
  - `lab`, `order_request`, `matcher_version`, `replay` (bool), `shadow` (bool)
  - `status`: `no_match` / `uncheckable` / `out_of_scope` / `suppressed` / `unarmed` / `late` /
    `held` / `alerted` / `shadow_only` / `clear`
  - `uncheckable_reason`, `hold_reason`, `late`
  - `quartzy_native_would_fire_raw`, `quartzy_native_would_fire_norm`
  - `searched_before` (null until step 8), `alert_in_time` (null until known)
  - `run` (CheckRun), `best_tier` (of the best unsuppressed match), `as_of` (when the check
    reflects: when it ran, or the request's time for a replay)
  - `created_at`, `decided_at`
  - Unique on (order_request, matcher_version, replay).
- **DuplicateMatch:**
  - `duplicate_check`, `tier` (`T1` / `T1x` / `T2` / `T3` / `T4`), `candidate_kind`
    (`stock` / `open` / `bought`), `score`, `probably` (unverified stock, or an open request
    older than `guard.open_probably_arrived_days`), `why` (the text people see)
  - `candidate_stock_item` or `candidate_order_request` (a check constraint requires exactly
    one; each is unique per check), `candidate_snapshot` (JSON: what the candidate was and who
    holds or asked for it; never where stock is, which only the presenter shows)
  - `suppressed` + `suppression_reason` (`recurring` / `pair`)
  - `label` (`true_dup` / `related_not_dup` / `false`, blank) + `labelled_by` + `labelled_at`;
    `label_suggestion` (a requester's "not the same thing", never a label)
- **Outcome:**
  - `duplicate_check`, `kind`: `borrow_requested`, `join_requested`, `cancel_intended`,
    `vendor_cancel_intended`, `vendor_cancel_confirmed`, `kept_offer_spare`, `not_same`,
    `need_own`, `borrowed`, `cancelled`, `joined_order`
  - `reason` (pick-list), `decided_by`, `decided_at`, `verified_at`, `est_value_avoided`,
    `notification`; unique on (duplicate_check, kind)
- **PairSuppression:** `lab`, `subject_product`, `candidate_product`, `scope` (`requester` /
  `lab`), `requester` (null), `expires_at`, `created_by`, `reason`. Lab scope only by a co-owner.

## 6. Matcher (`apps/guard/matcher.py`, `suppression.py`)

`find_matches(order_request, *, as_of=None, replay=False) -> list[Candidate]`, best first. It
is pure apart from DB reads and deterministic. `MATCHER_VERSION = 1`; a change to what matches
bumps it (a new version checks every request again, and labels carry over by pair).

- **Subject:** the request's supplier product (family, key, base, identity, mfr keys), its
  product and its item type (QuartzyType → ItemType, else the product's).
  - Uncheckable (recorded with a reason): resolution `no_catalog` / `generic` / `unresolved`, or
    no key. For a key-less listing, use the catalog's reason (`NormalizedCatalog.no_key_reason`:
    `placeholder` / `excel_mangled` / `too_long`). A request in the `unknown` vendor family
    whose key fails `cross_family_key_ok` is uncheckable with reason `no_vendor`: no tier can
    ever match it. These are the reasons in `apps.guard.baseline.UNCHECKABLE_REASONS`, so the
    baseline and the guard report the same uncheckable share.
  - Out of scope: `ItemType.in_scope` is False.
- **Candidates:**
  1. Active stock (`selectors.active_stock`). Unverified stock is shown as "probably — ask X".
  2. Open requests: any status other than RECEIVED/CANCELLED, not deleted, not this request.
     After `guard.open_probably_arrived_days` they show as "probably arrived".
  3. "Probably bought" requests: not CANCELLED, older than `guard.probably_bought_days`, inside
     the item type's `lookback_months` (0 means none).
     - As built: an open request is always kind `open` (so it is listed once), and a RECEIVED
       one counts as bought at any age inside the look-back (it is bought for sure). With no
       item type known, the look-back is 0.
  - Only requests made before the subject are candidates (by `requested_at`, else
    `first_seen_at`; ties by pk). A request whose active stock is a candidate is listed once,
    as that stock.
  - Same-requester candidates (the custodian, for stock) are skipped unless
    `guard.include_same_requester`.
  - A replay (`replay=True`) leaves stock out (LabStock's stock records didn't exist back then)
    and reads each earlier request's status at the subject's time from the polling's status
    observations, else from its current status: RECEIVED counts as received only when it was
    requested 28 days before (otherwise it was on its way), CANCELLED is left out.
- **Tiers:**
  - **T1:** same vendor family and same `catalog_key`, or the same manufacturer family + key.
  - **T1x:** the same key in different families. Only when `cross_family_key_ok(key)` holds
    (≥ `guard.cross_family_min_key_length`) **and** the item type matches or the name trigram
    similarity is ≥ `guard.cross_family_min_name_similarity`. Use
    `apps.guard.baseline.trigram_similarity`, so replay and baseline agree.
    While item types aren't mapped, the same Quartzy type name counts as the same type (as the
    baseline compares them).
  - **A same-key match inside the shared `unknown` vendor family is treated as T1x.**
  - **T2:** same family and `catalog_base`, different key (a pack variant). As built, also the
    same canonical product through another listing, or stock with no listing (products merged
    in the catalog, or stock added by name): "Same product, another listing".
  - T3/T4 come in v1. Generic supplier products never match on catalog #.
- **Suppression** (`suppression.py`):
  - excluded types: as built, these are the `out_of_scope` checks (`ItemType.in_scope`)
  - recurring consumables (≥ `guard.recurring_per_year` requests in 12 months), unless two are
    open at once
  - active PairSuppressions (requester scope by default; `need_own` lasts
    `guard.need_own_suppress_days`, `not_same` lasts `guard.not_same_suppress_days`)
- **Score:** tier first (T1 > T1x > T2), then candidate kind (stock > open > bought), then recency.

## 7. Checks pipeline (`apps/guard/checks.py`, `tasks.py`)

- **Trigger:** `apps.quartzy.signals.requests_synced(qlab_id, request_ids, via)`; its docstring
  is the contract. It is sent on commit, once per run, by head, sweep and hot runs and by
  catalog re-resolution, for the live requests created or re-resolved in that run (never one
  deleted in Quartzy or of an inactive Quartzy lab). The backfill doesn't send it: history is
  the replay's job. A run that rolls back sends nothing; one that failed part-way sends what it
  saved. The receiver only enqueues a task with the ids; one that raises is logged and changes
  nothing for the run.
  - A safety-net schedule every 10 minutes checks armed requests first seen in the last 2 days
    that have no DuplicateCheck for the current matcher version.
  - A request that was `uncheckable` only because its resolution was `unresolved` is checked
    again, in place, once it resolves. Every other check is final for its matcher version.
  - As built: `receivers.requests_synced` enqueues `tasks.check_requests(ids, via)`, which checks
    50 per task in one CheckRun and re-enqueues the rest; `tasks.safety_net` is the schedule
    `guard.safety_net` (registered through `apps.core.schedules`; `setup_schedules` syncs every
    app's prefix). Each `checks.check_request` runs in one transaction under a per-lab
    `pg_advisory_xact_lock`, so concurrent runs make one check, and counts per cycle hold.
- **Per request, in order:**
  1. Build the check and its matches, and record the native-warning flags.
  2. **Arming:** `requested_at >= QuartzyLab.alerts_armed_at`, else `unarmed` (recorded, never
     sent).
  3. **Late:** the check runs more than `alerts.late_after_hours` after `requested_at` (normally
     because the request was first seen late) → `late`. No immediate alert; the digest uses
     post-order wording.
  4. **Live or shadow:** live only when `alerts.enabled` and the best match's tier is in
     `alerts.tiers_live`; otherwise `shadow_only`, with no Notification.
  5. **Burst hold:** more than `alerts.burst_max_requests` new requests from one creator within
     `alerts.burst_window_minutes` → the alerts are held, and the system owner gets one email.
  6. **Circuit breaker:** more than `alerts.circuit_breaker_max` alerts in one cycle → hold
     them all, and email the system owner a one-tap *Release* link.
  7. **No match:** for an in-scope request with no match, send a "No match — clear to order"
     receipt while today ≤ `alerts.clear_to_order_until`.
- **Send time:** `register_send_check("duplicate_alert")` re-reads `alerts.enabled` and the tier
  list, so the kill switches work on queued mail.
- **Recipients:**
  - the requester, always
  - for an open-request match, also the earlier requester, who can share the order (a separate
    notification with a different context)
  - stock owners only when the requester taps *Borrow*
- **Replay** (`replay.py`, `manage.py guard_replay --lab SLUG --months 12`):
  - Runs the matcher over history in time order, as of each request's `requested_at`.
  - "Open at t" comes from status observations where they exist; otherwise from the current
    status plus age, as the baseline does.
  - Stored as `replay=True, shadow=True` checks.
  - As built: re-running skips requests already replayed with this matcher version, and labels
    carry over by pair (subject request, candidate) between replay and live checks and across
    matcher versions, so nobody labels a pair twice. `apps.guard.records.request_record` turns
    an OrderRequest into the baseline's `RequestRecord` (from its stored Quartzy payload), and
    `tests/guard/test_replay.py` holds the replay to the baseline's pairs on the sample export.
- `manage.py guard_report` and `precision_report(lab)` give, per tier, the number labelled, the
  true duplicates, the precision and the gate: ≥ `guard.go_live_min_labelled` labelled and
  ≥ `guard.go_live_min_precision`. As built, also per tier and candidate kind (`T1/open`, so
  "T1 against open requests" can go live first), with a pair labelled in both replay and live
  counted once, the Wilson lower bound for information, and the uncheckable share next to the
  baseline's.

## 8. Alerts, actions, outcomes (`apps/guard/alerts.py`, `outcomes.py`, `views.py`)

- **The alert email** (kind `duplicate_alert`, from `alerts@`):
  - Subject: "Before you order: Priya has CST 2118S (GAPDH 14C10) in −20 B".
  - Body:
    - the request line
    - the best match through the presenter (owner, location or "ask", date, why it matched)
    - **"Have you placed the vendor order yet?"**
    - the footer "Only you receive this; the PI sees lab totals only", plus the search-keyword tip
- **Actions** (registered for `duplicate_alert`; each records an Outcome):
  - **Not yet:**
    - `borrow` asks the custodian by email (kind `ask_owner`, Reply-To the requester).
    - `share` emails the earlier requester.
    - `cancel` is recorded as `cancel_intended`. The confirm page links to the request's Quartzy
      `app_url`, and names the Lab Admin when the requester can't cancel (status beyond
      CREATED). It also sets `hot_until` so the sync verifies the cancel.
  - **Yes:**
    - `vendor_cancel` (the confirm page shows the vendor and says to quote the Quartzy
      confirmation #)
    - `keep_offer` (keep it, offer the spare)
  - **Always:**
    - `not_same` → a requester-scoped PairSuppression, plus a label *suggestion* for the
      co-owner (never a label)
    - `need_own`, with reasons `lot` / `application` / `urgent` / `other`
  - The answer to "placed the order yet?" sets `alert_in_time`.
- **Verified outcomes and $ avoided:**
  - The value is the request's total, or unit price × quantity.
  - It counts only verified `cancelled` (CANCELLED seen in the sync after the alert),
    `vendor_cancel_confirmed` and `borrowed`.
  - It is excluded when the same person makes an equivalent request within 30 days, or when
    Quartzy's own warning would have fired (counted separately).
- **As built** (`apps/guard/alerts.py`, `actions.py`, `outcomes.py`;
  `apps/inventory/asks.py`):
  - `alerts.tiers_live` takes tiers (`"T1"`) or a tier and candidate kind (`"T1/open"`).
    `alerts.plan_alert` runs inside the check's transaction; a requester who isn't a current
    member with an email gets no alert (the check stays `shadow_only`, with the reason in
    `hold_reason`).
  - Alerts of a CheckRun are queued held (`cycle:<run>`); `alerts.finish_cycle` releases them
    when the run ends, or trips the breaker (`circuit:<run>`, the checks become `held`) and
    emails the system owners a SYSTEM notification whose `context["actions"]` carries the
    one-tap `release_alerts`. `tasks.verify_outcomes` (every 15 minutes) also releases the
    holds of runs that never finished.
  - The earlier requester of an open match gets an informational copy (`role="earlier"`,
    Reply-To the new requester, no action links; its links refuse to act).
  - Borrow and share go through `AskThread`/`AskReply` (inventory): kind `ask_owner` mail
    with *Yes, borrow some* / *None left* (or *Yes, share my order*); a "lend" reply sends
    `inventory.signals.ask_answered`, and the guard records a verified `borrowed`.
  - Follow-ups after the single-use alert link: `/guard/mine/` (*The vendor confirmed the
    cancellation*, *I borrowed it*).
  - `outcomes.value_avoided(lab, start, end)` returns the amount and count, and apart: the
    ones Quartzy's warning would have caught and the ones bought again within 30 days.
  - Plain-text email parts and subjects render with autoescape off.
- **Labelling page** (`/guard/label/`, `can_label_matches` only):
  - Shows unlabelled matches, live shadow first and then replay, sorted by $.
  - Each row has three htmx buttons (same thing / related, not a duplicate / different) and is
    rendered through the presenter.
  - Per-tier precision and gate status are shown at the top. Going live stays a PolicySetting
    change in the admin.

## 9. New policy keys

These are already in `apps.core.policies.DEFAULTS`. Shared wiring is in place too:
`apps/bench/urls.py` (included at `/`, after core) and `apps/guard/urls.py` (at `/guard/`) are
empty stubs to fill in. `settings.LABSTOCK_MAX_PHOTO_BYTES` caps lid photos (10 MB by default).

| Key | Default | Used by |
|---|---|---|
| `inventory.default_visibility` | `"ask_first"` | services |
| `inventory.quartzy_split_max` | `20` | link_quartzy_inventory |
| `claims.batch_size` | `15` | claims email |
| `guard.include_same_requester` | `False` | matcher |
| `guard.need_own_suppress_days` | `30` | suppression |
| `guard.not_same_suppress_days` | `180` | suppression |

## 10. Step 8: search, asks, board, nudges, digests, dashboard

Same conventions as §0. Every page and email shows stock through `apps.inventory.presenters`
and requests through `apps.quartzy.presenters`; PI-facing text goes through `NameScrubber`.

**10.1 Vendor URLs** (`apps/catalog/vendor_urls.py`, pure Python, for v1's extension too):
`parse_vendor_url(text) -> VendorURL(family, catalog_raw, words) | None`. Thermo, Fisher,
Sigma/Merck, CST, Abcam, NEB, ATCC, IDT, Qiagen, VWR, plus Promega, Bio-Rad and R&D Systems.
`catalog_raw` is None when the URL names no number (IDT's pages); `words` comes from the slug.

**10.2 Search** (`apps/inventory/search.py`, model `SearchLog`):
- `search(lab, viewer, q, *, log=True) -> SearchResult(query_kind, products, in_lab, probably,
  on_order, bought_before, boxes)`. A pasted URL goes through `parse_vendor_url`; a
  catalog-like query matches catalog keys (and bases, manufacturer keys, cross-family keys
  long enough for T1x); anything else matches product names (trigram), LabProduct synonyms,
  and box names and tags.
- Groups: *In the lab* (active stock, shared first, through the presenter; restricted rows say
  whom to ask), *Probably: ask X* (unverified stock), *On order* (open requests), *Bought
  before* (not cancelled, within `search.bought_before_months`), *Boxes*.
- `SearchLog`: lab, user, at, kind (`url` / `catalog` / `name`), vendor family, catalog key,
  product ids, result counts; never the raw text. `purge_search_logs()` deletes rows older
  than `search.log_retention_days` (daily). `searched_before(order_request)`: did the
  requester search for this product within `search.searched_before_days` before the request?
  The check stores it in `DuplicateCheck.searched_before`.
- Pages: `/s?q=` (`/s/results` is the htmx partial), `/s/setup/` (address-bar keyword and
  bookmarklet), `/opensearch.xml`. Caddy keeps `/s` query strings out of its logs.

**10.3 Asks** (`apps/inventory/asks.py`): `ask_owner` (§8), and
`ask_lab(*, lab, asker, product=None, text="", urgent=False)`: one `ask_lab` email per
current member (not the asker) with *I have it* / *Maybe* / *No* and Reply-To the asker.
- Asker side: `AskLimit` once the asker started `asks.max_per_person_per_day` lab-wide asks
  today (lab time).
- Recipient side: held asks (`hold_reason="ask_batch"`) go out at `asks.batch_hours`, at most
  one per recipient per day; an urgent ask goes out at once but counts toward the day.
- Pages: POST `/asks/owner/<short_id>`, POST `/asks/lab/`, GET `/asks/` (my asks and replies).

**10.4 In-flight board** (`/board/`): the lab's open requests grouped by product, in three
sections: not marked ordered (CREATED/APPROVED), ordered (ORDERED/BACKORDERED), probably
arrived (open longer than `guard.open_probably_arrived_days`).

**10.5 Nudges** (`apps/reports/nudges.py`, daily from 09:00 lab time): one `stale_nudge` per
Quartzy request still open `nudges.stale_days` after it was made, never for requests before
alerts were armed. Actions: *It arrived* and *Never ordered: cancel* (both watch the request),
*Still waiting*.

**10.6 Personal digest** (`apps/reports/digests.py`, Monday from 08:00 lab time, kind
`personal_digest`, period = ISO week), sent only when it has content: late detections
(post-order wording), unanswered alerts, replies to my asks, asks waiting for me, pending
claims, my open nudged requests. Ids in the stored context; views built at send time.

**10.7 PI lab summary** (`lab_summary`, Monday from 08:30, to PIs and co-owners): lab totals
only, numbers under 3 shown as "<3", item text through `NameScrubber`; lenders named only with
`Membership.thank_by_name`.

**10.8 Dashboard** (`apps/reports/metrics.py`, `/reports/`): for the PI, co-owners and system
owners (`reports.dashboard_audience` = "members" opens it to every member). Metrics over a
date range, never per person: duplicate $ found by the replay (upper bound and reviewed, this
quarter vs the same quarter last year), verified $ avoided, alert-in-time rate,
search-before-buy rate, coverage (claims answered, boxes with an owner per freezer), asks that
ended in a borrow, per-tier precision, uncheckable share.

**10.9 Email budget** (`apps/reports/budget.py`): send checks on `ask_lab` and `claims`
return `Suppressed("budget")` once the recipient had `email.max_non_alert_per_week` non-alert
emails in the last 7 days. The personal digest is never suppressed (it consolidates what
didn't go out) but counts. Exempt and not counted: `duplicate_alert`, `clear_to_order`,
`ask_owner`, `stale_nudge`, `system`, `lab_summary`.
