"""Receiving vNext Stage 2 — review / finalize / push service.

Stage 2 of the case-first receiving model
(``docs/design/2026-06-25-receiving-vnext.md``). All work here is gated
at the route layer by ``settings.RECEIVING_VNEXT_ENABLED``; this module
is data-layer and is always importable so migrations + tests work.

Pipeline (per design § 3.2 steps 11–15):

1. ``validate_receive_for_finalize`` — pure read; returns
   ``(blockers, warnings)``. Blockers prevent finalize; warnings
   require explicit operator confirmation but do not block.
2. ``materialize_box_receipts`` — one DB transaction; creates exactly
   one ``BoxReceipt`` per ``ReceiveCaseLine``, flips
   ``Receive.status -> FINALIZED``, emits ``receive_finalized``.
   No external calls happen inside the transaction.
3. ``push_receive_to_integrations`` — runs AFTER materialization
   commits; calls the existing ``submit_zoho_receives`` and
   ``push_luma_receipt`` byte-for-byte unchanged. Updates per-leaf
   ``BoxReceipt.luma_push_status`` + ``Receive.status``.
4. ``retry_push_for_receive`` — re-fires only leaves that are still
   pending/not-ready/failed. Successful leaves are not re-pushed.
   Zoho's Idempotency-Key ``PACK_TRACK_RECEIVE_{packtrack_receipt_id}``
   makes a re-submit safe.

Hard contract preserved from v2.4.1:

* PackTrack idempotency = ``submission_id + submission_line_index``
* Luma compatibility ``box_number = "PT-{packtrack_receipt_id}"``
* No change to either integration payload shape.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlmodel import Session, select

from packtrack.models import (
    BoxReceipt,
    Item,
    LumaPushStatus,
    POEvent,
    POLine,
    PurchaseOrder,
    Receive,
    ReceiveCase,
    ReceiveCaseLine,
    ReceiveStatus,
    ShipmentKind,
    User,
    ZohoMirror,
)
from packtrack.services.box_receipt import (
    compute_accepted,
    compute_confidence,
    compute_luma_readiness,
)
from packtrack.services.receiving import (
    ZohoReceiveSubmission,
    build_photo_url,
    ensure_material_code,
    push_luma_receipt,
    register_material_with_luma,
    submit_zoho_receives,
)

logger = logging.getLogger("packtrack.receiving_v2")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FinalizeIssue:
    """One blocker or warning. ``scope`` and ``ref_id`` point at the
    relevant row so the review template can highlight where it lives."""

    code: str
    message: str
    scope: str = "receive"  # "receive" | "case" | "line"
    ref_id: int | None = None


def _luma_compat_box_number(packtrack_receipt_id: str) -> str:
    """``box_number`` value sent to Luma per the v2.4.1 contract.

    Luma requires ``box_number: z.string().min(1)`` and dedups on
    ``(packtrack_receipt_id, box_number)``. The "PT-" prefix marks
    this as a PackTrack-generated compatibility mirror, NOT a real
    supplier carton id. See docs/PACKTRACK_LUMA_CONTRACT.md § 8.
    """
    return f"PT-{packtrack_receipt_id}"


def _terminal_statuses() -> set[ReceiveStatus]:
    """Status values that mean finalize must not run again."""
    return {
        ReceiveStatus.FINALIZED,
        ReceiveStatus.PUSHED_OK,
        ReceiveStatus.CANCELLED,
    }


def _po_remaining_for_item(
    session: Session, po_id: int, item_id: int,
) -> tuple[float, float] | None:
    """``(remaining, ordered)`` across all PO lines on this item, or None
    when the item isn't on the PO at all."""
    rows = session.exec(
        select(POLine).where(POLine.po_id == po_id, POLine.item_id == item_id)
    ).all()
    if not rows:
        return None
    ordered = sum(float(line.quantity or 0) for line in rows)
    received = sum(float(line.received_quantity or 0) for line in rows)
    return max(0.0, ordered - received), ordered


