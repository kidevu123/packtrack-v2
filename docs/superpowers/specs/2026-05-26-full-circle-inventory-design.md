# Full-Circle Inventory Design
**Date:** 2026-05-26  
**Status:** Approved for implementation planning  
**Scope:** PackTrack · Luma · Zoho · Nexus Resolve

---

## Problem

PackTrack knows what was ordered and received. Luma knows what was consumed and produced. Zoho knows what was sold. Nexus agents guess which production batch a complaint belongs to. None of these systems talk back to each other, so:

- Logistics can't see real on-hand inventory without manually counting
- Reorder decisions are gut feel, not data
- "How many bottles do we have?" requires calling three people
- A customer complaint can't be reliably traced to a production batch

---

## Goal

A closed loop where every material move — received, consumed, assembled, sold — is visible in one operational place (PackTrack), enabling the logistics team to make fast, predictable procurement decisions backed by real data.

---

## Architecture

### Data ownership — fixed, never changes

| Data | Owner | Rationale |
|---|---|---|
| `material_code` assignment | PackTrack | Authority on packaging identity |
| PO lifecycle, supplier records | PackTrack | Procurement is PackTrack's job |
| Packaging consumption detail (lot, machine, batch) | Luma | Production floor is Luma's job |
| Finished batch genealogy | Luma | Rich event log stays close to production |
| Business inventory, finished goods counts | Zoho | ERP for accounting and management reporting |
| Sales orders and fulfillment | Zoho | That's what Zoho is built for |
| Customer complaints, quality incidents | Nexus | Support platform is Nexus's job |
| **Operational on-hand balance per material** | **PackTrack** | Fast reads, no third-party dependency for alerts |

### Integration pattern — direct point-to-point webhooks

All inter-system calls are async HTTP POSTs with shared-secret header auth. No message broker. No polling. Failures are logged and retried; they never block the triggering action. This matches every integration already in production.

### The two event triggers

Everything flows from two moments:

```
1. finishedLot → RELEASED in Luma
   → Phase A: tell PackTrack "consumed X units of material Y"
   → Phase B: tell Zoho gateway "assemble Z finished units of product P"
   → Phase E: tell Nexus "batch lot# L for product P is real, here's the metadata"

2. Sales order confirmed in Zoho
   → Phase C: tell PackTrack "sold N units of product P"
   → Phase C: tell Luma "sold N units of product P"
```

### Why not Zoho as the single ledger

Zoho is a SaaS API with rate limits and latency. Real-time reorder alerts can't depend on a third-party API being up. The hybrid approach keeps Zoho accurate for business reporting while PackTrack maintains a lightweight operational balance for fast reads.

---

## Phase A — Packaging consumption reporting (Luma → PackTrack)

### What it does
When Luma releases a finished lot, it reports which packaging materials were consumed and in what quantities. PackTrack updates on-hand stock and fires reorder alerts.

### Trigger
`setFinishedLotStatus()` in Luma, when new status is `RELEASED`. Runs alongside Phase B and Phase E in the same status-change hook.

### New endpoint in PackTrack
`POST /api/internal/luma-consumption`

Auth: `X-Luma-PackTrack-Secret` header (reuses existing `LUMA_PACKTRACK_SECRET` env var — already configured).

**Request payload:**
```json
{
  "source": "LUMA",
  "finished_lot_id": "uuid",
  "finished_lot_number": "FL-2024-001",
  "product_sku": "HN-001",
  "units_produced": 1000,
  "released_at": "2024-01-15T10:30:00Z",
  "consumed_materials": [
    {
      "material_code": "PT-00095",
      "qty_consumed": 1000,
      "packaging_lot_id": "uuid",
      "supplier_lot_number": "SL-2024-001"
    }
  ]
}
```

**PackTrack processing (per material in consumed_materials):**
1. Resolve `material_code` → `Item`; skip with warning if not found
2. Write one `MaterialConsumptionEvent` row (audit log)
3. Decrement `Item.current_stock` by `qty_consumed`
4. Recompute `Item.daily_usage_rate` as 30-day rolling average from `material_consumption_events`
5. After all materials updated: check each against `reorder_point` and `critical_point`
6. For any item newly below threshold: call `notify()` with `stock.reorder_urgent` or `stock.critical` event → Telegram fires + inbox badge

**Idempotency:** keyed on `(finished_lot_id, material_code)` — re-sending the same release is a no-op.

