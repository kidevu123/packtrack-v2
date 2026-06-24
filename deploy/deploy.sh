#!/usr/bin/env bash
# Canonical Pack Track deploy. The ONLY approved path — see
# docs/RUNBOOK_DEPLOY.md for the full runbook and the rules around
# any out-of-band deploy (which must run the same CSS build + smoke
# checks this script enforces, or it WILL break prod the same way the
# v2.2.0 unstyled-UI incident did).
#
#   First run:  bash deploy/deploy.sh --first-run
#   Updates:    bash deploy/deploy.sh
#
# Requires direct SSH key access to LXC_HOST (default 192.168.1.206).
# Out-of-band deploys from hosts without that access must follow the
# verification recipe in docs/RUNBOOK_DEPLOY.md § "Out-of-band deploys".
set -euo pipefail

LXC_HOST="${LXC_HOST:-192.168.1.206}"
APP_DIR="/opt/packtrack/app"

FIRST_RUN=0
if [[ "${1:-}" == "--first-run" ]]; then
  FIRST_RUN=1
fi

cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "→ rsyncing app to ${LXC_HOST}:${APP_DIR}"
rsync -a --delete \
  --exclude '__pycache__' --exclude '.venv' --exclude '.ruff_cache' \
  --exclude '.pytest_cache' --exclude 'uploads' --exclude '.git' \
  --exclude 'tailwindcss' --exclude '*.pyc' \
  ./ "root@${LXC_HOST}:${APP_DIR}/"

# Bootstrap on the host: ensure venv, install deps, build CSS, run migrations,
# (re)install systemd unit + Caddyfile, restart services. Idempotent.
ssh "root@${LXC_HOST}" FIRST_RUN="${FIRST_RUN}" bash -s <<'REMOTE'
set -euo pipefail
APP_DIR=/opt/packtrack/app
chown -R packtrack:packtrack "$APP_DIR"

# Tailwind CLI (linux-x64) — fetched once, kept in /opt/packtrack/bin.
mkdir -p /opt/packtrack/bin
if [[ ! -x /opt/packtrack/bin/tailwindcss ]]; then
  echo "→ fetching tailwindcss linux-x64"
  curl -fsSL -o /opt/packtrack/bin/tailwindcss \
    https://github.com/tailwindlabs/tailwindcss/releases/download/v4.0.6/tailwindcss-linux-x64
  chmod +x /opt/packtrack/bin/tailwindcss
fi

# venv + deps
sudo -u packtrack bash -lc "
  set -euo pipefail
  cd '$APP_DIR'
  if [[ ! -d .venv ]]; then python3 -m venv .venv; fi
  . .venv/bin/activate
  pip install --quiet --upgrade pip
  pip install --quiet -e .
"

# Build CSS as packtrack so file ownership is correct. The output must
# exist, exceed a sane size, and contain a few sentinel utilities — without
# these every template renders un-styled (regression caught in v2.2.0).
CSS_OUT="$APP_DIR/static/styles.css"
sudo -u packtrack /opt/packtrack/bin/tailwindcss \
  -i "$APP_DIR/static/styles.src.css" \
  -o "$CSS_OUT" \
  --minify
ls -la "$CSS_OUT"
CSS_BYTES=$(wc -c <"$CSS_OUT")
if [[ "$CSS_BYTES" -lt 5000 ]]; then
  echo "ERROR: styles.css is only ${CSS_BYTES} bytes — Tailwind build produced no utilities. Refusing to deploy." >&2
  exit 1
fi
for sentinel in 'bg-stone-900' 'grid' 'max-w-md'; do
  if ! grep -q "\\.${sentinel}\\b" "$CSS_OUT"; then
    echo "ERROR: styles.css is missing the .${sentinel} utility — UI will render un-styled. Refusing to deploy." >&2
    exit 1
  fi
done
echo "✓ CSS build: ${CSS_BYTES} bytes, sentinels present"

# Migrations
sudo -u packtrack bash -lc "
  cd '$APP_DIR' && . .venv/bin/activate
  set -a; source /etc/packtrack/packtrack.env; set +a
  if [[ ! -d migrations/versions ]] || [[ -z \"\$(ls -A migrations/versions 2>/dev/null)\" ]]; then
    alembic revision --autogenerate -m 'initial schema'
  fi
  alembic upgrade head
"

# Systemd units (app + nightly backup)
install -m 644 -o root -g root "$APP_DIR/deploy/packtrack.service" /etc/systemd/system/packtrack.service
install -m 644 -o root -g root "$APP_DIR/deploy/packtrack-backup.service" /etc/systemd/system/packtrack-backup.service
install -m 644 -o root -g root "$APP_DIR/deploy/packtrack-backup.timer"   /etc/systemd/system/packtrack-backup.timer
chmod +x "$APP_DIR/deploy/backup.sh" "$APP_DIR/deploy/restore.sh"
mkdir -p /var/backups/packtrack
chown packtrack:packtrack /var/backups/packtrack
systemctl daemon-reload
systemctl enable packtrack.service packtrack-backup.timer
systemctl start packtrack-backup.timer || true

# Caddy
install -m 644 -o root -g root "$APP_DIR/deploy/Caddyfile" /etc/caddy/Caddyfile
mkdir -p /var/log/caddy && chown caddy:caddy /var/log/caddy

# Start / restart
systemctl restart packtrack.service
systemctl restart caddy.service
sleep 1
systemctl --no-pager --lines=8 status packtrack.service || true

# Post-restart smoke — fails the deploy if /healthz or /static/styles.css
# is broken. Catches both runtime crashes and the missing-CSS regression.
echo "----"
"$APP_DIR/scripts/smoke_test.sh" --base http://127.0.0.1
REMOTE

echo "→ done. http://${LXC_HOST}/"
