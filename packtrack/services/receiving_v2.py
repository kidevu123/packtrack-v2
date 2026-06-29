"""Receiving vNext (v2.5.0 Stage 1) — pure-Python helpers.

Keeps the route layer thin: receive-number generation, PO-scoped item
search, totals-by-item, and the small data shapes the templates need.

Per the design (``docs/design/2026-06-25-receiving-vnext.md``), Stage 1
is draft + counting only. There is no finalize here, no
BoxReceipt materialization, no Zoho push, no Luma push.
"""
from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, or_
from sqlmodel import Session, select

from packtrack.models import (
    Item,
    POLine,
    PurchaseOrder,
    Receive,
    ReceiveCase,
    ReceiveCaseLine,
    ZohoMirror,
)

RECEIVE_NUMBER_RETRY_LIMIT = 8


def _year_prefix(now: datetime | None = None) -> str:
    n = now or datetime.utcnow()
    return f"R-{n.year:04d}-"


def generate_receive_number(session: Session, *, now: datetime | None = None) -> str:
    """Server-generated human-friendly receive id of the form ``R-YYYY-NNNN``.

    Yearly sequence — count existing receives whose number starts with
    ``R-<year>-`` and pick the next integer. A few retries protect against
    a race where two concurrent creates pick the same N (DB UNIQUE on
    ``receive_number`` would otherwise raise); the caller can also just
    rely on the DB constraint and retry the whole transaction.
    """
    prefix = _year_prefix(now)
    last_attempt: str = ""
    for _ in range(RECEIVE_NUMBER_RETRY_LIMIT):
        existing = session.scalar(
            select(func.count())
            .select_from(Receive)
            .where(Receive.receive_number.like(f"{prefix}%"))
        ) or 0
        candidate = f"{prefix}{existing + 1:04d}"
        collision = session.exec(
            select(Receive.id).where(Receive.receive_number == candidate)
        ).first()
        if collision is None:
            return candidate
        last_attempt = candidate
    # Should be unreachable in normal operation — yearly sequences don't
    # routinely race 8 times. Fall back to a UUID-suffixed receive number
    # so the create still succeeds rather than 500ing.
    return f"{last_attempt}-{secrets.token_hex(2)}"


def make_submission_id() -> str:
    """One per Receive — propagates v2.4.1 idempotency at finalize."""
    return uuid.uuid4().hex


@dataclass
class POItemChoice:
    """One row in the per-line item dropdown.

    Pre-rendered server-side from the PO's lines so the case-block
    template drops the values straight into a ``<select>`` — no raw-ID
    typing required of the operator.
    """
    item_id: int
    label: str


def po_item_choices(session: Session, po_id: int) -> list[POItemChoice]:
    """Item choices for the case-line ``<select>``, scoped to the PO's lines.

    One option per (item, po_line) pair, ordered by item name. The
    label is ``Item name · MATERIAL_CODE · N <unit> remaining`` (or
    ``N <unit> ordered`` when nothing is received yet), so operators
    can tell similar items apart without typing.

    Remaining quantity reflects Zoho-side ``quantity_received`` when
    available (the mirror is authoritative for receive progress under
    vNext — ``POLine.received_quantity`` is not bumped by the vNext
    finalize path, see ``docs/CURRENT_PHASE_STATUS.md``). When the
    mirror has no line for the item OR the item has no
    ``zoho_item_id``, falls back to ``POLine.received_quantity`` and
    finally to "remaining unknown".
    """
    rows = session.exec(
        select(Item, POLine)
        .join(POLine, POLine.item_id == Item.id)
        .where(POLine.po_id == po_id)
        .order_by(Item.name, POLine.id)
    ).all()

    # Look up the Zoho mirror once and build an item_id -> received map.
    po = session.get(PurchaseOrder, po_id)
    mirror = None
    if po is not None and po.zoho_po_id:
        mirror = session.exec(
            select(ZohoMirror).where(ZohoMirror.zoho_purchaseorder_id == po.zoho_po_id)
        ).first()
    zoho_received_by_item: dict[str, float] = {}
    if mirror is not None:
        for li in (mirror.line_items or []):
            zid = str(li.get("item_id") or "")
            if not zid:
                continue
            try:
                zoho_received_by_item[zid] = float(li.get("quantity_received") or 0)
            except (TypeError, ValueError):
                continue

    out: list[POItemChoice] = []
    for item, line in rows:
        bits: list[str] = [item.name]
        if item.material_code:
            bits.append(item.material_code)
        ordered = float(line.quantity or 0)
        unit = (item.unit or "").strip()
        unit_suffix = f" {unit}" if unit else ""
        received = None
        if item.zoho_item_id and item.zoho_item_id in zoho_received_by_item:
            received = zoho_received_by_item[item.zoho_item_id]
        elif line.received_quantity is not None:
            received = float(line.received_quantity)

        if ordered > 0 and received is not None:
            remaining = max(0.0, ordered - received)
            bits.append(f"{int(remaining):,}{unit_suffix} remaining")
        elif ordered > 0:
            bits.append(f"{int(ordered):,}{unit_suffix} ordered")
        else:
            bits.append("remaining unknown")
        out.append(POItemChoice(item_id=item.id, label=" · ".join(bits)))
    return out