### New table in PackTrack — `material_consumption_events`

| Column | Type | Notes |
|---|---|---|
| id | int PK | auto |
| item_id | FK → Item | |
| qty_consumed | float | |
| finished_lot_id | string | Luma's UUID |
| finished_lot_number | string | human-readable |
| supplier_lot_number | string \| null | for traceability |
| packaging_lot_id | string \| null | Luma's packaging_lot UUID |
| consumed_at | datetime | from Luma's `released_at` |
| received_at | datetime | when PackTrack got this push |

### What changes on existing models
- `Item.current_stock` — auto-maintained (no more manual entry)
- `Item.daily_usage_rate` — auto-maintained from event log rolling average
- `Item.reorder_point` and `Item.critical_point` — still manually set by owner (thresholds are business decisions)

### What this unlocks
Dashboard stock alerts and inbox items become accurate. Logistics team can trust the number because the `material_consumption_events` log shows exactly which batch consumed what and when.

---

## Phase B — Finished goods assembly (Luma → Zoho)

### What it does
When Luma releases a finished lot, it creates a Zoho Manufacture Order — converting packaging material stock into finished goods in Zoho's inventory. Without this, Zoho's raw material counts are stale after every production run.

### Trigger
Same `setFinishedLotStatus()` RELEASED hook as Phase A. Both fire in sequence.

### Part 1 — New gateway route (LXC 9503, zoho-integration-service)
`POST /zoho/manufacturing_orders/create`

The gateway owns all Zoho OAuth credentials. Luma never holds tokens. This mirrors the existing purchase-receives route pattern.

```json
{
  "composite_item_id": "zoho-composite-item-id",
  "quantity_to_manufacture": 1000,
  "manufacture_date": "2024-01-15",
  "bill_of_materials": [
    { "item_id": "zoho-item-id", "quantity": 1000 }
  ]
}
```

### Part 2 — New Luma integration file
`lib/integrations/zoho/manufacturing.ts` — mirrors the structure of `lib/integrations/nexus/finished-lots.ts`:
- `validateManufacturingConfig()` — checks `ZOHO_INTEGRATION_URL`, `ZOHO_INTEGRATION_SECRET`, `ZOHO_BRAND`
- `buildManufactureOrderPayload()` — derives from `finishedLot` row: units produced, product's `zohoItemId`, BOM from `product_packaging_specs` joined with `packaging_materials.zohoItemId`
- `sendManufactureOrderToZoho()` — POST to gateway, 20s timeout
- On success: write `finished_lots.zoho_manufacture_order_id`
- On failure: write `finished_lots.zoho_manufacture_error` — does **not** block lot release

### Pre-condition to verify before implementing
`packaging_materials.zohoItemId` must be populated for every BOM component. Verify during implementation; raise a warning and skip non-blocking if any are missing.

### What this unlocks
Zoho inventory is accurate. Finished goods count is real. Phase C (sales webhook) and Phase D (forecasting) build on accurate Zoho data.

---

## Phase C — Sales feedback (Zoho → PackTrack + Luma)

### What it does
When Zoho confirms a sale, both PackTrack and Luma are notified. PackTrack updates its demand signal. Luma records which finished lot was sold (closing the genealogy loop).

### Trigger
Zoho webhook on sales order confirmation / invoice creation. Configured once in Zoho → Settings → Automation → Webhooks. Points at both PackTrack and Luma.

### New endpoint in PackTrack
`POST /api/webhooks/zoho-sales`

