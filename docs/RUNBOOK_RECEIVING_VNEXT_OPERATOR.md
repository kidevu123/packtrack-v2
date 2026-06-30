# Receiving vNext — operator runbook

Practical, end-to-end. PackTrack v2.13.0+.

This is the runbook the warehouse operator and the owner consult before
the first real shipment goes through Receiving vNext. Everything that
came before v2.13.0 — the immutable adjustment ledger, the Zoho stock
ownership policy, the import/preview flow, the canary smoke runs — was
infrastructure for the moment you actually run a real receive. After
that real receive, the next development decisions should be driven by
the friction you find, not by speculation.

---

## 1. How a PO appears on the Receiving page

The chain that puts a PO card on `/receive`:

1. Vendor PO is created in Zoho Inventory.
2. PackTrack's scheduled background sync (`_zoho_sync_job`, every 30
   minutes) reads Zoho via `zoho-integration-service` and writes a
   ``ZohoMirror`` row keyed by `zoho_purchaseorder_id`.
3. If a matching internal ``PurchaseOrder`` exists (or is created via
   the PO adopt/link flow), the mirror gets a "Linked to PT" badge on
   the card. Without a linked PT PO, the card shows but the **Start
   receive (vNext)** button does not appear.
4. The v2.13.0 launch-readiness pill (✓ Ready for vNext / ⚠ Needs
   attention) summarises any issues that would hurt the receive
   workflow — missing material codes, no linked PO, no mirror line
   data, unknown vendor. The pill is **advisory only** and does not
   block Start Receive.

If a PO you expect doesn't show up: the Zoho mirror sync hasn't yet
imported it. Wait for the next 30-minute cycle or trigger Settings →
Sync.

## 2. Starting a receive

1. From `/receive`, find the PO card. Verify the readiness pill.
2. Click **Start receive (vNext)**. PackTrack creates a draft
   ``Receive`` row (`R-YYYY-NNNN`) and redirects to the receive page.
3. The draft has no cases, no expected lines, no packing list yet.
   Status is `DRAFT`.

## 3. Attaching the packing list

The vendor's printed/PDF/CSV packing list is for **reference and
audit**. PackTrack does not parse it.

1. On the receive page, the "Packing list" card in the right rail has
   an upload form.
2. Accepted file types: `.pdf, .csv, .xls, .xlsx, .jpg, .jpeg, .png,
   .webp, .heic`. Files are stored under `UPLOAD_DIR/packing_list/`
   and linked to this receive via `Receive.packing_list_attachment_id`.
3. Replacing an attachment increments the version number; the prior
   row is kept on disk for audit. No Zoho/Luma traffic.

## 4. Entering expected lines

These are the operator's "what the vendor said is in the shipment"
records. They drive the Review reconciliation.

### Option A — manual

1. In "Packing list — expected lines" pick the item from the dropdown
   (PO-scoped), enter `Expected`, `unit`, optional vendor case#, note.
2. Add as many rows as needed; the table lists them with a per-row
   Remove button (disabled once the receive is finalized).

### Option B — CSV / pasted text import (v2.7.6+)

1. Click `Import expected lines from CSV or pasted text…` to expand.
2. Click `↓ Download CSV template` (v2.13.0) to get a CSV pre-populated
   with the PO's items. The filename is
   `R-YYYY-NNNN-packing-list-template.csv`. Open it in
   Excel / Numbers / Sheets, fill in quantities, **Save As CSV (UTF-8)**.
3. Either paste the rows into the textarea or upload the CSV file
   (`.csv`, `.tsv`, `.txt`). XLSX is **not** supported — the upload
   route will return a 400 explaining how to export to CSV instead.
4. Click `Preview`. The preview page (v2.13.0) shows per-status
   counts (Ready / Unmatched / Ambiguous / Invalid qty / Invalid)
   plus a row-by-row breakdown. Unmatched and Ambiguous rows show
   suggested PO items below the row so you can spot a typo.
5. Optionally check `Replace existing expected lines before import`
   then click `Import N ready row(s)`. Only `Ready` rows are
   persisted; everything else is skipped.

The committed lines become normal `ReceivePackingListLine` records —
the source field reads `csv_import` — and feed the Review
reconciliation just like manually-entered rows.

## 5. Entering cases and lines

This is the operational core: you're recording what physically
arrived, vendor case by vendor case.

1. In "Cases", click `+ Add case`. Enter the vendor case number
   (e.g. `C-001`) and optionally the case kind.
