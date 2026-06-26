"""Receiving vNext routes (v2.5.0 Stage 1).

Mounted at ``/receive/v2`` and gated by ``settings.RECEIVING_VNEXT_ENABLED``
— when the flag is False every route returns 404 so neither
operators nor crawlers can stumble into a half-built flow. Legacy
``/receive/{zoho_po_id}`` is unchanged and remains the only enabled
receive path in production.

Stage 1 = draft + counting only:
  * create receive from a PO
  * view the receive page
  * add/edit/delete cases
  * add/edit/delete case lines
  * PO-scoped item search

Finalize, BoxReceipt materialization, Zoho push, and Luma push are
Stage 2 (v2.6.0).
"""
from __future__ import annotations

import shutil
import uuid
from datetime import date, datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session

from packtrack.config import settings
from packtrack.db import get_session
from packtrack.deps import require_user
from packtrack.models import (
    Attachment,
    AttachmentKind,
    BoxReceipt,
    Item,
    POEvent,
    POLine,
    PurchaseOrder,
    Receive,
    ReceiveCase,
    ReceiveCaseLine,
    ReceiveStatus,
    Role,
    ShipmentKind,
    User,
)
from packtrack.services.receiving_v2 import (
    generate_receive_number,
    items_for_po,
    make_submission_id,
    next_case_sequence,
    po_item_choices,
    receive_cases,
    totals_by_item,
)

router = APIRouter(prefix="/receive/v2")


# ---------------------------------------------------------------------------
# Flag gate + access control
# ---------------------------------------------------------------------------


def _is_duplicate_case_violation(msg: str) -> bool:
    """Recognize the duplicate-case-number integrity error from either
    Postgres (index name in message) or SQLite (constraint text in
    message). Returning True here lets the route convert the 500 from
    a raw IntegrityError into a clean 409 for the operator UI.
    """
    if "uq_receive_cases_receive_case_number" in msg:
        return True
    return (
        "UNIQUE constraint failed" in msg
        and "receive_cases" in msg
        and "vendor_case_number" in msg
    )


def _require_vnext_flag() -> None:
    """Returns 404 on every Receiving vNext route when the flag is off.

    404 (not 403) so the existence of the route surface is not
    advertised to clients. The legacy /receive flow keeps working
    regardless.
    """
    if not settings.RECEIVING_VNEXT_ENABLED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


def _require_receiving_or_owner(user: User) -> None:
    if user.role not in (Role.OWNER, Role.RECEIVING):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)


def _load_receive(session: Session, receive_id: int) -> Receive:
    rec = session.get(Receive, receive_id)
    if rec is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return rec


def _load_case(session: Session, receive_id: int, case_id: int) -> ReceiveCase:
    case = session.get(ReceiveCase, case_id)
    if case is None or case.receive_id != receive_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return case


def _load_line(session: Session, receive_id: int, line_id: int) -> ReceiveCaseLine:
    line = session.get(ReceiveCaseLine, line_id)
    if line is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    case = session.get(ReceiveCase, line.receive_case_id)
    if case is None or case.receive_id != receive_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return line


# ---------------------------------------------------------------------------
# Templates helper (lazy import to avoid circular dep with main)
# ---------------------------------------------------------------------------


def _templates():
    from packtrack.main import templates
    return templates


def _render(request: Request, name: str, ctx: dict) -> HTMLResponse:
    return _templates().TemplateResponse(request, name, ctx)


def _bump_updated(rec: Receive) -> None:
    rec.updated_at = datetime.utcnow()
    if rec.status == ReceiveStatus.DRAFT:
        rec.status = ReceiveStatus.COUNTING


# ---------------------------------------------------------------------------
# Create + view
# ---------------------------------------------------------------------------


