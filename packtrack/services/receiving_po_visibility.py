"""Receiving PO visibility diagnostic (v2.15.0).

Single source of truth for "where is PO X and why is it / isn't it
actionable on /receive?". Pure read — no DB writes, no upstream calls.

The pipeline this service introspects (see
``docs/RUNBOOK_RECEIVING_VNEXT_OPERATOR.md``):

  Zoho PO
    → ``packtrack.zoho.sync_open_pos`` (excludes cancelled/void,
      requires ``cf_packaging_unformatted == True``, wipe-and-replace
      every 30 min via APScheduler + on Settings → Sync)
    → ``ZohoMirror`` row keyed by ``zoho_purchaseorder_id``
    → optional internal ``PurchaseOrder`` linked via ``zoho_po_id``
      (adopted lazily via ``services.receiving.adopt_zoho_po`` when
      the operator starts a receive on the legacy route, or when the
      vNext route runs through the same adopt path)
    → ``/receive`` lists every mirror, classifies by line-item totals
      (pending / partial / fully_received), Start Receive button is
      shown only when vnext is enabled AND a linked PT PO exists AND
      the PO is not fully received

This module returns a ``MirrorDiagnostic`` per row so the
``/receive/find`` page can answer the operator's three real questions:

  1. Is this PO visible on /receive?
  2. Is the Start receive button available?
  3. If no, what's the exact reason and what should I do?
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlmodel import Session, select

from packtrack.config import settings
from packtrack.models import (
    Item,
    POLine,
    PurchaseOrder,
    SyncRun,
    ZohoMirror,
)

# ---------------------------------------------------------------------------
# Output shapes
# ---------------------------------------------------------------------------


@dataclass
class MirrorDiagnostic:
    """One synced Zoho PO + everything the diagnostic page needs to
    explain its current visibility on /receive."""

    # Identity
    mirror_id: int
    zoho_purchaseorder_id: str
    purchaseorder_number: str | None
    vendor_name: str | None
    zoho_status: str | None
    po_date: str | None

    # Quantities (rolled across mirror.line_items)
    line_count: int
    total_ordered: float
    total_received: float

    # Bucket (matches the /receive template logic exactly)
    bucket: str  # "pending" | "partial" | "fully_received" | "no_line_items"

    # Linkage to internal PackTrack PurchaseOrder
    pt_po_id: int | None
    pt_po_number: str | None
    pt_po_status: str | None

    # Readiness flags (cheap checks; same spirit as v2.13.0)
    missing_material_code_count: int

    # Derived: visible on /receive and Start-receive available?
    appears_on_receive: bool
    start_receive_available: bool

    # Exact reason if Start receive isn't available
    hidden_reason: str | None
    start_receive_reason: str | None

    # Action link when the operator can act now
    start_receive_url: str | None


@dataclass
class VisibilityReport:
    """All diagnostics + optional filtered subset + sync metadata."""

    diagnostics: list[MirrorDiagnostic] = field(default_factory=list)
    last_sync: SyncRun | None = None
    counts: dict[str, int] = field(default_factory=dict)
    vnext_enabled: bool = False

    # When the operator searches and no mirror matches, we surface a
    # plain-text explanation of where to look (in Zoho) so they can
    # fix it upstream.
    not_in_mirror_hint: str = (
        "If the PO you expected is not in this list, it is not yet "
        "in the Zoho mirror. Most likely causes: (1) Zoho status is "
        "'cancelled' or 'void' (excluded by sync); (2) the Zoho "
        "'Packaging?' checkbox (cf_packaging_unformatted) is not "
        "checked — packaging-only filter excludes the PO; (3) the "
        "30-min auto-sync has not yet run since the PO was created — "
        "trigger Settings → Sync."
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mirror_totals(line_items: list[dict[str, Any]] | None) -> tuple[float, float]:
    ordered = received = 0.0
    for li in (line_items or []):
        with contextlib.suppress(TypeError, ValueError):
            ordered += float(li.get("quantity") or 0)
        with contextlib.suppress(TypeError, ValueError):
            received += float(li.get("quantity_received") or 0)
    return ordered, received


def _classify(total_ordered: float, total_received: float, line_count: int) -> str:
    if line_count == 0 or total_ordered == 0:
        return "no_line_items"
    if total_received >= total_ordered:
        return "fully_received"
    if total_received > 0:
        return "partial"
    return "pending"


def _missing_material_code_count(session: Session, po_id: int) -> int:
    rows = session.exec(
        select(Item)
        .join(POLine, POLine.item_id == Item.id)
        .where(POLine.po_id == po_id)
    ).all()
    return sum(1 for it in rows if not (it.material_code or "").strip())


# ---------------------------------------------------------------------------
# Per-mirror diagnostic
# ---------------------------------------------------------------------------


def diagnose_mirror(
    session: Session, mirror: ZohoMirror, *, linked_po: PurchaseOrder | None,
    vnext_enabled: bool,
) -> MirrorDiagnostic:
    """Build a MirrorDiagnostic for one Zoho mirror row.

    ``linked_po`` is pre-fetched by the route to avoid an N+1 across
    the full list. Pass ``None`` when there is no PT row yet.
    """
    line_items = mirror.line_items or []
    line_count = len(line_items)
    total_ordered, total_received = _mirror_totals(line_items)
    bucket = _classify(total_ordered, total_received, line_count)

    # Readiness — only meaningful when linked to a PT PO.
    missing_codes = (
        _missing_material_code_count(session, linked_po.id) if linked_po else 0
    )

    # Visibility & action eligibility, matching the /receive template
    # logic exactly (including the "Fully received" section: those rows
    # ARE on the page, just in a separate collapsed section without a
    # Start Receive button).
    fully_received = bucket == "fully_received"
    appears_on_receive = True  # every mirror is rendered SOMEWHERE on /receive
    start_receive_available = (
        vnext_enabled
        and linked_po is not None
        and not fully_received
    )

    hidden_reason: str | None = None
    if not start_receive_available:
        if fully_received:
            hidden_reason = (
                "Mirror reports this PO is fully received "
                f"({int(total_received):,}/{int(total_ordered):,}). "
                "It appears in the 'Fully received' section, no Start "
                "receive button. If Zoho is wrong, fix upstream first."
            )
        elif linked_po is None:
            hidden_reason = (
                "Synced from Zoho but no PackTrack PurchaseOrder row "
                "is linked yet. The legacy /receive/{zoho_po_id} "
                "route adopts lazily when an operator opens it; the "
                "vNext Start receive button requires the link to "
                "already exist."
            )
        elif not vnext_enabled:
            hidden_reason = (
                "Receiving vNext flag (RECEIVING_VNEXT_ENABLED) is "
                "off, so the case-first Start receive button is "
                "hidden. Legacy /receive/{zoho_po_id} is still "
                "available."
            )

    start_receive_url = (
        f"/receive/v2/new?po_id={linked_po.id}"
        if start_receive_available and linked_po else None
    )

    return MirrorDiagnostic(
        mirror_id=mirror.id,
        zoho_purchaseorder_id=mirror.zoho_purchaseorder_id,
        purchaseorder_number=mirror.purchaseorder_number,
        vendor_name=mirror.vendor_name,
        zoho_status=mirror.status,
        po_date=mirror.date,
        line_count=line_count,
        total_ordered=total_ordered,
        total_received=total_received,
        bucket=bucket,
        pt_po_id=linked_po.id if linked_po else None,
        pt_po_number=linked_po.po_number if linked_po else None,
        pt_po_status=(
            linked_po.status.value if linked_po and linked_po.status else None
        ),
        missing_material_code_count=missing_codes,
        appears_on_receive=appears_on_receive,
        start_receive_available=start_receive_available,
        hidden_reason=hidden_reason,
        start_receive_reason=(
            "All checks passed — open the receive form to start counting."
            if start_receive_available else hidden_reason
        ),
        start_receive_url=start_receive_url,
    )


# ---------------------------------------------------------------------------
# Full report
# ---------------------------------------------------------------------------


def build_visibility_report(
    session: Session, *, query: str | None = None,
) -> VisibilityReport:
    """Build the full diagnostic report for the /receive/find page.

    ``query`` is a free-text filter — case-insensitive substring match
    over PO number / vendor / line-item item names / mirror id. Empty
    query returns every mirror.
    """
    mirrors = session.exec(
        select(ZohoMirror).order_by(ZohoMirror.date.desc())
    ).all()

    # Pre-fetch linked POs in one query (no N+1).
    linked: dict[str, PurchaseOrder] = {}
    po_rows = session.exec(
        select(PurchaseOrder).where(PurchaseOrder.zoho_po_id.is_not(None))
    ).all()
    for po in po_rows:
        if po.zoho_po_id:
            linked[po.zoho_po_id] = po

    vnext_enabled = bool(getattr(settings, "RECEIVING_VNEXT_ENABLED", False))

    q = (query or "").strip().lower()

    diagnostics: list[MirrorDiagnostic] = []
    for m in mirrors:
        # Filter early when a query is set.
        if q:
            haystack_bits: list[str] = [
                (m.purchaseorder_number or "").lower(),
                (m.zoho_purchaseorder_id or "").lower(),
                (m.vendor_name or "").lower(),
                str(m.id or ""),
            ]
            for li in (m.line_items or []):
                haystack_bits.append((li.get("name") or "").lower())
            if not any(q in h for h in haystack_bits if h):
                continue
        diagnostics.append(diagnose_mirror(
            session, m,
            linked_po=linked.get(m.zoho_purchaseorder_id),
            vnext_enabled=vnext_enabled,
        ))

    last_sync = session.exec(
        select(SyncRun).order_by(SyncRun.started_at.desc()).limit(1)
    ).first()

    counts: dict[str, int] = {
        "total_mirrors": len(diagnostics),
        "actionable_start_receive": sum(
            1 for d in diagnostics if d.start_receive_available
        ),
        "fully_received": sum(1 for d in diagnostics if d.bucket == "fully_received"),
        "partial": sum(1 for d in diagnostics if d.bucket == "partial"),
        "pending": sum(1 for d in diagnostics if d.bucket == "pending"),
        "no_line_items": sum(1 for d in diagnostics if d.bucket == "no_line_items"),
        "not_linked": sum(1 for d in diagnostics if d.pt_po_id is None),
        "missing_material_codes": sum(
            1 for d in diagnostics if d.missing_material_code_count > 0
        ),
    }

    return VisibilityReport(
        diagnostics=diagnostics,
        last_sync=last_sync,
        counts=counts,
        vnext_enabled=vnext_enabled,
    )


def minutes_since(ts: datetime | None) -> int | None:
    if ts is None:
        return None
    delta = datetime.utcnow() - ts
    return max(0, int(delta.total_seconds() // 60))
