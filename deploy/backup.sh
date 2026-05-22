#!/usr/bin/env bash
# Nightly Postgres backup. Runs as the packtrack user via the systemd timer.
# Dumps /var/backups/packtrack/{date}.sql.gz, keeps the last 7 days.
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/var/backups/packtrack}"
RETAIN_DAYS="${RETAIN_DAYS:-7}"

mkdir -p "$BACKUP_DIR"
chown packtrack:packtrack "$BACKUP_DIR"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$BACKUP_DIR/packtrack-${STAMP}.sql.gz"

# Read DATABASE_URL out of the env file so we don't hard-code creds here.
# shellcheck disable=SC1091
source /etc/packtrack/packtrack.env
DB_URL="${DATABASE_URL:?DATABASE_URL not set in /etc/packtrack/packtrack.env}"

# pg_dump understands postgresql:// URLs but not psycopg-flavored
# postgresql+psycopg:// — strip the driver suffix.
PG_URL="${DB_URL/postgresql+psycopg:/postgresql:}"

pg_dump --no-owner --clean --if-exists --quote-all-identifiers --format=plain "$PG_URL" \
  | gzip -9 > "$OUT"

echo "Wrote $OUT ($(du -h "$OUT" | cut -f1))"

# Retention — older than RETAIN_DAYS, drop. Robust to filename format.
find "$BACKUP_DIR" -maxdepth 1 -name 'packtrack-*.sql.gz' -mtime "+$((RETAIN_DAYS - 1))" -print -delete || true
