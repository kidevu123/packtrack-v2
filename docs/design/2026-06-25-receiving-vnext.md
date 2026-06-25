# PackTrack Receiving vNext — Design

**Status:** Design only — no code, no deploy, no tag.
**Author:** PackTrack working session, 2026-06-25.
**Preserves:** v2.4.1 Luma idempotency contract (see `docs/PACKTRACK_LUMA_CONTRACT.md` § 7 / § 8).

---

## 0. Locked design decisions

These decisions are settled for v1. Re-opening any of them requires an explicit follow-up doc.

1. **Case-first domain model.** Receiving is modeled as:
   - `Receive` (header / one delivery event)
   - `ReceiveCase` (one vendor-labeled carton)
   - `ReceiveCaseLine` (item rows inside each case)
   - `BoxReceipt` **remains the integration leaf**, created at finalize. It is the row Zoho and Luma actually see.
2. **v1 is single-PO only in the UI.** The schema is future-ready for multi-PO (`Receive.purchase_order_id` is nullable; each `ReceiveCaseLine` carries its own `purchase_order_id`), but the UI does not expose multi-PO until a later stage.
3. **Vendor case numbers are permissive free text.** Examples: `1`, `C-001`, `BOX-A-7`. Stored as `varchar(120)`. No numeric-only validation.
4. **Over- and under-counts are warnings, not blockers.** Finalize is allowed with operator confirmation. Both cases emit a `POEvent` for the audit trail.
5. **Finalize blockers for v1** (each prevents finalize until resolved):
   - Parcel-mode shipment with no tracking number.
   - Case with zero item lines.
   - Line missing item.
   - Line with `declared_quantity <= 0`.
   - Missing vendor case number on any case.
6. **Packing list v1: one primary packing list attachment per Receive.** Modeled via `Receive.packing_list_attachment_id → attachments.id` with new `AttachmentKind.PACKING_LIST`. Future support for multiple attachments is anticipated; the schema only needs a relaxation of that single FK (e.g. join table) when the time comes.
7. **Feature flag `RECEIVING_VNEXT_ENABLED`, default OFF.** The legacy receive flow (`POST /receive/{zoho_po_id}`) remains the default until vNext is proven in production. The operator-typed supplier-carton flow (`POST /po/{id}/boxes`) is unaffected by the flag.
8. **v2.4.1 Luma semantics preserved verbatim.**
   - PackTrack receiving idempotency uses `submission_id + submission_line_index` on `box_receipts`, propagated from `Receive.submission_id` at finalize.
   - Luma compatibility `box_number = "PT-{packtrack_receipt_id}"` is kept at the `BoxReceipt` leaf.
   - Vendor case number is **never** sent as Luma's `box_number`. The two fields are semantically distinct: `ReceiveCase.vendor_case_number` is the supplier's carton label; Luma's `box_number` is the PT-side dedup mirror.
