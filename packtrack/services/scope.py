"""Vendor scope.

The owner buys from many vendors but wants to focus on one at a time. We
store a single ``vendor_scope`` AppSetting (substring, case-insensitive).
When set, every list view filters to items / POs whose vendor matches it.

Matching is generous on purpose: a setting of ``Helen`` matches both
``Helen Industries`` and ``Shenzhen Helen Co.`` because vendor strings vary
between Zoho records (sometimes the legal name, sometimes the agent's name).
If that turns out to be too generous in production, narrow the matcher in
``_matches`` — every caller goes through it.
"""
from __future__ import annotations

from collections.abc import Iterable

from sqlmodel import Session, col, select

from packtrack.models import AppSetting, Item, POLine, PurchaseOrder

SETTING_KEY = "vendor_scope"


def get_scope(session: Session) -> str | None:
    """Return the current vendor scope (stripped). None means 'all vendors'."""
    row = session.get(AppSetting, SETTING_KEY)
    if row is None:
        return None
    val = (row.value or "").strip()
    return val or None


def set_scope(session: Session, value: str | None, *, actor_id: int | None = None) -> None:
    val = (value or "").strip()
    row = session.get(AppSetting, SETTING_KEY)
    if row is None:
        row = AppSetting(key=SETTING_KEY, value=val, updated_by_id=actor_id)
        session.add(row)
    else:
        row.value = val
        row.updated_by_id = actor_id
    session.commit()


def _matches(vendor: str | None, scope: str) -> bool:
    if not vendor or not scope:
        return False
    return scope.lower() in vendor.lower()


def filter_items_query(stmt, scope: str | None):
    """Apply scope filter to a ``SELECT Item`` statement."""
    if not scope:
        return stmt
    return stmt.where(col(Item.vendor).ilike(f"%{scope}%"))


def filter_items_iter(items: Iterable[Item], scope: str | None) -> list[Item]:
    if not scope:
        return list(items)
    return [it for it in items if _matches(it.vendor, scope)]


def items_in_scope_ids(session: Session, scope: str | None) -> set[int]:
    if not scope:
        return set()  # caller treats empty set as "no filter"
    rows = session.exec(
        select(Item.id).where(col(Item.vendor).ilike(f"%{scope}%"))
    ).all()
    return {r for r in rows if r is not None}


def filter_pos_query(stmt, session: Session, scope: str | None):
    """Apply scope to a ``SELECT PurchaseOrder`` statement.

    A PO is in scope if AT LEAST ONE of its lines references an item whose
    vendor matches the scope. POs with zero lines are filtered out (they
    can't be in scope by definition).
    """
    if not scope:
        return stmt
    in_scope_ids = items_in_scope_ids(session, scope)
    if not in_scope_ids:
        # No items match the scope — short-circuit to 0-row result.
        return stmt.where(False)  # type: ignore[arg-type]
    sub = select(POLine.po_id).where(col(POLine.item_id).in_(in_scope_ids)).distinct()
    return stmt.where(col(PurchaseOrder.id).in_(sub))


def filter_pos_iter(
    pos: Iterable[PurchaseOrder], scope: str | None
) -> list[PurchaseOrder]:
    if not scope:
        return list(pos)
    out = []
    for po in pos:
        if any(_matches(line.item.vendor if line.item else None, scope) for line in po.lines):
            out.append(po)
    return out


def distinct_vendors(session: Session) -> list[str]:
    """Vendor names actually present on Items + Zoho mirror — for the picker."""
    from packtrack.models import ZohoMirror

    seen: set[str] = set()
    for v in session.exec(
        select(Item.vendor).where(col(Item.vendor).is_not(None))
    ).all():
        if v and v.strip():
            seen.add(v.strip())
    for v in session.exec(
        select(ZohoMirror.vendor_name).where(col(ZohoMirror.vendor_name).is_not(None))
    ).all():
        if v and v.strip():
            seen.add(v.strip())
    return sorted(seen, key=str.casefold)
