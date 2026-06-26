# Receiving vNext canary cleanup audit + plan

**Status:** AUDIT ONLY. Nothing in this document has been executed.
**Date:** 2026-06-26.
**Author / safety reviewer:** Sahil + the AI assistant pair.

## Subject

The Receiving vNext Stage 2 canary on 2026-06-26 created a real
production receive:

| System | Reference | State |
|---|---|---|
| PackTrack | `Receive id=1`, `Receive.receive_number=R-2026-0001` | `status=pushed_ok` |
| PackTrack | `BoxReceipt id=139`, `packtrack_receipt_id=4a9905a3fda247c39f4d4431ebc05e8a` | `luma_push_status=pushed`, qty=1 |
| Zoho | Purchase receive `PR-00583` against PO `5254962000006335019` (PO-00250) | committed; `quantity_received` increased by 1 on the matched line |
| Luma | Packaging lot `995751ce-6d85-45cc-a772-8fe775699ec7` | created; events `MATERIAL_RECEIVED` + `PACKAGING_BOX_RECEIVED` emitted, qty=1 |

The canary was intentionally a 1-unit test against PO-00250 (line:
"Hyroxi Mit-A 12ct Variety Pack - 28mg - Display box [Packaging]",
PO-line quantity 3600). The 1-unit footprint is operationally
invisible.

## Audit findings (PackTrack-side)

* `Item.current_stock=2059` on item 172 — **not changed by this receive**. PackTrack's stock counter is driven by Luma consumption events, not receives, so there is nothing to roll back here.
* `POLine.received_quantity=0` on the matched PO line — **not changed by this receive**. PackTrack's receive flow does not bump `POLine.received_quantity`; that field is reserved for legacy paths. No counter to undo.
* The only **PackTrack-side artifacts** of the canary are the four rows: `receives` row id=1, `receive_cases` row id=1, `receive_case_lines` row id=1, `box_receipts` row id=139, plus two `po_events` rows (id 130 `receive_finalized`, id 131 `receive_pushed_ok`).
* `Receive.notes` already labels it as "CANARY DRAFT — minimal vNext Stage 2 readiness check; do not finalize without operator approval." — set by the draft-creation script.

## Reversibility surface

### PackTrack
The `ReceiveStatus` enum already includes a `cancelled` value, but
there is no Stage 2 route that transitions a receive into it. To
mark Receive id=1 as cancelled / test, choose one of:

1. **(a) Use the v2.7.2 `POST /receive/v2/{id}/mark-test` route** (NEW — OWNER-only, emits a `receive_marked_test` POEvent, appends a marker to `Receive.notes`, and re-renders the result page with a prominent "External Zoho/Luma records were NOT reversed" banner. Does NOT call Zoho or Luma. Requires the verbose confirmation string `"I UNDERSTAND ZOHO AND LUMA ARE NOT REVERSED"` so an accidental click cannot fire it). **Recommended path.**
2. **(b) One-shot DB update**: `UPDATE receives SET status='cancelled', notes = notes || E'\n[Marked as test by ops 2026-MM-DD]' WHERE id=1;` (bypasses POEvent audit; only if option (a) cannot be used).
3. **(c) Leave it alone**. The `Receive.notes` already contains the "CANARY DRAFT" marker from the draft-creation script; the `receive_pushed_ok` POEvent is honest about what happened. The audit trail is already correct.

**Do not delete** the `receives` / `box_receipts` / `po_events` rows. External systems (Zoho, Luma) hold `packtrack_receipt_id=4a9905a3fda247c39f4d4431ebc05e8a` and reverse-lookups would silently fail. Soft-delete via the `cancelled` status (option a or b) is the only reasonable choice.

### Zoho
Zoho Inventory supports **voiding** purchase receives via the
`POST /inventory/v1/purchasereceives/{id}/markasvoid` API. The
`zoho-integration-service` does **not** expose a route for this today —
adding one would be a tiny new endpoint in
`app/api/purchase_receives.py`.

Two options:

1. **(a) Manual void via Zoho web UI** — log in, find PR-00583, click "Mark as void". Zoho will subtract 1 from the PO line's `quantity_received`. The next `sync_open_pos` run in PackTrack will refresh the mirror to match.
2. **(b) Leave PR-00583 alone**. The 1-unit over-count on a 3600-unit PO line is operationally invisible; the PO is not closed by 1 over-count, and end-of-PO reconciliation can match it up later.

### Luma
Packaging-lot reversal is not in the public PackTrack ↔ Luma contract
docs. The lot `995751ce-…` exists with qty=1; consumption events later
will draw against the wider material pool. The +1 lot is essentially
noise — there's no `cf_id` to reconcile against and a lot is not a
counter (it's a created object).

1. **(a) Manual cleanup in Luma admin UI** — if Luma exposes a delete/void on packaging lots.
2. **(b) Leave the lot alone**. It will simply be one extra packaging-lot record with qty=1. Future consumption events do not need to reference it.

## Recommendation

**Leave external systems alone. Annotate PT-side only if you want a clearer audit trail.**

Reasoning:
* Zero-cost: a 1-unit footprint on a 3600-unit line is rounding error.
* The audit trail in all three systems is honest about what happened (a successful 1-unit receive).
* Building Zoho void + Luma reversal paths to clean up 1 unit is the wrong return on engineering time. Add those paths if/when a real operator-mistake cleanup workflow is needed.
* `Receive.notes` already labels the row as a canary.

If you want extra clarity in PackTrack, prefer **option (b)** under "PackTrack" above: a single DB statement to set `Receive.status='cancelled'` and amend the note. Otherwise, leave it.

## Execution

**Nothing in this document has been executed.** Run any of the steps above only after explicit operator confirmation.
