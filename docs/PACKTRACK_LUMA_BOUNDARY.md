# PackTrack v2 ↔ Luma Boundary

PackTrack v2 owns packaging and material inventory authority. Luma owns
tablet production. This document defines the new ownership split, the
pull-first integration model, the two narrow write paths Luma has back
to PackTrack, the four-axis confidence model, and the Luma tables that
PackTrack must never touch.

This supersedes the earlier push-only boundary description. Earlier
language such as "PackTrack never sees burn events", "PackTrack must
not decrement post-receipt stock", and "PackTrack pushes once to two
destinations" is no longer the target. See § 10 Supersedes at the end
of this document.

> Companion docs: [`PACKTRACK_API_SURFACE.md`](./PACKTRACK_API_SURFACE.md)
> for the planned endpoints, [`PACKTRACK_CONFIDENCE_MODEL.md`](./PACKTRACK_CONFIDENCE_MODEL.md)
> for the four-axis confidence model, [`PACKTRACK_BUILD_QUEUE.md`](./PACKTRACK_BUILD_QUEUE.md)
> for the phased execution plan.

---

## 1. Ownership

### PackTrack owns

- **Packaging item master** — product-specific items: printed blister
  cards, printed display boxes, product labels, master cases, and any
  product-specific packaging.
- **Generic material item master** — generic consumables: PVC rolls,
  foil rolls, premade blisters, bottles, lids/caps, induction seals,
  shrink bands, cotton, desiccants.
- **`material_code` identity** — the shared key both systems agree on.
  PackTrack owns issuance, uniqueness, and lifecycle of `material_code`.
- **Procurement / POs** — full PO lifecycle, design review, PI upload,
  approval, production tracking, shipping, receiving.
- **Receiving** — supplier carton-level receipts; supplier-declared
  quantities; counted-at-receipt quantities; accepted quantity.
- **Authoritative current stock** — the on-hand number Luma sees is
  computed in PackTrack.
- **Stock movement ledger** — every receipt, consumption, adjustment,
  and transfer is one signed row. (To be added in Phase 5; today
  PackTrack does not yet have this table — see § 9.)
- **`receipt_source`** — how an original receipt quantity was obtained.
  See [`PACKTRACK_CONFIDENCE_MODEL.md`](./PACKTRACK_CONFIDENCE_MODEL.md).
- **`receipt_validation_status`** — how later evidence (especially
  Luma consumption events) has validated or disputed the original
  receipt. See [`PACKTRACK_CONFIDENCE_MODEL.md`](./PACKTRACK_CONFIDENCE_MODEL.md).
- **Reorder planning** — daily-usage / lead-time / threshold math.
- **Inventory deductions** — including deductions triggered by Luma
  consumption events. PackTrack is the authoritative ledger writer.

### Luma owns

- **Tablet production** — manufacturing of finished tablet products.
- **Product master and product BOMs** — recipes that map a product to
  the PackTrack packaging items and materials it consumes.
- **Product-to-PackTrack item/material mappings** — local map from a
  Luma product to one or more PackTrack `material_code` values.
- **Production scheduling, production runs, workflow events, batches.**
- **Finished lots and genealogy** — finished-product identity,
  traceability, expiry, and the lineage of which packaging/material
  lots ended up in which finished lot.
- **Packing-as-counting math** — see § 6.
- **Cards/single units produced, displays produced, cases/master
  cases produced** — these are *counted* through the packing
  workflow, not estimated.
- **Damaged / discarded / returned capture** during production.
- **`production_confidence`** — how cleanly a production run's events
  reconcile internally.
- **User-facing `forecast_confidence`** — a planning confidence the
  Luma UI computes from PackTrack `receipt_validation_status` plus
  Luma `production_confidence`.

### Shared identity

Both sides agree on these keys; PackTrack issues them, Luma mirrors:

- `material_code` — owner-controlled stable code.
- `packtrack_po_id`
- `packtrack_receipt_id`
- `box_number`
- `supplier_lot_number`

Luma stores these on its `packaging_lots` mirror for reconciliation
and genealogy linkage but does not consider them authoritative
inventory ownership.

---

## 2. Terminology — packaging items vs materials

These two categories must remain visibly distinct in future UI/API
language. They are not the same thing.

**Packaging items** (product-specific):

- printed blister cards
- printed display boxes
- product labels
- master cases
- any other product-specific packaging

**Materials** (generic consumables):

- PVC rolls
- foil rolls
- premade blisters
- bottles
- lids/caps
- induction seals
- shrink bands
- cotton
- desiccants

