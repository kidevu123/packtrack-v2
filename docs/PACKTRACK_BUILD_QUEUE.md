# PackTrack v2 Build Queue

Execution order. **Each phase has a single goal and a hard acceptance test.
Do not skip ahead. Do not implement the next phase until the previous phase's
acceptance is verified.**

---

## P0 — Audit + queue creation `[done]`

**Goal:** establish the boundary, the gateway plan, and this queue before
writing integration code.

**Deliverables (docs only):**

- [x] `docs/PACKTRACK_BUILD_QUEUE.md` — this file
- [x] `docs/CURRENT_PHASE_STATUS.md`
- [x] `docs/PACKTRACK_LUMA_BOUNDARY.md`
- [x] `docs/ZOHO_API_GATEWAY_PLAN.md`

**Acceptance:** all four docs exist; final report names the first
unchecked phase as P1.

---

## P1 — Material code / item identity audit `[done]`

**Goal:** verify PackTrack `Item` has a stable code that can map 1:1 to
Luma's `material_item.code`.

**Outcome:** added a dedicated `Item.material_code` column (nullable,
partial-unique). Owner-controlled. `sku_code` can seed it via the
`--apply-safe-defaults` flag on the audit script — but only when sku is
unique + non-empty + the row's material_code is currently null.

**Empirical state:** DB has zero items today (Zoho creds not yet filled).
Schema is in place so the first real sync produces a clean audit.

**Files:** `packtrack/services/material_audit.py`,
`scripts/audit_material_codes.py`,
`migrations/versions/a3f1b2c4d5e6_item_material_code.py`,
`tests/test_material_audit.py`.

**What to inspect:**

- Current `Item` fields: `zoho_item_id`, `sku_code`, `name`. Which one is
  the operator's mental "this is the material" identifier?
- Are `sku_code` values unique across the active item set? (column is
  indexed but **not** unique today.)
- Are `zoho_item_id` values present for every active item, or are some
  PackTrack-only items missing it?
- What does Luma key on for `material_item.code`? Confirm with Luma docs
  or owner.

**Decision (chosen):** option 3 — add a dedicated `Item.material_code`
column. Reasons captured in `CURRENT_PHASE_STATUS.md` and
`PACKTRACK_LUMA_BOUNDARY.md`. The column is nullable today; a partial
unique index in Postgres enforces uniqueness only among populated values.
That keeps the integration identity decoupled from Zoho's ids and from
sku_code drift, while leaving room for an audited backfill before P2.

**Acceptance (met):**

- [x] Documented choice in `CURRENT_PHASE_STATUS.md`.
- [x] Audit script (`scripts/audit_material_codes.py`) reports
      collisions + NULLs in a structured way.
- [x] Schema change: one new column + two indexes (filter + partial
      unique). No renames of existing fields.

---

## P1.5 — Real Zoho item catalog sync + audit gate `[done]`

**Goal:** load actual packaging items into PackTrack and run the P1 audit
against real data before box-level receiving (P2) starts referencing
material codes that don't yet exist.

**Path chosen:** the existing **Zoho Integration Service** (LXC 9503,
`http://192.168.1.205:8000`). PackTrack does not store any new Zoho
OAuth state; it calls the gateway with a service token. PackTrack's
existing `packtrack/zoho.py` is untouched (the full migration of that
client lives in P8).

**New artifacts:**

- `scripts/sync_items_via_gateway.py` — paginated read of
  `/zoho/items/list?per_page=200`, filtered to `cf_item_type == 'Packaging'`,
  upserts by `zoho_item_id`. Idempotent. Records a `SyncRun`.
- `docs/P1_5_MANUAL_CLEANUP.md` — list of items (49) needing manual
  material_code assignment.
- Three new env keys on the LXC: `ZOHO_GATEWAY_URL`, `ZOHO_GATEWAY_TOKEN`,
  `ZOHO_GATEWAY_BRAND`. None of them are Zoho OAuth tokens.

**Gap reported:** the gateway's `/openapi.json` does **not** advertise
Zoho Inventory routes — items are reachable only via the generic
`/zoho/{service}/{action}` dispatcher. P8 should formalise this surface.

**Acceptance (met):**

