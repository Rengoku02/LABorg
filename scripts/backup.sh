#!/usr/bin/env bash
# Nightly backup, run from the HOST (cron or a systemd timer), never from Django-Q2 —
# a dead worker must not silently stop backups (plan §6).
#
#   RESTIC_REPOSITORY=s3:... RESTIC_PASSWORD_FILE=/root/.restic-pass \
#   LABSTOCK_HEARTBEAT_BACKUP_URL=https://hc-ping.com/<uuid> ./scripts/backup.sh
#
# The restic password, ESP key and Quartzy token must also live in a password manager the
# deputy maintainer can open: an off-site backup you can't decrypt is not a backup.
set -euo pipefail

cd "$(dirname "$0")/.."
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

# shellcheck disable=SC1091
source .env

docker compose exec -T db pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc > "$workdir/labstock-$stamp.dump"
# The app image runs as an unprivileged user; root is needed to write into $workdir.
docker compose run --rm --no-deps -T --user root -v "$workdir:/backup" web \
  tar -C /app -czf "/backup/media-$stamp.tar.gz" media

restic backup --tag labstock "$workdir"
restic forget --tag labstock --keep-daily 7 --keep-weekly 4 --keep-monthly 12 --prune

if [[ -n "${LABSTOCK_HEARTBEAT_BACKUP_URL:-}" ]]; then
  curl -fsS -m 10 --retry 3 "$LABSTOCK_HEARTBEAT_BACKUP_URL" > /dev/null
fi
echo "backup $stamp ok"
