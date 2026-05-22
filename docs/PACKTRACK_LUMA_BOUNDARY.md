# PackTrack ↔ Luma Boundary

PackTrack and Luma will share packaging-receipt data. This document defines
who owns what so the two systems never fight over the same inventory record.

---

## Ownership

### PackTrack owns

- **Packaging supplier POs** — creation, design review, PI upload, approval,
  production, shipping, receiving lifecycle.
- **Design review / PI / approval workflow** — the 10-state PO machine in
  `packtrack/services/workflow.py`.
- **Packaging supplier communication** — Telegram inline approvals, comments,
  artwork attachments, file uploads.
- **Shipping / receiving workflow** — express vs sea split, ETA, carrier,
  tracking, per-shipment receipt rows.
- **Supplier-declared box quantities** — the number printed on the box label
  by the supplier ("declared").
- **Optional physical counted quantities** — what the receiving team
  re-counted on arrival ("counted"). Counted overrides declared when present.
- **Luma receipt push status** — per-box `luma_push_status`, last response,
  retry state. Lives on the PackTrack box-receipt row, not on Luma.

### Luma owns

- **Production consumption** — every burn of a packaging unit during a
  production run.
- **Material burn** — quantity-in vs quantity-out.
- **PVC / foil roll usage** — roll-level draw-down events.
- **Reconciliation** — comparing supplier-declared vs counted vs consumed.
- **Shortage projection** — "we will run out of X by date Y" forecasting.
- **Genealogy** — which production lot used which supplier lot.
- **Production loss vs supplier shortage classification** — was the gap due
  to short supplier delivery, or to overuse during production?

### Shared identity (the keys both sides agree on)

| Field | Source | Stable across | Notes |
|---|---|---|---|
| `material_code` | PackTrack `Item.material_code` *(new column added in P1)* | both | Must match Luma's `material_item.code`. Owner-controlled, decoupled from Zoho ids. Nullable today; a partial unique index enforces no-duplicates among populated values. May be seeded from `sku_code` via the audit script when sku is unique + non-empty. |
| `packtrack_po_id` | PackTrack `PurchaseOrder.id` (or `po_number`) | both | Stable identifier of the supplier order. |
| `packtrack_receipt_id` | PackTrack box-receipt row id (P2) | both | One per supplier box. |
| `box_number` | Supplier-printed box id | both | The actual carton/case label. |
| `supplier_lot_number` | Supplier-printed lot id | both | When supplier provides one. |

Luma idempotency = `(packtrack_receipt_id, box_number)` — the same pair must
not produce two Luma receipts.

---

## Hard rule (no double-decrement)

> **PackTrack receives packaging. Luma consumes packaging.**
> **Do not make both systems decrement the same inventory independently.**

### Today's risk surface

PackTrack's current `receive_shipment` route
(`packtrack/routes/purchase_orders.py`) calls `zoho.adjust_stock()` which
posts a positive `inventoryadjustments` record to Zoho Inventory and adds
to `Item.current_stock` locally. That is an *increment* (receiving), not a
decrement, so it does not by itself violate the rule. **But** the moment
Luma also posts the same receipt on its side, both systems will record the
same packaging arriving twice unless the boundary is explicit:

| Event | Where it's recorded |
|---|---|
| Supplier delivers N units | PackTrack box-receipt row (truth source) |
| Financial / accounting view of inventory | Zoho Inventory (PackTrack pushes via `adjust_stock`) |
| Production-planning view of inventory | Luma (PackTrack pushes via `/api/integrations/packtrack/receipts`) |
| Production consumes N units | **Luma only** — never PackTrack |
| Audit trail of who received | PackTrack `POEvent` + Luma `received_by` |

Zoho records *financial* stock (used by Books for COGS, vendor reconciliation).
Luma records *production-floor* stock (used for run planning, shortage
projection). The two views diverge naturally — Zoho counts what PackTrack
received; Luma counts what's left after burn. **Reconciliation between the
two views is a Luma responsibility, not PackTrack's.**

### Decisions deferred (call out before P5 live push)

- Should PackTrack stop pushing `adjust_stock` to Zoho once Luma is live?
  Default answer: **no, keep the Zoho push** — Books still needs the
  receipt for vendor liability and COGS. Luma consumes from a different
  ledger.
- If counted ≠ declared, which value does Zoho see? Default: the
  **accepted** quantity (counted if present, else declared). Same value
  Luma sees. Both systems agree on what arrived; they may disagree on
  what's left after consumption — that's by design.

---

## Receipt direction

```
                       declared (supplier label)
                                │
                                ▼
   ┌──────────────────────────────────────────┐
   │   PackTrack — box-level receipt rows     │
   │   counted (optional, by receiving team)  │
   │   accepted = counted || declared         │
   └──────────────────────────────────────────┘
              │                       │
              │ adjust_stock          │ POST /api/integrations/packtrack/receipts
              ▼                       ▼
   ┌─────────────────┐    ┌──────────────────────┐
   │  Zoho Inventory │    │        Luma          │
   │  (financial)    │    │  (production floor)  │
   └─────────────────┘    └──────────────────────┘
                                     │
                                     │ consumption events (production runs)
                                     ▼
                           Luma decrements its own ledger.
                           PackTrack never sees burn events.
```

PackTrack pushes once, to two destinations, with idempotent keys. Luma is
the only system that touches the production-floor ledger after that.

---

## Out-of-scope for PackTrack

- Production scheduling
- Material burn / consumption tracking
- Roll-level usage events
- Shortage forecasting
- Cross-batch genealogy
- Any decrement of post-receipt packaging stock