@router.get("/new", response_class=HTMLResponse)
def new_receive_form(
    request: Request,
    po_id: int,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
    _flag: None = Depends(_require_vnext_flag),
):
    """Confirmation page for starting a new receive.

    GET must NOT mutate state — refreshes, crawlers, and prefetching
    would otherwise litter the table with empty drafts. The page just
    shows the PO context and a POST button; ``POST /receive/v2/new``
    is what actually creates the Receive.

    v1 requires ``po_id``. The schema is multi-PO-ready
    (``Receive.purchase_order_id`` is nullable) but the UI does not
    expose that until a later stage.
    """
    _require_receiving_or_owner(user)
    po = session.get(PurchaseOrder, po_id)
    if po is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PO not found")
    return _render(
        request,
        "receive_v2/new.html",
        {"user": user, "po": po, "choices": po_item_choices(session, po_id)},
    )


@router.post("/new", response_class=HTMLResponse)
def create_receive(
    request: Request,
    po_id: int = Form(...),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
    _flag: None = Depends(_require_vnext_flag),
):
    """Actually create the draft Receive. 303 to the receive page.

    POST so refreshes/prefetching cannot accidentally create rows. PO
    is taken from the form body, not the query string.
    """
    _require_receiving_or_owner(user)
    po = session.get(PurchaseOrder, po_id)
    if po is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PO not found")
    now = datetime.utcnow()
    rec = Receive(
        receive_number=generate_receive_number(session, now=now),
        purchase_order_id=po_id,
        shipment_kind=ShipmentKind.PARCEL,
        delivery_date=date.today(),
        received_by_user_id=user.id,
        status=ReceiveStatus.DRAFT,
        submission_id=make_submission_id(),
        created_at=now,
        updated_at=now,
    )
    session.add(rec)
    session.commit()
    session.refresh(rec)
    return RedirectResponse(url=f"/receive/v2/{rec.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/{receive_id}", response_class=HTMLResponse)
def view_receive(
    request: Request,
    receive_id: int,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
    _flag: None = Depends(_require_vnext_flag),
):
    _require_receiving_or_owner(user)
    rec = _load_receive(session, receive_id)
    po = session.get(PurchaseOrder, rec.purchase_order_id) if rec.purchase_order_id else None
    cases = receive_cases(session, rec.id)
    case_to_lines: dict[int, list[ReceiveCaseLine]] = {}
    case_to_items: dict[int, dict[int, Item]] = {}
    for case in cases:
        lines = session.exec(
            __import__("sqlmodel").select(ReceiveCaseLine).where(ReceiveCaseLine.receive_case_id == case.id).order_by(ReceiveCaseLine.id)
        ).all()
        case_to_lines[case.id] = lines
        items = {
            it.id: it
            for it in (session.get(Item, line.item_id) for line in lines)
            if it is not None
        }
        case_to_items[case.id] = items
    totals = totals_by_item(session, rec.id)
    packing_list = (
        session.get(Attachment, rec.packing_list_attachment_id)
        if rec.packing_list_attachment_id
        else None
    )
    choices = po_item_choices(session, rec.purchase_order_id) if rec.purchase_order_id else []
    return _render(
        request,
        "receive_v2/index.html",
        {
            "user": user,
            "receive": rec,
            "po": po,
            "cases": cases,
            "case_lines": case_to_lines,
            "case_items": case_to_items,
            "totals": totals,
            "packing_list": packing_list,
            "choices": choices,
        },
    )


# ---------------------------------------------------------------------------
# Case CRUD (HTMX)
# ---------------------------------------------------------------------------


@router.post("/{receive_id}/cases", response_class=HTMLResponse)
def add_case(
    request: Request,
    receive_id: int,
    vendor_case_number: str = Form(""),
    case_kind: str = Form(""),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
    _flag: None = Depends(_require_vnext_flag),
):
    _require_receiving_or_owner(user)
    rec = _load_receive(session, receive_id)
    vendor_case_number = (vendor_case_number or "").strip() or None
    case = ReceiveCase(
        receive_id=rec.id,
        vendor_case_number=vendor_case_number,
        sequence=next_case_sequence(session, rec.id),
        case_kind=case_kind or None,
    )
    session.add(case)
    try:
        _bump_updated(rec)
        session.commit()
    except Exception as exc:
        session.rollback()
        # Friendly surface for the partial-UNIQUE duplicate-case-number
        # violation. SQLite/Postgres both surface this as IntegrityError
        # with the index name in the message; we match on the index name
        # to avoid taking a hard dep on the driver text.
        msg = str(exc)
        if _is_duplicate_case_violation(msg):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Case {vendor_case_number!r} already exists on this receive",
            ) from None
        raise
    session.refresh(case)
    choices = po_item_choices(session, rec.purchase_order_id) if rec.purchase_order_id else []
    return _render(
        request,
        "receive_v2/_case_block.html",
        {
            "case": case,
            "lines": [],
            "items": {},
            "receive": rec,
            "choices": choices,
        },
    )


