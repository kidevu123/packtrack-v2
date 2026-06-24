# PackTrack API Surface — Luma Integration

This document defines the planned PackTrack API surface for Luma.
**No code in this document is implemented yet.** The endpoints below
are the Phase 1 (read) and Phase 3/5 (write) targets. Implementation
order is: read APIs first (Phase 1), then Luma-initiated material
receipt (Phase 3), then consumption events (Phase 5).

See [`PACKTRACK_LUMA_BOUNDARY.md`](./PACKTRACK_LUMA_BOUNDARY.md) for
ownership context and [`PACKTRACK_CONFIDENCE_MODEL.md`](./PACKTRACK_CONFIDENCE_MODEL.md)
for the four-axis confidence model these endpoints expose.

---

## Conventions

- **Base path:** `/api/luma/` for all integration endpoints.
- **Auth:** `x-packtrack-secret: <LUMA_PACKTRACK_SECRET>` header on
  every request. Reuses the same secret PackTrack already uses for
  its outbound push to Luma. Phase 1 introduces a
  `require_service_token` FastAPI dependency in
  `packtrack/services/api_auth.py` (planned, not built).
- **Transport:** HTTPS in production; LAN HTTP allowed in dev only.
- **Content type:** `application/json` for all bodies.
- **Times:** ISO-8601 with timezone. PackTrack stores UTC; clients
  may send any zone.
- **Pagination:** cursor-based `?cursor=...&limit=...` on list
  endpoints. Default limit 200, max 1000.
- **Errors:** standard shape
  `{ "error": "CODE", "message": "...", "details": {...} }`.
  Error codes used:
  - `UNAUTHORIZED` (HTTP 401)
  - `FORBIDDEN` (HTTP 403)
  - `NOT_FOUND` (HTTP 404)
  - `BAD_REQUEST` (HTTP 400)
  - `MAPPING_MISSING` (HTTP 422) — `material_code` unknown to PackTrack
  - `IDEMPOTENCY_HIT` (HTTP 200) — already processed, returns cached
    result
  - `RATE_LIMITED` (HTTP 429)
  - `INTERNAL` (HTTP 500)

---

## Read endpoints (Phase 1)

### GET /api/luma/items

**Purpose:** list PackTrack items so Luma can refresh its
`packaging_materials` cache.

**Owner:** PackTrack.

**Auth:** `x-packtrack-secret`.

**Idempotency:** pure read; safe to repeat. Clients should send
`If-None-Match` once ETags are added (post-Phase 1).

**Query fields:**

- `cursor` — opaque pagination cursor; omit for first page.
- `limit` — 1..1000, default 200.
- `item_class` — filter to `PACKAGING_ITEM` or `MATERIAL`. Column is
  added in Phase 1; until then this filter is a no-op.
- `material_code` — exact-match filter for a single item.
- `updated_since` — ISO-8601; return items with any field changed at
  or after this time (used for incremental pulls).

**Response fields (per item):**

- `material_code` — shared identity.
- `item_class` — `PACKAGING_ITEM | MATERIAL` (post-Phase 1).
- `name`
- `unit_of_measure`
- `current_stock` — authoritative on-hand (Phase 5+ accurate;
  pre-Phase 5 reads from `Item.current_stock` which has known drift —
  see boundary doc § 9).
- `reorder_point`
- `critical_point`
- `daily_usage_rate`
- `last_receipt_at` — most recent receipt timestamp for this item.
- `last_validated_at` — most recent receipt validation transition
  (Phase 3+).
- `updated_at`

**Reads/writes (planned):** reads `items`. Writes nothing.

**Must not:** expose Zoho ids, internal `id`, or session/user data.
Must not block on a Zoho sync.

---

### GET /api/luma/items/{material_code}

**Purpose:** fetch a single item's detail, including a small receipt
validation rollup.

**Owner:** PackTrack.

**Auth:** `x-packtrack-secret`.

**Idempotency:** pure read.

**Path fields:**

- `material_code` — required.

