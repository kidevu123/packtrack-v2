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


_ZOHO_OK_STATUSES = {"committed", "blocked", "skipped", "disabled", "not_configured"}
"""Per-line Zoho outcomes that do NOT cause the receive to be
``PUSH_FAILED``. ``blocked`` / ``disabled`` / ``not_configured``
means "live writes off / ops decided" — local state is consistent;
that's deliberate non-failure."""


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

    # ── Zoho (one submission per eligible leaf) ────────────────────────
    zoho_submissions: list[ZohoReceiveSubmission] = []
    box_by_submission: dict[int, BoxReceipt] = {}
    for box in eligible:
        item = session.get(Item, box.item_id) if box.item_id else None
        zoho_item_id = (item.zoho_item_id if item else None) or ""
        zoho_line_item_id = _zoho_line_lookup(mirror, zoho_item_id)
        sub = ZohoReceiveSubmission(
            box_receipt_id=box.id,
            packtrack_receipt_id=box.packtrack_receipt_id,
            zoho_item_id=zoho_item_id,
            zoho_line_item_id=zoho_line_item_id,
            quantity=float(box.accepted_quantity or box.declared_quantity or 0),
            item_name=item.name if item else box.material_name,
        )
        zoho_submissions.append(sub)
        box_by_submission[box.id] = box

    if mirror is not None and zoho_submissions:
        zoho_results = submit_zoho_receives(
            mirror, zoho_submissions,
            operator=user,
            session_id=(receive.submission_id or "")[:64],
            notes=receive.notes,
        )
    else:
        zoho_results = []

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
            zoho_error=zr.message if not zr.ok and zr.status not in _ZOHO_OK_STATUSES else None,
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
    zoho_ok = all(
        (r.status in _ZOHO_OK_STATUSES) for r in zoho_results
    )
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
        bad_zoho = sum(1 for r in zoho_results if r.status not in _ZOHO_OK_STATUSES)
        msg = (
            f"Receive {receive.receive_number} push had failures — "
            f"luma_failed={bad_luma}, zoho_failed={bad_zoho}."
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
