#!/usr/bin/env bash
# Canonical Pack Track deploy — the ONLY approved deploy path.
# See docs/RUNBOOK_DEPLOY.md for the full runbook. Any out-of-band
# deploy (manual rsync, ad-hoc `pct push`, etc.) MUST end with the
# same Tailwind CSS build + scripts/smoke_test.sh checks this script
# enforces, or it WILL break prod the way the v2.2.0 unstyled-UI
# incident did.
#
# Defaults match production:
#   PVE_HOST=192.168.1.190 LXC_ID=200 bash deploy/deploy.sh
#
# Direct-to-container SSH (skips the PVE jump) is also supported when
# the workstation has key access to the LXC:
#   LXC_HOST=192.168.1.206 bash deploy/deploy.sh
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

# ---------------------------------------------------------------------------
# Repo-state guard (v2.16.1)
# ---------------------------------------------------------------------------
#
# Production deploys MUST come from `main` at exactly `origin/main`, with
# a clean working tree. The script refuses otherwise. Permanent defense
# against the v2.7.4 and v2.16.0 incidents where the deploy was
# accidentally run from a worktree on a feature branch.
#
# Escape hatch (NOT for routine prod use): ALLOW_NON_MAIN_DEPLOY=1
#   * skips the branch + freshness checks
#   * still refuses a dirty working tree
#   * prints a loud warning + the branch / SHA it is about to ship
#
# The guard never runs git in a destructive mode (no checkout / pull /
# reset). On any failure it prints the exact remediation command.

_read_version() {
  if [[ -f pyproject.toml ]]; then
    local v
    v="$(awk -F'"' '/^version[[:space:]]*=/{print $2; exit}' pyproject.toml 2>/dev/null)"
    if [[ -n "$v" ]]; then echo "$v"; return; fi
  fi
  if [[ -f packtrack/__init__.py ]]; then
    awk -F'"' '/^__version__[[:space:]]*=/{print $2; exit}' packtrack/__init__.py 2>/dev/null || echo unknown
    return
  fi
  echo unknown
}