**Response fields:** all fields from `GET /api/luma/items` plus:

- `validation_rollup` — small object summarizing per-status receipt
  counts for this item, e.g.
  `{ "UNVALIDATED": 3, "PARTIALLY_VALIDATED": 1, "VALIDATED": 12,
     "DISPUTED": 0, "OVER_CONSUMED": 0 }`.

**Reads/writes (planned):** reads `items` + `box_receipts`. Writes
nothing.

**Must not:** include the full receipt list; use the receipts
endpoint for that.

---

### GET /api/luma/receipts

**Purpose:** receipt history for Luma forecasting and reconciliation.

**Owner:** PackTrack.

**Auth:** `x-packtrack-secret`.

**Idempotency:** pure read.

**Query fields:**

- `cursor`, `limit` — as above.
- `since` — ISO-8601 lower bound on `received_at`. Required.
- `until` — ISO-8601 upper bound; defaults to now.
- `material_code` — optional filter.
- `validation_status` — optional filter, one of the
  `receipt_validation_status` values.

**Response fields (per receipt):**

- `packtrack_receipt_id`
- `material_code`
- `material_name` — snapshot at receive time.
- `supplier`
- `supplier_lot_number`
- `box_number`
- `declared_quantity`
- `counted_quantity`
- `accepted_quantity`
- `unit_of_measure`
- `received_at`
- `received_by`
- `receipt_source` — Phase 3+ field; pre-Phase 3 reads derive from
  the legacy `confidence` enum (`HIGH → COUNTED_AT_RECEIPT`,
  `MEDIUM → SUPPLIER_DECLARED`).
- `receipt_validation_status` — Phase 3+ field; defaults
  `UNVALIDATED` pre-Phase 5.
- `packtrack_po_id`
- `source_system` — `PACKTRACK` for receipts created in PackTrack;
  `LUMA` for receipts created via `POST /api/luma/material-receipts`.

**Reads/writes (planned):** reads `box_receipts`. Writes nothing.

**Must not:** include photo binary data; surface photo URLs only.

---

### GET /api/luma/stock-summary

**Purpose:** compact snapshot for Luma's inventory page render and
the JIT pre-finalize check.

**Owner:** PackTrack.

**Auth:** `x-packtrack-secret`.

**Idempotency:** pure read; clients should call this on page load
and JIT.

**Query fields:**

- `material_codes` — optional comma-separated filter. When omitted,
  returns all items with non-zero stock or non-null reorder_point.

**Response fields:**

- `as_of` — ISO-8601 timestamp.
- `items` — array of
  `{ material_code, item_class, name, current_stock,
     reorder_point, critical_point, receipt_validation_summary,
     last_receipt_at }`.

**Reads/writes (planned):** reads `items` (+ Phase 5: `stock_movement`).
Writes nothing.

**Must not:** be expensive enough to block a Luma page load.
Implementation should cache for 30–60 seconds in-process.

---

## Write endpoints (Phase 3 and Phase 5)

### POST /api/luma/material-receipts  (Phase 3)

**Purpose:** Luma-initiated receipt of a **generic material**
(materials only — never packaging items).

**Owner:** PackTrack.

**Auth:** `x-packtrack-secret`.

**Idempotency:** required. Client supplies `idempotency_key`
(string, ≤120 chars). Duplicate replays return the original
result with HTTP 200 + `IDEMPOTENCY_HIT` body code.

**Request body:**

- `idempotency_key` (required) — caller-generated unique id.
- `material_code` (required) — must resolve to an existing PackTrack
  item with `item_class = MATERIAL`. Otherwise `MAPPING_MISSING`.
- `quantity` (required, > 0) — counted quantity.
- `unit_of_measure` (optional) — defaults to the item's UoM.
- `supplier_lot_number` (optional).
- `box_number` (optional) — supplier carton id if known; defaults to
  a generated `LUMA-RCPT-<idempotency_key_short>` value.