def validate_receive_for_finalize(
    session: Session, receive: Receive,
) -> tuple[list[FinalizeIssue], list[FinalizeIssue]]:
    """Pure read. Returns (blockers, warnings).

    Blockers (per Stage 2 spec):
      * receive already finalized / pushed / cancelled
      * receive has no PO (v1 route-layer rule, mirrored here as a guard)
      * receive has no cases
      * parcel-mode shipment with no tracking
      * any case with zero lines
      * any case missing vendor_case_number
      * any line missing item
      * any line with declared_quantity <= 0

    Warnings:
      * line for an item not on the PO (would 400 from add_line, but
        catch it here too as a safety net)
      * over-count vs PO line remaining quantity
      * under-count vs PO line remaining quantity (per item summed across
        cases — under-shipment of the open balance)
      * missing material_code on item → Luma push will mark NOT_READY
    """
    blockers: list[FinalizeIssue] = []
    warnings: list[FinalizeIssue] = []

    if receive.status in _terminal_statuses():
        blockers.append(FinalizeIssue(
            code="ALREADY_FINALIZED",
            message=f"Receive is in status {receive.status.value!r}; cannot finalize again.",
        ))
        # No point reporting line-level issues on a finalized receive.
        return blockers, warnings

    if receive.purchase_order_id is None:
        blockers.append(FinalizeIssue(
            code="NO_PO",
            message="Receive is not bound to a purchase order.",
        ))
    else:
        # Pre-flight: surface "Zoho will not be reachable" as a warning
        # so the operator sees it before finalizing. It is NOT a blocker
        # (the operator may still want to materialize for PT records and
        # retry-push after re-syncing); the push service will mark the
        # receive PUSH_FAILED if the mirror is still absent at push time.
        po = session.get(PurchaseOrder, receive.purchase_order_id)
        if po is not None:
            if not (po.zoho_po_id or "").strip():
                warnings.append(FinalizeIssue(
                    code="PO_NO_ZOHO_ID",
                    message=(
                        f"PO {po.po_number} has no zoho_po_id — Zoho receive "
                        "push will fail (re-sync the PO from Settings -> Sync first)."
                    ),
                ))
            else:
                mirror = session.exec(
                    select(ZohoMirror).where(ZohoMirror.zoho_purchaseorder_id == po.zoho_po_id)
                ).first()
                if mirror is None:
                    warnings.append(FinalizeIssue(
                        code="ZOHO_NO_MIRROR",
                        message=(
                            f"No ZohoMirror row for PO {po.po_number} "
                            f"(zoho_po_id={po.zoho_po_id}) — re-sync first or "
                            "expect Zoho push to fail."
                        ),
                    ))

    if receive.shipment_kind == ShipmentKind.PARCEL and not (receive.tracking_number or "").strip():
        blockers.append(FinalizeIssue(
            code="PARCEL_MISSING_TRACKING",
            message="Parcel-mode receive requires a tracking number.",
        ))

    cases = session.exec(
        select(ReceiveCase)
        .where(ReceiveCase.receive_id == receive.id)
        .order_by(ReceiveCase.sequence, ReceiveCase.id)
    ).all()
    if not cases:
        blockers.append(FinalizeIssue(
            code="NO_CASES",
            message="Receive has no cases.",
        ))

    item_totals: dict[int, float] = {}

    for case in cases:
        case_label = case.vendor_case_number or f"#{case.sequence}"
        if not (case.vendor_case_number or "").strip():
            blockers.append(FinalizeIssue(
                code="CASE_MISSING_VENDOR_NUMBER",
                message=f"Case {case_label} is missing a vendor case number.",
                scope="case", ref_id=case.id,
            ))

        lines = session.exec(
            select(ReceiveCaseLine)
            .where(ReceiveCaseLine.receive_case_id == case.id)
            .order_by(ReceiveCaseLine.id)
        ).all()
        if not lines:
            blockers.append(FinalizeIssue(
                code="CASE_NO_LINES",
                message=f"Case {case_label} has no item lines.",
                scope="case", ref_id=case.id,
            ))

        for line in lines:
            qty = compute_accepted(line.declared_quantity, line.counted_quantity)
            if line.item_id is None:
                blockers.append(FinalizeIssue(
                    code="LINE_NO_ITEM",
                    message=f"A line in case {case_label} has no item.",
                    scope="line", ref_id=line.id,
                ))
                continue
            if line.declared_quantity is None or float(line.declared_quantity) <= 0:
                blockers.append(FinalizeIssue(
                    code="LINE_NONPOSITIVE_QTY",
                    message=f"A line in case {case_label} has declared_quantity <= 0.",
                    scope="line", ref_id=line.id,
                ))
                continue

            item = session.get(Item, line.item_id)
            if item is None:
                blockers.append(FinalizeIssue(
                    code="LINE_ITEM_NOT_FOUND",
                    message=f"Line in case {case_label} references a missing item.",
                    scope="line", ref_id=line.id,
                ))
                continue

            if not (item.material_code or "").strip():
                warnings.append(FinalizeIssue(
                    code="ITEM_NO_MATERIAL_CODE",
                    message=(
                        f"{item.name}: no material_code — Luma push will park as NOT_READY "
                        "until owner fills the code."
                    ),
                    scope="line", ref_id=line.id,
                ))

            if receive.purchase_order_id is not None:
                rem = _po_remaining_for_item(
                    session, receive.purchase_order_id, item.id,
                )
                if rem is None:
                    warnings.append(FinalizeIssue(
                        code="ITEM_NOT_ON_PO",
                        message=f"{item.name}: not on this PO — Zoho push will fail.",
                        scope="line", ref_id=line.id,
                    ))

            item_totals[item.id] = item_totals.get(item.id, 0.0) + qty

    # Per-item over/under vs PO remaining (rolled across cases).
    if receive.purchase_order_id is not None:
        for item_id, total in item_totals.items():
            rem = _po_remaining_for_item(session, receive.purchase_order_id, item_id)
            if rem is None:
                continue
            remaining, _ordered = rem
            item = session.get(Item, item_id)
            name = item.name if item else f"item {item_id}"
            if total > remaining and remaining >= 0:
                warnings.append(FinalizeIssue(
                    code="ITEM_OVER_PO",
                    message=(
                        f"{name}: counting {total:g} {item.unit if item else ''} "
                        f"exceeds PO remaining of {remaining:g}."
                    ),
                ))
            elif total < remaining and remaining > 0:
                warnings.append(FinalizeIssue(
                    code="ITEM_UNDER_PO",
                    message=(
                        f"{name}: counting {total:g} of {remaining:g} remaining on PO."
                    ),
                ))

    return blockers, warnings


