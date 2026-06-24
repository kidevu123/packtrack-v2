# PackTrack ↔ Luma — Wire Contract

> Companion to [`PACKTRACK_LUMA_BOUNDARY.md`](./PACKTRACK_LUMA_BOUNDARY.md) (ownership / responsibility model) and [`PACKTRACK_API_SURFACE.md`](./PACKTRACK_API_SURFACE.md) (planned read APIs). This doc is the **as-built** wire contract — exact headers, fields, statuses, and gotchas of what PackTrack v2.4.0 actually sends and receives.

Reverse-engineered from `packtrack/services/receiving.py`, `packtrack/services/consumption.py`, `packtrack/services/forecast.py`, `packtrack/routes/internal.py`, and `packtrack/services/box_receipt.py`. Validated by `tests/test_luma_registration.py`, `tests/test_luma_receipt_push.py`, `tests/test_consumption.py`, `tests/test_luma_consumption_edges.py`.

## 1. Env vars

| Name | Required for | Notes |
|---|---|---|
| `LUMA_RECEIPT_WEBHOOK_URL` | every PackTrack → Luma write | The receipts URL, e.g. `https://luma.../api/integrations/packtrack/receipts`. Items URL is derived as `<rsplit('/', 1)[0]>/items` — fragile, see § 7. |
| `LUMA_PACKTRACK_SECRET` | every PT → Luma write AND Luma → PT inbound | Single shared secret. PackTrack sends it as `x-packtrack-secret` (receipts/items) and `X-Luma-PackTrack-Secret` (BOM fetch). Luma → PT sends it as `x-luma-packtrack-secret`. **Three different header names for the same secret** — see § 7-G1. |
| `LUMA_URL` | forecast BOM read | Base URL only (e.g. `http://192.168.1.134:3000`). Used to construct `<LUMA_URL>/api/internal/product-packaging-specs`. |

`/healthz` does **not** currently expose a `luma_configured` flag — gap **P1-1**.

## 2. PackTrack → Luma

### 2.1 Register packaging material

| | |
|---|---|
| Endpoint | `POST {LUMA_RECEIPT_WEBHOOK_URL.rsplit('/', 1)[0]}/items` |
| Auth | header `x-packtrack-secret: ${LUMA_PACKTRACK_SECRET}` |
| Code path | `packtrack/services/receiving.py::register_item_with_luma` |
| Body | `{"material_code": str, "material_name": str, "kind": <enum>, "unit_of_measure": str, "zoho_item_id"?: str}` |
| `kind` values | `BLISTER_CARD` / `DISPLAY` / `CASE` / `LABEL` / `INSERT` / `BOTTLE` / `CAP` / `INDUCTION_SEAL` / `OTHER` — inferred from item name via `_infer_luma_kind`. |
| Triggers | (a) before every receipt push (`submit_receiving`, `retry_luma_push`); (b) inside `ensure_material_code` when a code is first assigned; (c) post-`sync_items` for freshly-created items; (d) backfill script `scripts/backfill_luma_packaging_material_zoho_ids.py`. |
| Idempotency | Yes — Luma's `/items` upserts on `material_code` (SKU) and updates `zoho_item_id` when it was previously NULL. Conflicting `zoho_item_id` returns 409 → `LumaRegistrationOutcome.CONFLICT` (never silently overwritten). |
| Outcomes | `REGISTERED` (201) · `UPDATED` (200, backfilled zoho_item_id) · `ALREADY_MAPPED` (200) · `CONFLICT` (409) · `SKIPPED_NO_CONFIG` · `SKIPPED_NO_MATERIAL_CODE` · `FAILED` (4xx/5xx/network). |
| Persistence | None directly. The receipt push that follows persists `box.luma_push_status`. |

### 2.2 Push purchase receipt

