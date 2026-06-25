# Current Phase Status

## v2.4.2 — Inventory pagination + lazy thumbnails (active on main)

| | |
|---|---|
| **Active version on main** | `2.4.2` |
| **Last deployed version** | `2.4.1` (production tagged `v2.4.1` at `a0ba7a6`) |
| **Public URL** | `https://packtrack.booute.duckdns.org` |
| **Deploy path** | `deploy/deploy.sh` only — see [`RUNBOOK_DEPLOY.md`](./RUNBOOK_DEPLOY.md). Ad-hoc `pct push` + `rsync --delete` is forbidden (caused the v2.2.0 unstyled-UI incident). |
| **Healthz axes (expected)** | `gateway_configured=true`, `zoho_integration_configured=true`, `legacy_zoho_configured=false`, `zoho_configured=false`, `telegram_configured=false` |
| **SSO** | Public `/auth/sso` redirect derives Authentik base from `OIDC_ISSUER_URL` — no LAN-IP leaks. State + nonce TTL 1800 s. Browser login round-trip verified by operator 2026-06-24. |
| **CSS smoke** | `scripts/smoke_test.sh` passes; deploy gate asserts size + sentinels. |
| **Alembic head** | `f4a5b6c7d8e9` (`forecast_alert_sent_stock`) — unchanged. |

**v2.4.2 scope:** server-side pagination on `/inventory` (default 50 items per page, `?page=N`), filter-aware Prev/Next links, `loading="lazy"` on item thumbnails. Fixes browser `ERR_HTTP2_PROTOCOL_ERROR` symptom caused by NPMplus/HTTP-2 upstream truncation at the proxy buffer boundary on the previous ~740 KB single-page inventory response. Reduces a default `/inventory` response to under 200 KB. No schema change. Receiving / Luma / Zoho unchanged.

**v2.4.1 scope:** the three P0 Luma findings from the v2.4.0 audit —
* **P0-1 fixed (schema-backed):** receiving form requires a hidden `submission_id` token. POST handler short-circuits when the same token already produced BoxReceipts on this PO. Migration `3c8a2b1e9d40` adds `submission_id` + `submission_line_index` columns and a partial UNIQUE index on `(purchase_order_id, submission_id, submission_line_index) WHERE submission_id IS NOT NULL` — the durable dedup backstop. **`box_number` is no longer the idempotency key.** Receive-form rows write `box_number = "PT-{packtrack_receipt_id}"` only because Luma still requires a non-empty value (`z.string().min(1)`).
* **P0-2 fixed:** `process_luma_consumption` rejects negative `qty_consumed` as `skipped_invalid`. Stock unchanged, no audit row written.
* **P0-3 fixed:** per-entry missing `material_code` is now `skipped_invalid` with a reason; the batch continues processing the remaining valid entries.
* docs/PACKTRACK_LUMA_CONTRACT.md updated; § 8 documents `box_number` semantics per-flow and § 9 captures the future coordinated Luma cleanup.

**Previously shipped (v2.4.0):** UI polish — `_partials/ui.html` macro library, inventory page widened + clearer per-row hierarchy, forecast page collapsed to one shared row macro with clickable summary anchors + collapsible "No demand data" section, home "Needs you" reorder items grouped into one card with View All link.

**Previously shipped (v2.3.0):** reconciliation of the v2.2.0 Zoho integration receive path with main's Phase A/C/D inventory + forecasting + UI overhaul. Tagged `v2.3.0` at commit `a582f38`.

**Open items:**
- Browser visual review of `/`, `/po`, `/inventory`, `/inventory/forecast`, `/receive` after v2.4.0 deploy.
- Optional `v2.4.0` git tag — held for after human visual review.
- Local-only backup branches `backup/local-{main,feature-zoho-receives}-pre-reconcile-2026-06-24` retained for now; user to decide when to delete.
- `.claude/` (per-user Claude Code settings) intentionally left untracked.

---

## Phase status (architecture roadmap)

**Active phase:** Phase 0 — Docs/boundary correction for the
Luma ↔ PackTrack separation (in progress).
**Most recent completed phase:** P1.5 — Real Zoho item catalog sync +
material_code audit gate.
**Next planned phase:** Phase 1 — PackTrack authoritative read APIs
for Luma (see [`PACKTRACK_BUILD_QUEUE.md`](./PACKTRACK_BUILD_QUEUE.md)).

