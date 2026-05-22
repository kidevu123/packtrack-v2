#!/usr/bin/env bash
# Deploy from Mac to LXC.
#   First run:  bash deploy/deploy.sh --first-run
#   Updates:    bash deploy/deploy.sh
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

# Build CSS as packtrack so file ownership is correct.
sudo -u packtrack /opt/packtrack/bin/tailwindcss \
  -i "$APP_DIR/static/styles.src.css" \
  -o "$APP_DIR/static/styles.css" \
  --minify
ls -la "$APP_DIR/static/styles.css"

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
echo "----"
curl -fsS http://127.0.0.1/healthz || true
echo
REMOTE

echo "→ done. http://${LXC_HOST}/"
