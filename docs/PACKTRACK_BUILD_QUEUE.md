# PackTrack v2 Build Queue

> **Re-sequenced May 2026 — separation work supersedes the original
> push-only roadmap.** The original P2 → P7 entries below are
> preserved because they accurately describe **what is running in
> production today**. They are no longer the future direction. All
> new work follows the **Phase 0 → Phase 6** master plan in this
> section. Cross-references:
> [`PACKTRACK_LUMA_BOUNDARY.md`](./PACKTRACK_LUMA_BOUNDARY.md),
> [`PACKTRACK_API_SURFACE.md`](./PACKTRACK_API_SURFACE.md),
> [`PACKTRACK_CONFIDENCE_MODEL.md`](./PACKTRACK_CONFIDENCE_MODEL.md),
> [`CURRENT_PHASE_STATUS.md`](./CURRENT_PHASE_STATUS.md).

Execution order, original rule: each phase has a single goal and a
hard acceptance test; do not skip ahead.

---

## Master plan (Phase 0 → Phase 6)  — current direction

### Phase 0 — Docs / boundary correction (PackTrack repo only)

**Status:** in progress (this commit).
**Touches:** docs, README.md, .env.example. No `.py`, no migration,
no template, no Luma file.

**Deliverables:**

- [ ] Rewrite [`PACKTRACK_LUMA_BOUNDARY.md`](./PACKTRACK_LUMA_BOUNDARY.md)
- [ ] Create [`PACKTRACK_API_SURFACE.md`](./PACKTRACK_API_SURFACE.md)
- [ ] Create [`PACKTRACK_CONFIDENCE_MODEL.md`](./PACKTRACK_CONFIDENCE_MODEL.md)
- [ ] Update [`CURRENT_PHASE_STATUS.md`](./CURRENT_PHASE_STATUS.md)
- [ ] Update this file
- [ ] Update `.env.example` with `LUMA_*` and `ZOHO_GATEWAY_*` placeholders
- [ ] Update `README.md` with a short Luma integration note

**Acceptance:** every doc above exists and consistently describes
the four-axis confidence model, the pull-first integration, and the
two narrow Luma write paths. Code unchanged.

### Phase 1 — PackTrack authoritative read APIs for Luma (PackTrack repo)

**Goal:** publish the read surface Luma will pull from on its
schedule.

**Tasks:**

- Add `packtrack/services/api_auth.py` with `require_service_token`
  dependency checking `x-packtrack-secret` against
  `LUMA_PACKTRACK_SECRET`.
- Add `packtrack/routes/integrations_luma.py` with:
  - `GET /api/luma/items`
  - `GET /api/luma/items/{material_code}`
  - `GET /api/luma/receipts?since=...`
  - `GET /api/luma/stock-summary`
- Add `item_class` column to `Item` (`PACKAGING_ITEM | MATERIAL`)
  with migration; backfill via existing
  `packtrack/services/receiving.py::_infer_luma_kind` heuristic +
  manual override allowed.
- First non-audit tests in the project.

**What NOT to touch in Phase 1:** receiving routes, scheduler, push
paths, Luma repo.

**Acceptance:** Luma can hit all four endpoints with the shared
secret and get correct data. `item_class` populated on all live
items.

### Phase 2 — Luma cache / read integration (Luma repo)

**Goal:** Luma uses PackTrack as the data source for inventory,
forecast, and BOM views via the schedule defined in the boundary
doc.

**Tasks (Luma side, owned by Luma work later):**

- Scheduled pull: every 15 min during 10:00–19:00 America/New_York
  + 03:59 America/New_York overnight + page-load + manual refresh
  + JIT pre-finalize check.
- Refactor Luma inventory / forecast / BOM views to read from a
  local PackTrack mirror cache. JIT pull is a soft check, not a
  hard gate (slow PackTrack must not block production finalization).

**What NOT to touch in Phase 2:** `workflow_events`,
`finished_lots*`, `batches`, finalization path.

**Acceptance:** Luma's inventory page renders from PackTrack data
on the documented schedule; manual refresh works; JIT call produces
a banner (not a block) on slow PackTrack.

### Phase 3 — Luma-initiated generic material receipt (both repos, narrow)

**Goal:** generic material receipts initiated in Luma create
authoritative PackTrack receipts.

**PackTrack side:**