> The original P2/P3/P4/P5 box-receipt push pipeline already shipped
> as production code (`box_receipts` schema in migration
> `b7c2d8e1f4a9_box_receipts.py`; push code in
> `packtrack/services/receiving.py::push_luma_receipt`). It is now
> classified as **legacy current behavior**, retained for back-compat,
> **not** the future architecture. See
> [`PACKTRACK_LUMA_BOUNDARY.md`](./PACKTRACK_LUMA_BOUNDARY.md) §§ 5
> and 10 for the supersession details.

---

## Infrastructure summary

| Layer | Detail |
|---|---|
| Host | Proxmox `pve` @ 192.168.1.190 |
| Container | LXC 200 `packtrack` @ 192.168.1.206 (Debian 13 unprivileged, 2c / 2 GB / 16 GB) |
| Service supervisor | systemd — `packtrack.service` (uvicorn) + `packtrack-backup.timer` |
| Reverse proxy | Caddy on port 80 (LAN-only today; ACME-ready) |
| Database | PostgreSQL 17.9, db `packtrack`, role `packtrack` |
| Backups | Nightly `pg_dump` 03:00 UTC, gzip, 7-day retention at `/var/backups/packtrack/` |
| Secrets | `/etc/packtrack/packtrack.env` (mode 640, root:packtrack) |
| Deploy | `bash deploy/deploy.sh` from Mac → rsync → CSS build → alembic upgrade → restart |
| Companion service | LXC 9503 `zoho-integration-service` @ 192.168.1.205:8000 (FastAPI gateway, multi-brand) |

---

## Stack summary

- Python 3.13.5 · FastAPI · SQLModel · Alembic
- Jinja2 · htmx · Alpine.js · Tailwind v4 (compiled on the LXC at deploy time)
- APScheduler in-process: Zoho sync every 30 min, push-retry every 5 min
- httpx for outbound HTTP
- argon2id passwords + signed-cookie sessions (itsdangerous)
- Telegram bot (webhook handler in `routes/telegram_webhook.py`)
- No tests yet (pytest declared in dev deps; `tests/` directory does not exist).

---

## Domain model snapshot

Tables (10):
`users`, `items`, `purchase_orders`, `po_lines`, `po_events`, `attachments`,
`shipments`, `zoho_mirror`, `sync_runs`, `app_settings`.

Migrations (3):
- `e7e893cfe315_initial_schema.py` — tables created
- `58274d5910dc_prices_currency_images_lastcost.py` — `po_lines.unit_price`,
  `purchase_orders.currency`, `items.image_path`, `items.last_unit_cost`
- `a3f1b2c4d5e6_item_material_code.py` — `items.material_code` (nullable)
  with a partial unique index `WHERE material_code IS NOT NULL`

Notable today:

- `Item` has `zoho_item_id` (unique), `sku_code` (indexed, **not unique**),
  `material_code` (nullable, partial-unique among populated values, added P1),
  `name`, `vendor`, `unit`, `current_stock`, `reorder_point`,
  `critical_point`, `daily_usage_rate`, `last_unit_cost`.
- `Shipment` has `quantity`, `received_quantity`, `discrepancy_notes`. **No
  box-level granularity, no lot, no per-box confidence.**
- `Attachment.kind` is `pi | artwork | other`. No receipt files yet.
- `POEvent.kind` includes `status_change`, `comment`, `attachment`,
  `received`, `sync`. Adding `luma_push` and `zoho_push` for P5/P8 is
  trivial — it's a string column.

---

## Current PackTrack ↔ Luma boundary state

### Today (live in production)

The code today implements the original push-only integration. It is
**legacy current behavior**, kept running while the new pull-first
architecture is built.

- PackTrack records each supplier box as a `BoxReceipt` row
  (`packtrack/models.py`, migration
  `b7c2d8e1f4a9_box_receipts.py`).
- The legacy shipment-level receive route
  (`packtrack/routes/purchase_orders.py::receive_shipment`) still
  exists and still calls `zoho.adjust_stock(item_id, qty)` directly
  and increments `Item.current_stock`. The new box-receipt path does
  **not** touch `Item.current_stock`.
- On submit, the receiving route
  (`packtrack/routes/receiving.py::submit_receiving`) immediately
  pushes each `BoxReceipt` to Luma's
  `POST /api/integrations/packtrack/receipts` via
  `packtrack/services/receiving.py::push_luma_receipt`, using
  `x-packtrack-secret`. Pre-registration of the material is done via
  `register_material_with_luma`.