- [x] 94 items in PackTrack, all with `zoho_item_id`.
- [x] Audit run on real data. 0 duplicate sku_code groups.
- [x] 45 items backfilled with safe defaults (UPC-12 + Uline codes), 49
      items flagged for manual review.
- [x] Pytest still 13/13 green.

---

## P2 — Box-level receiving model

**Goal:** receiving can record each supplier box separately, instead of
collapsing to a single `Shipment.received_quantity`.

**New model `BoxReceipt` (or extend Shipment with child rows — pick during
implementation):**

| Field | Type | Notes |
|---|---|---|
| `id` | int PK | becomes `packtrack_receipt_id` outbound |
| `po_id` | FK → purchase_orders | required |
| `shipment_id` | FK → shipments | nullable; receiving can pre-date the shipment row |
| `item_id` | FK → items | required |
| `material_code` | str | denormalized snapshot at receive time (so a later item rename can't change history) |
| `box_number` | str | supplier-printed carton id |
| `supplier_lot_number` | str \| null | supplier-printed lot id |
| `declared_quantity` | float | from the box label |
| `counted_quantity` | float \| null | optional physical count |
| `accepted_quantity` | float | computed: counted if present, else declared |
| `unit_of_measure` | str | default `EACH`, override per item |
| `confidence` | enum | `HIGH` if counted, `MEDIUM` if declared-only |
| `received_by_user_id` | FK → users | required |
| `received_at` | datetime | required, UTC |
| `luma_push_status` | str \| null | `pending` \| `dry_run_ok` \| `pushed` \| `failed` |
| `luma_pushed_at` | datetime \| null | last successful push |
| `luma_response` | JSONB \| null | last Luma response body, redacted |
| `notes` | text \| null | free text |

**Rules:**

- `accepted_quantity = counted_quantity if counted_quantity is not None else declared_quantity`
- `confidence = HIGH if counted_quantity is not None else MEDIUM`
- **Never write declared into counted.** They are different facts.
- `(po_id, box_number)` should be unique within a PO so the same box can't
  be entered twice.

**Acceptance:**

- Migration generated, reviewed, deployed.
- Existing `Shipment.received_quantity` flow continues to work for
  shipments that were received before P2 (backwards-compat read).
- New receiving form (P6) is the *only* code path that creates new box
  receipts — old `receive_shipment` route is fenced off or made internal.

---

## P3 — Luma payload builder

**Goal:** typed pure-Python helper that turns a `BoxReceipt` row into the
Luma webhook payload.

**Signature:**

```python
def build_luma_payload(box: BoxReceipt) -> dict: ...
```

**Output (matches Luma's spec exactly):**

```json
{
  "source_system": "PACKTRACK",
  "packtrack_po_id": "...",
  "packtrack_receipt_id": "...",
  "material_code": "...",
  "material_name": "...",
  "supplier": "...",
  "supplier_lot_number": "...",
  "box_number": "...",
  "declared_quantity": 1000,
  "counted_quantity": null,
  "unit_of_measure": "EACH",
  "received_at": "...",
  "received_by": "..."
}
```

**Validation:**

- Required: `packtrack_po_id`, `packtrack_receipt_id`, `material_code`,
  `material_name`, `box_number`, `declared_quantity`, `received_at`,
  `received_by`.
- `received_at` is ISO-8601 with timezone.
- `declared_quantity > 0`. If `counted_quantity` is provided, also `> 0`.
- No inventory mutation in this function. Pure builder.

**Acceptance:**

- Unit tests cover: full happy path, counted-only, declared-only,
  missing material_code → raises `MappingMissing`, missing user →
  raises `BuilderError`.
- `pytest tests/test_luma_payload.py` passes.

---

## P4 — Luma dry-run push

**Goal:** receiving (or owner) can hit a "Dry-run to Luma" button and see
exactly what Luma would have done, without writing to Luma's ledger.

**Wiring:**

- Env: `LUMA_RECEIPT_WEBHOOK_URL`, `LUMA_PACKTRACK_SECRET`.
- Headers on the request:
  - `x-packtrack-secret: <LUMA_PACKTRACK_SECRET>`
  - `x-packtrack-dry-run: true`
  - `Content-Type: application/json`
- Endpoint: `POST {LUMA_RECEIPT_WEBHOOK_URL}` (defaults to `/api/integrations/packtrack/receipts`).
- Failures categorized:
  - `MAPPING_MISSING` → surface "Luma doesn't recognize material_code X" with
    a link to fix the item code in PackTrack.
  - `IDEMPOTENCY_HIT` → surface "Already received by Luma — no change".
  - `BAD_REQUEST` → show field-level errors.
  - `5xx` / network → surface "Luma unavailable, retry".
- Logs: never log the secret. Body of request OK, but redact headers.

**Acceptance:**

- Button on the box-receipt row labeled "Dry-run".
- Click produces a toast + a row in the box's `luma_push_status` history
  showing dry-run result.
- No rows mutated on Luma side (verified by Luma owner).

---

## P5 — Luma live push (manual approval)

**Goal:** the same endpoint, called for real, gated behind a deliberate
button click.

**Rules:**

- Manual button only — **no auto-push on receive**.
- Retry-safe — calling twice with the same `(packtrack_receipt_id, box_number)`
  is fine; Luma returns the existing receipt.
- Stores Luma's response on `BoxReceipt.luma_response` + sets
  `luma_push_status = 'pushed'` and `luma_pushed_at = now`.
- Duplicate response (`IDEMPOTENCY_HIT`) is handled cleanly: state
  becomes `pushed`, `luma_response` recorded, no error toast.
- **No automatic inventory burn.** PackTrack does not decrement.
- Audit: a `POEvent` row of kind `luma_push` per attempt with the box id
  and outcome.

**Acceptance:**

- A box can be pushed live, response stored.
- Pushing the same box twice produces no duplicate Luma receipt and a
  clear "already there" UI state.
- Owner can see push status across all boxes for a PO from the PO detail
  page.

---

## P6 — Receiving UI refinement

**Goal:** the receiving team can record a box in 10 seconds without
digging.

**UX requirements:**

- Per-shipment view shows a list of boxes received-so-far + an "Add box"
  form.
- Add-box form fields: box number, declared qty (from label), optional
  counted qty, supplier lot.
- Live preview row showing accepted qty + confidence badge as the form
  is filled.
- After save, the new row inline-shows the push-to-Luma status with a
  button (dry-run first, then live).
- Mobile-friendly — receiving may be doing this on a phone at a dock.

**Acceptance:**

- 10-box receipt can be entered in under 2 minutes by a non-technical
  user.
- Each box has its own `BoxReceipt` row, independently push-able.

---

## P7 — Luma shortage recommendations inbound

**Goal:** Luma can recommend a packaging reorder back to PackTrack
("you'll be out of bottle X in 12 days, suggest ordering N").

**Rules:**

- Read-only first — recommendation lands as a `Recommendation` row in
  PackTrack with status `pending_owner_review`.
- **No automatic PO creation.**
- Owner approval required to convert a recommendation into a draft PO.
- Source field on Recommendation stores Luma's payload for audit.

**Acceptance:**

- A Luma recommendation appears in PackTrack's home dashboard "Needs you"
  section for the owner.
- Clicking through and approving creates a draft PO with the recommended
  line — but the PO is still in `draft` and requires the normal review
  flow to push.

---

## P8 — Zoho API gateway migration

**Goal:** PackTrack consumes Zoho exclusively through the gateway at
`192.168.1.205:8000` (see `ZOHO_API_GATEWAY_PLAN.md`).

**Acceptance:**

- `/etc/packtrack/packtrack.env` no longer holds Zoho OAuth secrets.
- `packtrack/zoho.py` is reduced to a gateway client.
- All sync runs go through the gateway successfully for at least a week.

---

## P9 — UI polish

**Goal:** only after P1–P8 are landed and the backend contract is safe.
Anything visual/CLS/UX here is fair game; until then, hold.

---

## Phase dependency graph

```
P0 ──▶ P1 ──▶ P2 ──▶ P3 ──▶ P4 ──▶ P5 ──▶ P6
                                            │
                            ┌───────────────┘
                            ▼
                           P7
                            │
                            ▼
                           P8 ──▶ P9
```

P7 requires the receipt-side roundtrip to be solid (P5+P6). P8 is mostly
independent but blocks P9 because gateway migration may surface UI changes.