Auth: `ZOHO_WEBHOOK_SECRET` (token configured in Zoho's webhook settings).

**Processing:**
1. Write one `SalesEvent` row (idempotent on `zoho_order_id`)
2. Update `daily_usage_rate` rolling average for any material that maps to the sold product (via BOM — fetched from Luma)
3. Re-check reorder thresholds for affected materials → alert if newly breached

### New endpoint in Luma
`POST /api/webhooks/zoho-sales`

Same auth pattern (`ZOHO_WEBHOOK_SECRET`).

**Processing:**
1. Match the sold product to the most recent RELEASED finished lot for that product
2. Link the sale to that lot (new `finished_lot_sales` junction table or update on finished lot)
3. This enables: "which batch was sold on date X to customer Y?" — answerable from Luma

### New table in PackTrack — `sales_events`

| Column | Type | Notes |
|---|---|---|
| id | int PK | auto |
| zoho_order_id | string unique | idempotency key |
| product_sku | string | Zoho item SKU |
| qty_sold | integer | |
| sold_at | datetime | from Zoho payload |
| received_at | datetime | when PackTrack got this |

---

## Phase D — Logistics forecasting dashboard (PackTrack)

### What it does
Gives the logistics team one screen to answer: "Do I need to order anything? How much? By when?" Driven entirely by real consumption data and real sales velocity — no manual estimates.

### The forecasting calculation

Runs on page load, cached 1 hour per user session.

```
sales_velocity[product]    = avg daily qty from sales_events, rolling 60 days
bom[product][material]     = from Luma GET /api/internal/product-packaging-specs (1-hour cache)
daily_demand[material]     = Σ (sales_velocity[P] × bom[P][material]) for all products
days_of_stock[material]    = current_stock[material] / daily_demand[material]
reorder_by_sea[material]   = today + days_of_stock - sea_lead_days
suggested_qty[material]    = (sea_lead_days + 30) × daily_demand - current_stock
```

`sea_lead_days` and `express_lead_days` already on `Item`. No new fields needed.

### New Luma internal API endpoint
`GET /api/internal/product-packaging-specs`

Auth: `X-Luma-PackTrack-Secret` (same secret). Returns:
```json
[
  {
    "product_sku": "HN-001",
    "components": [
      { "material_code": "PT-00095", "qty_per_unit": 1 },
      { "material_code": "PT-00102", "qty_per_unit": 1 }
    ]
  }
]
```

PackTrack never stores a BOM copy. Luma is the BOM authority.

### The logistics view — one new page `/inventory/forecast`

Four panels, all from the same data:

| Panel | Trigger condition | Color | Primary action |
|---|---|---|---|
| **Order now** | `reorder_by_sea <= today + 7 days` | Red | Button → `/po/new` prefilled with item + suggested_qty |
| **Watch** | `reorder_by_sea <= today + 30 days` | Amber | Monitor; plan sea shipment |
| **Full forecast table** | All materials | — | On-hand, burn rate, days of stock, reorder date, suggested qty |
| **Item drill-down** | Click any row | — | Consumption history, sales drivers, sea vs express cost comparison |

### Telegram alert
Fires when `reorder_by_sea` enters the 7-day window. One alert per item per restock cycle — does not repeat until `current_stock` rises back above `reorder_point`. Uses existing `notify()` dispatcher with new `stock.reorder_urgent` event type.

### No new tables
Uses: `Item` (current_stock, lead days), `material_consumption_events` (Phase A), `sales_events` (Phase C), Luma BOM API.

---

## Phase E — Batch traceability (Luma → Nexus, automatic)

### What it does
Every production batch is automatically registered in Nexus when it's released. When a customer complaint comes in, agents select from real production data instead of guessing. Full packaging genealogy is available for root-cause investigation.

### Distinction from existing Luma → Nexus integration
The existing integration (`lib/integrations/nexus/finished-lots.ts`) sends customer-specific shipped lots to Nexus with full shipment and customer context. That flow is for commercial traceability. **This is different.** This is for quality/complaint traceability — every batch, regardless of customer or shipment status, needs to be registered as soon as it's released.

### Trigger
Same `setFinishedLotStatus()` RELEASED hook as Phase A and Phase B.

### New endpoint in Nexus — `POST /api/batches/import`
Auth: `X-Luma-Nexus-Secret` header.

**Request payload:**
```json
{
  "lot_number": "FL-2024-001",
  "product_sku": "HN-001",
  "produced_on": "2024-01-15",
  "units_produced": 1000,
  "luma_finished_lot_id": "uuid",
  "packaging_inputs": [
    {
      "material_code": "PT-00095",
      "material_name": "Blister Card",
      "supplier_lot_number": "SL-2024-001"
    }
  ]
}
```

**Nexus processing:**
1. Resolve `product_sku` → `Product` (create if new, keyed on SKU)
2. Upsert `Batch` (keyed on `lot_number + product`): set `manufactured_on`, `metadata` (luma_id, units, packaging_inputs)
3. Return `{ ok: true, batch_id, created }` — 201 on creation, 200 on update

### New Luma outbound call
New function in `lib/integrations/nexus/batch-registration.ts` — separate file from the existing `finished-lots.ts`. Does not touch the existing flow.

- On `RELEASED`: fire and forget, 10s timeout
- On success: write `finished_lots.nexus_batch_registered_at`
- On failure: write `finished_lots.nexus_batch_register_error` — does not block release
- Existing "Send to Nexus" button for shipped lots is **unchanged**

### What Nexus agents gain
Complaint form dropdown: `FL-2024-001 — HN-001 — produced 2024-01-15 — 1,000 units`. 
Quality investigation can drill from batch → packaging inputs → `material_code` → PackTrack BoxReceipts → supplier lot.

---

## New data models summary

### PackTrack — new table: `material_consumption_events`
One row per material per finished lot release from Luma. Audit log for `current_stock` accuracy.

### PackTrack — new table: `sales_events`
One row per Zoho sales order. Feeds `daily_usage_rate` recomputation and demand forecasting.

### PackTrack — new fields on `Item`
None new — `current_stock` and `daily_usage_rate` already exist. They become auto-maintained instead of manually set.

### Luma — new fields on `finished_lots`
- `zoho_manufacture_order_id` string null (Phase B)
- `zoho_manufacture_error` string null (Phase B)
- `nexus_batch_registered_at` timestamptz null (Phase E)
- `nexus_batch_register_error` string null (Phase E)

### Luma — new table: `finished_lot_sales`
One row per (finished_lot, zoho_order) pair. A single lot can fulfill multiple orders over time (partial shipments), so a junction table is correct. Columns: `finished_lot_id`, `zoho_order_id`, `product_sku`, `qty_sold`, `sold_at`, `linked_at`.

### Nexus — new endpoint
`POST /api/batches/import` in the `api` app (new view + serializer + URL).

---

## Error handling principles

Consistent across all phases:

1. **Never block the triggering action.** A failed Zoho push, PackTrack push, or Nexus push must not prevent a lot from being released or a receipt from being recorded.
2. **Always log the failure.** Every outbound call writes either a success timestamp or an error string to the relevant table.
3. **Make retry visible.** Failed pushes surface in the admin UI so an operator can re-trigger without code changes.
4. **Idempotency everywhere.** Every endpoint accepts the same payload twice without side effects. Keys: `(finished_lot_id, material_code)` for consumption, `zoho_order_id` for sales, `(lot_number, product_sku)` for Nexus batches.

---

## Build order

| Phase | Systems touched | Depends on |
|---|---|---|
| **A** | PackTrack (new endpoint + table), Luma (new outbound call) | Nothing — can ship first |
| **B** | Zoho gateway (new route), Luma (new integration file) | Verify `zohoItemId` on packaging_materials |
| **E** | Nexus (new endpoint), Luma (new outbound call) | Can ship in parallel with B |
| **C** | PackTrack (new endpoint + table), Luma (new endpoint) | Phase B live (so Zoho has accurate data) |
| **D** | PackTrack (new page + forecast service), Luma (new internal API) | Phase A + C live (need real consumption + sales data) |

Phase A and Phase E can ship first and independently. Phase B and E can be built in parallel. Phase C after B. Phase D last — it's the output layer that consumes everything else.

---

## Pre-implementation checklist

Before writing any code, verify:

- [ ] `packaging_materials.zohoItemId` — what percentage of rows are populated? (Phase B dependency)
- [ ] `product_packaging_specs` in Luma — is it populated with real BOM data? (Phase D dependency)
- [ ] Zoho Inventory module supports Manufacture Orders via API (confirm with gateway team)
- [ ] Nexus `Batch` model — confirm `lot_number + product` uniqueness constraint doesn't conflict with existing data
- [ ] `LUMA_PACKTRACK_SECRET` — confirm same secret is used for both receipts and new consumption endpoint (it is, by design)
- [ ] Luma `setFinishedLotStatus()` — confirm no existing hooks fire on RELEASED that would conflict

---

## What the logistics team sees when this is done

One page. Opens fast. Shows:

- **Red panel:** "Order blister cards now — 4 days of stock left at current sales pace. Suggested: 50,000 units. Sea lead time: 45 days."
- **Amber panel:** "Labels running low — 22 days left. Watch."
- **Green rows:** Everything else, with days-of-stock column so nothing surprises them.
- Click any row: see the consumption history chart, which products are burning it, what the last 3 POs cost per unit.
- One button per red item → pre-filled PO form. Owner approves, agent in China sources it.

Every number on that page is traceable to a real event. If a logistics person questions a number, they can click through to the consumption log and see every batch that consumed it.