- A retry route exists at `POST /receive/{zoho_po_id}/retry-luma` for
  `FAILED` / `NOT_READY` rows; no scheduled Luma reaper runs.
- `BoxReceipt.confidence` is a single enum (`HIGH | MEDIUM`) that
  conflates "how was it sourced?" with "has it been validated?". The
  four-axis confidence model (see
  [`PACKTRACK_CONFIDENCE_MODEL.md`](./PACKTRACK_CONFIDENCE_MODEL.md))
  is **not** yet in code.
- There is no `stock_movement` ledger, no service-token middleware,
  no `item_class`, no Luma consumption receiver, and no Luma pull
  client on the PackTrack side.

### Honest gap list (this is what Phase 0–6 will close)

- **No stock movement ledger.** Phase 5 introduces it.
- **`Item.current_stock` drift.** Partly Zoho-overwritten, partly
  legacy-route incremented, never decremented; not authoritative yet.
- **Two coexisting receive paths** (legacy `receive_shipment` vs new
  box-receipt path). Reconciled in Phase 5/6.
- **Push-oriented receipt integration is the only live path today.**
  Pull APIs and Luma write paths are planned, not built.
- **No Luma consumption receiver.** Phase 5.
- **Single-axis `confidence` enum on `BoxReceipt`.** Phase 3 splits
  it into `receipt_source` + `receipt_validation_status`.
- **No `item_class` axis on `Item`.** Packaging items vs materials
  lives only in the boundary doc until Phase 1.

### Target architecture (Phase 0–6)

PackTrack becomes the authoritative packaging/material inventory
system. Luma becomes the tablet-production authority. Luma pulls from
PackTrack on a schedule and writes back only two narrow flows
(Luma-initiated generic material receipt; production-consumption
events). PackTrack maintains the authoritative ledger and the
receipt-validation status; Luma maintains production confidence and
the user-facing forecast confidence.

See [`PACKTRACK_LUMA_BOUNDARY.md`](./PACKTRACK_LUMA_BOUNDARY.md) for
the full ownership map, [`PACKTRACK_API_SURFACE.md`](./PACKTRACK_API_SURFACE.md)
for the planned endpoints, and
[`PACKTRACK_CONFIDENCE_MODEL.md`](./PACKTRACK_CONFIDENCE_MODEL.md)
for the four-axis confidence model. Phase order lives in
[`PACKTRACK_BUILD_QUEUE.md`](./PACKTRACK_BUILD_QUEUE.md).

---

## Current Zoho integration findings

PackTrack runs its own Zoho client (`packtrack/zoho.py`, ~280 LOC public
surface) hitting `https://www.zohoapis.com/inventory/v1/...` directly. It
holds its own refresh token in `/etc/packtrack/packtrack.env`. **This
duplicates what the gateway service at LXC 9503 already does for other
properties (Books / CRM / Expense / Payroll).**

The gateway today does **not** expose Zoho Inventory routes — it would
need the equivalent of `/zoho/items/list`, `/zoho/itemdetails/list`,
`/zoho/inventoryadjustments/create`, etc. added to its route table
before PackTrack can fully migrate.

Gateway's `boomin_brands` tokens currently show `token_status: expired` —
operational hygiene gap, but not blocking PackTrack today since PackTrack
is using its own creds.

Migration plan in `docs/ZOHO_API_GATEWAY_PLAN.md`. Implementation deferred
to **P8**.

---

## Material identity decision (P1 outcome)

**Chosen strategy:** add a dedicated `Item.material_code` column.

**Why not `zoho_item_id`?** It is stable but opaque (a long Zoho-internal
numeric string). Humans don't read it on POs, the supplier doesn't print it
on box labels, and Luma operators won't recognise it.

**Why not `sku_code` directly?** It is indexed but not unique today. A
Zoho-side rename or duplicate creation would cascade into Luma. Decoupling
keeps the integration identity owner-controlled.

**Backfill rule:** the audit script (`scripts/audit_material_codes.py`)
proposes safe defaults — copy `sku_code` into `material_code` only when
`sku_code` is unique across the active set AND non-empty AND the row's
`material_code` is currently null. Never overwrites. The owner runs the
script with `--apply-safe-defaults` after reviewing the dry-run output.

## P1.5 outcome (live data)

**Sync path chosen:** the existing **Zoho Integration Service** at LXC 9503
(`http://192.168.1.205:8000`). PackTrack's local Zoho creds remain blank
on purpose — no OAuth state in `/etc/packtrack/packtrack.env`. A new
script `scripts/sync_items_via_gateway.py` was added; the existing
`packtrack/zoho.py` was left untouched.