@router.post("/{receive_id}/cases/{case_id}", response_class=HTMLResponse)
def edit_case(
    request: Request,
    receive_id: int,
    case_id: int,
    vendor_case_number: str = Form(""),
    case_kind: str = Form(""),
    notes: str = Form(""),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
    _flag: None = Depends(_require_vnext_flag),
):
    _require_receiving_or_owner(user)
    rec = _load_receive(session, receive_id)
    case = _load_case(session, receive_id, case_id)
    case.vendor_case_number = (vendor_case_number or "").strip() or None
    case.case_kind = case_kind or None
    case.notes = notes or None
    case.updated_at = datetime.utcnow()
    try:
        _bump_updated(rec)
        session.commit()
    except Exception as exc:
        session.rollback()
        if _is_duplicate_case_violation(str(exc)):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Case {case.vendor_case_number!r} already exists on this receive",
            ) from None
        raise
    session.refresh(case)
    lines = session.exec(
        __import__("sqlmodel").select(ReceiveCaseLine).where(ReceiveCaseLine.receive_case_id == case.id).order_by(ReceiveCaseLine.id)
    ).all()
    items = {
        it.id: it
        for it in (session.get(Item, line.item_id) for line in lines)
        if it is not None
    }
    choices = po_item_choices(session, rec.purchase_order_id) if rec.purchase_order_id else []
    return _render(
        request,
        "receive_v2/_case_block.html",
        {"case": case, "lines": lines, "items": items, "receive": rec, "choices": choices},
    )


@router.post("/{receive_id}/cases/{case_id}/delete", response_class=HTMLResponse)
def delete_case(
    request: Request,
    receive_id: int,
    case_id: int,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
    _flag: None = Depends(_require_vnext_flag),
):
    _require_receiving_or_owner(user)
    rec = _load_receive(session, receive_id)
    case = _load_case(session, receive_id, case_id)
    # CASCADE in the migration drops the case lines automatically.
    session.delete(case)
    _bump_updated(rec)
    session.commit()
    # Empty body — HTMX target removes itself.
    return HTMLResponse(content="", status_code=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Line CRUD (HTMX)
# ---------------------------------------------------------------------------


@router.post("/{receive_id}/cases/{case_id}/lines", response_class=HTMLResponse)
def add_line(
    request: Request,
    receive_id: int,
    case_id: int,
    item_id: int = Form(...),
    declared_quantity: float = Form(...),
    counted_quantity: str = Form(""),
    supplier_lot_number: str = Form(""),
    notes: str = Form(""),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
    _flag: None = Depends(_require_vnext_flag),
):
    _require_receiving_or_owner(user)
    rec = _load_receive(session, receive_id)
    case = _load_case(session, receive_id, case_id)
    if rec.purchase_order_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="receive has no PO")
    item = session.get(Item, item_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown item")
    # PO-scope guard — Stage 1 only allows items present on the PO.
    on_po = session.exec(
        __import__("sqlmodel").select(POLine.id)
        .where(POLine.po_id == rec.purchase_order_id, POLine.item_id == item_id)
    ).first()
    if on_po is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="item is not on this PO",
        )
    if declared_quantity is None or float(declared_quantity) <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="declared_quantity must be > 0",
        )
    counted = float(counted_quantity) if counted_quantity not in ("", None) else None
    line = ReceiveCaseLine(
        receive_case_id=case.id,
        purchase_order_id=rec.purchase_order_id,
        po_line_id=on_po,
        item_id=item_id,
        declared_quantity=float(declared_quantity),
        counted_quantity=counted,
        unit_of_measure=item.unit or "EACH",
        supplier_lot_number=(supplier_lot_number or "").strip() or None,
        notes=notes or None,
    )
    session.add(line)
    _bump_updated(rec)
    session.commit()
    session.refresh(line)
    return _render(
        request,
        "receive_v2/_line_row.html",
        {"line": line, "item": item, "case": case, "receive": rec},
    )


