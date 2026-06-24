#!/usr/bin/env bash
# Pack Track post-deploy smoke test.
#
# Fails (exit 1) if any of these checks miss:
#   1. /healthz returns 200 with "ok":true
#   2. /static/styles.css returns 200
#   3. /static/styles.css exceeds CSS_MIN_BYTES (default 5000)
#   4. /static/styles.css contains all expected utility sentinels
#   5. /login references /static/styles.css
#
# Usage:
#   scripts/smoke_test.sh                              # defaults to http://127.0.0.1
#   scripts/smoke_test.sh --base https://packtrack.booute.duckdns.org
#   CSS_MIN_BYTES=10000 scripts/smoke_test.sh
#
# Intended to run from the deploy script (post-restart) and from the
# workstation against the public URL after any deploy path that bypasses
# deploy/deploy.sh.

set -euo pipefail

BASE="http://127.0.0.1"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --base) BASE="$2"; shift 2 ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \?//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
BASE="${BASE%/}"
CSS_MIN_BYTES="${CSS_MIN_BYTES:-5000}"
SENTINELS=("bg-stone-900" "grid" "size-12" "max-w-md")

fail=0
ok()    { printf "  ✓ %s\n" "$1"; }
bad()   { printf "  ✗ %s\n" "$1" >&2; fail=1; }

echo "smoke: ${BASE}"

# 1. /healthz
body=$(curl -fsS --max-time 10 "${BASE}/healthz") \
  && echo "$body" | grep -q '"ok":true' \
  && ok "/healthz returns ok=true" \
  || bad "/healthz did not return ok=true (body=${body:-<no body>})"

# 2 + 3. /static/styles.css size
http_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "${BASE}/static/styles.css") || http_code=000
if [[ "$http_code" == "200" ]]; then
  ok "/static/styles.css returns 200"
else
  bad "/static/styles.css returned HTTP ${http_code}"
fi

bytes=$(curl -fsS --max-time 10 "${BASE}/static/styles.css" 2>/dev/null | wc -c | tr -d ' ' || echo 0)
if [[ "$bytes" -ge "$CSS_MIN_BYTES" ]]; then
  ok "/static/styles.css is ${bytes} bytes (≥ ${CSS_MIN_BYTES})"
else
  bad "/static/styles.css is only ${bytes} bytes (< ${CSS_MIN_BYTES}) — Tailwind build likely empty"
fi

# 4. Sentinel utilities. Use a fresh GET per sentinel? No — one fetch, grep many.
css=$(curl -fsS --max-time 10 "${BASE}/static/styles.css" 2>/dev/null || true)
for s in "${SENTINELS[@]}"; do
  if printf "%s" "$css" | grep -qE "\.${s}\b"; then
    ok "css contains .${s}"
  else
    bad "css missing .${s}"
  fi
done

# 5. /login references /static/styles.css
login_body=$(curl -fsS --max-time 10 "${BASE}/login" 2>/dev/null || true)
if printf "%s" "$login_body" | grep -q "/static/styles.css"; then
  ok "/login references /static/styles.css"
else
  bad "/login did not reference /static/styles.css"
fi

if [[ $fail -eq 0 ]]; then
  echo "smoke: PASS"
  exit 0
fi
echo "smoke: FAIL" >&2
exit 1