# ---------------------------------------------------------------------------
# Materialization
# ---------------------------------------------------------------------------


def _stable_line_order(
    session: Session, receive_id: int,
) -> list[tuple[ReceiveCase, ReceiveCaseLine]]:
    """Deterministic iteration order for materialization.

    Cases by ``(sequence, id)``, lines within each case by ``id``.
    Two finalize attempts on the same receive yield the same global
    index for the same line — important so ``submission_line_index``
    is stable across retries.
    """
    out: list[tuple[ReceiveCase, ReceiveCaseLine]] = []
    cases = session.exec(
        select(ReceiveCase)
        .where(ReceiveCase.receive_id == receive_id)
        .order_by(ReceiveCase.sequence, ReceiveCase.id)
    ).all()
    for case in cases:
        lines = session.exec(
            select(ReceiveCaseLine)
            .where(ReceiveCaseLine.receive_case_id == case.id)
            .order_by(ReceiveCaseLine.id)
        ).all()
        for line in lines:
            out.append((case, line))
    return out


def materialize_box_receipts(
    session: Session, receive: Receive, user: User,
) -> list[BoxReceipt]:
    """Create exactly one BoxReceipt per ReceiveCaseLine inside a single
    DB transaction. Idempotent: lines that already have
    ``box_receipt_id`` are skipped.

    Does NOT call Zoho or Luma. The caller (route) commits and then
    invokes ``push_receive_to_integrations`` separately.
    """
    materialized: list[BoxReceipt] = []
    now = datetime.utcnow()

    for index, (_case, line) in enumerate(_stable_line_order(session, receive.id), start=1):
        if line.box_receipt_id is not None:
            existing = session.get(BoxReceipt, line.box_receipt_id)
            if existing is not None:
                materialized.append(existing)
                continue

        item = session.get(Item, line.item_id)
        if item is None:
            # Validation should have blocked this; safety net so we don't
            # write a half-empty BoxReceipt.
            continue

        declared = float(line.declared_quantity or 0)
        counted = float(line.counted_quantity) if line.counted_quantity is not None else None
        accepted = compute_accepted(declared, counted)
        confidence = compute_confidence(counted)
        luma_status = compute_luma_readiness(item.material_code)

        receipt_id = uuid.uuid4().hex
        box = BoxReceipt(
            packtrack_receipt_id=receipt_id,
            purchase_order_id=line.purchase_order_id,
            shipment_id=receive.shipment_id,
            item_id=item.id,
            material_code=(item.material_code or "").strip() or None,
            material_name=(item.name or "")[:240],
            supplier=item.vendor,
            supplier_lot_number=(line.supplier_lot_number or "").strip() or None,
            box_number=_luma_compat_box_number(receipt_id),
            submission_id=receive.submission_id,
            submission_line_index=index,
            declared_quantity=declared,
            counted_quantity=counted,
            accepted_quantity=accepted,
            unit_of_measure=(line.unit_of_measure or item.unit or "EACH")[:20],
            confidence=confidence,
            received_by_user_id=user.id,
            received_at=now,
            luma_push_status=luma_status,
            photo_paths=line.photo_paths or None,
            notes=line.notes,
            receive_id=receive.id,
            receive_case_line_id=line.id,
            created_at=now,
            updated_at=now,
        )
        session.add(box)
        session.flush()
        line.box_receipt_id = box.id
        materialized.append(box)

    receive.status = ReceiveStatus.FINALIZED
    receive.finalized_at = now
    receive.finalized_by_user_id = user.id
    receive.updated_at = now

    if receive.purchase_order_id is not None:
        session.add(POEvent(
            po_id=receive.purchase_order_id,
            kind="receive_finalized",
            message=(
                f"Receive {receive.receive_number} finalized with "
                f"{len(materialized)} box receipt{'s' if len(materialized) != 1 else ''}."
            ),
            actor_id=user.id,
        ))

    session.commit()
    for box in materialized:
        session.refresh(box)
    session.refresh(receive)
    return materialized


