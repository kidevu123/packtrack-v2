"""Inventory adjustment routes (v2.9.0).

Adds three operator-facing routes plus a global history list:

  GET  /inventory/{item_id}/adjust           — show the form
  POST /inventory/{item_id}/adjust           — create the ledger row
  GET  /inventory/{item_id}/adjustments      — per-item history
  GET  /inventory/adjustments                — global history (filterable)

The POST is OWNER-only at the route layer (server-side, not just
hidden buttons). There is intentionally NO edit/delete route — the
``InventoryAdjustment`` table is append-only by design.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session

from packtrack.db import get_session
from packtrack.deps import require_user
from packtrack.models import (
    AdjustmentDirection,
    AdjustmentMode,
    AdjustmentReason,
    InventoryAdjustment,
    Item,
    Role,
    User,
    ZohoSyncStatus,
)
from packtrack.services.inventory_adjustment_sync import try_sync_adjustment
from packtrack.services.inventory_adjustments import (
    REASON_LABELS,
    AdjustmentError,
    create_adjustment,
    global_history,
    history_for_item,
    reason_choices,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_owner(user: User) -> None:
    if user.role != Role.OWNER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)


def _load_item(session: Session, item_id: int) -> Item:
    item = session.get(Item, item_id)
    if item is None:
        raise HTTPException(status_code=404)
    return item


def _templates():
    from packtrack.main import templates
    return templates


def _render(request: Request, name: str, ctx: dict) -> HTMLResponse:
    return _templates().TemplateResponse(request, name, ctx)


def _parse_mode(raw: str) -> AdjustmentMode:
    try:
        return AdjustmentMode(raw)
    except ValueError:
        raise HTTPException(
            status_code=400, detail=f"Unknown adjustment mode {raw!r}.",
        ) from None


def _parse_direction(raw: str | None) -> AdjustmentDirection | None:
    if not raw:
        return None
    try:
        return AdjustmentDirection(raw)
    except ValueError:
        raise HTTPException(
            status_code=400, detail=f"Unknown adjustment direction {raw!r}.",
        ) from None


def _parse_reason(raw: str) -> AdjustmentReason:
    try:
        return AdjustmentReason(raw)
    except ValueError:
        raise HTTPException(
            status_code=400, detail=f"Unknown reason code {raw!r}.",
        ) from None


# ---------------------------------------------------------------------------
# Per-item routes
# ---------------------------------------------------------------------------


@router.get("/inventory/{item_id:int}/adjust", response_class=HTMLResponse)
def adjust_form(
    request: Request,
    item_id: int,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    """Render the adjustment form. Non-owners get 403 — the form itself
    is the mutation surface, so the GET is owner-gated too. The
    read-only history is on a separate route."""
    _require_owner(user)
    item = _load_item(session, item_id)
    return _render(
        request,
        "inventory_adjustments/form.html",
        {
            "user": user,
            "item": item,
            "reasons": reason_choices(),
            "reason_labels": REASON_LABELS,
        },
    )


@router.post("/inventory/{item_id:int}/adjust", response_class=HTMLResponse)
def submit_adjustment(
    request: Request,
    item_id: int,
    mode: str = Form(...),
    direction: str = Form(""),
    quantity: str = Form(""),
    reason_code: str = Form(...),
    notes: str = Form(""),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    """Owner-only. Validates input, calls the transactional service.
    Adjustment-table row is immutable — no PATCH/PUT/DELETE exists."""
    _require_owner(user)
    item = _load_item(session, item_id)
    mode_e = _parse_mode(mode)
    direction_e = _parse_direction(direction or None)
    reason_e = _parse_reason(reason_code)
    try:
        result = create_adjustment(
            session,
            item_id=item.id,
            actor=user,
            mode=mode_e,
            direction=direction_e,
            raw_quantity=quantity,
            reason_code=reason_e,
            notes=notes or None,
        )
    except AdjustmentError as exc:
        # Re-render the form with the validation message so the operator
        # keeps their typed values + sees the reason inline. 400 is the
        # right status for a form-validation failure.
        return _render(
            request,
            "inventory_adjustments/form.html",
            {
                "user": user,
                "item": item,
                "reasons": reason_choices(),
                "reason_labels": REASON_LABELS,
                "error": str(exc),
                "submitted_mode": mode,
                "submitted_direction": direction,
                "submitted_quantity": quantity,
                "submitted_reason_code": reason_code,
                "submitted_notes": notes,
            },
        )

    # v2.10.0 — attempt the Zoho sync immediately after the local
    # commit. PackTrack is the source of truth: if Zoho fails, the
    # ledger row stays and the operator gets a Retry button in
    # history. Status is PENDING when configured; this call advances it
    # to SYNCED / FAILED / SKIPPED. NOT_CONFIGURED rows are left alone.
    sync_outcome = try_sync_adjustment(
        session, result.adjustment, item, actor=user,
    )
    sync_status = sync_outcome.to_status()

    return RedirectResponse(
        url=(
            f"/inventory/{item.id}/adjustments"
            f"?saved={result.adjustment.adjustment_number}"
            f"&sync_status={sync_status.value}"
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get(
    "/inventory/{item_id:int}/adjustments", response_class=HTMLResponse,
)
def item_adjustment_history(
    request: Request,
    item_id: int,
    saved: str | None = None,
    sync_status: str | None = None,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    """Per-item ledger view. Visible to ALL authenticated roles —
    OWNER sees the Adjust button as well, others see read-only history."""
    item = _load_item(session, item_id)
    rows = history_for_item(session, item.id)
    flash = None
    if saved:
        sync_label = sync_status or ZohoSyncStatus.NOT_CONFIGURED.value
        flash = {"adjustment_number": saved, "sync_status": sync_label}
    return _render(
        request,
        "inventory_adjustments/history.html",
        {
            "user": user,
            "item": item,
            "rows": rows,
            "reason_labels": REASON_LABELS,
            "is_owner": user.role == Role.OWNER,
            "flash": flash,
            "filter_item": item,
            "global_view": False,
        },
    )


# ---------------------------------------------------------------------------
# Global history
# ---------------------------------------------------------------------------


@router.get("/inventory/adjustments", response_class=HTMLResponse)
def global_adjustment_history(
    request: Request,
    item_id: int | None = None,
    reason: str | None = None,
    sync_status: str | None = None,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    """All adjustments, newest first, with optional filters."""
    reason_e = _parse_reason(reason) if reason else None
    sync_e: ZohoSyncStatus | None = None
    if sync_status:
        try:
            sync_e = ZohoSyncStatus(sync_status)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown sync_status filter {sync_status!r}.",
            ) from None
    rows = global_history(
        session, item_id=item_id, reason_code=reason_e, sync_status=sync_e,
    )
    item_lookup: dict[int, Item] = {}
    for r in rows:
        if r.item_id not in item_lookup:
            item = session.get(Item, r.item_id)
            if item is not None:
                item_lookup[r.item_id] = item
    filter_item = session.get(Item, item_id) if item_id else None
    return _render(
        request,
        "inventory_adjustments/history.html",
        {
            "user": user,
            "rows": rows,
            "item_lookup": item_lookup,
            "reason_labels": REASON_LABELS,
            "is_owner": user.role == Role.OWNER,
            "filter_item": filter_item,
            "filter_reason": reason or "",
            "filter_sync_status": sync_status or "",
            "reasons": reason_choices(),
            "sync_status_choices": [(s.value, s.value.replace("_", " ").title()) for s in ZohoSyncStatus],
            "global_view": True,
        },
    )


# ---------------------------------------------------------------------------
# Retry sync (v2.10.0)
# ---------------------------------------------------------------------------
#
# Owner-only. Re-runs the integration-service push for a single
# adjustment row that is FAILED / NOT_CONFIGURED / PENDING / SKIPPED.
# The orchestrator no-ops on SYNCED rows (returns the existing
# reference); the route surfaces that as a friendly flash, not an
# error. Idempotency is preserved by reusing the original
# adjustment.idempotency_key — so even if the previous attempt
# succeeded upstream but the connection dropped before we got the
# response, the retry will receive the SYNCED_IDEMPOTENT outcome
# and mark the row SYNCED with the right reference.


@router.post(
    "/inventory/adjustments/{adjustment_id:int}/sync",
    response_class=HTMLResponse,
)
def retry_adjustment_sync(
    adjustment_id: int,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    _require_owner(user)
    adjustment = session.get(InventoryAdjustment, adjustment_id)
    if adjustment is None:
        raise HTTPException(status_code=404)
    item = session.get(Item, adjustment.item_id)
    if item is None:
        # Defensive — an adjustment shouldn't outlive its item, but if
        # it does we don't want a 500. Tell the operator and stop.
        raise HTTPException(
            status_code=409,
            detail="Adjustment's item no longer exists; cannot sync.",
        )

    outcome = try_sync_adjustment(session, adjustment, item, actor=user)
    return RedirectResponse(
        url=(
            f"/inventory/{item.id}/adjustments"
            f"?saved={adjustment.adjustment_number}"
            f"&sync_status={outcome.to_status().value}"
            f"&retry=1"
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )
