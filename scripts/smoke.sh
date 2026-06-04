#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || command -v python)}"

curl -fsS "${BASE_URL}/healthz" | "$PYTHON_BIN" -m json.tool
echo "smoke ok: ${BASE_URL}/healthz"
