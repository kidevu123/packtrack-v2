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
    Receive,
    ReceiveCase,
    ReceiveCaseLine,
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
