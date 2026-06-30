"""Receiving PO visibility diagnostic route (v2.15.0).

One route, ``GET /receive/find``, that answers the operator's real
question: "where is PO X and why isn't Start receive available?"

Pure read — no DB mutation, no upstream calls, never creates a
Receive row. Owner + Receiving (same audience as ``/receive``).

This module's router MUST be registered BEFORE ``receiving.router``
in ``main.py`` so the static ``/receive/find`` path wins over the
existing dynamic ``/receive/{zoho_po_id}`` route.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlmodel import Session

from packtrack.db import get_session
from packtrack.deps import require_user
from packtrack.models import Role, User
from packtrack.services.receiving_po_visibility import (
    build_visibility_report,
    minutes_since,
)

router = APIRouter()


@router.get("/receive/find", response_class=HTMLResponse)
def find_po(
    request: Request,
    q: str | None = None,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    """Search synced Zoho POs and explain visibility on /receive.

    Same audience as /receive itself: OWNER + RECEIVING. Returns a
    list of mirrors (optionally filtered by ``q``) with classification,
    linkage, Start-receive eligibility, and exact reason when an
    action isn't available.
    """
    if user.role not in (Role.RECEIVING, Role.OWNER):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)

    report = build_visibility_report(session, query=q or None)

    from packtrack.main import templates
    return templates.TemplateResponse(
        request, "receiving_diagnostics/find.html",
        {
            "user": user,
            "report": report,
            "query": q or "",
            "minutes_since_last_sync": minutes_since(
                report.last_sync.finished_at or report.last_sync.started_at
                if report.last_sync else None
            ),
        },
    )
