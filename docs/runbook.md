# LabStock runbook

This is for the system owner and the deputy. Keep it short, and keep it true: update it when
the procedures change.

## Secrets (password manager shared with the deputy)

- `DJANGO_SECRET_KEY`. Rotating it invalidates every emailed action link and every session.
- `QUARTZY_ACCESS_TOKEN`. Record the token owner and their Quartzy role here: _<name, role>_.
- ESP API key, `POSTGRES_PASSWORD`, the restic repository + password, heartbeat URLs.

## Deploy / upgrade

```bash
git pull
docker compose up -d --build        # web runs migrate, createcachetable, seed_catalog, setup_schedules
docker compose logs -f web worker
```

Check the health panel (`/quartzy/health/`, staff only) and `https://<host>/healthz`.

## Catalog fixes: resolving requests again

- Changing a Quartzy type's item type in the admin resolves that type's requests again in the
  background ("Catalog re-resolution" runs in Admin → Sync runs, one per 200 requests; the
  health panel shows the last one and how many requests it resolved differently). So does
  changing an item type's identity rule (Admin → Catalog → Item types, or a `seed_catalog`
  that changes it), for every Quartzy type mapped to it, and the nightly sync when it suggests
  an item type for an unmapped type (e.g. after the first `seed_catalog`); a type someone has
  saved or confirmed is never changed by the sync. If such a run shows errors, those requests
  kept their old resolution: run `quartzy_resolve --type <the type's Quartzy id>` once the
  cause is fixed.
- A request whose catalog resolution failed is tried again once a day when the sync re-reads
  it. To retry them all now: `docker compose exec web python manage.py quartzy_resolve --unresolved`.
- After a deploy that changes catalog normalization or `vendors.yaml` (web has re-run
  `seed_catalog`), re-key the listings, then re-resolve the requests that were matched under
  the old rules. Listings and provisional products nothing uses any more are retired.

  ```bash
  docker compose exec web python manage.py refresh_catalog_keys
  docker compose exec web python manage.py quartzy_resolve --all --dry-run   # what would change
  docker compose exec web python manage.py quartzy_resolve --all
  ```

## Quartzy labs: which ones are mirrored

The token sees every Quartzy lab its owner belongs to, a collaborator's included. The sync
records each one it finds, but mirrors (syncs, and shows the rows of) only the active ones:

- A lab found for the first time is active only if its id is in `QUARTZY_LAB_IDS` (`.env`,
  comma-separated), or, while that is empty, if it is the only lab the token sees and this
  lab has no Quartzy lab yet. Any other is left inactive; `quartzy_backfill`, `quartzy_sync`
  and the health panel list those.
- To mirror one: Admin → Quartzy mirror → Quartzy labs → tick "Is active", then run
  `docker compose exec web python manage.py quartzy_backfill --lab <slug>`.
- To stop mirroring one: untick "Is active". Its rows stay but are hidden from every page. To
  delete them (and the people only it knew), run the purge; it is a dry run without `--commit`:

  ```bash
  docker compose exec web python manage.py quartzy_purge_lab <Quartzy lab id>
  docker compose exec web python manage.py quartzy_purge_lab <Quartzy lab id> --commit
  ```

## Alerts: arming

`quartzy_backfill --arm` arms duplicate alerts from now on; backfilled history never alerts.
It refuses while a Quartzy type that has requests still carries an item type the sync only
suggested (Admin → Quartzy types: confirm or correct those first). Running it again keeps the
first arming time, so requests made since then (say, while the worker was down) can still
alert; `--rearm` moves it to now on purpose.

## Quartzy token rotation (owner leaving, leak, expiry)

1. The new owner creates a token in Quartzy: Profile Settings → Access Tokens. A Lab Member
   role is enough.
2. Put it in `.env`, then run `docker compose up -d` (this recreates web and worker).
3. Delete the old token in Quartzy.
4. Update the owner line under "Secrets" above.

## Alerts: emergency stop

- Turn off every alert: Admin → Core → Policy settings → `alerts.enabled` = `false`.
  Send checks re-read it, so queued alerts are held, not sent.
- Turn off one tier: remove it from `alerts.tiers_live`.
- Held notifications: Admin → Notifications → filter status "Held" → "Release held" when safe.
- Revoke emailed links: select the notifications, then run the "Revoke emailed links" action.

## Backups and the monthly restore drill

Install `scripts/backup.sh` as a nightly cron job or systemd timer on the host. Monthly drill:

```bash
restic snapshots --tag labstock
restic restore latest --tag labstock --target /tmp/labstock-restore
docker compose -p labstock-drill -f compose.yaml up -d db
docker compose -p labstock-drill exec -T db pg_restore -U labstock -d labstock --clean --if-exists < /tmp/labstock-restore/.../labstock-*.dump
docker compose -p labstock-drill exec -T db psql -U labstock -c "select count(*) from quartzy_orderrequest;"
docker compose -p labstock-drill down -v
```

Record the date and the row counts in the drill log at the end of this file. The drill doubles
as migration practice when you change hosts: restore, then change DNS.

## Monitoring

- **External dead-man's switch** (e.g. healthchecks.io), with three checks. All of them email
  both the owner and the deputy.
  - The worker heartbeat (`LABSTOCK_HEARTBEAT_WORKER_URL`): every 5 minutes.
  - The sync (`LABSTOCK_HEARTBEAT_SYNC_URL`): pinged only after a head run that reached
    Quartzy and succeeded, and only while no active Quartzy lab is behind (no successful head
    sync for 30 minutes). Sweep, hot-set, types, inventory and catalog re-resolution runs
    never ping. So a revoked token, a dead head probe, or one lab out of several falling
    behind all make it fire. Give it a period of 10 minutes and a grace time of about 30, so
    the 10-minute night cadence doesn't page anyone.
  - The nightly backup.
- **In-app banner:** every page tells the lab's members "Quartzy sync is behind" while the
  last successful head sync of one of its Quartzy labs is more than 30 minutes old. The health
  panel (`/quartzy/health/`) says which lab, and shows each job's last run.
- **Emails to the system owners** (SYSTEM notifications, at most one of each kind per day):
  - Quartzy refused the token (401/403): every sync of that lab stops until the token works
    again. See "Quartzy token rotation".
  - A sync run aborted: too many 404s by id, the canary request missing too, the lab itself
    gone from the token's view, or Quartzy listing no requests for a lab that has them.
    Nothing is marked deleted in those cases; check the token and the lab id.
  - The daily deletion cap (policy `sync.max_tombstones_per_day`, 50) was reached: more
    requests or items returned 404 than that in 24 hours. The rest wait. Raise the cap in
    Admin → Policy settings if the deletions are real.
- A record Quartzy won't serve by id (a 403 for a request moved to another lab, a persistent
  500) is a sync error of kind "read" (Admin → Sync errors), and the others are read anyway.
  A request Quartzy refuses by id (403) never stops the hot set, however many there are, and
  never sends the "refused the token" email; a 401 or a 429 stops it at once, and so do three
  reads in a row with no answer or a 5xx (an outage). A token that lost sight of the whole lab
  shows in the head and sweep runs, which read its request list.

## Drill log

| Date | Snapshot | Rows (order requests) | Who |
|---|---|---|---|
