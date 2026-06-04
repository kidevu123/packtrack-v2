#!/usr/bin/env bash
# Deploy PackTrack from this workstation to a Proxmox-managed LXC.
#
# Defaults match the production target:
#   PVE_HOST=192.168.1.190 LXC_ID=200 bash deploy/deploy.sh
#
# Direct-to-container SSH is still supported:
#   LXC_HOST=192.168.1.50 bash deploy/deploy.sh
set -Eeuo pipefail

PVE_HOST="${PVE_HOST:-192.168.1.190}"
LXC_ID="${LXC_ID:-200}"
LXC_HOST="${LXC_HOST:-}"
APP_DIR="${APP_DIR:-/opt/packtrack/app}"
UPLOAD_DIR="${UPLOAD_DIR:-/opt/packtrack/uploads}"
ENV_FILE="${ENV_FILE:-/etc/packtrack/packtrack.env}"
REMOTE_USER="${REMOTE_USER:-root}"
FIRST_RUN=0

if [[ "${1:-}" == "--first-run" ]]; then
  FIRST_RUN=1
fi

cd "$(dirname "${BASH_SOURCE[0]}")/.."

die() {
  echo "ERROR: $*" >&2
  exit 1
}

need() {
  command -v "$1" >/dev/null 2>&1 || die "Missing required local command: $1"
}

preflight_local() {
  need ssh
  need tar
  if [[ -n "$LXC_HOST" ]]; then
    need rsync
    ssh -o BatchMode=yes -o ConnectTimeout=8 "${REMOTE_USER}@${LXC_HOST}" "true" \
      || die "Cannot SSH to LXC_HOST=${LXC_HOST}"
  else
    need scp
    ssh -o BatchMode=yes -o ConnectTimeout=8 "${REMOTE_USER}@${PVE_HOST}" "command -v pct >/dev/null && pct status ${LXC_ID}" \
      || die "Cannot SSH to Proxmox ${PVE_HOST} or LXC ${LXC_ID} is unavailable"
  fi
}

make_bundle() {
  local bundle
  bundle="$(mktemp -t packtrack-src.XXXXXX.tgz)"
  COPYFILE_DISABLE=1 tar \
    --exclude='./.git' \
    --exclude='./.venv' \
    --exclude='./.ruff_cache' \
    --exclude='./.pytest_cache' \
    --exclude='./__pycache__' \
    --exclude='./uploads' \
    --exclude='./logs' \
    --exclude='./tailwindcss' \
    --exclude='._*' \
    --exclude='.DS_Store' \
    --exclude='*.pyc' \
    -czf "$bundle" .
  echo "$bundle"
}

remote_script='
set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/packtrack/app}"
UPLOAD_DIR="${UPLOAD_DIR:-/opt/packtrack/uploads}"
ENV_FILE="${ENV_FILE:-/etc/packtrack/packtrack.env}"
FIRST_RUN="${FIRST_RUN:-0}"
SRC_TGZ="${SRC_TGZ:-/tmp/packtrack-src.tgz}"

fail() {
  echo "ERROR: $*" >&2
  if command -v systemctl >/dev/null 2>&1; then
    echo "---- packtrack logs ----" >&2
    journalctl -u packtrack.service --no-pager --lines=40 >&2 || true
  fi
  exit 1
}

command -v python3 >/dev/null 2>&1 || fail "python3 is required"
python3 - <<PY || fail "Python 3.12+ is required"
import sys
raise SystemExit(0 if sys.version_info >= (3, 12) else 1)
PY
command -v systemctl >/dev/null 2>&1 || fail "systemd is required"
command -v curl >/dev/null 2>&1 || fail "curl is required"
command -v psql >/dev/null 2>&1 || echo "WARN: psql not found; relying on app health check for Postgres"

id packtrack >/dev/null 2>&1 || useradd --system --home /opt/packtrack --shell /usr/sbin/nologin packtrack
test -f "$ENV_FILE" || fail "Missing env file: $ENV_FILE"

mkdir -p "$APP_DIR" "$UPLOAD_DIR" /var/log/packtrack /opt/packtrack/bin
chown -R packtrack:packtrack /opt/packtrack /var/log/packtrack

if [[ -n "$SRC_TGZ" ]]; then
  test -f "$SRC_TGZ" || fail "Missing staged source bundle: $SRC_TGZ"
  find "$APP_DIR" -mindepth 1 \
    ! -path "$APP_DIR/.venv*" \
    ! -path "$APP_DIR/uploads*" \
    -exec rm -rf {} +
  tar -xzf "$SRC_TGZ" -C "$APP_DIR"
fi
chown -R packtrack:packtrack "$APP_DIR"

