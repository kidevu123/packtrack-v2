# Zoho API Gateway — PackTrack Migration Plan

There is already a dedicated **Zoho Integration Service** (LXC 9503,
`192.168.1.205:8000`, FastAPI/uvicorn, multi-brand) that owns Zoho OAuth
tokens, encryption, and a generic `/zoho/{service}/{action}` proxy. This
doc plans how PackTrack should consume that gateway instead of holding
its own Zoho credentials.

> Implementation deferred to **P8**. P0 is planning only.

---

## 1. What does PackTrack's local Zoho client do today?

`packtrack/zoho.py` exposes 8 public functions, all hitting Zoho Inventory
directly via `https://www.zohoapis.com/inventory/v1/...` with a refresh
token PackTrack holds locally:

| Function | Verb | Zoho endpoint | Triggered by |
|---|---|---|---|
| `sync_items(session)` | GET | `/items` (paginated), `/itemdetails` (batched), `/items/{id}/image` | APScheduler every 30 min + Admin → Sync Now |
| `sync_open_pos(session)` | GET | `/purchaseorders`, `/purchaseorders/{id}` | same scheduler tick |
| `push_po(session, po)` | POST | `/purchaseorders` | PO create + retry job every 5 min |
| `adjust_stock(item_id, qty, reason)` | POST | `/inventoryadjustments` | Receiving flow |
| `retry_unpushed(session)` | (orchestrator) | n/a | APScheduler every 5 min |
| `_get_access_token` | POST | `/oauth/v2/token` | Internal — token refresh on the local refresh token |
| `_download_item_image` | GET | `/items/{id}/image` | Per-item during sync |
| `_fetch_itemdetails` | GET | `/itemdetails` | Sync enrichment |

PackTrack stores `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`, `ZOHO_REFRESH_TOKEN`,
and `ZOHO_ORG_ID` in `/etc/packtrack/packtrack.env`. **This duplicates what
the gateway already manages.**

---

## 2. What does the gateway expose today?

From `/opt/zoho-integration-service/API_ROUTES.md` and `/openapi.json` on
192.168.1.205:8000:

```
GET   /health, /live, /ready, /status           # health + brand/token state
ANY   /zoho/{service}/{action}                  # generic proxy
ANY   /zoho/{service}/{action}/{resource_id}    # generic proxy with id
POST  /webhooks/zoho/{brand}/{product}          # inbound from Zoho
GET   /webhooks/events                          # event stream
POST  /webhooks/events/{event_id}/ack
```

Documented services in v1.2.1 (39 routes):

- **Zoho CRM**: contacts (create / get / list)
- **Zoho Books**: invoices, purchaseorders (read-only), vendors (read-only),
  expenses (Books + Expense app), chartofaccounts, organizations
- **Milo Payroll** route pack: 74 routes (separate concern)

**Zoho Inventory is NOT yet exposed** by the gateway. The PackTrack-relevant
calls (`/items`, `/itemdetails`, `/inventoryadjustments`,
`/purchaseorders` for *Inventory*, `/items/{id}/image`) need to be added
before PackTrack can fully migrate.

Gateway auth: `X-Brand: <brand_name>` + `X-Internal-Token: <shared secret>`.

Gateway token state today: **boomin_brands tokens are expired**
(`token_status: expired`). Operational hygiene gap.

---

## 3. Per-call migration decisions

| Local call | Migrate? | Reason |
|---|---|---|
| `sync_items` (read items + details + images) | **Yes** — once gateway adds Inventory routes | Read-only. Heavy, scheduled. Gateway can cache + batch better than PackTrack. |
| `sync_open_pos` (read POs) | **Yes** | Read-only. Books PO list already exists in the gateway (different scope, but same pattern). |
| `push_po` (create PO in Zoho Inventory) | **Yes** — but require explicit approval flag | Write. Currently fires automatically on PackTrack PO create. After migration, gateway should accept it but PackTrack should only call it from a button or after explicit owner sign-off, not from background create. |
| `adjust_stock` (post inventory adjustment) | **Yes** — with care | Write. Fires on receive today. Once Luma is the consumption system, the Zoho push is still wanted (financial inventory) but should route through the gateway. |
| `_download_item_image` | **Yes** — gateway can cache + serve | Saves PackTrack the storage and rate-limit grief. |
| OAuth refresh token logic | **Drop locally** | Gateway already handles brand-scoped tokens with encryption. PackTrack should hold no Zoho refresh token. |

### Stay local (none recommended long-term)

If the gateway can't add Inventory routes soon, PackTrack keeps the local
client as a fallback — but everything else moves once it can.

---

## 4. Gateway endpoint format PackTrack will call

Following the gateway's existing convention, the new Inventory routes
should look like:

