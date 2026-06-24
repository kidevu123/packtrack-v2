# PackTrack Confidence & Validation Model

A single "confidence" field is forbidden. PackTrack and Luma each own
distinct concepts that must remain separable in storage, in API
payloads, and in UI labels.

This document defines the four axes, the enum values, transition
rules, and worked examples. It is the source of truth referenced by:

- [`PACKTRACK_LUMA_BOUNDARY.md`](./PACKTRACK_LUMA_BOUNDARY.md) § 7
- [`PACKTRACK_API_SURFACE.md`](./PACKTRACK_API_SURFACE.md) (receipt-shaped responses)
- [`PACKTRACK_BUILD_QUEUE.md`](./PACKTRACK_BUILD_QUEUE.md) Phase 3 (schema split) and Phase 5 (transition implementation)

---

## The four axes

### 1. `receipt_source`  (PackTrack)

**Owner:** PackTrack.
**Where:** column on the receipt row (today `BoxReceipt`; in Phase 3
renamed from the existing `confidence` field).
**Meaning:** how the *original* receipt quantity was obtained.
**Lifecycle:** set at creation. **Never rewritten.**

**Values:**

- `SUPPLIER_DECLARED` — quantity is the supplier's label / packing
  list / declared number. Receiving team did not physically count.
- `COUNTED_AT_RECEIPT` — receiving team physically counted the
  carton (or initiated a Luma-side material receipt that they
  counted in person).
- `IMPORTED` — quantity came from a historical import or migration,
  not from a live receiving event. Reserved for data backfills.
- `MANUAL_ADJUSTED` — owner / admin set the quantity by hand to
  correct a known data error. Should be rare and always audited.

**Rule:** `receipt_source` is historical fact. If the supplier
declared 10,000 cards and Luma later proves only 9,400 actually
existed, the source stays `SUPPLIER_DECLARED` forever. The change
flows through `receipt_validation_status`, not here.

### 2. `receipt_validation_status`  (PackTrack)

**Owner:** PackTrack.
**Where:** column on the receipt row (added in Phase 3 alongside
the `receipt_source` rename).
**Meaning:** how later evidence has validated or disputed the
original receipt quantity.
**Lifecycle:** starts `UNVALIDATED` at creation; advances or
regresses as consumption events arrive (Phase 5).

**Values (planning state machine):**

- `UNVALIDATED` — no consumption evidence yet against this receipt.
- `PARTIALLY_VALIDATED` — some consumption events have drawn down
  this receipt, but the cumulative validated quantity is well below
  `accepted_quantity` (suggested threshold: < 25%).
- `MOSTLY_VALIDATED` — cumulative validated quantity is between
  25% and 95% of `accepted_quantity` (thresholds tunable in Phase 5).
- `VALIDATED` — cumulative validated quantity reaches 95–105% of
  `accepted_quantity` without overshoot. The supplier's declared
  (or counted) number is supported by production evidence.
- `DISPUTED` — cumulative consumption has gone meaningfully *above*
  `accepted_quantity` while not yet at `OVER_CONSUMED` thresholds;
  Luma's evidence and PackTrack's receipt disagree. Surfaces as a
  banner in PackTrack admin.
- `OVER_CONSUMED` — cumulative consumption exceeds
  `accepted_quantity` by a hard margin (suggested: > 110%).
  Operationally indistinguishable from `DISPUTED` for ledger math
  but flagged separately for operator attention.

**Transitions are monotonic for the validated path:**

```
UNVALIDATED → PARTIALLY_VALIDATED → MOSTLY_VALIDATED → VALIDATED
```

**Disputed transitions are terminal** until manual intervention:

```
* → DISPUTED → (manual review) → VALIDATED or stays DISPUTED
* → OVER_CONSUMED → (manual review) → VALIDATED or stays OVER_CONSUMED
```

**Receipt selection rule (Phase 5):** when a consumption event
arrives for `material_code = X`, evidence is applied to the
oldest-non-fully-validated receipt for `X` first
(FIFO-by-`received_at`). When that receipt reaches `VALIDATED`,
subsequent evidence flows to the next-oldest. This mirrors typical
FIFO inventory burn-down.

### 3. `production_confidence`  (Luma)

**Owner:** Luma.
**Where:** column on the production-event / finalized-run row in
Luma (already exists in some form; spec freeze deferred to Phase 4).
**Meaning:** how cleanly a single Luma production run's events
reconcile internally (workflow events, BOM, packing counts).

**Values:** `HIGH | MEDIUM | LOW`.