2. In the case block, use the line-add form: pick the item from the
   PO-scoped select (label shows remaining qty + expected hint when
   you've entered an expected line), enter `Declared`, optional
   `Counted` (defaults to declared), optional supplier lot.
3. Add as many cases and lines as needed. The right rail's
   "Totals by item" updates live.

## 6. Reading review warnings

Click `Review` (or proceed to `/receive/v2/{id}/review`).

The review page surfaces:

* **Blockers** — finalize is disabled until these are resolved.
  Examples: parcel shipment with no tracking number, a case with no
  lines, a case missing a vendor case number, a line with zero declared.
* **Warnings** — finalize is allowed with explicit acknowledgement
  via the `I acknowledge the N warning(s)` checkbox. Examples:
  over-count vs PO remaining, under-count vs PO remaining, item missing
  `material_code` (Luma push will park as NOT_READY).
* **Packing list vs counted reconciliation** — Match / Short / Over /
  Unexpected / Missing per item. **These are advisory only** — never
  blockers. Zoho/Luma payloads still use the actual counted
  `ReceiveCaseLine` totals, not the expected lines.
* **Recent activity** — the last few `POEvent` rows scoped to
  receive-lifecycle kinds (packing list upload, expected-line
  import/add/delete, finalize, push results, test marker).

## 7. When to finalize

Only when all of the following are true:

1. Every blocker is gone.
2. Every warning is read and intentional. If the warning says "missing
   material_code", set the code in the inventory editor **first** so
   Luma doesn't park the BoxReceipt as NOT_READY.
3. The packing list is attached for audit.
4. The expected-lines reconciliation looks reasonable (matches,
   plus any short/over you can explain).
5. **This is a real, physical shipment**, not a test or canary. The
   Mark as test marker is the right tool for non-production receives.

Click `Finalize & push`, check the acknowledgement box if there are
warnings, submit.

## 8. What happens after finalize

In one DB transaction, then a sequence of upstream pushes:

1. PackTrack materializes one ``BoxReceipt`` per case-line. Each row
   carries the v2.4.1 idempotency contract:
   * `submission_id` + `submission_line_index` for PT-side dedup.
   * `box_number = "PT-{packtrack_receipt_id}"` for Luma dedup.
2. Zoho receive push fires via `zoho-integration-service` (Idempotency
   Key = `PACK_TRACK_RECEIVE_{packtrack_receipt_id}`).
3. Luma push fires per BoxReceipt to the Luma webhook.
4. `Receive.status` advances to `PUSHED_OK`, `PUSH_FAILED`, or
   `FINALIZED` depending on outcomes.
5. Result page summarises which leaves succeeded and which failed.
   Failed leaves get a `Retry push` action (idempotent at the
   integration service).

**PackTrack stock**: under the v2.11.0 stock-ownership policy, the
receive flow does not touch `Item.current_stock` directly — that's the
inventory-adjustment ledger's job. If you want PackTrack stock to move
on receive, the right path is a separate adjustment via
`/inventory/{id}/adjust` after the receive is finalized.

## 9. What NOT to do

* **Do not finalize a fabricated or test receive.** Use the
  `mark-test` route on the receive page to flag it; the canary
  banner makes it visible on every screen.
* **Do not retry a failed push blindly.** Read the error first. A
  recurring failure usually means a missing `material_code`, a stale
  Zoho mirror, or an upstream Luma config issue. Fix the cause, not
  the symptom.
* **Do not delete external receive records.** PackTrack treats Zoho
  and Luma as immutable upstream history — the right correction is a
  reversing adjustment in PackTrack and a follow-up message to the
  upstream owner if needed.
* **Do not parse packing-list PDFs or run OCR.** Not supported. Use the
  CSV template + Save As CSV path.
* **Do not edit a finalized adjustment ledger row.** The
  `inventory_adjustments` table is append-only by design (v2.9.0). To
  correct, create a reversing adjustment that references the original
  via `reversal_of_adjustment_id`.
* **Do not edit `Item.current_stock` from the master-data editor.**
  The v2.12.0 editor explicitly does not collect that field. Adjust
  via the ledger instead.

## 10. First-real-receive checklist

Before clicking Finalize on the **first** real Receiving vNext shipment:

- [ ] Pick a low-risk PO with a small number of cases and items the
      warehouse is already comfortable with.
- [ ] Confirm the readiness pill says ✓ Ready for vNext (or the
      ⚠ Needs attention details are understood and intentional).
- [ ] Attach the vendor packing list.
- [ ] Enter or import expected lines.
- [ ] Walk through the cases, entering one at a time.
- [ ] Open the Review page — confirm:
      * zero blockers
      * each warning is understood
      * reconciliation Δ values are explainable
- [ ] Verify the receive is **not** marked test/canary.
- [ ] Click Finalize & push.
- [ ] Open the result page. Confirm:
      * Zoho push: ok / response visible
      * Luma push leaves: count + statuses
- [ ] Open Zoho Inventory and Luma in another tab. Confirm the receive
      appears upstream with the expected quantities.
- [ ] Run `/inventory/{id}` for one received item. If the receive
      flow did not bump `current_stock` (it doesn't), record an
      inventory adjustment with reason `manual_correction` to mirror
      the physical change.
- [ ] Note any friction. The next sprint should fix what you found,
      not what someone speculated about.