@dataclass
class ItemTotalsRow:
    item_id: int
    item_name: str
    unit: str
    total_declared: float
    total_counted: float
    has_count: bool


def totals_by_item(session: Session, receive_id: int) -> list[ItemTotalsRow]:
    """Aggregate accepted (count if present else declared) per item
    across all case lines on this receive. Used by the right-rail
    summary.
    """
    rows = session.exec(
        select(ReceiveCaseLine, Item)
        .join(ReceiveCase, ReceiveCase.id == ReceiveCaseLine.receive_case_id)
        .join(Item, Item.id == ReceiveCaseLine.item_id)
        .where(ReceiveCase.receive_id == receive_id)
    ).all()
    bucket: dict[int, ItemTotalsRow] = {}
    for line, item in rows:
        b = bucket.setdefault(
            item.id,
            ItemTotalsRow(
                item_id=item.id,
                item_name=item.name,
                unit=item.unit or "EACH",
                total_declared=0.0,
                total_counted=0.0,
                has_count=False,
            ),
        )
        b.total_declared += float(line.declared_quantity or 0)
        if line.counted_quantity is not None:
            b.total_counted += float(line.counted_quantity)
            b.has_count = True
        else:
            # Default counted to declared when not explicitly counted yet,
            # so the right-rail total reflects current intent.
            b.total_counted += float(line.declared_quantity or 0)
    return sorted(bucket.values(), key=lambda r: r.item_name.lower())


def items_for_po(session: Session, po_id: int, *, q: str | None = None, limit: int = 30) -> list[Item]:
    """Item-search results scoped to the PO's lines.

    Per design decision § 0.12, vNext item picking does NOT fall back
    to a vendor-wide list. Returns the distinct ``Item`` rows attached
    to ``po_lines`` for this PO, optionally filtered by case-insensitive
    substring match on name/sku/material_code.
    """
    stmt = (
        select(Item)
        .join(POLine, POLine.item_id == Item.id)
        .where(POLine.po_id == po_id)
    )
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Item.name.ilike(like),
                Item.sku_code.ilike(like),
                Item.material_code.ilike(like),
            )
        )
    # Distinct on Item.id to dedupe when the same Item is on the PO twice.
    items = session.exec(stmt.order_by(Item.name)).all()
    seen: set[int] = set()
    out: list[Item] = []
    for it in items:
        if it.id in seen:
            continue
        seen.add(it.id)
        out.append(it)
        if len(out) >= limit:
            break
    return out


def case_lines(session: Session, case_id: int) -> list[ReceiveCaseLine]:
    """All ``ReceiveCaseLine``s for one case, ordered by id for stable UI."""
    return session.exec(
        select(ReceiveCaseLine).where(ReceiveCaseLine.receive_case_id == case_id).order_by(ReceiveCaseLine.id)
    ).all()


def receive_cases(session: Session, receive_id: int) -> list[ReceiveCase]:
    return session.exec(
        select(ReceiveCase)
        .where(ReceiveCase.receive_id == receive_id)
        .order_by(ReceiveCase.sequence, ReceiveCase.id)
    ).all()


def next_case_sequence(session: Session, receive_id: int) -> int:
    current_max = session.scalar(
        select(func.coalesce(func.max(ReceiveCase.sequence), 0))
        .where(ReceiveCase.receive_id == receive_id)
    )
    return int(current_max or 0) + 1


# ---------------------------------------------------------------------------
# Test / canary marker (v2.7.2 introduced the mark-test route; v2.7.4
# centralizes the detection so every template that renders a Receive can
# show the banner consistently).
# ---------------------------------------------------------------------------


_TEST_MARKER_PREFIX = "[Marked as TEST/CANARY"


def is_test_receive(receive: Receive | None) -> bool:
    """True when this Receive carries the v2.7.2 mark-test marker line
    in its notes.

    Detection is on the marker prefix only — the rest of the line
    (operator name, timestamp, reason) varies but the prefix is fixed
    by the ``mark_receive_as_test`` route. No DB column for this in
    v2.7.x; the marker lives in ``Receive.notes``.
    """
    if receive is None:
        return False
    return _TEST_MARKER_PREFIX in (receive.notes or "")


def test_receive_marker_text(receive: Receive | None) -> str | None:
    """Return the human-readable marker line from ``Receive.notes`` if
    present, otherwise None. Useful for showing the reason next to the
    banner without dumping the whole notes blob into the page."""
    if not is_test_receive(receive):
        return None
    for raw in (receive.notes or "").splitlines():
        line = raw.strip()
        if line.startswith(_TEST_MARKER_PREFIX):
            return line
    return None