# ---------------------------------------------------------------------------
# Push (Zoho + Luma) — runs AFTER materialization commits
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LeafPushResult:
    """One per BoxReceipt the push attempted to handle."""

    box_receipt_id: int
    item_name: str
    luma_status: LumaPushStatus
    luma_error: str | None
    zoho_status: str | None  # e.g. "committed" | "blocked" | "validation_failed"
    zoho_error: str | None


@dataclass(frozen=True)
class ReceivePushOutcome:
    receive_status: ReceiveStatus
    results: list[LeafPushResult]


def build_zoho_receive_notes(
    session: Session,
    receive: Receive,
    box_receipts: list[BoxReceipt],
) -> str:
    """Compose a human-readable ``notes`` string for the Zoho receive.

    The upstream zoho-integration-service prepends its own audit trace
    ("[zoho-integration] pack_track_receipt_id=...") to whatever we send,
    so this function MUST NOT duplicate those ids. We focus on the
    information an operations/warehouse user needs:

      * which Receive this came from
      * which PO + vendor case(s)
      * who finalized it
      * the items + quantities
      * any operator-supplied free-text notes from the Receive

    Returns plain text, joined with ``\\n``. Capped at ~1800 chars so
    the upstream service's trace + truncation marker still fit under
    Zoho's ~2000-char limit.
    """
    po = session.get(PurchaseOrder, receive.purchase_order_id) if receive.purchase_order_id else None
    operator = session.get(User, receive.finalized_by_user_id or receive.received_by_user_id)

    # Unique vendor case numbers, in declaration order, NULL-safe.
    cases = session.exec(
        select(ReceiveCase)
        .where(ReceiveCase.receive_id == receive.id)
        .order_by(ReceiveCase.sequence, ReceiveCase.id)
    ).all()
    case_labels: list[str] = []
    seen_cases: set[str] = set()
    for case in cases:
        label = (case.vendor_case_number or "").strip() or f"#{case.sequence}"
        if label not in seen_cases:
            seen_cases.add(label)
            case_labels.append(label)

    # Per-item line summary (one bullet per leaf, ordered by submission_line_index).
    item_lines: list[str] = []
    for box in sorted(box_receipts, key=lambda b: (b.submission_line_index or 0, b.id or 0)):
        qty = box.accepted_quantity if box.accepted_quantity is not None else box.declared_quantity
        unit = (box.unit_of_measure or "").strip() or ""
        name = (box.material_name or "").strip() or f"item {box.item_id}"
        item_lines.append(f"  - {name}: {qty:g}{(' ' + unit) if unit else ''}")

    lines: list[str] = ["Received via PackTrack", ""]
    lines.append(f"Receive: {receive.receive_number}")
    if po is not None:
        lines.append(f"PO: {po.po_number}")
    if case_labels:
        # "Case: A; B; C" — keep short to avoid bloating the description.
        lines.append(f"Case: {'; '.join(case_labels[:6])}" + (" (+more)" if len(case_labels) > 6 else ""))
    if operator is not None:
        name = (operator.name or "").strip() or (operator.email or f"user {operator.id}")
        lines.append(f"Operator: {name}")
    if receive.delivery_date is not None:
        lines.append(f"Delivery date: {receive.delivery_date.isoformat()}")
    if item_lines:
        lines.append("")
        lines.append(f"Items ({len(item_lines)}):")
        lines.extend(item_lines)

    receive_note = (receive.notes or "").strip()
    if receive_note:
        lines.append("")
        lines.append("Notes:")
        lines.append(receive_note)

    out = "\n".join(lines)
    if len(out) > 1800:
        out = out[:1800].rstrip() + "\n[truncated]"
    return out