```
GET   /zoho/items/list                          # paginated items
GET   /zoho/items/get/{item_id}
GET   /zoho/items/image/{item_id}               # binary image proxy
GET   /zoho/itemdetails/list?item_ids=a,b,c
GET   /zoho/inventory_purchaseorders/list?status=open
GET   /zoho/inventory_purchaseorders/get/{id}
POST  /zoho/inventory_purchaseorders/create     # create PO
POST  /zoho/inventoryadjustments/create
```

Body and query params pass through verbatim to Zoho. Gateway adds:

- token scoping by `X-Brand`
- token refresh on 401
- response caching (where safe — items list is the obvious win)
- rate-limit aware backoff
- audit logging per call (for SOX-ish trail, not PackTrack's job to keep)

PackTrack's job is to **call**, not to manage tokens, retries, or 429s.

---

## 5. Downtime handling

If the gateway is unreachable from PackTrack:

1. **Read-side (sync)** — APScheduler logs the failure into `SyncRun` with
   `status='error'`, surfaces it on `/admin/sync`, and tries again next
   tick. No user impact unless the gap exceeds operational tolerance
   (default: amber after 2 missed ticks = 1 hour, red after 4 = 2 hours).
2. **Write-side (push_po)** — `PurchaseOrder.push_status` already supports
   `'failed'` with `push_error` text and a retry job. Gateway downtime
   becomes just another transient error; the retry semantics already work.
3. **Write-side (adjust_stock on receive)** — current code wraps the call
   in try/except and continues on failure (logs to PO event). That stays.
4. **Health gate** — before any write call, PackTrack calls `GET /health`
   on the gateway. If `db_connected: false` or `version` missing, refuse
   the write and surface "Zoho gateway unavailable, try again later"
   instead of partial state.

PackTrack must **never** silently swallow a gateway failure on a write.
Every failed write produces a visible status row.

---

## 6. Avoiding duplicate Zoho credentials

Once migration completes:

- `/etc/packtrack/packtrack.env` drops these keys:
  `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`, `ZOHO_REFRESH_TOKEN`,
  `ZOHO_ORG_ID`, `ZOHO_TOKEN_URL`, `ZOHO_API_BASE`.
- It gains:
  ```
  ZOHO_GATEWAY_URL=http://192.168.1.205:8000
  ZOHO_GATEWAY_TOKEN=<INTERNAL_API_TOKEN from gateway>
  ZOHO_GATEWAY_BRAND=haute_brands
  ```
- `packtrack/zoho.py` becomes a thin gateway client (~150 lines): one
  HTTP client, one auth header pair, calls into the gateway's documented
  endpoints. No OAuth flow. No image circuit breaker. Token refresh is
  the gateway's problem.

If multiple brands share PackTrack later, `ZOHO_GATEWAY_BRAND` becomes a
per-vendor scope value or a per-PO override.

---

## 7. Read-only by default

These calls **must remain read-only** (PackTrack consumes, gateway returns):

- items list / get / image
- itemdetails
- inventory PO list / get (the mirror)
- vendors list / get (when added)

These calls write to Zoho and **require explicit approval before firing**:

- `inventory_purchaseorders/create` — currently auto-fires on PO save in
  PackTrack. **After migration this should either (a) only fire from a
  button, or (b) require the owner to explicitly toggle "send to Zoho"**.
  Not auto.
- `inventoryadjustments/create` — fires on receive. Keep auto for
  receive (it's the natural place), but log it as a `POEvent` of kind
  `zoho_push` so it's auditable. **No fire-and-forget; persist the
  response.**

Once Luma is in the picture (P5+), `inventoryadjustments` becomes the
"Zoho-side" mirror of the Luma push. PackTrack already has `push_status`
plumbing on `PurchaseOrder` — extend the same pattern to receipts.

---

## 8. Writes that require human approval

| Action | Who can fire | UX gate |
|---|---|---|
| Push new PO to Zoho | Owner | Implicit on PO create today; **change to explicit "Send to Zoho" button after P8** |
| Stock adjustment on receive | Receiving / Owner | Auto on receipt confirm, with the PO event log row |
| Push receipt to Luma | Receiving / Owner | **Explicit button** — covered by P4 (dry-run) and P5 (live, manual) |
| Reorder recommendations from Luma | Owner | **Read-only first** (P7); no auto-PO creation |

The boundary: **read freely; write only with intent**.

---

## Open questions to resolve before P8

1. Does the gateway team want PackTrack to add the Zoho Inventory route
   pack itself, or do they own that addition?
2. Is `haute_brands` the correct `X-Brand` for PackTrack's packaging
   procurement, or is there a separate brand for procurement?
3. The gateway currently shows expired tokens. Who refreshes them
   operationally? Does PackTrack need to surface gateway token health
   on its admin page, or does the gateway's own `/status` cover it?
4. Multi-org: PackTrack today assumes a single `ZOHO_ORG_ID`. The
   gateway is multi-brand. Single PackTrack instance per brand, or one
   PackTrack with a brand selector? (Recommend: one PackTrack per brand
   for now, matches the LXC isolation pattern already in use.)
