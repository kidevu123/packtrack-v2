# Pack Track → Zoho purchase receives via zoho-integration-service

Pack Track does **not** call Zoho directly for purchase-receive writes. Every
receive that lands in Zoho goes through
[zoho-integration-service](https://github.com/sahiwal283/zoho-integration-service)
(`v1.26.0+`). The service owns Zoho OAuth, rate-limit handling, and the
operator-controlled `ENABLE_LIVE_INVENTORY_WRITES` switch.

## 1. Environment configuration

Add these to `/etc/packtrack/packtrack.env` (or your local `.env`):

```
ZOHO_INTEGRATION_BASE_URL=http://192.168.1.205:8000
ZOHO_INTEGRATION_APP_TOKEN=<app credential from the integration service>
ZOHO_INTEGRATION_BRAND=haute_brands
ZOHO_INTEGRATION_TIMEOUT_SECONDS=30
ZOHO_INTEGRATION_RECEIVE_ENABLED=true
```

| Var | Purpose |
|---|---|
| `ZOHO_INTEGRATION_BASE_URL` | Reachable URL for the integration service. Prefer the LAN address; the host header is set automatically. |
| `ZOHO_INTEGRATION_APP_TOKEN` | Bearer credential the service issues per app. Treated as a secret; never logged. |
| `ZOHO_INTEGRATION_BRAND` | Sent as `X-Brand`. The service uses it to pick the right Zoho org/credentials. |
| `ZOHO_INTEGRATION_TIMEOUT_SECONDS` | HTTP timeout for both preview and commit. Default 30s. |
| `ZOHO_INTEGRATION_RECEIVE_ENABLED` | Operator off-switch — flip to `false` to stop Pack Track from calling the service during an incident or freeze. Receipts stay in Pack Track. |

The legacy `ZOHO_GATEWAY_*` settings stay for read syncs (item + open PO
mirror) only. Receive writes never use them.

## 2. Flow

```
operator submits /receive/{zoho_po_id}
        │
        ▼
record BoxReceipt rows in Pack Track   ← single source of truth, always written
push receipts to Luma (per line)       ← independent of Zoho
        │
        ▼
for each line: commit_receive() ───────► zoho-integration-service
                                           /zoho/pack_track/receive/commit
```

* **Idempotency-Key:** `PACK_TRACK_RECEIVE_<BoxReceipt.packtrack_receipt_id>`.
  Same key + same body on retry is a 200 (no duplicate). Same key + different
  body is a 409 — surfaced as a `zoho_receive` POEvent for an operator to
  investigate.
* **Headers:** `Authorization: Bearer <token>`, `X-Brand: <brand>`,
  `Content-Type: application/json`, `Accept: application/json`. The legacy
  `X-Internal-Token` header is **not** valid here.
* **`purchaseorder_line_item_id`:** sourced from the synced ZohoMirror. The
  open-PO sync now persists it; existing mirrors need a re-sync once after
  upgrade (Admin → Sync → "Sync now").

## 3. Preview vs commit

| Endpoint | Reads Zoho? | Writes Zoho? | Used by |
|---|---|---|---|
| `/zoho/pack_track/receive/preview` | yes | no | Smoke tests, future "dry-run" admin button |
| `/zoho/pack_track/receive/commit`  | yes | yes (when `ENABLE_LIVE_INVENTORY_WRITES=true`) | The receiving form |

`commit` returns `403 LIVE_WRITE_DISABLED` when the service is in dry-run
mode. Pack Track records this per-line as a `blocked` status, NOT as a
failure — the receipt stays in Pack Track and Luma, and the next live-write-
enabled commit (or a Zoho catch-up sync) reconciles the state.

## 4. Failure states

| Service response | Pack Track status | Operator action |
|---|---|---|
| `200` | `committed` | None — done. |
| `403 LIVE_WRITE_DISABLED` | `blocked` | None for ops yet; will reconcile when live writes are enabled. |
| `403 ZOHO_AUTH_FORBIDDEN` | `auth_failed` | Refresh Zoho creds on the service. |
| `400 BRAND_REQUIRED` / `404 BRAND_NOT_FOUND` | `validation_failed` / `config_error` | Fix `ZOHO_INTEGRATION_BRAND` or service brand config. |
| `404 CREDENTIAL_NOT_FOUND` / `ORG_NOT_CONFIGURED` / `PRODUCT_NOT_CONFIGURED` | `config_error` | Fix on the service side. |
| `400 PO_LINE_ITEM_NOT_FOUND` | `validation_failed` | Re-sync open POs from Admin → Sync to refresh `line_item_id`. |
| `422 ITEM_PO_MISMATCH` / `INSUFFICIENT_PO_REMAINING` | `validation_failed` | Wrong line/qty for that PO — check the form input. |
| `409` | `idempotency_conflict` | Loud — indicates a data-consistency bug. Investigate the BoxReceipt and the prior call. |
| `429 RATE_LIMIT_EXCEEDED` | `rate_limited` | Backoff and retry. |
| `5xx` / network error | `gateway_error` | Re-submit the affected lines after the service recovers. |

Receipts are always recorded in Pack Track and Luma first, then submitted to
the service — so a service outage cannot lose data; it can only delay the
Zoho write.

`PO_NOT_MARKED_AS_PACKAGING` is documented as advisory by the service —
Pack Track does not treat it as a failure (the call succeeds with a warning).

## 5. Deployment

The service runs on Proxmox at LXC 9503 (`192.168.1.205:8000`).
Pack Track LXC 200 (`192.168.1.206`) reaches it over the LAN.

```
ssh root@192.168.1.190   # Proxmox host
pct enter 200            # Pack Track LXC
nano /etc/packtrack/packtrack.env   # add ZOHO_INTEGRATION_* keys
exit
# from workstation:
bash deploy/deploy.sh
```

The deploy script restarts `packtrack.service` automatically. After it
finishes, hit `/healthz` once and tail logs:

```
ssh root@192.168.1.206 'journalctl -u packtrack -n 100 --no-pager'
```

## 6. Safe smoke test (while `ENABLE_LIVE_INVENTORY_WRITES=false`)

The service must be at v1.26.0+ and reporting `db=true` on `/health`.

**Preview** (returns the would-be Zoho payload; never writes):

```bash
TOKEN=$ZOHO_INTEGRATION_APP_TOKEN
BRAND=$ZOHO_INTEGRATION_BRAND
SERVICE=$ZOHO_INTEGRATION_BASE_URL

curl -sS -X POST "$SERVICE/zoho/pack_track/receive/preview" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Brand: $BRAND" \
  -H "Idempotency-Key: PACK_TRACK_RECEIVE_SMOKE-$(date +%s)" \
  -H "Content-Type: application/json" \
  -d '{
    "pack_track_receipt_id":"SMOKE-PREVIEW",
    "purchaseorder_id":"<a real open PO id from the mirror>",
    "purchaseorder_line_item_id":"<line_item_id from the mirror>",
    "item_id":"<zoho_item_id>",
    "received_quantity":1,
    "received_date":"'"$(date -u +%F)"'"
  }' | jq .
```

**Commit** (must return 403 LIVE_WRITE_DISABLED — no Zoho write happens):

```bash
curl -sS -X POST "$SERVICE/zoho/pack_track/receive/commit" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Brand: $BRAND" \
  -H "Idempotency-Key: PACK_TRACK_RECEIVE_SMOKE-$(date +%s)" \
  -H "Content-Type: application/json" \
  -d '{...same body as above...}' -i | head -20
# Expect HTTP/1.1 403  and  {"error":"LIVE_WRITE_DISABLED", ...}
```

**Confirm no direct Zoho calls from Pack Track:** during a real receive
submission, watch the Pack Track logs and look for the integration service
host — never `zohoapis.com` or `accounts.zoho.com`:

```bash
ssh root@192.168.1.206 \
  "journalctl -u packtrack -n 200 --no-pager | egrep -i 'zoho|integration|receive' || true"
```