@router.post("/{receive_id}/lines/{line_id}", response_class=HTMLResponse)
def edit_line(
    request: Request,
    receive_id: int,
    line_id: int,
    declared_quantity: float = Form(...),
    counted_quantity: str = Form(""),
    supplier_lot_number: str = Form(""),
    notes: str = Form(""),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
    _flag: None = Depends(_require_vnext_flag),
):
    _require_receiving_or_owner(user)
    rec = _load_receive(session, receive_id)
    line = _load_line(session, receive_id, line_id)
    if declared_quantity is None or float(declared_quantity) <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="declared_quantity must be > 0",
        )
    line.declared_quantity = float(declared_quantity)
    line.counted_quantity = float(counted_quantity) if counted_quantity not in ("", None) else None
    line.supplier_lot_number = (supplier_lot_number or "").strip() or None
    line.notes = notes or None
    line.updated_at = datetime.utcnow()
    _bump_updated(rec)
    session.commit()
    session.refresh(line)
    item = session.get(Item, line.item_id)
    case = session.get(ReceiveCase, line.receive_case_id)
    return _render(
        request,
        "receive_v2/_line_row.html",
        {"line": line, "item": item, "case": case, "receive": rec},
    )


@router.post("/{receive_id}/lines/{line_id}/delete", response_class=HTMLResponse)
def delete_line(
    request: Request,
    receive_id: int,
    line_id: int,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
    _flag: None = Depends(_require_vnext_flag),
):
    _require_receiving_or_owner(user)
    rec = _load_receive(session, receive_id)
    line = _load_line(session, receive_id, line_id)
    session.delete(line)
    _bump_updated(rec)
    session.commit()
    return HTMLResponse(content="", status_code=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Item search (HTMX, scoped to PO)
# ---------------------------------------------------------------------------


@router.get("/{receive_id}/items-search", response_class=HTMLResponse)
def items_search(
    request: Request,
    receive_id: int,
    q: str | None = None,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
    _flag: None = Depends(_require_vnext_flag),
):
    _require_receiving_or_owner(user)
    rec = _load_receive(session, receive_id)
    if rec.purchase_order_id is None:
        return HTMLResponse(content="", status_code=status.HTTP_200_OK)
    items = items_for_po(session, rec.purchase_order_id, q=q)
    return _render(
        request,
        "receive_v2/_items_search.html",
        {"items": items},
    )


# ---------------------------------------------------------------------------
# Totals (HTMX swap — useful when an external trigger refreshes the rail)
# ---------------------------------------------------------------------------


@router.get("/{receive_id}/totals", response_class=HTMLResponse)
def view_totals(
    request: Request,
    receive_id: int,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
    _flag: None = Depends(_require_vnext_flag),
):
    _require_receiving_or_owner(user)
    rec = _load_receive(session, receive_id)
    return _render(
        request,
        "receive_v2/_totals.html",
        {"totals": totals_by_item(session, rec.id)},
    )


# ---------------------------------------------------------------------------
# Stage 2 (v2.6.0) — review / finalize / retry-push
# ---------------------------------------------------------------------------


@router.get("/{receive_id}/review", response_class=HTMLResponse)
def review_receive(
    request: Request,
    receive_id: int,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
    _flag: None = Depends(_require_vnext_flag),
):
    """Show blockers + warnings + a confirmation page. Pure read."""
    from packtrack.services.receiving_v2_finalize import (
        validate_receive_for_finalize,
    )

    _require_receiving_or_owner(user)
    rec = _load_receive(session, receive_id)
    po = session.get(PurchaseOrder, rec.purchase_order_id) if rec.purchase_order_id else None
    blockers, warnings = validate_receive_for_finalize(session, rec)
    cases = receive_cases(session, rec.id)
    totals = totals_by_item(session, rec.id)
    return _render(
        request,
        "receive_v2/review.html",
        {
            "user": user,
            "receive": rec,
            "po": po,
            "cases": cases,
            "totals": totals,
            "blockers": blockers,
            "warnings": warnings,
            "can_finalize": not blockers,
        },
    )


@router.post("/{receive_id}/finalize", response_class=HTMLResponse)
def finalize_receive(
    request: Request,
    receive_id: int,
    confirm_warnings: str = Form(""),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
    _flag: None = Depends(_require_vnext_flag),
):
    """Materialize BoxReceipts inside one DB transaction, then push to
    Zoho + Luma. Warnings require ``confirm_warnings=true``; blockers
    always reject."""
    from packtrack.services.receiving_v2_finalize import (
        materialize_box_receipts,
        push_receive_to_integrations,
        validate_receive_for_finalize,
    )

    _require_receiving_or_owner(user)
    rec = _load_receive(session, receive_id)
    blockers, warnings = validate_receive_for_finalize(session, rec)
    if blockers:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Receive has {len(blockers)} blocker(s); resolve and retry.",
        )
    if warnings and (confirm_warnings or "").lower() not in ("true", "1", "on", "yes"):
        # 422 to distinguish from blocker rejection — operator must
        # explicitly acknowledge warnings.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Receive has {len(warnings)} warning(s); re-submit with "
                "confirm_warnings=true to acknowledge."
            ),
        )

    boxes = materialize_box_receipts(session, rec, user)
    outcome = push_receive_to_integrations(session, rec, user, box_receipts=boxes)
    po = session.get(PurchaseOrder, rec.purchase_order_id) if rec.purchase_order_id else None
    return _render(
        request,
        "receive_v2/result.html",
        {
            "user": user,
            "receive": rec,
            "po": po,
            "outcome": outcome,
            "boxes": boxes,
        },
    )