_ZOHO_OK_STATUSES = {"committed", "blocked", "skipped"}
"""Per-line Zoho outcomes that do NOT cause the receive to be
``PUSH_FAILED``.

  * ``committed``  — Zoho accepted the receive.
  * ``blocked``    — zoho-integration-service has live writes turned off
                     (``LIVE_WRITE_DISABLED``). Local state is consistent;
                     this is an intentional operator-controlled state and
                     the per-line Zoho status surfaces it for visibility.
  * ``skipped``    — zero-quantity submission; nothing to send.

Everything else is a real failure that must surface to the operator
and flip ``Receive.status -> PUSH_FAILED``:

  * ``disabled`` / ``not_configured`` — global integration is off in
    config; treating these as success would silently mask "Zoho was
    never updated" and leave PackTrack disagreeing with Zoho.
  * ``validation_failed`` (incl. ``PO_LINE_ITOM_NOT_FOUND`` when an item
    on the receive isn't on the synced Zoho PO mirror).
  * ``gateway_error`` / ``rate_limited`` / ``idempotency_conflict`` /
    ``config_error`` / ``auth_failed`` — service errors.
  * ``missing_mirror`` — synthetic status this service emits when the
    receive's PO has no synced ``ZohoMirror`` row (i.e. we couldn't
    even attempt a Zoho call). See ``_synth_missing_mirror_results``.
"""