- `received_at` (required) — ISO-8601.
- `received_by` (required) — human-readable Luma user identifier.
- `receipt_source` (optional) — defaults to `COUNTED_AT_RECEIPT`;
  may be `IMPORTED` or `MANUAL_ADJUSTED` if explicit.
- `notes` (optional).

**Response fields:**

- `packtrack_receipt_id` — caller caches this.
- `accepted_quantity`
- `receipt_source`
- `receipt_validation_status` — always `UNVALIDATED` at creation.
- `created_at`
- `source_system` — always `LUMA` for this endpoint.

**Reads/writes (planned):** writes one `box_receipts` row + one
`stock_movement` row of kind `RECEIPT` (Phase 5). Reads `items` for
class check.

**Must not:**

- Accept payloads whose `material_code` resolves to a
  `PACKAGING_ITEM`. Reject with `BAD_REQUEST`.
- Push to Luma in response (this endpoint is inbound only).
- Trust client-supplied `packtrack_receipt_id`; PackTrack issues it.
- Update Zoho stock as a side effect (deferred decision).

---

### POST /api/luma/consumption-events  (Phase 5)

**Purpose:** post-finalization production consumption evidence from
Luma. Drives ledger decrements and `receipt_validation_status`
transitions.

**Owner:** PackTrack.

**Auth:** `x-packtrack-secret`.

**Idempotency:** required. Client supplies `idempotency_key`
(string, ≤120 chars) per event. Duplicate replays return original
result with HTTP 200 + `IDEMPOTENCY_HIT` body code.

**Request body:**

- `idempotency_key` (required).
- `event_time` (required) — ISO-8601; when production finalized.
- `finished_lot_reference` (optional) — Luma's opaque identifier for
  audit linkage. PackTrack stores but does not interpret.
- `production_confidence` (optional) — Luma's per-event
  `HIGH | MEDIUM | LOW`. Stored as metadata; does not change
  PackTrack's `receipt_validation_status` logic.
- `items` (required, non-empty array) — each:
  - `material_code` (required).
  - `consumed_quantity` (required, ≥ 0).
  - `damaged_quantity` (optional, ≥ 0).
  - `discarded_quantity` (optional, ≥ 0).
  - `returned_quantity` (optional, ≥ 0).
  - `unit_of_measure` (optional) — defaults to the item's UoM.
  - `notes` (optional).

**Response fields:**

- `accepted_at` — ISO-8601.
- `items` — per-input item, with:
  - `material_code`
  - `ledger_entry_id`
  - `new_current_stock`
  - `affected_receipts` — array of
    `{ packtrack_receipt_id, prior_status, new_status,
       evidence_quantity }`.

**Reads/writes (planned):** writes N `stock_movement` rows (Phase 5)
of kind `CONSUMPTION`; updates `items.current_stock`; updates
`box_receipts.receipt_validation_status` via
oldest-non-fully-validated-first rule.

**Must not:**

- Be required for production to finalize. Luma must be able to
  retry; PackTrack must accept stale events.
- Push anything back to Luma. Response is the only feedback.
- Update Zoho stock as a side effect.
- Mutate the `receipt_source` of the affected receipts.

---

## Endpoints intentionally NOT in scope

For clarity:

- **No** PackTrack endpoint for Luma to write to packaging items
  (only materials). Receiving printed/product-specific packaging
  stays in the PackTrack UI.
- **No** PackTrack endpoint for Luma to mutate
  `production_confidence`, `forecast_confidence`, or any Luma-owned
  field.
- **No** PackTrack endpoint for Luma to upload finished-lot data,
  genealogy, or workflow events. Those are Luma-owned (see boundary
  doc § 8).
- **No** PackTrack-side "shortage recommendation receiver" in
  Phase 0–6. The Luma → PackTrack shortage recommendations idea
  (previous P7) is deferred indefinitely; Luma's existing client for
  that endpoint is currently dormant.

---

## Versioning

The `/api/luma/` surface is unversioned in v1. Breaking changes
require a `/api/luma/v2/` path; additive fields do not. Until Phase 1
ships, this surface only exists in this document.