| | |
|---|---|
| Endpoint | `POST ${LUMA_RECEIPT_WEBHOOK_URL}` |
| Auth | header `x-packtrack-secret: ${LUMA_PACKTRACK_SECRET}` |
| Optional header | `x-packtrack-dry-run: true` to skip Luma's write side |
| Code path | `packtrack/services/receiving.py::push_luma_receipt` |
| Body | `source_system="PACKTRACK"`, `packtrack_po_id`, `packtrack_receipt_id` (UUID4), `material_code`, `material_name`, `supplier`, `supplier_lot_number`, `box_number`, `declared_quantity` (int), `counted_quantity` (int\|null), `unit_of_measure`, `received_at` (ISO + Z), `received_by`, `payload.photo_urls?` |
| Triggers | (a) per-line during `submit_receiving`; (b) per-box during `retry_luma_push`. |
| Pre-condition | `BoxReceipt.luma_push_status` must not be `NOT_READY` (computed at receive time — `NOT_READY` iff `material_code` blank). |
| Idempotency at Luma | Luma's contract defines this — PackTrack does **not** assume server-side dedup. PackTrack-side dedup happens **only in the retry path** (`luma_push_status=DUPLICATE` for older same-item rows). Double-submitting the same form generates fresh `packtrack_receipt_id` UUIDs → potential double-push — see gap **P0-1**. |
| Success → persistence | `luma_push_status = PUSHED`, `luma_pushed_at = now`, `luma_response = <body>` |
| Failure → persistence | `luma_push_status = FAILED`, `luma_response = {"error": <reason>}`. Receipt is **never** lost in PackTrack regardless of Luma outcome. |
| Operator retry | `POST /receive/{zoho_po_id}/retry-luma` — re-pushes all `FAILED|NOT_READY` boxes on that PO, oldest-per-item collapsed to DUPLICATE. **No global retry, no scheduled retry** — see **P1-2**. |

### 2.3 Fetch product BOM (forecast)

| | |
|---|---|
| Endpoint | `GET ${LUMA_URL}/api/internal/product-packaging-specs` |
| Auth | header `X-Luma-PackTrack-Secret: ${LUMA_PACKTRACK_SECRET}` (mixed-case — different name from the receipt headers; see **G1**) |
| Code path | `packtrack/services/forecast.py::_fetch_bom` |
| Response | `[ {"product_sku": str, "components": [{"material_code": str, "qty_per_unit": float}, …]} ]` |
| Cached | Module-level dict, **1 hour TTL**. No cache invalidation. |
| On failure | Returns `{}` (logged). Forecast continues with an empty BOM → all rows fall into "No demand data". |

## 3. Luma → PackTrack

### 3.1 Packaging consumption (finished-lot release)

| | |
|---|---|
| Endpoint | `POST /api/internal/luma-consumption` |
| Auth | header `x-luma-packtrack-secret: ${LUMA_PACKTRACK_SECRET}` — name **differs** from outbound headers; see **G1** |
| Code path | `packtrack/routes/internal.py::luma_consumption` → `packtrack/services/consumption.py::process_luma_consumption` |
| Required fields | `finished_lot_id` · `consumed_materials` (list) · `released_at` |
| Optional fields | `finished_lot_number` · per-material `supplier_lot_number` · per-material `packaging_lot_id` |
| Per-material fields | `material_code` (required — see **P0-3**) · `qty_consumed` (float — negative values increment stock; see **P0-2**) |
| Status codes | `200` success · `400` missing required top-level fields · `400` invalid JSON · `401` bad/missing secret · `500` on `KeyError` from missing per-material `material_code` (P0-3) |
| Idempotency | UNIQUE constraint `(finished_lot_id, item_id)` on `material_consumption_events` — replays are safe (returns `status: "already_processed"`). |
| Persistence | One `MaterialConsumptionEvent` per (finished_lot, material). `Item.current_stock` decremented (floored at 0). `Item.daily_usage_rate` recomputed from rolling 30-day sum. |
| Threshold alerts | When `current_stock` crosses below `reorder_point` or `critical_point` (strictly: was-above → now-at-or-below), fires `notify_stock_alert` for Telegram. |
| Per-material outcomes | `updated` · `already_processed` (idempotent replay) · `skipped_not_found` (unknown `material_code`) |

## 4. Persistence model

| Table | Owned-by | Purpose |
|---|---|---|
| `box_receipts.luma_push_status` | PackTrack | enum `pending / not_ready / dry_run_ok / pushed / failed / duplicate` |
| `box_receipts.luma_pushed_at` | PackTrack | UTC timestamp of successful push |
| `box_receipts.luma_response` | PackTrack | JSON blob of Luma's reply (success or error body) |
| `material_consumption_events` | PackTrack | Append-only audit log of Luma consumption events; UNIQUE(finished_lot_id, item_id) |

## 5. Operator visibility today

| Surface | What it shows |
|---|---|
| Receiving form (`/receive/{po}`) | "Luma connected" badge derived from env presence. "N items waiting to sync to Luma" alert when failed/not-ready exist on that PO. "Sync to Luma →" button → retry-luma route. |
| Receiving result page | Per-line `luma_ok` flag + last `luma_err` message. |
| `/healthz` | **No Luma flag.** Gap **P1-1**. |
| Logs | `packtrack.receiving` logs every Luma push, every error. `packtrack.consumption` logs every consume event with prev → new. Both go to `journalctl -u packtrack.service`. Secrets are **not** logged. |
| Admin retry | Per-PO only. **No global "retry all failed Luma pushes"** — gap **P1-2**. |