**Gateway routes used:**

- `GET /zoho/items/list` (paginated; `cf_item_type == "Packaging"` filter)
- Auth: `X-Brand: haute_brands` + `X-Internal-Token: <gateway secret>`

The gateway's `/openapi.json` does **not** advertise inventory routes — the
generic `/zoho/{service}/{action}` dispatcher accepts `items` and proxies
through. Tokens reported "expired" via the gateway's `/status`, but the
actual call succeeded (the gateway auto-refreshed under the hood).

**Numbers:**

| Metric | Count |
|---|---:|
| Items pulled from gateway (cf_item_type == Packaging) | 94 |
| Items created in PackTrack | 94 |
| Items with non-empty `sku_code` | 45 |
| Items with blank `sku_code` | 49 |
| Items with `zoho_item_id` populated | 94 |
| Duplicate `sku_code` groups | 0 |
| Safe-default proposals (sku_code → material_code) | 45 |
| Safe defaults applied | 45 |
| Items with `material_code` populated post-backfill | 45 |
| Items still requiring manual material_code | 49 |

The 45 backfilled values are mostly UPC-12 barcodes (`850060…`) plus 2
Uline-style codes (`S-23976`, `S-4717`) — clean, unique, and exactly the
shared identity Luma will key on.

**Manual cleanup required before P2:** the 49 items with blank `sku_code`
(see `docs/P1_5_MANUAL_CLEANUP.md` for the full list). Each needs a
material_code chosen by the owner. Until at least the items the operator
plans to receive on the **first** P5 Luma push are cleaned up, P2's
box-receipt rows can be created but cannot push to Luma — the payload
builder (P3) refuses payloads with missing material_code.

---

## Risk list

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| 1 | `Item.sku_code` is indexed but not unique. After first Zoho sync, P1 audit may discover collisions | Medium | Audit script reports them; safe-default backfill refuses ambiguous rows. Operator resolves manually before P2. |
| 2 | Two coexisting receive paths: legacy `receive_shipment` mutates `Item.current_stock` + Zoho; new box-receipt path does not. `current_stock` drift is real today. | Medium–High | Phase 5 introduces the authoritative `stock_movement` ledger and backfills `Item.current_stock` from receipts. Phase 6 decides the fate of `receive_shipment` (fence off vs remove). |
| 3 | Luma push is an outbound webhook — secret in env, transport is HTTP today. After Phase 2 ships, push is no longer the primary integration but still runs alongside the Luma pull. | Medium | Caddy on the Luma side should terminate TLS; PackTrack should require `https://` in `LUMA_RECEIPT_WEBHOOK_URL` and refuse plain HTTP unless explicitly allowed for LAN. Phase 6 decides whether to demote or remove the push entirely. |
| 4 | No tests exist; the state machine + receiving are the most fragile parts | Medium | P3 introduces the first tests (Luma payload builder). Backfill tests for the state machine before P5. |
| 5 | Legacy shipment-level receiving collapses 100 boxes into one number; still wired up alongside the box-receipt path. | High for Luma | Box-receipt schema is live (migration `b7c2d8e1f4a9_box_receipts.py`). Phase 6 decides whether to fence off or remove the legacy `receive_shipment` route. |
| 6 | Mid-migration window after Phase 3 / before Phase 5: PackTrack accepts Luma-initiated material receipts but Luma still treats its local `packaging_lots.qtyOnHand` as authoritative for some material types → duplicate inventory authority. | High | Phase 2 gates Luma's inventory view switchover behind a Luma feature flag. Phase 5 lands the consumption receiver + ledger before the flag flips for all material classes. Boundary doc § 1 / § 4 codifies the new rule: PackTrack owns the authoritative stock ledger; Luma is a consumer reporting usage back. |
| 7 | Zoho gateway tokens expire and PackTrack only learns when a sync fails | Low | Add gateway `/health` check to PackTrack `/admin/sync` page during P8. |
| 8 | `Shipment.item_id` is nullable; box receipt requires a hard FK to `items` | Resolved | `BoxReceipt.item_id` is non-null in the live schema; addressed when migration `b7c2d8e1f4a9_box_receipts.py` shipped. |
| 9 | Single `confidence` enum on `BoxReceipt` conflates `receipt_source` and `receipt_validation_status`; cannot grow into the four-axis confidence model without a rename + migration. | Medium | Phase 3 splits it: rename `confidence` → `receipt_source` with new enum values, add `receipt_validation_status`. Backfill: `HIGH → COUNTED_AT_RECEIPT`, `MEDIUM → SUPPLIER_DECLARED`. |
| 10 | No service-token middleware exists today; `LUMA_PACKTRACK_SECRET` is outbound-only. Phase 1 reuses the same secret for inbound Luma → PackTrack calls. | Low | Phase 1 adds `packtrack/services/api_auth.py::require_service_token`. Operationally clean; if security review later wants separate inbound/outbound secrets, that is an additive change. |
| 11 | Luma's production tables (`workflow_events`, `batches`, `finished_lots`, `finished_lot_inputs`, `finished_lot_raw_bags`, `finished_lot_packaging_lots`, `packaging_lots.qtyOnHand` writers) are high-risk and must never be written by PackTrack. | High | Boundary doc § 8 codifies this. Every phase here is additive on the Luma side; Phase 4 explicitly builds-only (no finalization-path changes). |

