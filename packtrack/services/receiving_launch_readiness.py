"""v2.13.0 — pre-receive launch-readiness diagnostic.

Pure read. Looks at a ``ZohoMirror`` + its linked PackTrack
``PurchaseOrder`` and returns a small advisory: is this PO ready for
the case-first Receiving vNext flow, or are there issues an operator
should know about before clicking Start Receive?

This is a **diagnostic only**. Nothing here blocks Start Receive; it
just surfaces the friction so the operator can decide whether to fix
upstream first (e.g. assign a material code in the master-data editor)
or proceed and patch as they go.

Categories of issues we look at (cheap to compute, none requires Zoho
or Luma round-trips):

* PO has no linked internal ``PurchaseOrder`` row — Start Receive
  won't show until a manual link/adopt action.
* PO is fully received (per mirror line totals) — no remaining qty to
  count.
* Any item on the PO lacks a ``material_code`` — finalize will park
  the corresponding ``BoxReceipt`` in NOT_READY against Luma until an
  owner fills the code.
* Mirror line items list is empty / missing quantity data — the
  per-item "remaining" hints in case-line entry won't render.
* Vendor is unknown across mirror + item fallback chain — the card
  will show "Vendor unknown" rather than a real name.

The result is intentionally human-readable; the template renders
``status`` directly and the issue list goes into a tooltip / details
block.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session, select

from packtrack.models import Item, POLine, PurchaseOrder, ZohoMirror


@dataclass
class ReadinessReport:
    status: str  # "ready" | "needs_attention" | "blocked"
    issues: list[str] = field(default_factory=list)

    @property
    def is_ready(self) -> bool:
        return self.status == "ready"

    @property
    def label(self) -> str:
        return {
            "ready": "Ready for vNext",
            "needs_attention": "Needs attention",
            "blocked": "Not ready",
        }.get(self.status, self.status)


def _mirror_totals(line_items: list[dict[str, Any]] | None) -> tuple[float, float]:
    ordered = received = 0.0
    for li in (line_items or []):
        with contextlib.suppress(TypeError, ValueError):
            ordered += float(li.get("quantity") or 0)
        with contextlib.suppress(TypeError, ValueError):
            received += float(li.get("quantity_received") or 0)
    return ordered, received


def assess_po_readiness(
    session: Session,
    mirror: ZohoMirror,
    *,
    linked_po: PurchaseOrder | None,
    vendor_label: str | None,
) -> ReadinessReport:
    """Build the diagnostic for one mirror row.

    ``linked_po`` and ``vendor_label`` are pre-computed by the route
    (it already builds those maps for the receiving list) — passing
    them in keeps this helper testable without re-running those scans.
    """
    issues: list[str] = []

    # Fully received?
    ordered, received = _mirror_totals(mirror.line_items)
    fully_received = ordered > 0 and received >= ordered
    if fully_received:
        # Not actionable as "needs attention" — just a status the UI
        # already shows via the "Fully received" badge. We return a
        # short blocked-with-reason so the template can opt out of
        # showing a Ready pill on a card that has no work left.
        return ReadinessReport(status="blocked", issues=["Fully received"])

    # Linked to a PackTrack PO?
    if linked_po is None:
        issues.append("Not yet linked to a PackTrack purchase order.")
        # Without a linked PO we can't run any of the per-item checks.
        return ReadinessReport(status="needs_attention", issues=issues)

    # Vendor known?
    if not (vendor_label or "").strip() or vendor_label == "Vendor unknown":
        issues.append("Vendor not on the mirror or any linked item.")

    # Material codes on every PO line?
    rows = session.exec(
        select(Item)
        .join(POLine, POLine.item_id == Item.id)
        .where(POLine.po_id == linked_po.id)
    ).all()
    if not rows:
        issues.append("Linked PO has no item lines.")
        return ReadinessReport(status="needs_attention", issues=issues)

    missing_code = [it for it in rows if not (it.material_code or "").strip()]
    if missing_code:
        names = ", ".join(
            (it.name or f"item {it.id}")[:40]
            for it in missing_code[:3]
        )
        more = "" if len(missing_code) <= 3 else f" (+{len(missing_code) - 3} more)"
        issues.append(
            f"Missing material_code on {len(missing_code)} item"
            f"{'' if len(missing_code) == 1 else 's'}: {names}{more}. "
            "Luma push will park as NOT_READY until set."
        )

    # Mirror quantity data present?
    if not (mirror.line_items or []):
        issues.append(
            "Zoho mirror has no line items — per-item 'remaining' hints "
            "will not render during case-line entry."
        )

    if issues:
        return ReadinessReport(status="needs_attention", issues=issues)
    return ReadinessReport(status="ready", issues=[])