@router.post("/{receive_id}/retry-push", response_class=HTMLResponse)
def retry_push(
    request: Request,
    receive_id: int,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
    _flag: None = Depends(_require_vnext_flag),
):
    """Re-fire push for leaves still in NOT_READY / PENDING / FAILED.
    Already-pushed leaves are not re-pushed. Zoho re-submission is
    safe via the existing ``PACK_TRACK_RECEIVE_{packtrack_receipt_id}``
    Idempotency-Key."""
    from packtrack.services.receiving_v2_finalize import (
        retry_push_for_receive,
    )

    _require_receiving_or_owner(user)
    rec = _load_receive(session, receive_id)
    if rec.status not in (ReceiveStatus.FINALIZED, ReceiveStatus.PUSH_FAILED, ReceiveStatus.PUSHED_OK):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Receive must be finalized before push can be retried.",
        )
    outcome = retry_push_for_receive(session, rec, user)
    po = session.get(PurchaseOrder, rec.purchase_order_id) if rec.purchase_order_id else None
    from sqlmodel import select as _select
    boxes = session.exec(
        _select(BoxReceipt).where(BoxReceipt.receive_id == rec.id)
    ).all()
    return _render(
        request,
        "receive_v2/result.html",
        {
            "user": user,
            "receive": rec,
            "po": po,
            "outcome": outcome,
            "boxes": boxes,
            "marked_test_banner": (rec.notes or "").find("[Marked as TEST/CANARY") != -1,
        },
    )


# ---------------------------------------------------------------------------
# Mark-as-test — owner-only audit marker (v2.7.2)
# ---------------------------------------------------------------------------
#
# This is NOT a reversal. External systems (Zoho, Luma) keep whatever they
# already recorded; the integration service has no reversal API exposed
# from PackTrack and Luma's lot reversal is not in the public contract.
# The marker only labels the PT-side Receive so it stops appearing as a
# normal operational receive in audit reports and result-page renders.
#
# Hard guarantees:
#   * No PT row is deleted (Receive, ReceiveCase, ReceiveCaseLine,
#     BoxReceipt, POEvent all remain intact and queryable).
#   * No external Zoho/Luma call is made.
#   * A POEvent records who marked it, when, and any operator-supplied
#     reason so the audit trail is honest.
#
# Designed for canary / sandbox / accidental receives where the operator
# wants the PT-side audit log to show "this was a test" without erasing
# any history.


