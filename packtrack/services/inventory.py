from __future__ import annotations

from dataclasses import dataclass, field
from math import ceil

from sqlalchemy import func
from sqlmodel import Session, col, or_, select

from packtrack.models import Item, POLine, POStatus, PurchaseOrder, ZohoMirror
from packtrack.services.product_line import (
    GENERIC_GROUP,
    group_sort_key,
)
from packtrack.services.scope import filter_items_query, get_scope


@dataclass
class CoverageRow:
    packtrack_open_qty: float = 0.0
    zoho_open_qty: float = 0.0
    packtrack_pos: list[PurchaseOrder] = field(default_factory=list)
    zoho_pos: list[ZohoMirror] = field(default_factory=list)

    @property
    def is_covered(self) -> bool:
        return self.packtrack_open_qty > 0 or self.zoho_open_qty > 0


def suggested_reorder_qty(item: Item, buffer_days: int = 14) -> int:
    """Suggest enough stock to cover sea lead time plus an operating buffer."""
    if item.daily_usage_rate and item.daily_usage_rate > 0:
        days = max(item.sea_lead_days or 45, 30) + buffer_days
        target_stock = item.daily_usage_rate * days
        gap = max(0.0, target_stock - max(item.current_stock, 0))
        if gap > 0:
            return ceil(gap)
    if item.reorder_point and item.reorder_point > 0:
        return ceil(item.reorder_point * 2)
    return 100


def _inventory_stmt(
    session: Session,
    *,
    q: str | None,
    vendor: str | None,
    stock_status: str | None,
    missing_material_code: bool,
    group: str | None = None,
):
    # Stable sort: product line, then name, then id — so grouped rendering
    # stays contiguous and pages don't shuffle when names tie.
    stmt = select(Item).order_by(Item.product_line, Item.name, Item.id)
    if not any([missing_material_code, stock_status, vendor, q]):
        stmt = stmt.where(
            or_(
                Item.material_code.is_not(None),
                Item.name.contains("[Packaging]"),
                Item.name.contains("[packaging]"),
            )
        )
    stmt = filter_items_query(stmt, get_scope(session))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Item.name.ilike(like),
                Item.sku_code.ilike(like),
                Item.material_code.ilike(like),
                Item.vendor.ilike(like),
            )
        )
    if vendor:
        stmt = stmt.where(Item.vendor.ilike(f"%{vendor.strip()}%"))
    if missing_material_code:
        stmt = stmt.where(or_(Item.material_code.is_(None), Item.material_code == ""))
    if group:
        if group == GENERIC_GROUP:
            # The generic bucket also catches legacy rows with no derived line.
            stmt = stmt.where(
                or_(
                    Item.product_line.is_(None),
                    Item.product_line == "",
                    Item.product_line == GENERIC_GROUP,
                )
            )
        else:
            stmt = stmt.where(Item.product_line == group)
    status = (stock_status or "").strip().lower()
    if status in {"critical", "low", "ok"}:
        if status == "critical":
            stmt = stmt.where(Item.critical_point > 0, Item.current_stock <= Item.critical_point)
        elif status == "low":
            stmt = stmt.where(
                or_(
                    (Item.critical_point > 0) & (Item.current_stock <= Item.critical_point),
                    (Item.reorder_point > 0) & (Item.current_stock <= Item.reorder_point),
                )
            )
        else:
            stmt = stmt.where(
                or_(Item.reorder_point <= 0, Item.current_stock > Item.reorder_point)
            )
    return stmt


def filter_inventory_items(
    session: Session,
    *,
    q: str | None = None,
    vendor: str | None = None,
    stock_status: str | None = None,
    missing_material_code: bool = False,
    group: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> list[Item]:
    stmt = _inventory_stmt(
        session,
        q=q, vendor=vendor, stock_status=stock_status,
        missing_material_code=missing_material_code, group=group,
    )
    if offset:
        stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    return session.exec(stmt).all()


def count_inventory_items(
    session: Session,
    *,
    q: str | None = None,
    vendor: str | None = None,
    stock_status: str | None = None,
    missing_material_code: bool = False,
    group: str | None = None,
) -> int:
    stmt = _inventory_stmt(
        session,
        q=q, vendor=vendor, stock_status=stock_status,
        missing_material_code=missing_material_code, group=group,
    ).order_by(None)
    return session.scalar(select(func.count()).select_from(stmt.subquery())) or 0


def group_counts(
    session: Session,
    *,
    q: str | None = None,
    vendor: str | None = None,
    stock_status: str | None = None,
    missing_material_code: bool = False,
) -> list[tuple[str, int]]:
    """Count items per product line across the (non-group) filtered set.

    Powers the group chips on /inventory. Null / empty product lines roll up
    into the generic bucket so every item is counted exactly once. The active
    ``group`` filter is intentionally excluded so the chips always show the
    full set of lines to jump between.
    """
    stmt = _inventory_stmt(
        session,
        q=q, vendor=vendor, stock_status=stock_status,
        missing_material_code=missing_material_code,
    ).order_by(None)
    sub = stmt.subquery()
    line = func.coalesce(func.nullif(sub.c.product_line, ""), GENERIC_GROUP)
    rows = session.exec(
        select(line.label("line"), func.count().label("n")).group_by(line)
    ).all()
    counts = [(row[0], int(row[1])) for row in rows]
    counts.sort(key=lambda pair: group_sort_key(pair[0]))
    return counts


def coverage_for_items(session: Session, items: list[Item]) -> dict[int, CoverageRow]:
    out = {item.id: CoverageRow() for item in items if item.id is not None}
    if not out:
        return out

    rows = session.exec(
        select(POLine, PurchaseOrder)
        .join(PurchaseOrder, POLine.po_id == PurchaseOrder.id)
        .where(col(POLine.item_id).in_(list(out)))
        .where(col(PurchaseOrder.status).notin_([POStatus.RECEIVED, POStatus.CANCELLED]))
    ).all()
    for line, po in rows:
        row = out.get(line.item_id)
        if row is None:
            continue
        remaining = max(0.0, float(line.quantity or 0) - float(line.received_quantity or 0))
        row.packtrack_open_qty += remaining
        if po not in row.packtrack_pos:
            row.packtrack_pos.append(po)

    by_zoho_id = {
        str(item.zoho_item_id): item.id
        for item in items
        if item.id is not None and item.zoho_item_id
    }
    mirrors = session.exec(select(ZohoMirror)).all()
    for mirror in mirrors:
        for line in mirror.line_items or []:
            item_id = by_zoho_id.get(str(line.get("item_id") or ""))
            if item_id is None or item_id not in out:
                continue
            qty = float(line.get("quantity") or 0)
            received = float(line.get("quantity_received") or 0)
            remaining = max(0.0, qty - received)
            if remaining <= 0:
                continue
            out[item_id].zoho_open_qty += remaining
            if mirror not in out[item_id].zoho_pos:
                out[item_id].zoho_pos.append(mirror)
    return out