# Synthetic per-leaf statuses this service emits (not from the Zoho client).
_ZOHO_STATUS_MISSING_MIRROR = "missing_mirror"


def _eligible_for_push(box: BoxReceipt) -> bool:
    """Re-fireable leaf states (retry semantics).

    PUSHED / DUPLICATE / DRY_RUN_OK leaves are not re-pushed; only
    NOT_READY / PENDING / FAILED leaves are."""
    return box.luma_push_status in (
        LumaPushStatus.NOT_READY,
        LumaPushStatus.PENDING,
        LumaPushStatus.FAILED,
    )


def _zoho_line_lookup(mirror: ZohoMirror | None, zoho_item_id: str | None) -> str:
    """Pull ``line_item_id`` for this item out of the mirror payload.

    Returns "" when the mirror has no line for this item — caller
    surfaces that as ``PO_LINE_ITEM_NOT_FOUND`` via the existing
    ``submit_zoho_receives`` validation branch."""
    if mirror is None or not zoho_item_id:
        return ""
    for line in mirror.line_items or []:
        if str(line.get("item_id") or "") == str(zoho_item_id):
            return str(line.get("line_item_id") or "")
    return ""


def _mirror_for_receive(session: Session, receive: Receive) -> ZohoMirror | None:
    if receive.purchase_order_id is None:
        return None
    po = session.get(PurchaseOrder, receive.purchase_order_id)
    if po is None or not po.zoho_po_id:
        return None
    return session.exec(
        select(ZohoMirror).where(ZohoMirror.zoho_purchaseorder_id == po.zoho_po_id)
    ).first()


def _po_number_for_receive(session: Session, receive: Receive) -> str:
    if receive.purchase_order_id is None:
        return ""
    po = session.get(PurchaseOrder, receive.purchase_order_id)
    return (po.po_number if po else "") or ""