MARK_TEST_CONFIRMATION_STRING = "I UNDERSTAND ZOHO AND LUMA ARE NOT REVERSED"
"""Required exact match in the ``confirm`` form field. The verbose
phrasing makes an accidental click impossible and makes the audit trail
self-documenting."""


@router.post("/{receive_id}/mark-test", response_class=HTMLResponse)
def mark_receive_as_test(
    request: Request,
    receive_id: int,
    confirm: str = Form(""),
    reason: str = Form(""),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
    _flag: None = Depends(_require_vnext_flag),
):
    """Mark a vNext Receive as a test/canary so it stops being treated as
    a normal operational receive in audit views. OWNER-only.

    Requires:
      * The user has OWNER role.
      * Form field ``confirm`` exactly equals
        :data:`MARK_TEST_CONFIRMATION_STRING` (so an accidental POST
        cannot mark a real receive).
      * Optional ``reason`` is recorded verbatim in the POEvent message
        and appended to ``Receive.notes``.

    Does NOT:
      * delete any row in any table
      * call ``submit_zoho_receives`` / ``push_luma_receipt`` / any
        external API
      * change ``BoxReceipt`` rows or ``luma_push_status``
      * void/reverse anything on Zoho or Luma

    See ``docs/RECEIVING_VNEXT_CANARY_CLEANUP.md`` for the operational
    context behind this marker.
    """
    if user.role != Role.OWNER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only OWNER may mark a receive as test/canary.",
        )

    if (confirm or "").strip() != MARK_TEST_CONFIRMATION_STRING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Confirmation string mismatch. To mark this receive as "
                f"test, submit confirm = {MARK_TEST_CONFIRMATION_STRING!r}. "
                "External Zoho/Luma records are NOT reversed by this action."
            ),
        )

    rec = _load_receive(session, receive_id)
    now = datetime.utcnow()
    cleaned_reason = (reason or "").strip()
    marker_line = (
        f"[Marked as TEST/CANARY by {user.name or user.email} at "
        f"{now.isoformat(timespec='seconds')}Z]"
        + (f" — {cleaned_reason}" if cleaned_reason else "")
        + " (External Zoho/Luma records were NOT reversed.)"
    )
    rec.notes = ((rec.notes or "").rstrip() + "\n\n" + marker_line).lstrip()
    rec.updated_at = now
    session.add(rec)
    if rec.purchase_order_id is not None:
        session.add(POEvent(
            po_id=rec.purchase_order_id,
            kind="receive_marked_test",
            message=(
                f"Receive {rec.receive_number} marked as TEST/CANARY by "
                f"{user.name or user.email}"
                + (f": {cleaned_reason}" if cleaned_reason else "")
                + " — external Zoho/Luma records were NOT reversed."
            ),
            actor_id=user.id,
        ))
    session.commit()
    session.refresh(rec)

    # Re-render the result page so the operator immediately sees the
    # new banner. No push outcome rerun.
    po = session.get(PurchaseOrder, rec.purchase_order_id) if rec.purchase_order_id else None
    from sqlmodel import select as _select
    boxes = session.exec(
        _select(BoxReceipt).where(BoxReceipt.receive_id == rec.id)
    ).all()
    return _render(
        request,
        "receive_v2/result.html",
        {
            "user": user,
            "receive": rec,
            "po": po,
            "outcome": None,  # no push happened in this action
            "boxes": boxes,
            "marked_test_banner": True,
        },
    )


# ---------------------------------------------------------------------------
# Packing-list attachment (v2.7.3)
# ---------------------------------------------------------------------------
#
# Stage 1 already added ``AttachmentKind.PACKING_LIST`` and the
# ``Receive.packing_list_attachment_id`` FK column. v2.7.3 wires the
# upload/display path:
#
#   * one primary packing list per Receive (v1)
#   * stored as a regular ``Attachment`` row with kind=PACKING_LIST,
#     using the same on-disk layout as other attachments (UPLOAD_DIR /
#     kind / safe_name)
#   * the new file is linked from the receive via
#     ``Receive.packing_list_attachment_id``
#   * uploading when one is already attached REPLACES the pointer
#     (the prior Attachment row is kept on disk + in DB — no deletion,
#     so audit history is preserved)
#
# No parsing. No structured rows. No expected-vs-actual yet.