## 6. Retry behaviour

* **Outbound receipt push:** automatic ONCE during the receive form submit; thereafter manual via `POST /receive/{po}/retry-luma`. Retry is per-PO; selects all `FAILED|NOT_READY` rows; collapses to one row per item (newest wins; rest → `DUPLICATE`).
* **Outbound registration:** fire-and-forget; failures logged but never retried automatically. Backfill script (`scripts/backfill_luma_packaging_material_zoho_ids.py`) is the manual catch-up.
* **Inbound consumption:** Luma's responsibility (PackTrack is idempotent so safe to retry).

## 7. Known gaps

| ID | Severity | Description |
|---|---|---|
| **P0-1** | data integrity | Double-submitting the receiving form creates two distinct `packtrack_receipt_id` UUIDs → two distinct Luma pushes. Luma may or may not dedup; PackTrack assumes nothing. No double-submit guard in `submit_receiving`. |
| **P0-2** | data integrity | `process_luma_consumption` accepts negative `qty_consumed` and INCREMENTS stock (`max(0, prev - (-x)) = prev + x`). Documented by `tests/test_luma_consumption_edges.py::test_negative_qty_currently_increments_stock_BUG`. Need product decision on the correct semantic (reject / clamp / treat as correction event with separate sign-aware ledger). |
| **P0-3** | crash on malformed | `consumed_materials[*].material_code` is accessed without a guard; a missing key raises `KeyError` → 500. Should return `skipped_invalid` and continue the batch. |
| **G1** | inconsistency | Three different header names for the same shared secret: outbound `x-packtrack-secret` (receipts/items), outbound `X-Luma-PackTrack-Secret` (BOM fetch), inbound `x-luma-packtrack-secret` (consumption). HTTP header names are case-insensitive, but the *content* names differ. Pick one canonical name on each direction. |
| **P1-1** | observability | `/healthz` has no `luma_configured` flag — operators can't see at a glance whether the integration is wired up. Easy add: a `settings.luma_configured` property + one extra healthz field. |
| **P1-2** | observability | No global retry path — operators must visit each affected PO. A cron / admin page that finds all `FAILED|NOT_READY` boxes across all POs and re-attempts would close this. |
| **P2-1** | maintainability | Items endpoint URL derived as `LUMA_RECEIPT_WEBHOOK_URL.rsplit('/', 1)[0] + "/items"` — brittle. Better: a dedicated `LUMA_ITEMS_URL` env or compose from `LUMA_URL`. |
| **P2-2** | maintainability | BOM cache has no invalidation — a new product on the Luma side takes up to 1 hour to surface. Adequate for now; revisit when forecast cadence tightens. |
| **P2-3** | test coverage | No test exercises the **submit_receiving** route end-to-end against a mocked Luma — only the helpers are unit-tested. Adding it requires the FastAPI app spin-up with a SQLite stand-in (the consumption tests already do this; receive needs Postgres JSONB workaround). |
| **P3** | docs | This contract was implicit until now; the surface has grown faster than docs. |

## 8. Operator troubleshooting

**Symptom: "Luma not connected" banner** → `LUMA_RECEIPT_WEBHOOK_URL` or `LUMA_PACKTRACK_SECRET` is empty in `/etc/packtrack/packtrack.env`. Check, then `systemctl restart packtrack`.

**Symptom: receipt recorded but `luma_push_status=FAILED`** → click "Sync to Luma →" on the PO's receiving page. Look at the result for the exact Luma error. Common ones: `MAPPING_MISSING` (Luma doesn't know this `material_code` — register fired but Luma rejected; check `register_item_with_luma`'s outcome in `journalctl`) or `401` (secret rotated on one side without the other).

**Symptom: Luma sees a different stock number than PackTrack** → look in `material_consumption_events` for the most recent rows. If Luma pushed twice with different `finished_lot_id`s for the same physical run, both will land (UNIQUE is per finished_lot, not per logical event).

**Symptom: forecast shows "No demand data" for everything** → `_fetch_bom` failed. Tail `journalctl -u packtrack.service | grep "Failed to fetch BOM"` for the URL and HTTP code. Restart packtrack to clear the 1-hour cache.

**Symptom: a registered material has the wrong `zoho_item_id`** → the Luma side returned 409 `CONFLICT`. Resolve manually on the Luma side (no auto-overwrite, by design).
