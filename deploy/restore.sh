#!/usr/bin/env bash
# Restore a backup created by deploy/backup.sh.
#
#   sudo bash deploy/restore.sh /var/backups/packtrack/packtrack-2026....sql.gz
#
# Stops the app, drops the existing schema, replays the dump, restarts.
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <backup.sql.gz>" >&2
  exit 1
fi
BACKUP="$1"
[[ -f "$BACKUP" ]] || { echo "File not found: $BACKUP" >&2; exit 1; }

# shellcheck disable=SC1091
source /etc/packtrack/packtrack.env
PG_URL="${DATABASE_URL/postgresql+psycopg:/postgresql:}"

echo "Stopping packtrack…"
systemctl stop packtrack.service

echo "Replaying $BACKUP"
gunzip -c "$BACKUP" | psql --quiet "$PG_URL"

echo "Restarting packtrack…"
systemctl start packtrack.service
sleep 2
systemctl --no-pager status packtrack.service | head -8