Today both kinds may live in PackTrack's flat `Item` table and Luma's
`packaging_materials` table without a stored class axis. Future API
and UI language must distinguish them; an `item_class` axis on
PackTrack `Item` is planned in Phase 1. Until that lands the
distinction lives only in this doc and in operator knowledge.

Two product-specific reasons not to collapse the terms:

- Luma-initiated receipt (see § 4.A) is only for materials, never for
  packaging items.
- Forecast / reorder behavior may differ: packaging items track to a
  specific SKU; materials track to a generic supply.

---

## 3. Pull-first integration model

Luma is the active integrator. PackTrack publishes data; Luma pulls.

### Schedule (Luma side)

- Every 15 minutes during working hours, 10:00–19:00 America/New_York.
- One overnight sync at 03:59 America/New_York.
- On page load for Luma inventory / forecast / BOM screens.
- Manual "Refresh from PackTrack" button.
- Just-in-time pull immediately before production finalization or
  consumption submission. Treated as a soft check; a slow PackTrack
  must not block production finalization.

### What Luma pulls

See [`PACKTRACK_API_SURFACE.md`](./PACKTRACK_API_SURFACE.md) for the
exact endpoints:

- Items (paginated; filterable by `item_class` once Phase 1 lands).
- Receipts with `receipt_source` + `receipt_validation_status`.
- Stock summary for inventory / forecast pages.

Luma keeps its existing `packaging_materials` / `packaging_lots`
tables as a read cache only. Luma must not treat them as authoritative
stock once Phase 2 lands.

PackTrack stays on UTC internally; the Eastern schedule lives entirely
in Luma's scheduler.

---

## 4. Luma write paths to PackTrack (two only)

### A. Luma-initiated generic material receipt

- **For generic materials only** — never packaging items.
- Luma UI initiates the action so production users can receive
  consumables without leaving Luma.
- PackTrack creates the authoritative receipt row (today's
  `BoxReceipt` table, evolving in Phase 3 to carry `receipt_source`
  + `receipt_validation_status` instead of the single `confidence`
  enum).
- PackTrack writes the stock movement ledger row (Phase 5).
- PackTrack sets `receipt_source = COUNTED_AT_RECEIPT` by default for
  this path, since the receiving user is physically present;
  `IMPORTED` or `MANUAL_ADJUSTED` are alternates if the Luma client
  specifies them.
- PackTrack returns `packtrack_receipt_id` for Luma to cache as a
  reference.
- Luma stores only the returned reference; it must not maintain a
  parallel authoritative quantity.

### B. Luma production-consumption event

- Triggered after Luma finalizes a production run.
- Payload must include `material_code` × `consumed_quantity` plus, as
  applicable: `damaged_quantity`, `discarded_quantity`,
  `returned_quantity`.
- Counts span: cards/single units, displays, cases/master cases — at
  whichever BOM-mapped `material_code` level corresponds to the
  packed unit.
- PackTrack records the authoritative inventory decrement in the
  stock movement ledger (Phase 5).
- PackTrack uses each event as evidence to advance
  `receipt_validation_status` for the relevant receipts over time
  (`UNVALIDATED → PARTIALLY_VALIDATED → MOSTLY_VALIDATED → VALIDATED`,
  or `DISPUTED` / `OVER_CONSUMED` on overshoot). See
  [`PACKTRACK_CONFIDENCE_MODEL.md`](./PACKTRACK_CONFIDENCE_MODEL.md).
- Idempotency: Luma supplies a unique `idempotency_key` per event;
  PackTrack rejects duplicates.

### Anything else

Anything else is **read-only Luma → PackTrack**. Luma does not write
to PackTrack outside the two paths above.

---

## 5. Legacy PackTrack → Luma push (current behavior)

PackTrack today still pushes per-box receipts to Luma's
`POST /api/integrations/packtrack/receipts` immediately on receive
(see `packtrack/services/receiving.py::push_luma_receipt`). This is
the *current* behavior, retained for back-compat while the pull model
is being built.

Push is **not** the future target architecture. After Phase 2 (Luma
pull) is live, the push path can be:

- left in place as a redundant fast-path,
- demoted to a manual "force push" admin tool, or
- removed.

That decision is deferred and is not part of Phase 0. Do not add new
push behaviors or rely on push as the primary integration in any new
work.

---

## 6. Production counting rule (packing is counting)

This rule prevents future agents from mis-modeling production output.

When Luma's packing workflow records that operators packed 100 cases,
and each case has X displays, and each display has Y cards/single
units, those derived counts are **production evidence**, not weak
estimates.

The only situations that change this are:

- Luma records an explicit override at a packing station.
- Luma records an incomplete pack (e.g. a case ended short).
- Luma records a missing or out-of-order workflow event.
- Luma records an exception (damaged/discarded/returned).