**Rule:** Luma may pass `production_confidence` along on a
consumption event payload (see API surface doc). PackTrack stores it
as metadata next to the event. **PackTrack does not feed
`production_confidence` into `receipt_validation_status`.** Low
production confidence does not retroactively invalidate a receipt;
it surfaces in Luma's forecast UI.

### 4. `forecast_confidence`  (Luma-facing view layer)

**Owner:** Luma forecast / report UI.
**Where:** **not** a stored column anywhere — computed on the fly.
**Meaning:** the user-facing planning confidence for "we will run
out of material X in Y days."

**Inputs:**

- PackTrack `receipt_validation_status` distribution for the affected
  `material_code`.
- Luma recent-run `production_confidence` distribution.
- Luma BOM completeness / packing exception rate.

**Rule:** Must not be collapsed back to a single PackTrack column.
This keeps PackTrack's receipt truth and Luma's production truth
auditable separately even after the forecast layer combines them.

---

## Why supplier-declared remains historical

Operators ask: "if Luma proves the supplier short-shipped, shouldn't
PackTrack 'fix' the supplier-declared number?" No. Two reasons:

1. **Audit trail.** The supplier's declared number is the basis for
   the invoice, vendor reconciliation, and any dispute conversation
   with the supplier. Overwriting it loses the evidence trail.
2. **Reversibility.** Today's "Luma proved a shortage" can be
   tomorrow's "we found the missing cartons in another bay."
   `receipt_validation_status` can move back. The historical
   `accepted_quantity` paired with `SUPPLIER_DECLARED` source is
   the anchor we reconcile against.

What PackTrack *can* change in response to Luma evidence:

- `receipt_validation_status` (axis 2).
- `current_stock` (via the Phase 5 ledger).
- Per-material reorder recommendations.

What PackTrack must not change in response to Luma evidence:

- `receipt_source` (axis 1).
- `declared_quantity`, `counted_quantity`, or `accepted_quantity` on
  the original receipt row.
- The original `supplier_lot_number`, `box_number`, or supplier
  identity.

---

## How Luma consumption events validate over time

Worked example. Supplier delivers 10,000 cards declared on the box
label. Receiving team scans the carton but does not physically count.
PackTrack creates a receipt:

- `declared_quantity = 10000`
- `counted_quantity = null`
- `accepted_quantity = 10000`
- `receipt_source = SUPPLIER_DECLARED`
- `receipt_validation_status = UNVALIDATED`

Over the following weeks Luma finalizes production runs that consume
these cards. Each finalization sends one consumption event to
PackTrack with the counted cards used.

Cumulative consumed against this receipt (oldest-first allocation):

- After 1,500 consumed → `PARTIALLY_VALIDATED` (15%).
- After 4,000 consumed → `MOSTLY_VALIDATED` (40%).
- After 9,600 consumed → `VALIDATED` (96%, within ±5% band).
- After 11,200 consumed → `DISPUTED` (112% — supplier likely
  short-shipped, or earlier consumption events double-counted).
- After 11,500 consumed → `OVER_CONSUMED` (115% — operator review
  required; receipt evidence cannot reconcile).

Notes:

- `receipt_source` never changes from `SUPPLIER_DECLARED`.
- `accepted_quantity` never changes from 10,000.
- `current_stock` does decrement each time, regardless of validation
  status.
- The 95–105% / 110% thresholds are planning numbers; final values
  are tunable in Phase 5 and should be configurable in
  `app_settings`.

---

## Why production counting is not a weak estimate

Phase 4/5 design must respect that **packing is counting**. Cards →
displays → cases → master cases counts are derived by deterministic
multiplication through the BOM during the packing workflow, but the
inputs to that multiplication are real packed-quantity counts entered
by operators, not estimates.

The consumption event payload (`POST /api/luma/consumption-events`)
treats `consumed_quantity` as authoritative evidence at whatever BOM
level Luma submits it. PackTrack's validation-status math therefore
does not need to discount these as soft.

When Luma needs to communicate that a particular event is *not*
fully trustworthy, the right knobs are:

- `production_confidence: LOW` on the event (metadata only — no
  ledger impact).
- `damaged_quantity` / `discarded_quantity` / `returned_quantity` on
  the per-item entry (explicit accounting that still decrements
  stock).
- A follow-up `MANUAL_ADJUSTED` adjustment if the math truly cannot
  be reconciled.

PackTrack treats all three as equally accountable evidence; only the
first changes how it surfaces in Luma's forecast UI.