- Migration: rename `box_receipts.confidence` →
  `box_receipts.receipt_source` with enum
  (`SUPPLIER_DECLARED | COUNTED_AT_RECEIPT | IMPORTED | MANUAL_ADJUSTED`).
- Migration: add `box_receipts.receipt_validation_status` defaulting
  to `UNVALIDATED`.
- Backfill: `HIGH → COUNTED_AT_RECEIPT`,
  `MEDIUM → SUPPLIER_DECLARED`. Keep `LumaPushStatus` for legacy
  compatibility.
- Add `POST /api/luma/material-receipts` endpoint; reuse
  `services/box_receipt.py::create_box_receipt`; constrain
  `item_class = MATERIAL` (reject packaging items with
  `BAD_REQUEST`).
- Idempotency table or constraint for Luma-supplied
  `idempotency_key`.

**Luma side:** wire existing "receive material" UI to POST to
PackTrack; cache returned `packtrack_receipt_id`. No Luma-local
authoritative quantity.

**What NOT to touch in Phase 3:** consumption events, packaging
items (only generic materials in this phase), production
finalization.

### Phase 4 — Luma consumption-event builder (Luma repo, build-only)

**Goal:** after finalization, Luma builds (but does not yet send)
the consumption event payload that Phase 5 will receive.

**Tasks (Luma side):**

- After `BAG_FINALIZED` (or equivalent), enqueue an async job that
  builds a consumption event payload: `items[]` of
  `material_code × consumed_quantity` plus
  `damaged | discarded | returned` where applicable. Include
  cards/single units, displays, cases, master cases at whichever
  BOM level corresponds to the packed unit.
- Job persists payloads to a local queue table for inspection only —
  **does not POST** yet.

**What NOT to touch in Phase 4:** finalization persistence, lot
tables, `packaging_lots.qtyOnHand` writers. Purely additive.

### Phase 5 — PackTrack consumption receiver + stock ledger (PackTrack repo, then Luma)

**Goal:** PackTrack accepts consumption events, decrements the
authoritative ledger, advances `receipt_validation_status`.

**PackTrack side:**

- Migration: add `stock_movement` table (`kind`, `delta_quantity`,
  `event_time`, `actor_kind`, `reference`).
- Backfill: compute `Item.current_stock` from existing
  `box_receipts.accepted_quantity` per `material_code`.
- Add `POST /api/luma/consumption-events` accepting Luma payloads
  with `idempotency_key`. Writes `stock_movement` rows of kind
  `CONSUMPTION` and updates `Item.current_stock`.
- Records evidence against affected receipts using the
  oldest-non-fully-validated-first (FIFO) rule from
  [`PACKTRACK_CONFIDENCE_MODEL.md`](./PACKTRACK_CONFIDENCE_MODEL.md).
- Keep Zoho `adjust_stock` push for receipts (financial / COGS);
  do **not** push consumption to Zoho.

**Luma side:** flip the Phase 4 job from "build and queue" to
"build, queue, and POST".

**What NOT to touch in Phase 5:** legacy `receive_shipment` route
(decision deferred to Phase 6); Luma production tables.

### Phase 6 — Forecast / reconciliation polish (Luma + minor PackTrack)

**Goal:** combined `forecast_confidence` lives in Luma's view layer;
legacy paths are fenced off or removed.

**Tasks:**

- Luma `forecast_confidence` view layer combines PackTrack
  `receipt_validation_status` (per material) with Luma
  `production_confidence` (per recent runs). Four axes stay
  separate at the data layer.
- Optional small PackTrack endpoint:
  `GET /api/luma/validation-rollup` for forecast cards.
- Decide fate of legacy
  `packtrack/routes/purchase_orders.py::receive_shipment`:
  fence off (e.g. return HTTP 410 for non-OWNER) or remove.
- Decide fate of the PackTrack → Luma push path
  (`packtrack/services/receiving.py::push_luma_receipt`): demote
  to "force push" admin tool, or remove.

**What NOT to touch in Phase 6:** BOM math, lot tables, anything
that would invalidate the boundary doc.

### What NOT to touch at any phase in this project

- Luma's `workflow_events`, `batches`, `finished_lots`,
  `finished_lot_inputs`, `finished_lot_raw_bags`,
  `finished_lot_packaging_lots` writers.
- Luma's `packaging_lots.qtyOnHand` writer logic.
- Luma's production finalization persistence path (Phase 4 only
  builds, queues, never mutates).