PackTrack should treat consumption events for cards, displays, and
cases/master cases as counted evidence unless the payload explicitly
flags them otherwise.

---

## 7. Four-axis confidence / validation model

A single "confidence" field is forbidden. See
[`PACKTRACK_CONFIDENCE_MODEL.md`](./PACKTRACK_CONFIDENCE_MODEL.md) for
full definitions, enums, and transition rules. Short version:

- **`receipt_source`** (PackTrack column, never rewritten):
  `SUPPLIER_DECLARED | COUNTED_AT_RECEIPT | IMPORTED | MANUAL_ADJUSTED`.
- **`receipt_validation_status`** (PackTrack column, mutates over
  time as evidence arrives):
  `UNVALIDATED | PARTIALLY_VALIDATED | MOSTLY_VALIDATED | VALIDATED | DISPUTED | OVER_CONSUMED`.
- **`production_confidence`** (Luma, per production-event row):
  `HIGH | MEDIUM | LOW`.
- **`forecast_confidence`** (Luma view layer): computed combination
  of the above two PackTrack values for the relevant material and
  Luma's recent `production_confidence`. **Must not** be stored as a
  single canonical column on PackTrack.

Key nuance: PackTrack must not rewrite the historical `receipt_source`.
If a supplier declared 10,000 cards, the source stays
`SUPPLIER_DECLARED` forever. As Luma's counted consumption events
flow in, only `receipt_validation_status` (and current stock) move.

---

## 8. Luma tables PackTrack must never touch (HIGH RISK)

These tables encode Luma's production authority and traceability.
PackTrack writes to none of them, and refactors none of them, in this
project.

- `workflow_events` — append-only production source of truth.
- `batches` — production batch state machine.
- `finished_lots` — finished-product identity, trace codes, expiry,
  output counts.
- `finished_lot_inputs` — genealogy edges.
- `finished_lot_raw_bags` — genealogy edges.
- `finished_lot_packaging_lots` — genealogy edges.
- `packaging_lots.qtyOnHand` writers — Luma-internal cache decrement
  path.

PackTrack may *read* a subset of the above when explicitly requested
for forecasting, but writes are out of scope.

`packaging_lots` rows that PackTrack induces via receipt or
consumption flow continue to exist as a Luma-side mirror of
authoritative PackTrack state.

---

## 9. Today's gaps the new model exposes

These are stated honestly so the next phase plans do not pretend code
already supports the new model:

- **No stock movement ledger.** PackTrack today has no
  `stock_movement` table. `Item.current_stock` is partly Zoho-sync
  overwritten, partly legacy-route incremented, and never decremented
  in PackTrack. Phase 5 introduces the ledger.
- **Two coexisting receive paths.** Legacy
  `packtrack/routes/purchase_orders.py::receive_shipment` still
  mutates `Item.current_stock` and calls `zoho.adjust_stock`. The
  new box-receipt path does not. Both are wired up today. Reconciled
  in Phase 5/6.
- **No service-token middleware.** All current routes require web
  session auth. `LUMA_PACKTRACK_SECRET` is outbound-only today;
  Phase 1 reuses it inbound.
- **No `item_class`.** Packaging items vs materials lives only in
  this doc. Phase 1 adds the column.
- **Single `confidence` enum.** `BoxReceipt.confidence`
  (`HIGH | MEDIUM`) is the wrong-shape field. Phase 3 splits it into
  `receipt_source` + `receipt_validation_status`.
- **No Luma consumption receiver.** Phase 5 adds it.
- **Push-oriented receipt integration is the only live path today.**
  Pull APIs and Luma write paths are planned, not built.

---

## 10. Supersedes

This document supersedes the prior push-only boundary description.
Specifically retired:

- "PackTrack pushes once, to two destinations, with idempotent keys.
  Luma is the only system that touches the production-floor ledger
  after that." — superseded by § 3 and § 4.
- "PackTrack never sees burn events." — superseded by § 4.B and § 9.
- "Out-of-scope for PackTrack: Material burn / consumption tracking,
  Any decrement of post-receipt packaging stock." — superseded by
  § 1 (PackTrack owns inventory deductions) and § 4.B.
- "Production consumes N units | Luma only — never PackTrack." —
  superseded by § 4.B.
- "Reconciliation between the two views is a Luma responsibility,
  not PackTrack's." — partially retained for production-internal
  reconciliation, but receipt-side validation now lives on PackTrack
  via `receipt_validation_status`.

The financial / Zoho push (`zoho.adjust_stock`) is independent and
remains in place for Books / COGS purposes; see § 9.
