"""Cycle-count routes (v2.14.0).

Two endpoints:

  GET  /inventory/cycle-count   — owner-only form with item list
  POST /inventory/cycle-count   — owner-only batch submit

The submit POST is all-or-nothing at validation time: a single bad row
keeps the entire batch from touching the database. Successfully
validated rows create one immutable adjustment per non-zero variance
via the existing v2.9.0 adjustment service, and the v2.10.0 Zoho sync
runs for each. Local stock is never written outside the existing
service.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select

from packtrack.db import get_session
from packtrack.deps import require_user
from packtrack.models import Item, Role, User, ZohoSyncStatus
from packtrack.services.cycle_count import (
    CycleCountInputRow,
    submit_cycle_count,
)

router = APIRouter()


def _require_owner(user: User) -> None:
    if user.role != Role.OWNER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)


def _templates():
    from packtrack.main import templates
    return templates


def _render(
    request: Request, name: str, ctx: dict, *, status_code: int = 200,
) -> HTMLResponse:
    return _templates().TemplateResponse(
        request, name, ctx, status_code=status_code,
    )


def _eligible_items(session: Session) -> list[Item]:
    """All items the cycle-count form should be able to count.

    For v2.14.0 we keep this simple: every item in the inventory
    (filtered/searched client-side via the existing input widget). A
    future iteration could PO-scope or product-line-scope the list.
    """
    return session.exec(select(Item).order_by(Item.name)).all()


@router.get("/inventory/cycle-count", response_class=HTMLResponse)
def cycle_count_form(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    """Render the cycle-count entry form. Owner-only.

    Non-owners get a hard 403 — there is no "view-only" cycle-count
    mode because the page exists to gather mutation input. The
    read-only history is available via the existing
    ``/inventory/adjustments`` page.
    """
    _require_owner(user)
    items = _eligible_items(session)
    return _render(
        request,
        "inventory_cycle_count/form.html",
        {
            "user": user,
            "items": items,
        },
    )


@router.post("/inventory/cycle-count", response_class=HTMLResponse)
async def submit_cycle_count_route(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    """Owner-only batch submit.

    Form contract:
      * ``shared_note`` — optional, applied when a row has no per-row note.
      * For each item in the form, ``counted_<item_id>`` is the typed
        quantity (blank rows are skipped — they're items the operator
        chose not to count this round). ``note_<item_id>`` is the
        optional per-row note.
    """
    _require_owner(user)
    form = await request.form()

    shared_note = str(form.get("shared_note") or "").strip()
    inputs: list[CycleCountInputRow] = []
    for key, value in form.multi_items():
        if not key.startswith("counted_"):
            continue
        raw = str(value or "").strip()
        if not raw:
            continue  # operator didn't count this row this round
        try:
            item_id = int(key[len("counted_"):])
        except ValueError:
            continue
        note = str(form.get(f"note_{item_id}") or "").strip() or None
        inputs.append(CycleCountInputRow(
            item_id=item_id, raw_counted=raw, note=note,
        ))

    if not inputs:
        # Re-render the form with a soft warning. Better UX than a 400.
        items = _eligible_items(session)
        return _render(
            request,
            "inventory_cycle_count/form.html",
            {
                "user": user,
                "items": items,
                "warning": "No counted quantities entered. "
                           "Fill in at least one row, then submit.",
                "shared_note_value": shared_note,
            },
        )

    outcome = submit_cycle_count(
        session, actor=user, inputs=inputs, shared_note=shared_note,
    )

    if outcome.errors:
        # All-or-nothing — re-render the form with inline errors.
        items = _eligible_items(session)
        # Preserve the operator's typed values so they don't have to
        # retype the whole batch.
        submitted_counted: dict[int, str] = {
            i.item_id: i.raw_counted for i in inputs
        }
        submitted_notes: dict[int, str] = {
            i.item_id: i.note or "" for i in inputs
        }
        return _render(
            request,
            "inventory_cycle_count/form.html",
            {
                "user": user,
                "items": items,
                "errors": outcome.errors,
                "submitted_counted": submitted_counted,
                "submitted_notes": submitted_notes,
                "shared_note_value": shared_note,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    return _render(
        request,
        "inventory_cycle_count/result.html",
        {
            "user": user,
            "outcome": outcome,
            "sync_status_labels": {s.value: s.value.replace("_", " ").title()
                                    for s in ZohoSyncStatus},
        },
    )
