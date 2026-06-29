# Current Phase Status

## v2.10.0 — Inventory adjustment → Zoho sync through zoho-integration-service v1.34.0 (feature branch, NOT yet deployed)

| | |
|---|---|
| **Branch** | `feature/inventory-adjustment-zoho-sync` (off `origin/main`, in a separate worktree so the master-data editor branch `feature/inventory-masterdata-editor-v2.8.0` @ `b643b4e` stays untouched). |
| **Alembic head** | `h4i5j6k7l8m9` (advances from v2.9.0's `g3h4i5j6k7l8`). Adds two nullable columns to `inventory_adjustments`. Additive only. |
| **Service contract** | `POST {ZOHO_INTEGRATION_BASE_URL}/zoho/pack_track/items/{zoho_item_id}/inventory-adjustments`, Bearer + X-Brand + Idempotency-Key. |
| **Status** | Code complete; tests green (382 passed; +24 over v2.9.0's 358); **not merged, not deployed, not tagged**. |

**v2.10.0 scope** — wires the v2.9.0 immutable adjustment ledger to the deployed zoho-integration-service v1.34.0 endpoint. PackTrack remains the source of truth: local commit happens first, and a failed Zoho sync NEVER rolls back the local stock.

* **Three-module architecture** so the source-level guards stay clean:
  * `packtrack/services/inventory_adjustments.py` — **unchanged from v2.9.0**, still no httpx / no Zoho / no OAuth import. Pure local ledger.
  * `packtrack/services/zoho_adjustment_client.py` — **new**, the only PackTrack module that makes an HTTP call related to adjustments. Hits exactly one URL pattern. Returns a `SyncOutcome`.
  * `packtrack/services/inventory_adjustment_sync.py` — **new** orchestrator. Decides whether to call the client (config + zoho_item_id gate), persists the outcome on the row, increments `sync_attempt_count`. Refuses to re-push a row already in `SYNCED`.
* **Adjustment-submit route** now calls `try_sync_adjustment` immediately after `create_adjustment` returns. Status transitions: `NOT_CONFIGURED` (config off) / `PENDING` → `SYNCED` (service ok=true) / `FAILED` (HTTP error / timeout / 4xx / 5xx) / `SKIPPED` (item has no `zoho_item_id`).
* **Retry route** — `POST /inventory/adjustments/{id}/sync`, owner-only. Reuses the same `idempotency_key` from the original attempt so the integration service deduplicates safely. No-op on rows already `SYNCED`. Visible as a "Retry" button in the per-item and global history tables for rows in `FAILED` / `PENDING` / `NOT_CONFIGURED` / `SKIPPED`.
* **Drift warning surfaced, never hidden** — `STOCK_DRIFT_DETECTED` from the service is stored in the new `zoho_sync_warning` column and rendered as an amber "⚠ …" line beside the sync status. The adjustment is still marked SYNCED if the service responded `ok=true` (the post happened upstream; the warning is just signal for the operator to investigate).
* **Sync metadata migration** (`h4i5j6k7l8m9`, additive):
  * `zoho_sync_warning TEXT NULL` — for `STOCK_DRIFT_DETECTED` and any future non-fatal signal
  * `sync_attempt_count INTEGER NOT NULL DEFAULT 0` — bumps on every real attempt (initial + every retry), surfaced as "×N" in history
* **Network discipline** — Decimal quantities are sent as 4-decimal-place strings via `_decimal_str`; no float ever crosses the wire. The payload is a closed dict that names exactly the contract fields — no `vendor`, `price`, `sku`, `sku_code`, `account`, `account_id`, `tags`, `category`, `stock_override`, `name`, `material_code`, or `unit`. Bearer is logged only by the integration service, never by PackTrack (verified by a guard test that ensures the token doesn't show up in the stored `zoho_sync_error`).
* **Existing v2.9.0 contract preserved** — `services/inventory_adjustments.py` still imports no Zoho/OAuth/HTTP client symbol (the v2.9.0 guard test passes unchanged). The new client lives in a separate module and is the only door to the network.

**Tests (+24 cases, total 382)** in `tests/test_v2_10_0_adjustment_zoho_sync.py`: config disabled → no HTTP + NOT_CONFIGURED · happy SYNCED path · exact payload shape (URL / Bearer / X-Brand / Idempotency-Key / 4dp strings / no master-data fields) · `build_payload` Decimal-only assertion · no-`zoho_item_id` → SKIPPED · HTTP 401/403/404/409/422/500 → FAILED with the right substring · timeout → FAILED · idempotent replay (`meta.idempotent=true`) → SYNCED with reference · STOCK_DRIFT_DETECTED warning stored + still SYNCED · owner retry of FAILED row → SYNCED via the route · retry reuses the same `idempotency_key` · non-owner retry 403 · retry on SYNCED row is a no-op (no HTTP call) · `sync_attempt_count` increments per real attempt · Bearer never leaks to the stored error · client module imports no `zoho.com` / `oauth` / `access_token` literal · neither new module mentions Receiving · master-data fields unchanged after a sync round-trip · `is_configured()` requires all four flags · `push_adjustment_to_zoho` raises on programmer-error item id mismatch.

**Hard contract preserved.** No Receiving file changed. PackTrack still never calls Zoho directly. The integration service is the sole seam. Local stock is never rolled back on Zoho failure. Adjustment rows remain immutable — there is still no PATCH / PUT / DELETE route for the row data itself; the retry route only writes to the sync-status columns.

---

## v2.9.0 — Inventory adjustments ledger: PackTrack as local source of truth (deployed + tagged via merge into main)

| | |
|---|---|
| **Branch** | `feature/inventory-adjustments-ledger` (off `origin/main`, in a separate worktree so the v2.8.0 inventory/master-data editor WIP stays untouched). |
| **Alembic head** | `g3h4i5j6k7l8` (advances from v2.7.5/v2.7.6's `f2g3h4i5j6k7`). Adds `inventory_adjustments` only — additive, safe. |
| **Feature flag** | None — this is the new canonical adjust path. The future Zoho push is gated by `ZOHO_INTEGRATION_ADJUST_ENABLED` (default OFF). |
| **Status** | Code complete; tests green (358 passed; +25 over v2.7.6's 333); **not merged, not deployed, not tagged**. |
| **Version note** | v2.8.0 is intentionally skipped — that slot is reserved for the active master-data editor branch (`feature/inventory-masterdata-editor-v2.8.0` @ `b643b4e`). v2.9.0 is the new feature line for inventory adjustments. |

**v2.9.0 scope** — PackTrack becomes the operational source of truth for packaging quantity counts. Operators get a sanctioned way to increase / decrease / set the on-hand quantity without anyone editing `Item.current_stock` directly.

* **Immutable movement ledger** — new `InventoryAdjustment(item_id, adjustment_number, mode, direction, quantity_before, quantity_delta, quantity_after, reason_code, notes, created_by_user_id, created_at, source, zoho_sync_status, zoho_sync_error, zoho_synced_at, zoho_reference, idempotency_key, voided_at, voided_by_user_id, void_reason, reversal_of_adjustment_id)`. Adjustment rows are append-only — no PATCH/PUT/DELETE route exists and the service never overwrites an existing row. Corrections are expressed by recording a new "reversal" adjustment that points at the original via `reversal_of_adjustment_id`.
* **Decimal-safe quantities** — `quantity_before` / `quantity_delta` / `quantity_after` are `NUMERIC(18, 4)` so the math is Decimal end-to-end. The existing `Item.current_stock` column stays `DOUBLE PRECISION` for this release (that column lives on the in-flight master-data editor's surface area); the service converts to `float` only at the single Item-write point.
* **Transactional write with row lock** — `services/inventory_adjustments.create_adjustment` selects the item `FOR UPDATE` (no-op on SQLite, real on Postgres), reads `current_stock`, computes the new total in Decimal, inserts the ledger row, writes the new `current_stock`, and commits — all in one DB transaction.
* **Reason allowlist** — `cycle_count_correction`, `damaged`, `lost_missing`, `sample_or_rd_use`, `production_consumption_correction`, `found_extra`, `manual_correction`, `other`. `other` requires non-empty `notes`; UI surfaces human-friendly labels.
* **Validation** — rejects zero delta, rejects negative resulting stock (configurable but defaults to reject), rejects unknown reason codes, rejects `set_quantity` that equals current stock (no-op).
* **Server-side ownership enforcement** — `if user.role != Role.OWNER: 403` on the form GET, the form POST, and the inline buttons (the buttons are also hidden in the template, but the server check is the actual gate).
* **Routes** — `GET /inventory/{id}/adjust`, `POST /inventory/{id}/adjust`, `GET /inventory/{id}/adjustments`, `GET /inventory/adjustments` (global, filter by item id / reason / sync status). Mounted AFTER the inventory router so the static `/inventory/adjustments` path wins over `/inventory/{item_id:int}`.
* **UI entry points** — "Adjust" link in each inventory list row (owner-only), "Adjust quantity" button in the item-detail Stock card (owner-only), "History →" link on the same card (everyone). Adjustment form has mode radios (Increase / Decrease / Set actual counted), quantity input, reason dropdown, notes textarea, inline 400 re-render when validation fails. Per-item and global history pages show timestamp + adjustment number + reason + before / Δ / after + sync status, with the reversal indicator and void marker rendered inline.
* **Zoho sync seam — PackTrack NEVER calls Zoho directly** — `enqueue_or_mark_adjustment_sync()` sets `zoho_sync_status` based on three settings flags (`ZOHO_INTEGRATION_ADJUST_ENABLED` + `ZOHO_INTEGRATION_BASE_URL` + `ZOHO_INTEGRATION_APP_TOKEN`). When all are set: `PENDING` (a future worker will push through zoho-integration-service). Otherwise: `NOT_CONFIGURED`. No HTTP call is made from the adjustment path; the worker is not part of v2.9.0. Source-level guard test verifies the adjustment service imports no Zoho symbol and no HTTP client.

**Tests (+25 cases, total 358)** in `tests/test_v2_9_0_inventory_adjustments.py`: owner vs non-owner perms on GET + POST · increase / decrease / set_quantity math · zero-delta reject · negative-stock reject · invalid-reason reject · `other`-without-notes reject · notes persisted · no PATCH/PUT/DELETE route exists for an adjustment · service never overwrites · failed validation does not change stock · item history page render · global history page render · sync default `NOT_CONFIGURED` · sync flips to `PENDING` when all three integration settings on · `enqueue_or_mark_adjustment_sync` makes no HTTP call (monkeypatched httpx surfaces it as a failure if it did) · adjustment service imports no Zoho/OAuth/HTTP symbol · master-data fields unchanged by adjustment · quantity columns round-trip as Decimal with no float drift · adjustment numbers sequential within year (`ADJ-YYYY-NNNN`) · neither adjustment module touches Receiving.

**Hard contract preserved.** No edit to existing `Item` columns. No Receiving file changed. No Zoho/OAuth import in the new service. No HTTP call at adjustment time. v2.8.0 WIP on `feature/inventory-masterdata-editor-v2.8.0` not touched.

---

## v2.7.6 — Receiving: import expected lines from CSV/text (deployed + tagged via merge into main)

| | |
|---|---|
| **Branch** | `feature/receiving-vnext-v2.7.6-import-expected-lines` (off `origin/main`, in a separate worktree so the v2.8.0 inventory/master-data WIP stays untouched). |
| **Alembic head** | `f2g3h4i5j6k7` (unchanged — no schema changes; import writes into the v2.7.5 `receive_packing_list_lines` table). |
| **Feature flag** | `RECEIVING_VNEXT_ENABLED` remains ON in production. |
| **Status** | Code complete; tests green (333 passed; +27 over v2.7.5's 306); **not merged, not deployed, not tagged**. |

**v2.7.6 scope** — first step beyond manual entry, but still not OCR or PDF parsing:

* **CSV / pasted-text import** — new `POST /receive/v2/{id}/expected-lines/import/preview` parses pasted text OR an uploaded `.csv` / `.tsv` / `.txt` file (1 MiB cap, `utf-8-sig` with `latin-1` fallback) and renders a preview page. **No DB write on preview.** `POST /receive/v2/{id}/expected-lines/import/commit` re-parses the same payload (so browser-side tampering can't bypass matching) and persists only `READY` rows into the existing `ReceivePackingListLine` table with `source='csv_import'`.
* **CSV format** — header-driven with synonym tolerance. Required: one of `{item, item_name, name, sku, material_code}` + one of `{quantity, expected_quantity, qty}`. Optional: `unit`, `vendor_case_number` (or `case` / `case_number`), `note`. Delimiter is auto-detected between comma / tab / semicolon / pipe by header-field count.
* **Matching** — deterministic only, scoped to the receive's PO items: `material_code` exact → `sku_code` exact → item `name` exact → unambiguous substring containment on name. Case-insensitive. Multiple hits → `AMBIGUOUS`; no hits → `UNMATCHED`; non-positive/non-numeric quantity → `INVALID_QTY`. No fuzzy/AI matching.
* **Replace-existing** — optional checkbox on both preview and commit forms. Off by default. When on, the existing `ReceivePackingListLine` rows for this receive are deleted before the ready rows are inserted, and the audit POEvent mentions the replacement count.
* **Audit** — commit emits one `POEvent(kind="receive_expected_lines_imported", message="Imported N packing-list expected lines for Receive R-...; M skipped.")`. Falls back to "1 packing-list expected line" / "0 skipped" when applicable. The activity strip from v2.7.5 already filters this kind in via the same allowlist (no allowlist update needed because `receive_expected_lines_imported` was added at the same time).
* **Reconciliation integration** — imported rows are normal `ReceivePackingListLine` records; the v2.7.5 review reconciliation surfaces them as Short/Over/Missing/Unexpected/Match warnings unchanged. Verified by `test_imported_lines_show_in_review_reconciliation`.
* **UI** — collapsed `<details>` "Import expected lines from CSV or pasted text…" section inside the existing expected-lines card on `/receive/v2/{id}`. Has a textarea, file picker, "Replace existing" checkbox, and an inline sample of the format. Preview page lists every parsed row with status + detail + skip vs commit summary.
* **XLSX intentionally not supported in v2.7.6** — the runtime carries no XLSX library (no `openpyxl`, no `pandas`, no `tablib`) and we're not pulling one in for a single feature. The preview route returns 400 with an explicit message when a `.xlsx` file is uploaded. The follow-up plan is to wait for 2–3 real vendor packing-list samples (CSV vs PDF vs XLSX) before deciding whether to ship a per-format adapter or stay on "operator pastes into the textarea."

**Tests (+27 cases, total 333)** in `tests/test_v2_7_6_expected_line_import.py`: service-level matching (material_code / sku / name exact, substring, ambiguous, unmatched); invalid + zero quantity classification; tab delimiter detection; OWNER vs DESIGN permissions on preview AND commit; flag-OFF blocks both; terminal-status blocks both; pasted CSV preview render; uploaded CSV preview render; preview writes nothing; mixed-row preview classification; commit imports ready / skips bad; replace_existing deletes old rows first; commit emits the summary POEvent; commit creates no BoxReceipts; import service file imports no Zoho/Luma symbol (source-level guard); imported rows surface in the v2.7.5 reconciliation report and on `/review`; XLSX upload rejected with a clear message; empty payload rejected with 400; legacy `/receive` regression; packing-list file upload form regression; manual expected-line CRUD regression.

**Hard contract preserved.** No schema change. No Zoho payload change. No Luma payload change. No finalize semantics change. No real receive data mutation. No PDF parsing. No OCR. No vendor portal. No multi-PO receiving. Feature flag default unchanged. v2.8.0 WIP on `feature/inventory-masterdata-editor-v2.8.0` not touched.

---

## v2.7.5 — Receiving MVP: manual packing-list reconciliation + activity strip (deployed + tagged via merge into main)

| | |
|---|---|
| **Branch** | `feature/receiving-vnext-v2.7.5-mvp-reconciliation` (off `origin/main`, in a separate worktree so the v2.8.0 inventory/master-data WIP in the main repo stays untouched). |
| **Alembic head** | `f2g3h4i5j6k7` (advances from Stage 1's `e1f2a3b4c5d7`). Adds `receive_packing_list_lines` only — additive. |
| **Feature flag** | `RECEIVING_VNEXT_ENABLED` remains ON in production. |
| **Status** | Code complete; tests green (306 passed; +26 over v2.7.4's 280); **not merged, not deployed, not tagged**. |

**v2.7.5 scope** — next spreadsheet-replacement layer, no file parsing yet:

* **Manual packing-list expected lines** — new `ReceivePackingListLine(receive_id, item_id, vendor_case_number?, expected_quantity, unit?, note?, source='manual', created_at, created_by_user_id?)` plus Alembic migration. Operator-entered "what the vendor said is in this shipment". Source is always `"manual"` for now — CSV/PDF/OCR parsing comes after real vendor sample files land.
* **Expected-lines UI on `/receive/v2/{id}`** — add-line form (PO-scoped item select, qty, unit, vendor case#, note), table of current expected lines, per-row delete, OWNER + RECEIVING perms, feature-flag gated. Form/Delete are 409-blocked once the receive enters a terminal status (`finalized`/`pushed_ok`/`push_failed`/`cancelled`) so the audit trail stays honest.
* **Review reconciliation** — new `services/receiving_v2_reconcile.py::build_reconciliation_report` groups by `item_id` and classifies each as `Match` / `Short` / `Over` / `Unexpected` / `Missing` with operator-friendly messages ("Mailer: packing list expected 100 pcs, you counted 95 pcs — short 5 pcs."). Rendered as a card on `/receive/v2/{id}/review`. **Warnings only — never blockers.** `validate_receive_for_finalize` is unchanged; Zoho/Luma payloads still use actual counted `ReceiveCaseLine` totals.
* **Recent activity strip** — `receive_activity()` filters this PO's `POEvent` rows to receive-lifecycle kinds (`receive_packing_list_uploaded`, `receive_marked_test`, `receive_finalized`, `receive_pushed_*`, `receive_expected_line_added`/`_deleted`) so the operator can see "what happened" without digging into the PO page. Rendered on both index and review.
* **UX hints** — `po_item_choices(..., expected_by_item=…)` opt-in adds `expected M unit` to the case-line `<select>` labels when the operator has entered expected lines.

**Tests (+26 cases, total 306)** in `tests/test_v2_7_5_packing_list_reconciliation.py`: migration head pin; row roundtrip; OWNER/DESIGN/flag-off perms; empty + populated table render; delete; read-only after finalize; Match/Short/Over/Unexpected/Missing classification; review card render; `validate_receive_for_finalize` unaffected; CRUD creates no BoxReceipts; CRUD does not change `ReceiveCaseLine`; packing-list upload still reachable; canary banner regression; legacy `/receive` regression; `po_item_choices` expected-label opt-in + opt-out; activity-strip POEvent emission; activity-strip filtering to receive-lifecycle kinds; operator-friendly copy.

**Hard contract preserved.** No Zoho payload change. No Luma payload change. No finalize semantics change. No real receive data mutation. No packing-list file parsing. No OCR. No vendor portal. No multi-PO receiving. Feature flag default unchanged. v2.8.0 WIP on `feature/inventory-masterdata-editor-v2.8.0` not touched.

---

## v2.7.4 — Receiving vNext polish: visible canary banner + remaining-qty labels + vendor fallback (deployed + tagged via merge into main)

| | |
|---|---|
| **Branch** | `feature/receiving-vnext-v2.7.4-polish` (off `origin/main`, in a separate worktree so the v2.8.0 WIP in the main repo stays untouched) |
| **Alembic head** | `e1f2a3b4c5d7` (unchanged — no schema changes in this release) |
| **Feature flag** | `RECEIVING_VNEXT_ENABLED` remains ON in production. |
| **Status** | Code complete; tests green (280 passed; +14 over v2.7.3's 266); **not merged, not deployed, not tagged**. |

**v2.7.4 scope** — focused UI/readability polish from the v2.7.3 readiness pass:

* **Canary/test banner visible everywhere.** Centralized the `[Marked as TEST/CANARY` marker detection in `packtrack/services/receiving_v2.py` (`is_test_receive`, `test_receive_marker_text`) and extracted the banner into a shared partial `templates/receive_v2/_test_banner.html`. Now rendered on `/receive/v2/{id}` (index), `/receive/v2/{id}/review` (review), AND `result.html` (finalize/retry-push/mark) — previously only `result.html` had it, so an operator landing on a marked receive from the list could be unaware.
* **Item-select labels show remaining.** `po_item_choices` now formats labels as `Item · MATERIAL_CODE · N <unit> remaining` (or `… ordered` when no receive info yet, `remaining unknown` when ordered quantity is 0). Remaining is computed from `ZohoMirror.line_items[].quantity_received` when available (mirror is authoritative under vNext), with graceful fallback to `POLine.received_quantity` then `remaining unknown`.
* **Vendor fallback on `/receive` cards.** `receiving_list` route now computes a server-side `vendor_for: {zoho_purchaseorder_id -> vendor_label}` map with priority `mirror.vendor_name` → first non-null `Item.vendor` on the linked PO's lines → `"Vendor unknown"`. Template uses this map and no longer shows `—`.
* **POLine docstring** documents the vNext semantics: `received_quantity` is NOT bumped by the vNext finalize path; reports must use `ZohoMirror.line_items[].quantity_received` or sum `BoxReceipt.accepted_quantity` over finalized receives.

**Tests (+14 cases, total 280)** in `tests/test_v2_7_4_polish.py`: `is_test_receive` + marker text helpers; index banner shown when marked, hidden when unmarked; review banner shown when marked; `po_item_choices` mirror-based remaining qty with unit; POLine fallback when no mirror; zero-ordered → "remaining unknown"; vendor display (mirror name, Item.vendor fallback, "Vendor unknown"); legacy `/receive` regression with flag off; POLine docstring contains "Receiving vNext" + "received_quantity" + ("mirror" or "BoxReceipt"). One pre-existing v2.7.2 mark-test assertion adjusted to a stable substring `"Zoho and Luma records were NOT reversed"` because the v2.7.4 banner copy normalized capital-E to lowercase.

**Hard contract preserved.** No schema change. No Zoho payload change. No Luma payload change. No finalize side effects. No real receive mutations. No packing-list parsing. Feature flag default unchanged. v2.8.0 WIP not touched.

---

## v2.7.3 — Receiving vNext: packing-list attachment + canary mark performed (deployed + tagged via merge into main)

| | |
|---|---|
| **Branch** | `feature/receiving-vnext-v2.7.3-packing-list` (off `origin/main` @ `2d5d1cb`, in a separate worktree so the v2.8.0 WIP in the main repo stays untouched) |
| **Alembic head** | `e1f2a3b4c5d7` (unchanged — `Receive.packing_list_attachment_id` + `AttachmentKind.PACKING_LIST` shipped in Stage 1) |
| **Feature flag** | `RECEIVING_VNEXT_ENABLED` remains ON in production. |
| **Status** | Code complete; tests green (266 passed; +11 over v2.7.2's 255); **not merged, not deployed, not tagged**. |

**v2.7.3 scope**:

* **Canary receive marked as test/canary in production.** Used the v2.7.2 `POST /receive/v2/1/mark-test` route from inside LXC 200 via an authenticated OWNER session cookie (user id=2, Sahil) at 2026-06-26 20:10 UTC. The mark added a marker line to `Receive 1.notes` and emitted **POEvent id=132** (`receive_marked_test`, actor=2, message includes the reason "vNext Stage 2 canary; Zoho PR-00583 and Luma lot 995751ce-6d85-45cc-a772-8fe775699ec7 intentionally left in place"). `BoxReceipt 139` remained intact with `luma_push_status=pushed`. **No external Zoho/Luma API was called** during the mark (verified by journal grep for `luma|zoho-integration|httpx` in the operation's time window).
* **Packing-list attachment upload + display** wired for Receiving vNext. One primary packing list per Receive (v1), stored as a regular `Attachment(kind=PACKING_LIST)` row pointed at by `Receive.packing_list_attachment_id`. Upload route `POST /receive/v2/{id}/packing-list` (OWNER + RECEIVING, flag-gated). Replace updates the pointer and bumps the attachment version; old attachment row is kept for audit. Files stored under `UPLOAD_DIR/packing_list/<PO_PL_<hex>>.<ext>`. Allowed extensions: PDF, CSV, XLS/XLSX, JPG, JPEG, PNG, WebP, HEIC. **No parsing**, **no expected-vs-actual reconciliation**, **no Zoho/Luma changes**, **no finalize/BoxReceipt mutations**.
* **Receive page UI**: right-rail "Packing list" card shows empty state with upload form, or attached filename + View link + Replace form when attached. Reuses existing `/uploads/<rel_path>` static mount.

**Tests (+11 cases, total 266)** in `tests/test_v2_7_3_packing_list.py`: flag-off → 404; OWNER + RECEIVING happy paths; DESIGN forbidden; Attachment row creation + pointer set + correct `kind`/`po_id`/`version`/`source`/`uploaded_by_id`; receive page renders attached state + replace form; replace swaps pointer + keeps old row + bumps version; disallowed extension rejected; receive without PO rejected; legacy `/receive/{zoho_po_id}` regression; mark-test route still reachable (smoke).

**Hard contract preserved.** No schema change. No Zoho payload change. No Luma payload change. No finalize side effects. No external API calls from the upload route.

---

## v2.7.2 — Stabilization: deterministic OIDC test + safe canary marker (deployed + tagged)

| | |
|---|---|
| **Tag** | `v2.7.2` (annotated `2d25328a…`) at commit `2d5d1cb` — pushed to origin. |
| **Production version** | `2.7.2` (per `/healthz` after deploy at 2026-06-26 19:57 UTC; **superseded by v2.7.3** if/when this PR ships). |
| **Merged via** | PR [#8](https://github.com/kidevu123/packtrack-v2/pull/8) (squash) into main on 2026-06-26 19:51 UTC. |
| **Alembic head** | `e1f2a3b4c5d7` (unchanged — no schema change) |
| **Canary mark on Receive id=1** | **PERFORMED 2026-06-26 20:10 UTC.** See v2.7.3 section above for POEvent id and verification details. |

Original v2.7.2 ship-state section:

| | |
|---|---|
| **Branch** | `feature/stabilization-v2.7.2` (off `origin/main` @ `4aaecfc` — separate worktree so v2.8.0 WIP in the main repo is not touched) |
| **Alembic head** | `e1f2a3b4c5d7` (unchanged — no schema change) |
| **Feature flag** | `RECEIVING_VNEXT_ENABLED` remains ON in production. |
| **Status** | Code complete; tests green (255 passed; +7 over v2.7.1's 248); **not merged, not deployed, not tagged**. |

**v2.7.2 scope** (two small safety/stability items):

* **Deterministic OIDC tamper test.** `tests/test_oidc_state.py::test_signer_tampered_raises_generic_badsignature_not_expired` was flaky (~1-in-5) because flipping only the trailing base64 character of an HMAC signature doesn't always change the decoded bytes (the trailing 4-bit padding can decode the same way). Fixed by replacing the entire signature segment with a fixed-length all-`A`s value (decodes to zero bytes) and also by running 5 distinct variant strings through a loop so a future `itsdangerous` release that accidentally validates one of them surfaces immediately rather than as a flake. Verified 0 failures in 50 consecutive runs locally. **Production auth behavior is unchanged** — this is a test-only fix.

* **OWNER-only `POST /receive/v2/{id}/mark-test` route.** Safe audit marker for canary/test receives. **Does NOT delete any PT row, does NOT call Zoho/Luma**. Requires:
  * OWNER role (RECEIVING/DESIGN → 403).
  * Form `confirm` field exactly equals `"I UNDERSTAND ZOHO AND LUMA ARE NOT REVERSED"` (impossible to fire by accident).
  * Optional `reason` field is appended to the POEvent + the receive's notes for audit.
  * Emits `POEvent(kind="receive_marked_test")` with operator name, ISO timestamp, and reason.
  * Appends a marker line to `Receive.notes` (operator-visible on `/receive/v2/{id}` and `/receive/v2/{id}/review`).
  * Re-renders the result page with a prominent amber **"Test / canary receive — external Zoho and Luma records were NOT reversed"** banner.
  * UI: when an OWNER views the result page of a non-test-marked receive, a collapsed `<details>` form lets them mark it with one click. Banner replaces the form once marked.
* **Result template hardened** so it tolerates `outcome=None` (the mark-test path doesn't re-run push). The retry-push path also passes the banner flag so the warning persists across re-renders.

**Tests:** 7 new cases in `tests/test_v2_7_2_mark_test.py` + 1 updated case in `tests/test_oidc_state.py`. Full suite **255 passed** (was 248). `ruff check .` clean. Legacy `/receive/{zoho_po_id}` regression test passes.

**Operator note — how to mark Receive id=1 (the v2.6.1 canary):**
After v2.7.2 deploys, an OWNER can mark the canary as test by either:

1. **UI**: visit `/receive/v2/1`, click into the result page, expand "Mark this receive as test / canary", check the confirmation box, click "Mark as test / canary".
2. **CLI** (canonical equivalent):
   ```bash
   # From a workstation logged in as OWNER (cookie in $COOKIE):
   curl -X POST 'https://packtrack.booute.duckdns.org/receive/v2/1/mark-test' \
        -b "packtrack_session=$COOKIE" \
        -F 'confirm=I UNDERSTAND ZOHO AND LUMA ARE NOT REVERSED' \
        -F 'reason=vNext Stage 2 canary; Zoho PR-00583 + Luma lot 995751ce intentionally left in place'
   ```
3. The receive's `notes` will then carry the marker line; a `receive_marked_test` POEvent will be visible on PO-00250's audit log; the result page will show the amber banner.

The action is purely PT-side. **Zoho PR-00583 and Luma lot `995751ce-…` remain in their respective systems unchanged** (which is the right call per `docs/RECEIVING_VNEXT_CANARY_CLEANUP.md` — a 1-unit footprint on a 3600-unit PO line is operationally invisible).

---

## v2.7.1 — Receiving vNext polish: Zoho notes + Start Receive entry (deployed + tagged)

| | |
|---|---|
| **Tag** | `v2.7.1` (annotated `ba3836df…`) at commit `d8ed5fc` — pushed to origin. |
| **Tag message** | "PackTrack v2.7.1 — Receiving vNext polish (Zoho notes + Start Receive UI)" |
| **Production version** | `2.7.1` (per `/healthz` after deploy at 2026-06-26 16:32 UTC) |
| **Merged via** | PR [#6](https://github.com/kidevu123/packtrack-v2/pull/6) (squash) into PR #5's branch as `d8ed5fc`, then PR [#5](https://github.com/kidevu123/packtrack-v2/pull/5) merged into `main` with a **true merge commit** `e47828e` on 2026-06-26 17:35 UTC. The merge-commit method (not squash) was chosen specifically to keep `d8ed5fc` reachable from `main` so the `v2.7.1` tag stays meaningful. |
| **Alembic head** | `e1f2a3b4c5d7` (unchanged — no schema change) |
| **Feature flag** | `RECEIVING_VNEXT_ENABLED=true` in production (kept ON post-canary). |
| **Main reconciled (2026-06-26)** | `main` at `e47828e`. Contains `d8ed5fc` (v2.7.1 ship), `42912e4` (v2.6.1 canary docs), and the reconciliation merge chain (`79d51bf` + `e47828e`). The deleted-after-merge feature branch was `feature/inventory-cf-product-line-edit-v2.7.0`. No deploy was triggered by the reconciliation (production already runs `d8ed5fc` byte-for-byte; only docs differ). |

**Deploy verification (post-PR-#6 deploy at 2026-06-26 16:32 UTC):**
* `/healthz` → `{"ok":true,"version":"2.7.1","db":"ok","gateway_configured":true,"zoho_integration_configured":true,...}`
* Production Alembic current/head = `e1f2a3b4c5d7`.
* Smoke (8/8) passed.
* `RECEIVING_VNEXT_ENABLED=true` in env + uvicorn process env.
* Journal clean since deploy.

**v2.7.1 scope** (small, focused polish on Stage 2):
* **Zoho receive notes are now human-readable.** New helper `services/receiving_v2_finalize.build_zoho_receive_notes(session, receive, box_receipts)` composes a clean, multi-line description: `"Received via PackTrack" / Receive: R-… / PO: PO-… / Case: … / Operator: … / Items (N): - name: qty unit"`. Operator-supplied `Receive.notes` is appended verbatim under a `Notes:` section. Capped at ~1800 chars so the upstream service's `[zoho-integration]` trace + `[truncated]` marker still fit under Zoho's ~2000-char limit.
* **PackTrack DOES NOT duplicate the upstream trace.** The integration service (`zoho-integration-service/app/writes/inventory_write_adapters.py::_compose_note_field` at v1.22.0+) is the sole source of the `[zoho-integration] pack_track_receipt_id=…;pack_track_operator_id=…;pack_track_workflow_session_id=…` line. PackTrack notes are operator-facing prose only — `pack_track_*` IDs and `[zoho-integration]` prefixes are explicitly forbidden via test (`test_build_zoho_receive_notes_does_not_duplicate_machine_metadata`).
* **"Start receive" UI entry point** on `/receive`. When `RECEIVING_VNEXT_ENABLED=true` AND the Zoho mirror is linked to a PackTrack PurchaseOrder AND the PO is not fully received, each card now shows a primary "Start receive" button pointing to `GET /receive/v2/new?po_id=<internal_po_id>` (the non-mutating start page from Stage 1). Legacy whole-card link to `/receive/{zoho_po_id}` is preserved; layout is otherwise unchanged.
* **No schema change. No Zoho/Luma payload shape change** (only the `notes` field's contents). `submit_zoho_receives` and `push_luma_receipt` are not modified.
* **Canary cleanup is documented but NOT executed.** See [`docs/RECEIVING_VNEXT_CANARY_CLEANUP.md`](./RECEIVING_VNEXT_CANARY_CLEANUP.md) for the audit + reversal-path proposal. The canary `Receive id=1` / `BoxReceipt id=139` / Zoho `PR-00583` / Luma lot `995751ce-…` remain in place; recommendation: leave Zoho + Luma untouched (a 1-unit footprint on a 3600-unit PO line is operationally invisible) and annotate Receive 1 in PT only.

**Tests:** 9 new cases in `tests/test_v2_6_2_polish.py`; full suite **248 passed** (was 239 on the v2.7.0 base; +9 new). Ruff clean.

---

## v2.6.1 — Receiving vNext Stage 2 canary PASSED + tagged (2026-06-26)

| | |
|---|---|
| **Tag** | `v2.6.1` (annotated `cfdc2d81…`) at commit `7ce0af6` — pushed to origin. |
| **Canary** | PASSED — real receive `R-2026-0001` against PO-00250 finalized + pushed live; Zoho `committed`, Luma `pushed`, BoxReceipt id `139`. |
| **Feature flag** | `RECEIVING_VNEXT_ENABLED` kept ON in `/etc/packtrack/packtrack.env`. |
| **Alembic head** | `e1f2a3b4c5d7`. |

(Full canary evidence is on `main` at commit `42912e4`. This branch was cut from PR #5's `b87ff20` before that docs commit landed on origin/main; the canary success record is unchanged regardless.)

---

## v2.6.x — Ship-state (2026-06-26): deployed, **pending Stage-2 canary**, NOT tagged

> Historical entry — superseded by the ## v2.6.1 canary success section above (which has the same data plus tag/POEvent IDs). Kept here for the pre-canary deploy timeline.

| | |
|---|---|
| **Tag** | `v2.6.1` (annotated `cfdc2d81…`) at commit `7ce0af6` — pushed to origin. |
| **Tag message** | "PackTrack v2.6.1 — Receiving vNext finalize canary" |
| **Canary verdict** | **PASS** — real receive `R-2026-0001` against PO-00250 finalized + pushed live. |
| **Tagged SHA** | `7ce0af667c457fe7f676531a5497e4f4237fca04` (the build that served the canary finalize) |
| **`RECEIVING_VNEXT_ENABLED`** | **ON** in `/etc/packtrack/packtrack.env` (set during canary, **kept ON** for continued controlled use by OWNER/RECEIVING). Legacy `/receive/{zoho_po_id}` flow remains available and unchanged. |
| **Alembic head** | `e1f2a3b4c5d7` (unchanged — no migration in Stage 2). |

**Canary evidence (Receive id=1, R-2026-0001, PO-00250, BoxReceipt id=139):**
* `Receive.status = pushed_ok` at 15:26:49.92 UTC, 34 ms after `finalized_at`.
* Exactly 1 BoxReceipt materialized (no duplicates). `submission_line_index=1`.
* `box_number = "PT-4a9905a3fda247c39f4d4431ebc05e8a"` — v2.4.1 Luma contract preserved.
* `submission_id` propagated from Receive → BoxReceipt verbatim.
* Luma response: `{ok: True, created: True, events_emitted: ['MATERIAL_RECEIVED', 'PACKAGING_BOX_RECEIVED'], accepted_quantity: 1, luma_packaging_lot_id: 995751ce-…}`.
* Zoho per-leaf status `committed` (rolled-up POEvent `receive_pushed_ok`, no `zoho_failed` reasons).
* POEvents emitted: `receive_finalized` (id=130) and `receive_pushed_ok` (id=131) on PO_id=3.
* Service journal clean since finalize. `/healthz`, `/inventory`, `/receive` all responding.

**Note on running production version**: `/healthz` may report a higher
version than `v2.6.1` if a subsequent unrelated deploy has happened (e.g.
the `2.7.0` line that began rolling shortly after this canary). The
`v2.6.1` tag specifically anchors the commit that the canary finalize
ran against (`7ce0af6`), independent of any later deploys.

**Earlier ship-state (now superseded by the tag above):**
* PR #3 (`f2d7f63`) merged 2026-06-25 — Receiving vNext Stage 2 code.
* PR #4 (`7ce0af6`) merged 2026-06-25 — Richer inventory item detail.
* Both deployed with `RECEIVING_VNEXT_ENABLED` default OFF.
* Per decision 2026-06-26, `v2.6.0` was skipped; single `v2.6.1` tag anchors the v2.6.x line at canary-verified SHA.

## v2.6.1 — Richer inventory item detail (read-only Zoho metadata)

| | |
|---|---|
| **Version** | `2.6.1` |
| **Alembic head** | `e1f2a3b4c5d7` (unchanged — no schema change) |
| **Scope** | Display / read-only only. No new Zoho writes. |

> Released right after the Receiving vNext Stage 2 `2.6.0` line; this is the
> inventory-side companion patch (separate work stream, no overlap).

**v2.6.0 scope:** The inventory item detail page now shows a much richer,
read-only view of the Zoho item, sourced through the `zoho-integration-service`
v1.31.0 Phase A endpoints. PackTrack still **never calls Zoho directly**.

* **New read-only client** — `services/zoho_item_detail.py` fetches
  `GET /zoho/pack_track/items/{id}` (expanded detail) and
  `GET /zoho/pack_track/items/metadata` (custom-field defs, dropdown options,
  categories, reporting tags, field policy) using `Authorization: Bearer <app
  token>` + `X-Brand`. Every failure degrades gracefully — the local detail
  still renders with a small operator note ("Zoho extended details
  unavailable.").
* **Metadata cache** — simple in-process TTL cache honoring
  `meta.cache_ttl_seconds` (3600s), with a stale/None fallback on fetch failure.
* **New detail sections** — Primary details, Packaging & custom fields, Zoho
  accounting & inventory, Images & attachments, plus the existing Sync status.
  Custom fields are merged from metadata defs (full, ordered, labeled) with the
  item's set values. Dropdowns (e.g. `cf_product_line` → 7OH / MIT A / MIT B)
  render as **disabled `<select>`s** — visible but not writable in this phase.
* **Naming kept distinct** — PackTrack's derived `product_line` (browsing group:
  FIX / FIX Beyond / Unassigned) is shown separately and is **never** overwritten
  by Zoho's `cf_product_line` custom field (labeled "Zoho Product Line"). The two
  descriptions (standard Zoho item description vs `cf_description` custom field)
  are also labeled distinctly.
* **No new writes** — no custom-field / pricing / account / valuation / vendor /
  reporting-tag / stock editing. The v2.5.1 editable path (name, description,
  unit via item PATCH; vendor read-only; retry; pending/failed/synced) is
  preserved unchanged. The outbound PATCH payload still only carries
  `name`/`description`/`unit`.
* **No schema change** — display-only; Alembic head unchanged.

## v2.6.0 — Receiving vNext Stage 2 (merged + deployed, pending canary)

| | |
|---|---|
| **Merge commit** | `f2d7f63` — PR [#3](https://github.com/kidevu123/packtrack-v2/pull/3) (squash) merged 2026-06-25. Branch `feature/receiving-vnext-stage2-finalize` retained on origin. |
| **Alembic head** | `e1f2a3b4c5d7` (`receive_vnext_stage1`) — **no new migration** in Stage 2; the two new `box_receipts` FK columns were already added in Stage 1. |
| **Feature flag** | `RECEIVING_VNEXT_ENABLED` — default **OFF** in production. Stage 2 routes (`/receive/v2/{id}/review`, `/finalize`, `/retry-push`) 404 unless flag is on. Legacy `/receive/{zoho_po_id}` remains the only operator-visible receive flow. |
| **Status** | Deployed, **NOT tagged**. Per decision (2026-06-26): skip the `v2.6.0` tag entirely; tag `v2.6.1` at `7ce0af6` after canary. Tests: 208 passed at merge; 222 passed on current main with PR #4 layered on. |

**v2.6.0 Stage 2 scope** (per `docs/design/2026-06-25-receiving-vnext.md` § 3.2 steps 11–15 + § 5.1):
* **Python model surfacing** — declare `BoxReceipt.receive_id` and `BoxReceipt.receive_case_line_id` on the SQLModel class. Legacy paths still leave both NULL; only Stage 2 finalize populates them. **No migration** — the columns shipped with Stage 1.
* **Validation service** `validate_receive_for_finalize(session, receive) → (blockers, warnings)`:
  * Blockers: already-finalized, no-PO, parcel-missing-tracking, no cases, case missing vendor #, case with zero lines, line missing item, line qty ≤ 0.
  * Warnings: item not on PO, over-count vs PO remaining, under-count vs PO remaining, missing material_code (→ Luma NOT_READY).
* **Materialization service** `materialize_box_receipts(session, receive, user)`:
  * Runs in one DB transaction; no external calls inside.
  * One `BoxReceipt` per `ReceiveCaseLine`. Idempotent: lines that already have `box_receipt_id` are skipped (so a retry-by-mistake doesn't double-write).
  * **v2.4.1 contract preserved verbatim**: `box_number = "PT-{packtrack_receipt_id}"`, `submission_id = Receive.submission_id`, `submission_line_index = <stable global index>` (cases by `(sequence, id)`, lines by `id`, starting at 1).
  * Snapshots `material_code` / `material_name` / `supplier` from the Item at finalize time.
  * Flips `Receive.status → FINALIZED`, sets `finalized_at` / `finalized_by_user_id`, emits `POEvent(kind="receive_finalized")`.
* **Push service** `push_receive_to_integrations(...)` — runs AFTER materialization commits:
  * Calls `submit_zoho_receives(...)` and `push_luma_receipt(...)` **byte-for-byte unchanged**. Looks up Zoho mirror + line_item_id via `PurchaseOrder.zoho_po_id` → `ZohoMirror.line_items`.
  * Sets per-leaf `BoxReceipt.luma_push_status` (PUSHED / FAILED / NOT_READY / DUPLICATE / DRY_RUN_OK).
  * Overall `Receive.status`: `PUSHED_OK` if every leaf is in {PUSHED, DUPLICATE, DRY_RUN_OK} for Luma AND every Zoho per-line outcome is in {committed, blocked, skipped, disabled, not_configured}; else `PUSH_FAILED`.
  * Emits `POEvent(kind="receive_pushed_ok" | "receive_push_failed")`.
* **Retry service** `retry_push_for_receive(...)`:
  * Re-fires only leaves in {NOT_READY, PENDING, FAILED}. Already-PUSHED leaves are not re-pushed. Safe because Zoho still keys idempotency on `PACK_TRACK_RECEIVE_{packtrack_receipt_id}`.
  * Auto-bumps NOT_READY → PENDING for leaves whose `material_code` was filled in between finalize and retry.
* **Routes** (all gated by `RECEIVING_VNEXT_ENABLED`, OWNER + RECEIVING permissions):
  * `GET /receive/v2/{id}/review` — pure read, renders blockers + warnings + finalize button.
  * `POST /receive/v2/{id}/finalize` — 400 on any blocker, 422 on un-confirmed warnings (`confirm_warnings=true` required), otherwise materialize + push.
  * `POST /receive/v2/{id}/retry-push` — re-fires failed/pending leaves.
* **Templates** `receive_v2/review.html` (blockers + warnings + finalize action) and `receive_v2/result.html` (per-leaf Luma + Zoho status + retry button on failure). Reuses existing `_partials/ui.html` macros; no UI rewrite.

**Tests:** 20 cases in `tests/test_receive_vnext_stage2_finalize.py`. Full suite **185 passed** (was 165; +20 new). `ruff check .` clean. Alembic head unchanged: `e1f2a3b4c5d7`. Legacy `/receive/{zoho_po_id}` regression test passes.

**Notes:**
* No schema migration in Stage 2 — the Stage 1 migration `e1f2a3b4c5d7_receive_vnext_stage1` already added the two `box_receipts` FK columns. Alembic head is unchanged.
* `submit_zoho_receives` and `push_luma_receipt` are **not modified** — Stage 2 only adds a new caller, preserving the v2.4.1 Luma payload shape and the existing Zoho integration contract.
* Push happens AFTER the materialization transaction commits — so a partial push outcome cannot leave dangling rows or orphan side effects.

## v2.5.1 — Real Zoho item-update path via integration service (deployed)

| | |
|---|---|
| **Last deployed version** | `2.5.1` (merged via PR #2; v2.5.0 lineage continues underneath) |
| **Alembic head** | `d5e6f7a8b9c0` (unchanged — no schema change) |

**v2.5.1 scope:** Completes the parked v2.5.0 outbound item-sync path. Editing a
Zoho-owned field on the item detail page now performs a **real PATCH** through
the `zoho-integration-service` PackTrack item endpoints (CT 9503, v1.30.0+):
`GET/PATCH /zoho/pack_track/items/{item_id}` and `GET .../items/list`. PackTrack
still **never calls Zoho directly**.
* **Writable allowlist** — only `name`, `description`, `unit` are ever sent.
  Auth via `Authorization: Bearer <app token>` + `X-Brand` (the proven receive
  scheme; `X-Internal-Token` is also sent but is not sufficient alone — it
  returns 401 on v1.30.0). `services/zoho_item_sync.py` owns the HTTP and never
  imports OAuth/`zohoapis`/direct-Zoho code.
* **Known service-side blocker (v1.30.0 / brand `haute_brands`)** — with correct
  Bearer auth the item endpoints currently return `403 ZOHO_AUTH_FORBIDDEN`
  ("App is not permitted to access this Zoho resource"). The PackTrack side is
  complete and fails cleanly (`failed` + retry); enabling the write requires the
  integration service to grant the app access to the Zoho items resource.
* **Vendor is Zoho-read-only** — the service rejects vendor writes
  (`422 VENDOR_UPDATE_NOT_SUPPORTED`), so vendor is never sent. The detail page
  renders vendor read-only for Zoho-synced items ("Vendor comes from Zoho and is
  not editable here yet."); it stays locally editable only for manual items with
  no `zoho_item_id`. Inbound sync always reflects Zoho's vendor.
* **State machine** — PATCH success → `synced` (clears error, optional
  read-after-write align of name/desc/unit); 4xx/5xx or network error →
  `failed` with a truncated error and the local edit kept; service unconfigured
  or item has no `zoho_item_id` → `pending` (local-only, protected from inbound
  clobber). No outbound loop.
* **Retry** — owner-only `POST /inventory/{id}/sync/retry` re-runs the push and
  redirects with `saved=synced|failed|local`. No outbox dashboard yet.
* **No schema change** — reuses the v2.5.0 `zoho_push_*` columns; Alembic head
  unchanged.

## v2.5.0 — Receiving vNext Stage 1 (deployed + tagged)

| | |
|---|---|
| **Active version on main** | `2.5.0` |
| **Last deployed version** | `2.5.0` (production at commit `c97ad6e`, tagged `v2.5.0`) |
| **Merged via** | PR [#1](https://github.com/kidevu123/packtrack-v2/pull/1) (squash) at 2026-06-25, base `3a2a7fa` → `c97ad6e` |
| **Alembic head** | `e1f2a3b4c5d7` (`receive_vnext_stage1`, down_revision `d5e6f7a8b9c0`) |
| **Feature flag** | `RECEIVING_VNEXT_ENABLED` — default **OFF** in production. New `/receive/v2/...` routes return 404 to authenticated operators; unauthenticated probes 401 (auth gate fires first). Legacy `/receive/{zoho_po_id}` remains the only enabled receive flow until ops flips the flag. |
| **Status** | Merged, deployed, tagged. Operator-visible behavior unchanged — vNext routes are reachable only by flipping the env var. |

**v2.5.0 Stage 1 scope** (per `docs/design/2026-06-25-receiving-vnext.md` § 6 Stage 1):
* **Schema** (additive only, no backfill, no destructive change):
  * New tables `receives`, `receive_cases`, `receive_case_lines`.
  * New nullable FK columns on `box_receipts`: `receive_id`, `receive_case_line_id` — **DB-only** in Stage 1, no ORM attributes added yet. Stage 2 will declare them on the `BoxReceipt` model when finalize materializes leaves.
  * Partial UNIQUE `uq_receive_cases_receive_case_number` on `(receive_id, vendor_case_number) WHERE vendor_case_number IS NOT NULL` — duplicate vendor case numbers rejected within one receive; NULL placeholders during drafting coexist.
  * UNIQUE on `receives.receive_number` and `receives.submission_id`.
  * `AttachmentKind.PACKING_LIST` added to the enum; `Receive.packing_list_attachment_id` FK pointer (single primary attachment per receive in v1).
* **SQLModel models** `Receive`, `ReceiveCase`, `ReceiveCaseLine` + enums `ReceiveStatus`, `ShipmentKind`, `CaseKind` (terminal states `finalized / pushed_ok / push_failed` defined but not used in Stage 1 — schema is forward-compatible).
* **Routes under `/receive/v2/...`** behind the flag: create (`R-YYYY-NNNN` server-generated id), view, case CRUD, line CRUD, PO-scoped item search, totals. Permissions: OWNER + RECEIVING.
* **Draft/counting UI** (HTMX + Alpine + Tailwind, matching existing macros): case blocks, line rows, totals-by-item right rail, packing-list placeholder, disabled "Finalize coming in v2.6.0" button.
* **Validation**: empty cases allowed in draft; line requires item present on the PO + `declared_quantity > 0`; duplicate vendor case # → clean 409 (not 500) from either Postgres or SQLite.
* **No** finalize / no BoxReceipt materialization / no Zoho push / no Luma push — all deferred to Stage 2 (v2.6.0). v2.4.1 Luma idempotency contract preserved verbatim for that future stage (`Receive.submission_id` generated at create time so it's ready).

**Tests:** 21 cases in `tests/test_receive_vnext_stage1.py`; full suite **165 passed** on `main` after merge. `ruff check .` clean.

**v2.5.0 ship-state (2026-06-25):**
* Deploy via canonical `deploy/deploy.sh` to LXC 200 on Proxmox `192.168.1.190`. Post-restart smoke (8/8) passed. CSS build 50684 bytes. Migration `e1f2a3b4c5d7` applied cleanly (`d5e6f7a8b9c0 -> e1f2a3b4c5d7`).
* `/healthz` returns `{"ok":true,"version":"2.5.0","db":"ok",...}`. Alembic current/head matches: `e1f2a3b4c5d7`.
* `RECEIVING_VNEXT_ENABLED` unset in `/etc/packtrack/packtrack.env` and not in the uvicorn process environment — flag uses Python default `False`. Operator-visible receiving flow is the legacy `/receive/{zoho_po_id}`, unchanged.
* `feature/receiving-vnext-v2.5.0-stage1` branch retained on origin for reference (Stage 2 will branch fresh from `main`).
* **Next:** Stage 2 (v2.6.0) — finalize + BoxReceipt materialization + Zoho/Luma push wiring. Branch `feature/receiving-vnext-stage2-finalize` created from `c97ad6e`; plan reported separately. v2.4.1 Luma idempotency contract preserved verbatim for that work (`Receive.submission_id` already generated at create time).

## v2.5.0 — Inventory grouping + clickable item detail/edit (feature release)

| | |
|---|---|
| **Active version on main** | `2.5.0` |
| **Alembic head** | `d5e6f7a8b9c0` (`item_product_line_and_push_state`, down_revision `3c8a2b1e9d40`) |

**v2.5.0 scope:** The Inventory tab becomes a practical one-stop shop.
* **Grouped browsing** — `/inventory` rows are grouped by derived brand /
  product line (parsed from `item.name`, e.g. `FIX`, `FIX Beyond`, with a
  `Unassigned / Generic` catch-all). New product-line chips jump between lines
  and preserve the active search/filters; existing pagination + summary cards
  are retained (page is never silently unbounded).
* **Clickable item detail** — `GET /inventory/{id}` shows name, SKU, material
  code, vendor, description, unit, stock + status, reorder/critical, daily
  usage, sea/express lead days, last unit cost, Zoho item id, last synced, image,
  suggested order, and open PackTrack + Zoho POs (via `coverage_for_items`).
  Item rows link to it from the thumb/name. Non-owners are read-only.
* **Owner edit workflow** — `POST /inventory/{id}` edits name, description,
  material_code, vendor, unit, daily_usage_rate, reorder_point, critical_point,
  sea_lead_days, express_lead_days (trimmed/validated). `current_stock` stays
  read-only (no manual-adjustment pattern exists). Owner edit locks
  `reorder_point` from Zoho overwrite, matching the inline-edit behavior.
* **Zoho boundary (honest, not faked)** — There is **no Zoho item-update write
  endpoint** on the read gateway or the integration service today. Editing a
  Zoho-owned field (name/description/vendor/unit) is saved locally and parked
  `zoho_push_status='pending'` via `services/zoho_item_sync.py` (a wireable
  wrapper, single TODO to enable a real push). Inbound `sync_items` will **not**
  overwrite those fields while pending, so the UI never silently reverts an
  owner edit. No outbound loop: a push is only triggered by an explicit edit,
  never by inbound sync. **Bidirectional item sync is NOT complete** — outbound
  is a pending/outbox state until a write path is wired.
* **Schema** — `items.product_line` (indexed, backfilled from names) +
  `items.zoho_push_status/zoho_push_error/zoho_push_attempted_at`. All nullable,
  no rewrite, safe for existing data.

## v2.4.3 — UI wholesale polish (deployed + tagged)

| | |
|---|---|
| **Active version on main** | `2.4.3` |
| **Last deployed version** | `2.4.3` (production at commit `fe603b3`, tagged `v2.4.3`) |
| **Previous deployed version** | `2.4.2` (commit `2524cb5`, tagged `v2.4.2`) |
| **Public URL** | `https://packtrack.booute.duckdns.org` |
| **Deploy path** | `deploy/deploy.sh` only — see [`RUNBOOK_DEPLOY.md`](./RUNBOOK_DEPLOY.md). Ad-hoc `pct push` + `rsync --delete` is forbidden (caused the v2.2.0 unstyled-UI incident). |
| **Healthz axes (expected)** | `gateway_configured=true`, `zoho_integration_configured=true`, `legacy_zoho_configured=false`, `zoho_configured=false`, `telegram_configured=false` |
| **SSO** | Public `/auth/sso` redirect derives Authentik base from `OIDC_ISSUER_URL` — no LAN-IP leaks. State + nonce TTL 1800 s. Browser login round-trip verified by operator 2026-06-24. |
| **CSS smoke** | `scripts/smoke_test.sh` passes; deploy gate asserts size + sentinels. |
| **Alembic head** | `f4a5b6c7d8e9` (`forecast_alert_sent_stock`) — unchanged. |

**v2.4.3 ship-state (2026-06-25):**
* Deployed via canonical `deploy/deploy.sh` to LXC 200 on Proxmox `192.168.1.190`. Post-restart + external smoke tests pass. CSS build 48301 bytes.

**v2.4.3 scope:** wholesale UI/UX polish — operations summary on Pipeline, inventory summary cards + filter chips (route-side aggregates), forecast "Needs setup" insight, receiving list buckets, sticky receive/PO-new action bars, PO detail two-column layout + timeline icons, `insight_card` macro, consistent `ui.page_header` across admin/search/error/login/home. No schema change. Receiving / Luma / Zoho behavior unchanged.

**v2.4.2 scope:** server-side pagination on `/inventory` (default 50 items per page, `?page=N`), filter-aware Prev/Next links, `loading="lazy"` on item thumbnails. Fixes browser `ERR_HTTP2_PROTOCOL_ERROR` symptom caused by NPMplus/HTTP-2 upstream truncation at the proxy buffer boundary on the previous ~740 KB single-page inventory response. Reduces a default `/inventory` response to under 200 KB. No schema change. Receiving / Luma / Zoho unchanged.

**v2.4.2 ship-state (2026-06-25):**
* Inventory P0 (browser-side ERR_HTTP2_PROTOCOL_ERROR) is **closed**. Root cause was a ~740 KB unpaginated `/inventory` response crossing an HTTP/2 / NPMplus upstream-buffer boundary; PackTrack-side pagination + lazy thumbnails removed the trigger entirely.
* NPMplus buffer mitigation applied and **left in place** on LXC 104 at `/opt/npmplus/custom_nginx/server_proxy.conf` (raised `proxy_buffers 8x256k → 16x256k`, `proxy_busy_buffers_size 512k → 1m`, `proxy_max_temp_file_size 2m → 10m`). Backup: `server_proxy.conf.bak.20260625-123402`. The bump alone did not stop the truncation (next failure landed at exactly 512 KiB), but it is benign and may help other apps; leaving it.
* HTTP/3 was **not** disabled for `packtrack.booute.duckdns.org`. The PackTrack-side pagination fix made HTTP/3 disable unnecessary. If proxy truncation reappears on a future large response, the next recommended step is to remove the two `listen 0.0.0.0:443 quic;` / `listen [::]:443 quic;` lines from `/opt/npmplus/nginx/proxy_host/18.conf` and reload — surgical and reversible.
* Receiving vNext design doc is **committed and pushed** (`docs/design/2026-06-25-receiving-vnext.md`, commits `fbec604` + `5de5530` on `main`). All 14 v1 decisions are locked in § 0. **Implementation has not started.**

**v2.4.1 scope:** the three P0 Luma findings from the v2.4.0 audit —
* **P0-1 fixed (schema-backed):** receiving form requires a hidden `submission_id` token. POST handler short-circuits when the same token already produced BoxReceipts on this PO. Migration `3c8a2b1e9d40` adds `submission_id` + `submission_line_index` columns and a partial UNIQUE index on `(purchase_order_id, submission_id, submission_line_index) WHERE submission_id IS NOT NULL` — the durable dedup backstop. **`box_number` is no longer the idempotency key.** Receive-form rows write `box_number = "PT-{packtrack_receipt_id}"` only because Luma still requires a non-empty value (`z.string().min(1)`).
* **P0-2 fixed:** `process_luma_consumption` rejects negative `qty_consumed` as `skipped_invalid`. Stock unchanged, no audit row written.
* **P0-3 fixed:** per-entry missing `material_code` is now `skipped_invalid` with a reason; the batch continues processing the remaining valid entries.
* docs/PACKTRACK_LUMA_CONTRACT.md updated; § 8 documents `box_number` semantics per-flow and § 9 captures the future coordinated Luma cleanup.

**Previously shipped (v2.4.0):** UI polish — `_partials/ui.html` macro library, inventory page widened + clearer per-row hierarchy, forecast page collapsed to one shared row macro with clickable summary anchors + collapsible "No demand data" section, home "Needs you" reorder items grouped into one card with View All link.

**Previously shipped (v2.3.0):** reconciliation of the v2.2.0 Zoho integration receive path with main's Phase A/C/D inventory + forecasting + UI overhaul. Tagged `v2.3.0` at commit `a582f38`.

**Open items (post v2.4.2):**
- **Receiving vNext v2.5.0 stage 1** — kickoff prompt is in `docs/design/2026-06-25-receiving-vnext.md` § 9. Scope: new tables (`receives`, `receive_cases`, `receive_case_lines`), `AttachmentKind.PACKING_LIST`, nullable FK additions on `box_receipts`, draft + counting UI behind `RECEIVING_VNEXT_ENABLED` (default OFF). No Zoho/Luma push wiring in this stage (that's v2.6.0).
- **Luma P1/P2 follow-ups** — tracked in `docs/PACKTRACK_LUMA_CONTRACT.md` § 7. P0s shipped in v2.4.1.
- **Future Luma `box_number` cleanup** (coordinated with Luma) — § 9 of the contract doc. Relax Luma's `z.string().min(1)` and change the partial unique index so PackTrack can stop sending the `PT-{uuid}` compatibility mirror.
- **Future HTTP/3 investigation** — only revisit if proxy-side truncation reappears on a large response after v2.4.2. Current state is healthy.
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