if [[ ! -x /opt/packtrack/bin/tailwindcss ]]; then
  echo "-> fetching tailwindcss linux-x64"
  curl -fsSL -o /opt/packtrack/bin/tailwindcss \
    https://github.com/tailwindlabs/tailwindcss/releases/download/v4.0.6/tailwindcss-linux-x64 \
    || fail "Could not download Tailwind CLI"
  chmod +x /opt/packtrack/bin/tailwindcss
fi

sudo -u packtrack bash -lc "
  set -Eeuo pipefail
  cd '\''$APP_DIR'\''
  if [[ ! -d .venv ]]; then python3 -m venv .venv; fi
  . .venv/bin/activate
  python -m pip install --quiet --upgrade pip
  pip install --quiet -e .
"

sudo -u packtrack /opt/packtrack/bin/tailwindcss \
  -i "$APP_DIR/static/styles.src.css" \
  -o "$APP_DIR/static/styles.css" \
  --minify || fail "Tailwind build failed"

sudo -u packtrack bash -lc "
  set -Eeuo pipefail
  cd '\''$APP_DIR'\''
  . .venv/bin/activate
  set -a; source '\''$ENV_FILE'\''; set +a
  alembic upgrade head
" || fail "Alembic migration failed"

install -m 644 -o root -g root "$APP_DIR/deploy/packtrack.service" /etc/systemd/system/packtrack.service
install -m 644 -o root -g root "$APP_DIR/deploy/packtrack-backup.service" /etc/systemd/system/packtrack-backup.service
install -m 644 -o root -g root "$APP_DIR/deploy/packtrack-backup.timer" /etc/systemd/system/packtrack-backup.timer
chmod +x "$APP_DIR/deploy/backup.sh" "$APP_DIR/deploy/restore.sh"
mkdir -p /var/backups/packtrack
chown packtrack:packtrack /var/backups/packtrack

if command -v caddy >/dev/null 2>&1; then
  install -m 644 -o root -g root "$APP_DIR/deploy/Caddyfile" /etc/caddy/Caddyfile
  mkdir -p /var/log/caddy
  chown caddy:caddy /var/log/caddy || true
fi

systemctl daemon-reload
systemctl enable packtrack.service packtrack-backup.timer >/dev/null
systemctl start packtrack-backup.timer || true
systemctl restart packtrack.service
if command -v caddy >/dev/null 2>&1; then
  systemctl restart caddy.service || fail "Caddy restart failed"
fi

sleep 2
systemctl is-active --quiet packtrack.service || fail "packtrack.service is not active"
curl -fsS http://127.0.0.1:8000/healthz >/tmp/packtrack-health.json || fail "healthz failed"
cat /tmp/packtrack-health.json
echo
echo "Deploy complete. Uploads preserved at $UPLOAD_DIR."
'

deploy_direct() {
  local bundle="$1"
  : "$bundle"
  echo "-> rsyncing app to ${LXC_HOST}:${APP_DIR}"
  rsync -a --delete \
    --exclude '__pycache__' --exclude '.venv' --exclude '.ruff_cache' \
    --exclude '.pytest_cache' --exclude 'uploads' --exclude 'logs' \
    --exclude '.git' --exclude 'tailwindcss' --exclude '*.pyc' \
    ./ "${REMOTE_USER}@${LXC_HOST}:${APP_DIR}/"
  ssh "${REMOTE_USER}@${LXC_HOST}" \
    APP_DIR="$APP_DIR" UPLOAD_DIR="$UPLOAD_DIR" ENV_FILE="$ENV_FILE" FIRST_RUN="$FIRST_RUN" SRC_TGZ="" \
    bash -s <<<"$remote_script"
}

deploy_via_pve() {
  local bundle="$1"
  local remote_bundle="/tmp/packtrack-src-${LXC_ID}.tgz"
  echo "-> staging bundle on Proxmox ${PVE_HOST}"
  scp "$bundle" "${REMOTE_USER}@${PVE_HOST}:${remote_bundle}" >/dev/null
  echo "-> pushing bundle into LXC ${LXC_ID}"
  ssh "${REMOTE_USER}@${PVE_HOST}" "pct push ${LXC_ID} '${remote_bundle}' /tmp/packtrack-src.tgz && rm -f '${remote_bundle}'"
  echo "-> deploying inside LXC ${LXC_ID}"
  ssh "${REMOTE_USER}@${PVE_HOST}" \
    "pct exec ${LXC_ID} -- env APP_DIR='${APP_DIR}' UPLOAD_DIR='${UPLOAD_DIR}' ENV_FILE='${ENV_FILE}' FIRST_RUN='${FIRST_RUN}' SRC_TGZ=/tmp/packtrack-src.tgz bash -lc $(printf %q "$remote_script")"
}

preflight_local
bundle="$(make_bundle)"
trap 'rm -f "$bundle"' EXIT

if [[ -n "$LXC_HOST" ]]; then
  deploy_direct "$bundle"
else
  deploy_via_pve "$bundle"
fi
