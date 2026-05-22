# Current Phase Status

**Phase:** P1.5 — Real Zoho item catalog sync + material_code audit gate
**Status:** complete — 94 packaging items synced via the Zoho gateway, 45 backfilled with safe defaults, 49 require manual cleanup
**Next unchecked phase:** P2 — Box-level receiving model **(soft-blocked on the 49-item manual cleanup; see ``docs/P1_5_MANUAL_CLEANUP.md``)**

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

**Today (P0, before P2):**

- PackTrack only records receipts at the *shipment* level
  (`Shipment.received_quantity`, single number, no box detail).
- On receive, PackTrack calls `zoho.adjust_stock(item_id, qty)` directly
  against Zoho Inventory. This is an *increment* (financial inventory).
- PackTrack does not yet talk to Luma at all. No env keys, no client, no
  payload builder, no push button.

**Target (after P2–P5):**

- PackTrack records each supplier box as a `BoxReceipt` row.
- On receive, the Zoho `adjust_stock` push continues (financial truth).
- Receiving / owner manually pushes the box receipt to Luma via the
  webhook at `LUMA_RECEIPT_WEBHOOK_URL` with `x-packtrack-secret`. Luma
  owns the production-floor ledger and consumption tracking.
- Idempotency on Luma side: `(packtrack_receipt_id, box_number)`.

See `docs/PACKTRACK_LUMA_BOUNDARY.md` for the full ownership table.

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
| 2 | Today's `receive_shipment` auto-fires `zoho.adjust_stock` — if it ever fails silently, financial inventory drifts | Low–Medium | Wrap exists; failure logs to PO event but UI doesn't surface clearly. P5 will introduce a `zoho_push` event kind alongside `luma_push` to make both auditable. |
| 3 | Luma push is an outbound webhook — secret in env, transport is HTTP today | Medium | Caddy on the Luma side should terminate TLS; PackTrack should require `https://` in `LUMA_RECEIPT_WEBHOOK_URL` and refuse plain HTTP unless explicitly allowed for LAN. |
| 4 | No tests exist; the state machine + receiving are the most fragile parts | Medium | P3 introduces the first tests (Luma payload builder). Backfill tests for the state machine before P5. |
| 5 | Shipment-level receiving (current) collapses 100 boxes into one number | High for Luma | P2 fixes this; until P2 ships, do not push to Luma. |
| 6 | Both PackTrack and Luma posting receipts → potential double-count if either system later treats receipt as "consumption" | High | Boundary doc explicitly forbids it. Reconciliation lives in Luma. |
| 7 | Zoho gateway tokens expire and PackTrack only learns when a sync fails | Low | Add gateway `/health` check to PackTrack `/admin/sync` page during P8. |
| 8 | `Shipment.item_id` is nullable; box receipt requires a hard FK to `items` | Low | P2 enforces non-null on `BoxReceipt.item_id`. |

---

## What is **not** to be touched yet (per directive)

- Luma codebase
- TabletTracker
- PackTrack rewrites
- UI redesign (P9 only)
- Automatic reorders (P7 outputs *recommendations* only)
- Auto-push to Luma (P5 is manual button only)

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