9. **Receive number format: `R-YYYY-NNNN`.** Server-generated from a yearly sequence (e.g. `R-2026-0042`). Stable, sortable, quotable by operators. No operator override.
10. **Permissions:** `OWNER` and `RECEIVING` have read + write on receives. `DESIGN` is read-only if it falls out cleanly; otherwise hidden in v1. No change to existing role plumbing.
11. **Photos are per-line in v1.** A `ReceiveCaseLine` may carry zero or more photos (stored in `photo_paths` JSON, mirroring today's `BoxReceipt.photo_paths`). Per-case (whole-carton) and per-receive (whole-pallet) photo slots are deferred to a later stage.
12. **Item search is scoped to the PO's lines in v1.** The HTMX type-ahead at `GET /receive/v2/<id>/items-search?q=…` returns only items present on `Receive.purchase_order_id`'s `po_lines`. No vendor-wide or global fallback in v1.
13. **Concurrency: optimistic locking via `Receive.updated_at`.** Every PATCH/finalize carries the loaded `updated_at` as a token; the server rejects stale writes with a banner ("Receive was updated by someone else — reload"). No per-row locks, no DB-level row-version columns.
14. **Carrier is free text in v1.** Plain `varchar(120)`. A curated carrier list is a later UX polish, not a v1 requirement.

---

## 1. Current-state audit

### 1.1 Existing receiving data model

| Table | Role | Receive-relevant fields |
|---|---|---|
| `purchase_orders` | Header of the order | `po_number`, `currency`, `created_by_id`, `zoho_po_id`, `push_status`, FK relationships |
| `po_lines` | Per-item-on-PO ordered quantity | `po_id`, `item_id`, `quantity`, `unit_price`, `target_arrival`, **`received_quantity`** |
| `shipments` | One leg of physical delivery | `po_id`, `item_id`, `method` (express/sea), `quantity`, `received_quantity`, `tracking_number`, `carrier`, `eta`, `shipped_date`, `received_date`, `status`, `notes`, `discrepancy_notes` |
| `attachments` | File store hanging off a PO | `po_id`, `kind` ∈ `{PI, ARTWORK, OTHER}`, `filename`, `file_path`, `external_url`, `uploaded_by_id` |
| `box_receipts` | One supplier carton OR one receive-form line (post-v2.4.1) | `purchase_order_id`, `item_id`, `shipment_id?`, `packtrack_receipt_id` (UUID4, unique), `box_number`, `material_code`/`material_name`/`supplier` (snapshots), `supplier_lot_number`, `declared_quantity`/`counted_quantity`/`accepted_quantity`, `confidence`, `luma_push_status`, `luma_pushed_at`, `luma_response`, `photo_paths`, **`submission_id`** + **`submission_line_index`** (v2.4.1 idempotency), `received_at`, `received_by_user_id`, `notes` |
| `zoho_mirror` | Cached snapshot of Zoho PO + line_items | `zoho_purchaseorder_id`, `purchaseorder_number`, `line_items` JSON |
| `material_consumption_events` | Luma-pushed audit log | downstream of receiving — out of scope here |

### 1.2 BoxReceipt semantics today
- Born as "one supplier carton". The supplier-carton flow `POST /po/{id}/boxes` still treats it that way: operator types a real `box_number`, FK'd to a specific `item_id`, optional `shipment_id`.
- Today's receive-form flow `POST /receive/{zoho_po_id}` (the high-volume path) creates **one BoxReceipt per PO-line**, not per carton. `box_number` for those rows is a stable `PT-{packtrack_receipt_id}` Luma-compatibility mirror — no operator carton information is captured.
- The catchup path generates synthetic `CATCHUP-{po_id}-{item_id}` rows.

So **`BoxReceipt` is already three things wearing the same hat**: a real carton, a receive-form line, and a back-fill stub. The new model must disentangle this without breaking the Zoho / Luma push contracts.

### 1.3 Existing receive form behavior
`GET /receive/{zoho_po_id}` → table of PO lines (one row per line). For each line operator enters: declared qty, optional counted qty, optional lot #, optional photo. No case grouping, no tracking, no packing-list link. Form-level `submission_id` token prevents double-submit (v2.4.1). On submit: one BoxReceipt per line with qty > 0, then push to Luma, then submit each line to `zoho-integration-service`.

### 1.4 Existing Zoho receive behavior
Per BoxReceipt → one `commit_receive` call to `zoho-integration-service`. Idempotency key: `PACK_TRACK_RECEIVE_{packtrack_receipt_id}`. Service returns `committed` / `blocked` (live writes disabled) / `validation_failed` / `gateway_error`. PT records per-line outcome as `POEvent`s. Survived v2.4.1 unchanged.

### 1.5 Existing Luma push behavior
Per BoxReceipt → one POST to `LUMA_RECEIPT_WEBHOOK_URL`. Payload includes `packtrack_receipt_id`, `box_number`, `material_code`, `supplier_lot_number`, `declared_quantity`, `counted_quantity`, `received_at`, `received_by`. Luma dedupes on `(packtrack_receipt_id, box_number)` and requires `box_number: z.string().min(1)` — **the contract we must not break**. Locked by `tests/test_luma_receipt_push.py`.

### 1.6 Existing packing list / upload behavior
**None.** `AttachmentKind` only has `PI / ARTWORK / OTHER`. No packing-list link to a receive session, no structured expectation rows, no upload UI for the receiving flow.

### 1.7 What can be reused
- **`BoxReceipt`** as the *push-to-integrations* leaf record. Don't touch its column layout; just add upward FKs.
- **`Shipment.tracking_number` / `carrier` / `method`** — already models physical delivery; the new Receive header can bind to it optionally.
- **`Attachment`** with a new `AttachmentKind.PACKING_LIST` and a direct FK from `Receive`.
- **`POEvent`** for receive audit trail entries (`kind="receive_started"`, etc.).
- **`packtrack_receipt_id` UUID + `submission_id`** v2.4.1 idempotency — applies cleanly to the new flow by propagating from header into each leaf BoxReceipt at finalize.

### 1.8 What should NOT be stretched further
- Treating one `BoxReceipt` as both "a case" and "an item-line within a case". The case-first model needs its own table for cases.
- `box_number` doing triple duty as carton id / synthetic compat string / dedup proxy. Already cleaned up in v2.4.1; future model must not re-overload it.
- Per-form `submission_id` as the only audit handle. A real Receive session deserves a stable, operator-visible `receive_number`.
- `Shipment` as receive-time mutable state. It remains "what we expect to arrive"; receives are their own concept that may (optionally) bind to a shipment.

---

## 2. Proposed domain model (Option B — new tables above `BoxReceipt`)

### 2.1 Coexistence recommendation: **Option B**

| Option | Verdict |
|---|---|
| **A** Evolve `BoxReceipt` into case-line | Rejected — `BoxReceipt` is the Luma/Zoho push artifact + has v2.4.1 idempotency + has `packtrack_receipt_id` Luma already knows. Reshaping it ripples into both integrations. |
| **B (chosen)** New `Receive` + `ReceiveCase` + `ReceiveCaseLine` above `BoxReceipt`; finalize materializes one `BoxReceipt` per case-line | Smallest blast radius. Integrations untouched until finalize. `BoxReceipt` stays the integration boundary. Reversible per stage. |
| **C** Replace `BoxReceipt` in a later migration | Defer indefinitely. Only revisit if `BoxReceipt` becomes too narrow; v2.4.1 docstrings already accept its dual role. |

### 2.2 New entities

#### `Receive` — receive header / one physical delivery event

| Field | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `receive_number` | str(40), UNIQUE | Human-friendly `R-YYYY-NNNN`; server-generated. Quoted by operators. |
| `purchase_order_id` | int FK → purchase_orders, **nullable** for future multi-PO; **required in v1** at app level | |
| `shipment_id` | int FK → shipments, nullable | Optional binding to an expected shipment leg. |
| `shipment_kind` | enum `parcel` / `palletized` | Drives whether tracking is expected. |
| `tracking_number` | str(120), nullable | Required at finalize when `shipment_kind = parcel`. |
| `carrier` | str(120), nullable | Free-text. |
| `delivery_date` | date | Default `date.today()` at creation; operator-editable. |
| `received_by_user_id` | int FK → users | Creator. |
| `finalized_by_user_id` | int FK → users, nullable | Set at finalize. |
| `status` | enum `draft / counting / review / finalized / pushed_ok / push_failed / cancelled` | State machine in § 3.1. |
| `notes` | text, nullable | |
| `submission_id` | str(64), UNIQUE, nullable | Carries v2.4.1 idempotency upward; one per Receive. |
| `created_at` / `updated_at` / `finalized_at` / `pushed_at` | timestamps | |
| `packing_list_attachment_id` | int FK → attachments, nullable | The single primary packing list for v1. |
| `expected_case_count` | int, nullable | From packing list / operator entry. |
| `expected_case_range` | str(40), nullable | Free text like `C-001..C-040`. |

UNIQUE: `receive_number`, `submission_id`. Index on `purchase_order_id`.

#### `ReceiveCase` — one vendor-labeled carton / case in this receive

| Field | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `receive_id` | int FK → receives | ON DELETE CASCADE. |
| `vendor_case_number` | str(120), nullable | Vendor's label. Permissive free text (`1`, `C-001`, `BOX-A-7`). Nullable while drafting; **required at finalize** (decision § 0.5). |
| `sequence` | int | 1-based ordering within the receive (UI sort). |
| `case_kind` | enum `master_case / display_case / pallet / loose / other`, nullable | Optional. |
| `notes` | text, nullable | |
| `created_at` / `updated_at` | | |

**Partial UNIQUE** `uq_receive_cases_receive_case_number` on `(receive_id, vendor_case_number) WHERE vendor_case_number IS NOT NULL` — prevents accidental duplicate case numbers within one receive while allowing NULLs during drafting.

#### `ReceiveCaseLine` — item-level qty within a case

| Field | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `receive_case_id` | int FK → receive_cases | ON DELETE CASCADE. |
| `purchase_order_id` | int FK → purchase_orders | Required so multi-PO is unambiguous at the leaf even in v1. |
| `po_line_id` | int FK → po_lines, nullable | Specific line if operator picks one. |
| `item_id` | int FK → items | Required. |
| `declared_quantity` | float | Required. |
| `counted_quantity` | float, nullable | If physically counted. |
| `accepted_quantity` | float | Set at finalize; defaults to `counted_quantity` if present else `declared_quantity`. |
| `unit_of_measure` | str(20) | Defaults from `Item.unit`. |
| `supplier_lot_number` | str(120), nullable | Per-line lot (a case may contain multiple lines with different lots). |
| `photo_paths` | JSON, nullable | Reuses the v2.4.1 SQLite-variant JSON column pattern. |
| `notes` | text, nullable | |
| `box_receipt_id` | int FK → box_receipts, nullable | **Populated only at finalize** when the leaf `BoxReceipt` is materialized. |
| `created_at` / `updated_at` | | |

#### `PackingList` (v1) — reuse `Attachment` with a new kind
- Add `AttachmentKind.PACKING_LIST`.
- `Receive.packing_list_attachment_id` is a direct FK for the single primary attachment (decision § 0.6).
- Future support for multiple attachments will introduce a join table; the v1 schema is migrate-compatible because the join can derive its first row from the FK.

#### `PackingListLine` (deferred, v2.7+)
Structured expected-quantity rows for vendor-uploaded packing lists. Not in v1; called out only so the v1 layout doesn't paint us into a corner.

### 2.3 BoxReceipt evolution (Option B specifics)
- **Add nullable FK** `box_receipts.receive_case_line_id → receive_case_lines.id`.
- **Add nullable FK** `box_receipts.receive_id → receives.id` (denormalized for cheap "receipts on this receive" queries; set at finalize).
- **Never backfill historical BoxReceipts.** Old rows keep NULL; the new tables are forward-only.
- The Luma push payload for new-flow rows is **identical at the wire**, so `services/receiving.py::push_luma_receipt` is unchanged.
- `submission_id` and `submission_line_index` propagate from `Receive` → leaf `BoxReceipt` at finalize. v2.4.1 idempotency still guards finalize and any retry.

### 2.4 Audit fields
- Every new table gets `created_at` + `updated_at`.
- Every state transition on `Receive` emits a `POEvent` (kinds: `receive_started`, `case_added`, `receive_finalized`, `receive_over_count`, `receive_short`, `receive_push_failed`, `receive_pushed_ok`, etc.).
- No Zoho / Luma side effects happen during DRAFT, COUNTING, or REVIEW.

---

## 3. Operator workflow

### 3.1 State machine

```
        ┌────────┐  add cases /     ┌─────────┐  ready?  ┌────────┐
START → │ DRAFT  │ ──── counting ──▶│COUNTING │ ───────▶ │ REVIEW │
        └────────┘                  └─────────┘          └────────┘
            │                            │                   │
            │ (operator may cancel)      │                   │ finalize
            ▼                            ▼                   ▼
        ┌──────────┐                                    ┌──────────┐
        │CANCELLED │                                    │FINALIZED │
        └──────────┘                                    └──────────┘
                                                              │
                                       ┌──────────────────────┴──────────────────────┐
                                       ▼                                             ▼
                              push Luma + Zoho                                   push fails
                                       │                                             │
                                       ▼                                             ▼
                                ┌─────────┐                                  ┌────────────┐
                                │PUSHED_OK│                                  │PUSH_FAILED │── retry ─▶ FINALIZED
                                └─────────┘                                  └────────────┘
```

### 3.2 The 15-step flow

1. **Start receive from a PO** → `POST /receive/v2/new?po_id=…` creates `Receive(status=DRAFT, submission_id=<uuid>, purchase_order_id=…, received_by=…, delivery_date=today)`. Operator lands on `/receive/v2/<id>`.
2. **Attach/view packing list** → Right rail shows `Attachment(kind=PI)` already on the PO + a button "Attach packing list" → drag-drop uploader. Creates an `Attachment(kind=PACKING_LIST)` and sets `Receive.packing_list_attachment_id`.
3. **Select shipment kind** → Toggle at top: Parcel vs Palletized. Drives whether the tracking-number field is shown / required.
4. **Enter expected case count / range** → Optional inputs in the right rail. Stored on `Receive`.
5. **Add/select case** → "Add case" creates `ReceiveCase(vendor_case_number=…, sequence=N)`. Operator may leave `vendor_case_number` blank while drafting; finalize requires it.
6. **Add item lines inside that case** → Expandable case block. "Add line" creates a `ReceiveCaseLine`.
7. **Searchable item dropdown populated from PO items** → HTMX-fed combobox scoped to the PO's lines. Picking auto-fills `po_line_id`, `item_id`, `unit_of_measure`.
8. **Enter quantity** → `declared_quantity` required, `counted_quantity` optional.
9. **Live totals by item** → Right rail computes `Σ accepted_quantity` per item across all cases (HTMX refresh after each save).
10. **Expected vs counted** → Compare line totals against `po_line.quantity - po_line.received_quantity` (and, when present, against `packing_list_lines.expected_quantity`). Show ✓ / over / under badges.
11. **Review exceptions** → REVIEW state surfaces: cases without `vendor_case_number`, items over-received, items under-received, items missing `material_code` (Luma push will mark `NOT_READY`), tracking missing on parcel mode.
12. **Finalize** → Button enabled only when REVIEW reports no blockers (§ 0.5). On finalize, inside one DB transaction:
    - For each `ReceiveCaseLine`, materialize a `BoxReceipt` with:
      - `packtrack_receipt_id = uuid4()`
      - `submission_id = Receive.submission_id`
      - `submission_line_index = <global line index>`
      - `box_number = "PT-{packtrack_receipt_id}"` (v2.4.1 contract)
      - `receive_id = Receive.id`
      - `receive_case_line_id = line.id`
      - snapshot of `material_code`, `material_name`, `supplier`, lot, qtys, photos
    - `Receive.status = FINALIZED`, `finalized_at = now()`, `finalized_by_user_id = current user`.
    - `line.box_receipt_id = box.id` for each line.
    - Emit `POEvent(kind="receive_finalized")`.
13. **Push to Zoho** → On finalize, run existing `submit_zoho_receives` per `BoxReceipt`. Idempotency key `PACK_TRACK_RECEIVE_{packtrack_receipt_id}`. Behavior unchanged.
14. **Push to Luma** → On finalize, run existing `push_luma_receipt` per `BoxReceipt`. Payload unchanged. Luma dedup contract preserved.
15. **Show success / failure / retry** → Per-case + per-line status. Push failures → `Receive.status = PUSH_FAILED`. "Retry push" re-runs only unpushed/failed leaves (reuses today's retry-luma logic at the `BoxReceipt` level).

### 3.3 Edge cases — explicit handling

| Edge case | Handling |
|---|---|
| **Case with multiple items** | One `ReceiveCase`, many `ReceiveCaseLine` rows. |
| **Tracking number absent** | If `shipment_kind=parcel`, tracking required at REVIEW. If `palletized`, NULL is fine. |
| **Same item across multiple cases** | Allowed. Right-rail totals roll up. |
| **Same vendor case number accidentally reused** | Partial UNIQUE rejects the second case at INSERT. UI: "Case X already exists — add lines under it?" with a scroll-to-existing button. |
| **Quantity over expected** | Soft warning at REVIEW (yellow). Finalize allowed with confirmation modal. Logged as `POEvent(kind="receive_over_count")`. |
| **Quantity under expected** | Soft warning + confirm. Logged as `POEvent(kind="receive_short")`. Remainder stays "open" on the PO line. |
| **Missing material code** | Soft warning (Luma push will mark `luma_push_status=NOT_READY`). Finalize allowed; operator runs `retry-luma` after fixing the code. |
| **Multi-PO receive (deferred to v2.7+)** | v1 enforces `Receive.purchase_order_id` is set and every case-line's PO matches it. Schema-ready for relaxation later. |
| **Vendor packing list uploaded before receiving** | Allowed: vendor or operator can `POST /po/{id}/attachments` with `kind=PACKING_LIST` ahead of time. When the Receive is started, the right rail surfaces the most-recent `PACKING_LIST` attachment on the PO and offers to bind it. |

---

## 4. UI concept

### 4.1 Page layout

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Receive R-2026-0042 · PO-00263 · Hyroxi MIT-B Sweet Trip · draft  · Cancel ▾ │
├────────────────────────────────────────┬─────────────────────────────────────┤
│  ┌─ Case C-001 (master) ─────────  ⌄ ┐ │  Packing list                       │
│  │ [+ Add line]                     │ │   📎 sweettrip_packing_list.pdf     │
│  │ ─────────────────────────────────┤ │   View · Replace                    │
│  │ Item          Lot     Decl  Cnt  │ │                                     │
│  │ ┃ Blister 4ct  SL-001  500  500✓ │ │  Tracking                           │
│  │ ┃ Display      —       250  —    │ │   ◉ Parcel  ○ Palletized            │
│  │ + Add another line               │ │   1Z12345... | FedEx                │
│  └──────────────────────────────────┘ │                                     │
│  ┌─ Case C-002 ────────────────  ⌄ ┐ │  Cases: 2 of expected 5             │
│  │ [+ Add line]                     │ │  Expected range C-001..C-005        │
│  │ ┃ Blister 4ct  SL-002  500  500  │ │                                     │
│  └──────────────────────────────────┘ │  Totals by item                     │
│                                       │  Blister 4ct  1,000 / 4,000  ◉ Under│
│  [+ Add case]                         │  Display       250 /   500  ◉ Under│
│                                       │  Label         —  /   500  ⚠ None  │
│                                       │                                     │
│  Status: counting → REVIEW available  │  Validation                         │
│         when at least 1 case has 1 ln │  ⚠ Case C-002 missing label         │
│                                       │  ⚠ Display under-shipped (250/500)  │
│                                       │  ✓ All cases have material codes    │
│ ┌──────────────────────────────────┐  │                                     │
│ │  Save draft        [Review →]    │  │  [Finalize & push]  (disabled)      │
│ └──────────────────────────────────┘  │                                     │
└────────────────────────────────────────┴─────────────────────────────────────┘
```

### 4.2 Components
- **Page header**: reuses `_partials/ui.html::page_header`; receive number, primary PO, status badge, owner-only Cancel.
- **Case block**: vendor case number input, case kind dropdown, optional notes, line table, "+ Add line". Collapses to one-row summary when not focused.
- **Line row**: item combobox (HTMX type-ahead), lot input, declared qty, counted qty, optional photo, inline validation.
- **Right rail** (sticky, hidden on mobile): packing list slot, tracking section, expected case progress, item totals (under/over coloring), validation warnings.
- **Action bar**: Save draft + Review.
- **Review modal/page**: lists blocking issues, soft warnings, sums; Finalize button enabled only when no blocking issues.
- **Result page** (after finalize): per-case, per-line Luma/Zoho status; retry button for failed pushes.

### 4.3 Key interactions
- **Item picker**: HTMX `GET /receive/v2/<id>/items-search?q=…` scoped to the PO. Returns option list. Picking sets `po_line_id`, `item_id`, `unit_of_measure`.
- **Add case**: HTMX `POST /receive/v2/<id>/cases` returns the new case block.
- **Add line**: HTMX `POST /receive/v2/<id>/cases/<case_id>/lines` returns the new line row.
- **Inline edit**: HTMX `PATCH /receive/v2/<id>/lines/<line_id>` returns the row + the right-rail totals fragment.
- **Delete line/case**: HTMX `DELETE` returns the trimmed list + recalculated totals.
- **Save draft**: form POST carrying `submission_id` (v2.4.1 token, idempotent at the header level).
- **Finalize**: form POST with explicit confirmation token + `submission_id`. Server enforces blockers + executes finalize transaction.

### 4.4 Validation behavior
- **Blocking** (finalize disabled, banner at top — per § 0.5):
  - Tracking missing on parcel mode.
  - Case with zero lines.
  - Line with declared qty ≤ 0 or no item.
  - Vendor case number missing on any case.
- **Soft warnings** (visible, finalize allowed, confirm modal):
  - Over-count vs PO line remaining.
  - Under-count vs packing list / PO line.
  - Missing `material_code` on item.
  - Duplicate vendor case number (UI catches before INSERT).

### 4.5 HTMX vs form-post split
- **HTMX**: case/line CRUD, item search, inline qty edits, right-rail totals re-render. Row-level idempotent.
- **Forms**: receive creation, save draft, finalize. All carry the v2.4.1 `submission_id` token.

### 4.6 Deferred to later stages
- Structured `packing_list_lines` (vendor-typed expectations).
- Multi-PO receive UI.
- Photo annotations / per-case (vs per-line) photos.
- Barcode / scanner integration ("scan → next case").

---

## 5. Integration mapping

### 5.1 Finalize-time materialization (per `ReceiveCaseLine`)

```python
for line in receive.iter_all_case_lines():
    box = BoxReceipt(
        packtrack_receipt_id=str(uuid.uuid4()),
        purchase_order_id=line.purchase_order_id,
        shipment_id=receive.shipment_id,
        item_id=line.item_id,
        material_code=line.item.material_code,           # snapshot
        material_name=line.item.name[:240],              # snapshot
        supplier=line.item.vendor,                       # snapshot
        supplier_lot_number=line.supplier_lot_number,
        box_number=_luma_compat_box_number(receipt_id),  # "PT-{uuid}" (v2.4.1)
        submission_id=receive.submission_id,
        submission_line_index=line.global_index,
        declared_quantity=line.declared_quantity,
        counted_quantity=line.counted_quantity,
        accepted_quantity=line.accepted_quantity,
        unit_of_measure=line.unit_of_measure,
        confidence=Confidence.HIGH if line.counted_quantity is not None else Confidence.MEDIUM,
        received_by_user_id=receive.received_by_user_id,
        received_at=receive.delivery_date,
        luma_push_status=compute_luma_readiness(line.item.material_code),
        photo_paths=line.photo_paths,
        notes=line.notes,
        receive_id=receive.id,
        receive_case_line_id=line.id,
    )
    session.add(box)
    line.box_receipt_id = box.id
```

### 5.2 Zoho payload
Unchanged at the wire. Existing `submit_zoho_receives(mirror, [ZohoReceiveSubmission(...) per BoxReceipt])` walks the new `BoxReceipt` rows exactly as today. `zoho_line_item_id` comes from `line.po_line.zoho_line_item_id` (already plumbed through the mirror).

### 5.3 Luma payload
**Unchanged at the wire.** `push_luma_receipt(box, …)` runs per finalized `BoxReceipt`. v2.4.1 contract preserved verbatim:
- `box_number` stays `PT-{packtrack_receipt_id}`.
- Luma dedupes on `(packtrack_receipt_id, box_number)`.
- `submission_id` + `submission_line_index` remain the PT-side idempotency mechanism, now propagated from `Receive`.
- Vendor case number is **never** sent to Luma as `box_number` (decision § 0.8).

### 5.4 PackTrack internal stock
No change. Receiving does not decrement PT stock; Luma decrements on consumption.

### 5.5 Audit trail / events
- `POEvent(kind="receive_started", payload={receive_id, receive_number})`
- `POEvent(kind="case_added", payload={receive_id, vendor_case_number})`
- `POEvent(kind="receive_finalized", payload={receive_id, line_count, total_qty_by_item})`
- `POEvent(kind="receive_pushed_ok" | "receive_push_failed", payload={…})`

### 5.6 What does NOT change in this design
- `LUMA_RECEIPT_WEBHOOK_URL` and `LUMA_PACKTRACK_SECRET` env vars.
- Luma's `z.string().min(1)` `box_number` requirement (still papered over with `PT-{receipt_id}`).
- The Zoho integration service contract.
- The legacy `POST /po/{id}/boxes` supplier-carton flow.
- The catchup back-fill.

---

## 6. Migration / rollout plan

| Stage | Version | Scope | Reversibility |
|---|---|---|---|
| **0** | (this doc) | Design only — committed to `docs/design/`. | n/a |
| **1** | **v2.5.0** | Add `receives`, `receive_cases`, `receive_case_lines` tables + `AttachmentKind.PACKING_LIST` + nullable `receive_id` / `receive_case_line_id` columns on `box_receipts`. Build the **draft + counting** UI behind `RECEIVING_VNEXT_ENABLED` (default OFF). Legacy receive remains the default. No Zoho/Luma push change. | Fully reversible — flag off + drop new tables. |
| **2** | **v2.5.1** | Add **REVIEW** state UI: totals, expected vs counted, blocker/warning detection. Still no integration push. | Reversible. |
| **3** | **v2.6.0** | Wire **finalize** to materialize `BoxReceipt`s and push to Zoho + Luma. Feature-flagged side-by-side with the legacy form for one release. | Reversible per-receive (cancel + use legacy). |
| **4** | **v2.6.1** | Flip default to vNext; keep legacy as `/receive/legacy/<zoho_po_id>` for one more release. | Reversible by flipping flag. |
| **5** | **v2.7.0** | Vendor uploads packing list (drag-drop UI for vendors) + structured `packing_list_lines`; expected-vs-actual badges become real. | Additive. |
| **6** | **v2.7.1** | Multi-PO receive support — relax header constraint; UI lets operator pick PO per case-line. | Additive. |
| **7** | **v2.8.0+** | Retire legacy `/receive/<zoho_po_id>` and possibly the `POST /po/{id}/boxes` supplier-carton flow if absorbed cleanly. | One-way; requires explicit approval. |

Each stage ships with: migration, tests, smoke (existing `scripts/smoke_test.sh`), and contract-doc update.

---

## 7. Risks / blind spots

- **Item search performance at type-ahead speed** — `Item.name = Field(index=True)` covers exact lookups but not `LIKE '%foo%'`. Mitigation: scope search to the PO's lines first (small set); only fall back to full-text on demand.
- **Concurrent edits to the same draft** — two operators opening the same receive on different devices. Pragmatic v1: optimistic locking via `Receive.updated_at` token on the form; reject stale edits with a banner.
- **Photo storage** — receive can accumulate many photos per line. Reuses `uploads/receiving/`; needs disk-space monitoring (P1, not blocking).
- **Receive cancellation after partial finalize** — if Zoho push lands but Luma fails, "cancel receive" is ambiguous. Decision: cancellation only allowed pre-finalize. Post-finalize: retry or operator-marked-resolved only.
- **Migration of in-flight receives during release** — operators mid-form when v2.5.0 deploys. Mitigation: flag-gated default; operators finish their current legacy form before switching.
- **`Receive.purchase_order_id` nullability** — schema is multi-PO-ready, but app-level v1 must require it on create. Test should lock this.
- **Two paths to `BoxReceipt`** — existing `POST /po/{id}/boxes` (operator carton) + new finalize path. Both must converge on Luma push semantics; mostly already aligned via v2.4.1.

---

## 8. Open questions for the user

*(All v1 ergonomic questions previously listed here have been answered and are now locked decisions in § 0.9–§ 0.14. None remain open as of 2026-06-25.)*

---

## 9. Recommended first implementation prompt (for v2.5.0)

> Implement v2.5.0 stage 1 of the Receiving vNext design (`docs/design/2026-06-25-receiving-vnext.md` § 6, stage 1). Scope: add Alembic migration for `receives`, `receive_cases`, `receive_case_lines` per § 2.2; add `AttachmentKind.PACKING_LIST`; add nullable `receive_id` + `receive_case_line_id` columns on `box_receipts`. Add SQLModel models with the column / index / partial-unique / status-enum semantics in the design. Add a feature flag `RECEIVING_VNEXT_ENABLED` (default OFF). Build the draft + counting UI behind that flag at `/receive/v2/...` with HTMX-driven case/line CRUD and item search scoped to the PO. **Do not** wire finalize, Zoho push, or Luma push in this stage — those are v2.6.0. Tests for: single alembic head + new revision in chain (extend `tests/test_alembic_chain.py`), model column shape, route auth, HTMX add/edit/delete case + line, item search scoped to PO, idempotency token on draft save. Legacy receive flow remains default. No deploy in the implementation pass; report and await deploy approval.

---

## 10. Hard rules observed in this design pass

- No code changes.
- No deploy.
- No tag.
- No live Luma / Zoho payloads.
- No secrets exposed.
- Case-first model with a leaf `BoxReceipt`, **not** a flat spreadsheet table.
- v2.4.1 Luma idempotency (`submission_id` + `submission_line_index` + `PT-{receipt_id}` `box_number` mirror) preserved verbatim and explicitly carried into the new model.