---

## What is **not** to be touched in Phase 0 (per directive)

Phase 0 is docs-only. Do not edit:

- Any `.py` file under `packtrack/`.
- Any Alembic migration or template.
- Any Luma codebase file (even for documentation mirrors — keep
  cross-doc references read-only from PackTrack's side until Luma
  reviews).
- Any deploy script.
- TabletTracker.
- The Luma production / traceability tables listed in
  [`PACKTRACK_LUMA_BOUNDARY.md`](./PACKTRACK_LUMA_BOUNDARY.md) § 8.
  These remain out of scope for the whole Phase 0–6 project, not
  only Phase 0.

What ships in Phase 0: docs + `README.md` + `.env.example` only.

---

## Files created in P0

```
docs/
├── PACKTRACK_BUILD_QUEUE.md
├── CURRENT_PHASE_STATUS.md
├── PACKTRACK_LUMA_BOUNDARY.md
└── ZOHO_API_GATEWAY_PLAN.md
```

P0: no code changed.

## Files created/changed in P1

```
packtrack/models.py                              modified — added Item.material_code
packtrack/services/material_audit.py             new — pure detection helpers
scripts/audit_material_codes.py                  new — CLI audit + safe-default backfill
migrations/versions/a3f1b2c4d5e6_item_material_code.py   new
tests/__init__.py                                new
tests/conftest.py                                new
tests/test_material_audit.py                     new
docs/CURRENT_PHASE_STATUS.md                     updated
docs/PACKTRACK_BUILD_QUEUE.md                    updated
docs/PACKTRACK_LUMA_BOUNDARY.md                  updated
```

No env vars added. No Zoho behaviour changed. No deploy.sh run.

---

## P0 → P1 readiness gate

- [x] Boundary documented
- [x] Gateway plan documented
- [x] Build queue ordered with acceptance criteria
- [x] Current state captured
- [x] Owner / Luma stakeholder has reviewed the boundary doc *(implied by P1 go-ahead)*

## P1 → P2 readiness gate

- [x] `Item.material_code` column added (nullable, partial-unique)
- [x] Alembic migration written + verified
- [x] Audit script in place
- [x] Audit unit tests pass
- [x] First real Zoho sync run via gateway (94 items)
- [x] Audit run on real data; safe defaults applied where unambiguous (45/94)
- [ ] Owner reviews `docs/P1_5_MANUAL_CLEANUP.md` and assigns material_code
      to the items they plan to receive on the first Luma push

P2 (box-level receiving) **may begin schema-wise**, but the first live
Luma push (P5) cannot complete for any item still missing a
`material_code`. Owner can clean items as needed without blocking P2's
schema work.

## Files added/changed in P1.5

```
scripts/sync_items_via_gateway.py        new — gateway-based item ingestion
docs/P1_5_MANUAL_CLEANUP.md              new — 49-item cleanup list
docs/CURRENT_PHASE_STATUS.md             updated
docs/PACKTRACK_BUILD_QUEUE.md            updated
/etc/packtrack/packtrack.env             on LXC: added ZOHO_GATEWAY_URL,
                                         ZOHO_GATEWAY_TOKEN, ZOHO_GATEWAY_BRAND
```

PackTrack's existing `packtrack/zoho.py` was **not modified**. No deploy
ran (no Python or template change needed by the running service — script
is invoked manually from the LXC; gateway env vars are read by the script
only).