_ALLOWED_PACKING_LIST_EXT = {
    "pdf", "csv", "xls", "xlsx", "jpg", "jpeg", "png", "webp", "heic",
}


def _save_packing_list_upload(
    file: UploadFile, po_number: str,
) -> tuple[str, str]:
    """Persist a packing-list upload to UPLOAD_DIR/packing_list and return
    ``(safe_name, relative_path)``. Mirrors the existing
    ``_save_upload`` pattern in purchase_orders.py but with the
    packing-list extension allowlist and its own subfolder."""
    if not file.filename or "." not in file.filename:
        raise HTTPException(status_code=400, detail="Invalid filename.")
    ext = file.filename.rsplit(".", 1)[1].lower()
    if ext not in _ALLOWED_PACKING_LIST_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"Allowed packing-list types: {', '.join(sorted(_ALLOWED_PACKING_LIST_EXT))}",
        )
    safe_po = (po_number or "RCV").replace("/", "_")[:40]
    safe_name = f"{safe_po}_PL_{uuid.uuid4().hex[:10]}.{ext}"
    target_dir = settings.UPLOAD_DIR / AttachmentKind.PACKING_LIST.value
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / safe_name
    with target_path.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    return safe_name, str(target_path.relative_to(settings.UPLOAD_DIR))


@router.post("/{receive_id}/packing-list", response_class=HTMLResponse)
def upload_receive_packing_list(
    request: Request,
    receive_id: int,
    file: UploadFile = File(...),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
    _flag: None = Depends(_require_vnext_flag),
):
    """Upload or REPLACE the primary packing list for a Receive.

    Permissions: OWNER + RECEIVING (same as the rest of the vNext
    receive-edit surface). Flag-gated.

    Stores the upload as an ``Attachment(kind=PACKING_LIST)`` linked
    to the receive's PO, then sets
    ``Receive.packing_list_attachment_id`` to the new attachment's id.
    Previous packing-list attachments are NOT deleted; the pointer
    swap is the only mutation needed and the old file stays on disk
    + in the audit table.

    Does NOT:
      * call Zoho or Luma
      * change Receive.status or trigger finalize
      * create or mutate any BoxReceipt
      * parse the file
    """
    _require_receiving_or_owner(user)
    rec = _load_receive(session, receive_id)
    if rec.purchase_order_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Receive has no PO — packing-list attachment requires a PO.",
        )
    po = session.get(PurchaseOrder, rec.purchase_order_id)
    if po is None:
        raise HTTPException(status_code=404, detail="PO not found")

    _safe_name, rel = _save_packing_list_upload(file, po.po_number)

    # next version: 1 + max version among PACKING_LIST attachments on
    # this PO (so a replace shows as version 2, 3, etc. in audit).
    from sqlmodel import select as _select
    existing = session.exec(
        _select(Attachment)
        .where(Attachment.po_id == po.id, Attachment.kind == AttachmentKind.PACKING_LIST)
    ).all()
    version = (max((a.version for a in existing), default=0)) + 1

    att = Attachment(
        po_id=po.id, kind=AttachmentKind.PACKING_LIST, version=version,
        filename=file.filename, file_path=rel, source="web",
        uploaded_by_id=user.id,
        notes=f"Packing list for Receive {rec.receive_number}",
    )
    session.add(att)
    session.flush()  # need att.id before pointing the receive at it
    rec.packing_list_attachment_id = att.id
    rec.updated_at = datetime.utcnow()
    session.add(rec)
    if rec.purchase_order_id is not None:
        session.add(POEvent(
            po_id=rec.purchase_order_id,
            kind="receive_packing_list_uploaded",
            message=(
                f"Packing list v{version} attached to Receive {rec.receive_number}: "
                f"{file.filename}"
            ),
            actor_id=user.id,
        ))
    session.commit()

    # Redirect back to the receive page so the operator sees the
    # attached state immediately.
    return RedirectResponse(
        url=f"/receive/v2/{rec.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