_read_alembic_head() {
  # Best-effort: pick the file whose `revision` is not referenced as
  # any other file's `down_revision`. If ambiguous, return "?".
  local dir=migrations/versions
  [[ -d "$dir" ]] || { echo "?"; return; }
  local files
  files=$(ls -1 "$dir"/*.py 2>/dev/null | grep -v '__init__' || true)
  [[ -n "$files" ]] || { echo "?"; return; }
  local downs heads head_count
  downs=$(awk -F"['\"]" '/^down_revision[[:space:]]*[:=]/{
    for (i=1; i<=NF; i++) if ($i != "" && $i ~ /^[a-zA-Z0-9_-]+$/) { print $i; break }
  }' $files 2>/dev/null | sort -u)
  heads=$(for f in $files; do
    awk -F"['\"]" '/^revision[[:space:]]*[:=]/{
      for (i=1; i<=NF; i++) if ($i != "" && $i ~ /^[a-zA-Z0-9_-]+$/) { print $i; break }
    }' "$f"
  done | sort -u | grep -v -F -x -f <(printf '%s\n' "$downs") 2>/dev/null || true)
  head_count=$(echo "$heads" | grep -c . || true)
  if [[ "$head_count" == "1" ]]; then
    echo "$heads"
  else
    echo "?"
  fi
}

guard_repo_state() {
  need git
  if ! git rev-parse --git-dir >/dev/null 2>&1; then
    die "Not inside a git repository (cwd=$PWD)"
  fi

  local branch sha pkg_version alembic_head
  branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
  sha="$(git rev-parse --short=12 HEAD 2>/dev/null || echo unknown)"
  pkg_version="$(_read_version)"
  alembic_head="$(_read_alembic_head)"

  local override="${ALLOW_NON_MAIN_DEPLOY:-0}"

  # --- Branch check ---
  if [[ "$branch" != "main" ]]; then
    if [[ "$override" == "1" ]]; then
      echo "" >&2
      echo "  =====================================================================" >&2
      echo "  WARNING: deploying from NON-main branch via ALLOW_NON_MAIN_DEPLOY=1" >&2
      echo "  branch:  $branch" >&2
      echo "  sha:     $sha" >&2
      echo "  version: $pkg_version" >&2
      echo "  alembic: $alembic_head" >&2
      echo "  This bypass is for testing / recovery. Do NOT use for routine prod." >&2
      echo "  =====================================================================" >&2
      echo "" >&2
    else
      die "Refusing to deploy from non-main branch.
  current branch: $branch
  current sha:    $sha
  current version: $pkg_version

  Production deploys MUST come from main at origin/main. Run:

    cd \$(git rev-parse --show-toplevel)
    git checkout main
    git pull --ff-only origin main
    PVE_HOST=${PVE_HOST} LXC_ID=${LXC_ID} bash deploy/deploy.sh

  To bypass (testing / recovery only):

    ALLOW_NON_MAIN_DEPLOY=1 PVE_HOST=${PVE_HOST} LXC_ID=${LXC_ID} bash deploy/deploy.sh"
    fi
  fi

  # --- Dirty-tree check (always enforced, even with override) ---
  # `git status --porcelain` lists staged, unstaged AND untracked files,
  # respecting .gitignore. If any line comes back, the tree is dirty.
  local dirty
  dirty="$(git status --porcelain 2>/dev/null || true)"
  if [[ -n "$dirty" ]]; then
    die "Refusing to deploy with a dirty working tree.
  branch: $branch
  sha:    $sha

  The following changes are present (commit / stash / remove first):

$(echo "$dirty" | sed 's/^/    /')

  After cleaning the tree, re-run the deploy."
  fi

  # --- Freshness check (skipped on override) ---
  if [[ "$override" != "1" ]]; then
    if ! git fetch --quiet origin main 2>/dev/null; then
      die "git fetch origin main failed. Network / auth / remote 'origin' misconfigured? Fix and retry."
    fi
    local local_main remote_main
    local_main="$(git rev-parse main 2>/dev/null || echo missing)"
    remote_main="$(git rev-parse origin/main 2>/dev/null || echo missing)"
    if [[ "$local_main" == "missing" || "$remote_main" == "missing" ]]; then
      die "Cannot resolve local 'main' or 'origin/main'. Ensure both refs exist."
    fi
    if [[ "$local_main" != "$remote_main" ]]; then
      local relation
      if git merge-base --is-ancestor "$local_main" "$remote_main" 2>/dev/null; then
        relation="behind origin/main"
      elif git merge-base --is-ancestor "$remote_main" "$local_main" 2>/dev/null; then
        relation="ahead of origin/main"
      else
        relation="diverged from origin/main"
      fi
      die "Local 'main' is $relation. Refusing to deploy a different commit than what's on origin.
  local  main: $local_main
  origin/main: $remote_main

  Fix:
    git checkout main
    git pull --ff-only origin main"
    fi
  fi

  # --- Pre-deploy banner ---
  echo ""
  echo "  ------------------------------------------------------------"
  echo "  PackTrack deploy"
  echo "  branch:  $branch"
  echo "  sha:     $sha"
  echo "  version: $pkg_version"
  echo "  alembic: $alembic_head"
  echo "  target:  PVE_HOST=${PVE_HOST}  LXC_ID=${LXC_ID}"
  echo "  ------------------------------------------------------------"
  echo ""
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

# Build CSS as packtrack so file ownership is correct. The output must
# exist, exceed a sane size, and contain a few sentinel utilities —
# without these every template renders un-styled (regression caught in
# the v2.2.0 unstyled-UI incident).
CSS_OUT="$APP_DIR/static/styles.css"
sudo -u packtrack /opt/packtrack/bin/tailwindcss \
  -i "$APP_DIR/static/styles.src.css" \
  -o "$CSS_OUT" \
  --minify || fail "Tailwind build failed"
ls -la "$CSS_OUT"
CSS_BYTES=$(wc -c <"$CSS_OUT")
if [[ "$CSS_BYTES" -lt 5000 ]]; then
  fail "styles.css is only ${CSS_BYTES} bytes — Tailwind build produced no utilities. Refusing to deploy."
fi
for sentinel in '\''bg-stone-900'\'' '\''grid'\'' '\''max-w-md'\''; do
  if ! grep -q "\\.${sentinel}\\b" "$CSS_OUT"; then
    fail "styles.css is missing the .${sentinel} utility — UI will render un-styled. Refusing to deploy."
  fi
done
echo "✓ CSS build: ${CSS_BYTES} bytes, sentinels present"


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

# Post-restart smoke — fails the deploy if /healthz or /static/styles.css
# is broken. Catches both runtime crashes and the missing-CSS regression
# that /healthz alone is blind to.
echo "---- post-restart smoke ----"
"$APP_DIR/scripts/smoke_test.sh" --base http://127.0.0.1:8000 || fail "post-restart smoke test failed"

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

guard_repo_state
preflight_local
bundle="$(make_bundle)"
trap 'rm -f "$bundle"' EXIT

if [[ -n "$LXC_HOST" ]]; then
  deploy_direct "$bundle"
else
  deploy_via_pve "$bundle"
fi
