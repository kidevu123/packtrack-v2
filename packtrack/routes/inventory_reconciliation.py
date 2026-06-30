"""Inventory reconciliation dashboard route (v2.17.0).

Single read-only endpoint that renders the PackTrack vs Zoho snapshot
view + adjustment-sync exceptions list. Visible to any authenticated
user (mirrors ``/inventory/adjustments`` per-item history visibility);
owner-only actions (Retry sync) are gated downstream by the existing
``POST /inventory/adjustments/{id}/sync`` route + the v2.16.3
``retry_eligibility`` check.

This route NEVER writes. The service layer is strictly read-only by
construction (see ``services/inventory_reconciliation.py``).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session

from packtrack.config import settings
from packtrack.db import get_session
from packtrack.deps import require_user
from packtrack.models import Role, User
from packtrack.services.inventory_adjustments import REASON_LABELS
from packtrack.services.inventory_reconciliation import (
    STALE_LABELS,
    VARIANCE_LABELS,
    Filters,
    build_dashboard,
)

router = APIRouter()


def _templates():
    from packtrack.main import templates
    return templates


@router.get("/inventory/reconciliation", response_class=HTMLResponse)
def reconciliation_dashboard(
    request: Request,
    variance_only: bool = False,
    stale_only: bool = False,
    failed_only: bool = False,
    retryable_only: bool = False,
    q: str = "",
    product_line: str = "",
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    filters = Filters(
        variance_only=variance_only,
        stale_only=stale_only,
        failed_only=failed_only,
        retryable_only=retryable_only,
        q=q,
        product_line=product_line,
    )
    dashboard = build_dashboard(
        session,
        filters=filters,
        stale_threshold_hours=settings.INVENTORY_RECONCILIATION_STALE_HOURS,
        reason_labels=REASON_LABELS,
    )
    return _templates().TemplateResponse(
        request,
        "inventory_reconciliation/dashboard.html",
        {
            "user": user,
            "is_owner": user.role == Role.OWNER,
            "dashboard": dashboard,
            "variance_labels": VARIANCE_LABELS,
            "stale_labels": STALE_LABELS,
        },
    )