def push_receive_to_integrations(
    session: Session,
    receive: Receive,
    user: User,
    box_receipts: list[BoxReceipt] | None = None,
) -> ReceivePushOutcome:
    """Push the receive's eligible leaves to Luma and Zoho.

    Must be called AFTER ``materialize_box_receipts`` has committed
    (idempotent: re-calling on already-PUSHED leaves is a no-op).
    Updates per-leaf ``BoxReceipt.luma_push_status`` and the receive's
    overall status, then commits.
    """
    if box_receipts is None:
        box_receipts = session.exec(
            select(BoxReceipt).where(BoxReceipt.receive_id == receive.id)
        ).all()
    eligible = [b for b in box_receipts if _eligible_for_push(b)]

    mirror = _mirror_for_receive(session, receive)
    po_number = _po_number_for_receive(session, receive)
    now = datetime.utcnow()

    results: dict[int, LeafPushResult] = {}

    # ── Luma (per leaf) ────────────────────────────────────────────────
    for box in eligible:
        item = session.get(Item, box.item_id) if box.item_id else None
        item_name = (item.name if item else box.material_name) or "?"

        luma_status: LumaPushStatus = box.luma_push_status
        luma_err: str | None = None

        if item is not None:
            # Item must have a non-empty material_code before Luma push.
            # ``ensure_material_code`` may assign ``PT-{id:05d}``; if so,
            # update the BoxReceipt snapshot so the payload sent to Luma
            # carries the fresh code.
            ensure_material_code(session, item)
            if not (box.material_code or "").strip() and (item.material_code or "").strip():
                box.material_code = item.material_code

        if (box.material_code or "").strip() and item is not None:
            register_material_with_luma(item)  # best-effort
            photo_urls = [build_photo_url(fn) for fn in (box.photo_paths or [])]
            ok, err, resp = push_luma_receipt(
                box, po_number, photo_urls, received_by=user.name,
            )
            if ok:
                luma_status = LumaPushStatus.PUSHED
                box.luma_pushed_at = now
                box.luma_response = resp
            else:
                luma_status = LumaPushStatus.FAILED
                box.luma_response = {"error": err}
                luma_err = err
        else:
            luma_status = LumaPushStatus.NOT_READY
            luma_err = "missing material_code"

        box.luma_push_status = luma_status
        box.updated_at = now
        session.add(box)
        results[box.id] = LeafPushResult(
            box_receipt_id=box.id,
            item_name=item_name,
            luma_status=luma_status,
            luma_error=luma_err,
            zoho_status=None,
            zoho_error=None,
        )

    # Ensure every leaf has a baseline result entry, even ones that were
    # not eligible for Luma re-push (so the result page shows them and
    # the retry-after-Zoho-fix path can record their Zoho outcome).
    for box in box_receipts:
        if box.id not in results:
            item_skipped = session.get(Item, box.item_id) if box.item_id else None
            name = (item_skipped.name if item_skipped else box.material_name) or "?"
            results[box.id] = LeafPushResult(
                box_receipt_id=box.id, item_name=name,
                luma_status=box.luma_push_status, luma_error=None,
                zoho_status=None, zoho_error=None,
            )

    # ── Zoho ───────────────────────────────────────────────────────────
    # IMPORTANT: build a submission for EVERY leaf on the receive, not
    # just the Luma-eligible ones. A leaf can have its Luma push succeed
    # and its Zoho push fail — on retry we still need Zoho to be re-fired
    # for that leaf. ``submit_zoho_receives`` keys idempotency on
    # ``PACK_TRACK_RECEIVE_{packtrack_receipt_id}`` so re-submission of
    # already-committed leaves is safe.
    zoho_submissions: list[ZohoReceiveSubmission] = []
    for box in box_receipts:
        item = session.get(Item, box.item_id) if box.item_id else None
        zoho_item_id = (item.zoho_item_id if item else None) or ""
        zoho_line_item_id = _zoho_line_lookup(mirror, zoho_item_id)
        sub = ZohoReceiveSubmission(
            box_receipt_id=box.id,
            packtrack_receipt_id=box.packtrack_receipt_id,
            zoho_item_id=zoho_item_id,
            zoho_line_item_id=zoho_line_item_id,
            quantity=float(box.accepted_quantity or box.declared_quantity or 0),
            item_name=(item.name if item else box.material_name) or "?",
        )
        zoho_submissions.append(sub)

    zoho_failure_reasons: list[str] = []

    if not zoho_submissions:
        zoho_results = []
    elif mirror is None:
        # Receive has leaves to push but the PO has no synced ZohoMirror —
        # we must NOT silently report this as success. The operator sees
        # "missing_mirror" on every leaf; the receive is marked
        # ``PUSH_FAILED`` so retry-push is available once the mirror is
        # synced. submit_zoho_receives is intentionally not called.
        po = session.get(PurchaseOrder, receive.purchase_order_id) if receive.purchase_order_id else None
        zoho_po_id = (po.zoho_po_id if po else None) or ""
        if zoho_po_id:
            reason = (
                f"No ZohoMirror row found for PO {po.po_number if po else '?'} "
                f"(zoho_po_id={zoho_po_id}). Re-sync from Settings -> Sync."
            )
        else:
            reason = (
                f"PO {po.po_number if po else '?'} has no zoho_po_id; "
                "this PO was never synced with Zoho — Zoho receive will not "
                "happen until the PO is mapped or pushed."
            )
        zoho_failure_reasons.append(f"missing_mirror ({len(zoho_submissions)})")
        zoho_results = []  # explicit no-call
        for sub in zoho_submissions:
            prev = results[sub.box_receipt_id]
            results[sub.box_receipt_id] = LeafPushResult(
                box_receipt_id=prev.box_receipt_id,
                item_name=prev.item_name,
                luma_status=prev.luma_status,
                luma_error=prev.luma_error,
                zoho_status=_ZOHO_STATUS_MISSING_MIRROR,
                zoho_error=reason,
            )
    else:
        zoho_results = submit_zoho_receives(
            mirror, zoho_submissions,
            operator=user,
            session_id=(receive.submission_id or "")[:64],
            notes=build_zoho_receive_notes(session, receive, box_receipts),
        )
        for zr in zoho_results:
            prev = results.get(zr.submission.box_receipt_id)
            if prev is None:
                continue
            results[zr.submission.box_receipt_id] = LeafPushResult(
                box_receipt_id=prev.box_receipt_id,
                item_name=prev.item_name,
                luma_status=prev.luma_status,
                luma_error=prev.luma_error,
                zoho_status=zr.status,
                zoho_error=zr.message if zr.status not in _ZOHO_OK_STATUSES else None,
            )

    # ── Overall receive status ─────────────────────────────────────────
    all_box_ids = {b.id for b in box_receipts}
    final_states: list[LumaPushStatus] = []
    for bid in all_box_ids:
        b = session.get(BoxReceipt, bid)
        if b is not None:
            final_states.append(b.luma_push_status)

    luma_ok = all(
        s in (LumaPushStatus.PUSHED, LumaPushStatus.DUPLICATE, LumaPushStatus.DRY_RUN_OK)
        for s in final_states
    )
    # Tally Zoho failures from BOTH the real per-line results and the
    # synthesized missing_mirror results — and bucket the reasons so the
    # POEvent message is operator-readable without diving into the row.
    zoho_status_counts: dict[str, int] = {}
    for r in results.values():
        if r.zoho_status:
            zoho_status_counts[r.zoho_status] = zoho_status_counts.get(r.zoho_status, 0) + 1
    bad_zoho_buckets = {
        s: n for s, n in zoho_status_counts.items() if s not in _ZOHO_OK_STATUSES
    }
    zoho_ok = not bad_zoho_buckets

    if luma_ok and zoho_ok:
        receive.status = ReceiveStatus.PUSHED_OK
        receive.pushed_at = now
        kind = "receive_pushed_ok"
        msg = f"Receive {receive.receive_number} pushed to Luma + Zoho."
    else:
        receive.status = ReceiveStatus.PUSH_FAILED
        kind = "receive_push_failed"
        bad_luma = sum(
            1 for s in final_states
            if s not in (LumaPushStatus.PUSHED, LumaPushStatus.DUPLICATE, LumaPushStatus.DRY_RUN_OK)
        )
        bad_zoho_str = ", ".join(
            f"{s}={n}" for s, n in sorted(bad_zoho_buckets.items())
        ) or "0"
        msg = (
            f"Receive {receive.receive_number} push had failures — "
            f"luma_failed={bad_luma}, zoho_failed=[{bad_zoho_str}]."
        )
    receive.updated_at = now
    if receive.purchase_order_id is not None:
        session.add(POEvent(
            po_id=receive.purchase_order_id,
            kind=kind,
            message=msg,
            actor_id=user.id,
        ))
    session.commit()

    return ReceivePushOutcome(
        receive_status=receive.status,
        results=sorted(results.values(), key=lambda r: r.box_receipt_id),
    )


def retry_push_for_receive(
    session: Session, receive: Receive, user: User,
) -> ReceivePushOutcome:
    """Re-fire the push for leaves that aren't already in a terminal-ok
    state. Already PUSHED / DUPLICATE / DRY_RUN_OK leaves are skipped.

    Resets stale ``FAILED`` Luma status to ``PENDING`` for leaves that
    now have a material_code (the operator likely filled it in between
    finalize and retry).
    """
    boxes = session.exec(
        select(BoxReceipt).where(BoxReceipt.receive_id == receive.id)
    ).all()
    bumped = False
    for b in boxes:
        if b.luma_push_status is LumaPushStatus.NOT_READY and (b.material_code or "").strip():
            b.luma_push_status = LumaPushStatus.PENDING
            bumped = True
    if bumped:
        session.commit()
    return push_receive_to_integrations(session, receive, user, box_receipts=boxes)