- PackTrack's PO state machine in
  `packtrack/services/workflow.py`.
- Telegram, Authentik, Caddy.
- Zoho gateway migration (`P8` below) — independent track, not
  required for separation work.

### Phase dependency graph

```
Phase 0 (docs)
     │
     ▼
Phase 1 (PackTrack read APIs)
     │
     ▼
Phase 2 (Luma pull integration)
     │
     ├──▶ Phase 3 (Luma-initiated material receipts)
     │         │
     │         ▼
     └──▶ Phase 4 (Luma consumption builder, build-only)
                       │
                       ▼
              Phase 5 (PackTrack consumption receiver + ledger)
                       │
                       ▼
              Phase 6 (forecast + legacy cleanup)
```

---

## Historical / current-behavior queue (push-only model)

The sections below are the original P0 → P9 queue. They are
preserved here because they accurately describe **what is running
in production today**. P2–P7 are the push-only direction we have
moved away from; see the Master plan above for the new direction.

Status legend in the section headers below:

- `[done]` — completed historically; still accurate.
- `[done in code, superseded by master plan]` — code shipped, but
  the master plan above (Phase 0–6) is the future direction.
- `[partial]` — code partially shipped under this name; future
  work rolls into the master plan above.
- `[deferred]` — no longer prioritized; may be revisited later.

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

## P2 — Box-level receiving model `[done in code, superseded by master plan]`

> **Status:** schema and route shipped (migration
> `b7c2d8e1f4a9_box_receipts.py`; route
> `POST /po/{po_id}/boxes` and the live
> `packtrack/routes/receiving.py::submit_receiving` flow). The
> single `confidence` enum below is replaced by `receipt_source` +
> `receipt_validation_status` in **Phase 3** of the master plan.

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

## P3 — Luma payload builder `[done in code, superseded by master plan]`

> **Status:** payload builder shipped inline in
> `packtrack/services/receiving.py::push_luma_receipt`. The push
> direction itself is **legacy current behavior** — see Master plan
> § 5 of [`PACKTRACK_LUMA_BOUNDARY.md`](./PACKTRACK_LUMA_BOUNDARY.md).
> No new push features should be added on top of this; new
> integration work follows the master plan.

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

## P4 — Luma dry-run push `[partial, superseded by master plan]`

> **Status:** the `x-packtrack-dry-run: true` header is implemented
> in `push_luma_receipt` but the UI button never landed; the live
> path auto-pushes on receive instead. Treated as legacy current
> behavior; future integration work follows the master plan above.

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

## P5 — Luma live push (manual approval) `[superseded by master plan]`

> **Status:** the live push is shipped, **but** the current code
> auto-pushes on receive rather than gating behind a manual button
> (see `packtrack/routes/receiving.py::submit_receiving` and
> `POST /receive/{zoho_po_id}/retry-luma`). The "manual button only"
> design below is **not** what is running in production. Push is
> retained as legacy behavior; the future integration is pull-first
> per the master plan. Push will be demoted or removed in **Phase
> 6** of the master plan.

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

## P6 — Receiving UI refinement `[partial, superseded by master plan]`

> **Status:** the live receiving form is functional but the
> per-shipment "Add box" wizard described below is not built. Any
> further UI work should align with the new pull-first / two-write
> contract in the master plan rather than the original push-only
> design.

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

## P7 — Luma shortage recommendations inbound `[deferred]`

> **Status:** deferred indefinitely. Luma has an outbound client
> (`lib/integrations/packtrack/recommendations.ts`) but PackTrack
> never implemented the receiving end and the master plan does not
> bring this back in Phase 0–6. May be revisited as an additive
> enhancement after Phase 6 ships.

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

## P9 — UI polish `[deferred]`

> **Status:** deferred. Should not start until the master plan
> (Phase 0–6) is at least through Phase 5; otherwise UI work risks
> being thrown away as the boundary changes.

**Goal:** only after P1–P8 are landed and the backend contract is safe.
Anything visual/CLS/UX here is fair game; until then, hold.

---

## Historical phase dependency graph (push-only model)

> Historical reference only. New work follows the **Phase 0 → Phase 6**
> graph in the Master plan section at the top of this document.

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

P7 deferred. P8 (Zoho gateway) remains a valid independent track and
is unaffected by the separation work.
