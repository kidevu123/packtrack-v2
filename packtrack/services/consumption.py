"""Luma → PackTrack packaging consumption service.

Called when Luma releases a finished lot. Updates Item.current_stock,
computes a rolling 30-day daily_usage_rate, and detects threshold crossings.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlmodel import Session, func, select

from packtrack.models import Item, MaterialConsumptionEvent

logger = logging.getLogger("packtrack.consumption")


def _threshold_crossed(item: Item, prev_stock: float, new_stock: float) -> str | None:
    """Return 'critical', 'reorder', or None when stock crosses a configured threshold."""
    if item.critical_point > 0 and prev_stock > item.critical_point >= new_stock:
        return "critical"
    if item.reorder_point > 0 and prev_stock > item.reorder_point >= new_stock:
        return "reorder"
    return None


def _recompute_daily_usage_rate(session: Session, item_id: int) -> float:
    """30-day rolling average. Includes any events already flushed in this transaction."""
    cutoff = datetime.utcnow() - timedelta(days=30)
    total = session.exec(
        select(func.coalesce(func.sum(MaterialConsumptionEvent.qty_consumed), 0.0))
        .where(MaterialConsumptionEvent.item_id == item_id)
        .where(MaterialConsumptionEvent.consumed_at >= cutoff)
    ).one()
    return round(float(total) / 30.0, 4)


def process_luma_consumption(session: Session, payload: dict) -> dict:
    """Process one consumption push from Luma. Idempotent on (finished_lot_id, item).

    Partial success is supported: one malformed entry never aborts the
    whole batch. Per-entry outcomes:

    * ``updated``              — stock decremented, MaterialConsumptionEvent created
    * ``already_processed``    — idempotent replay (UNIQUE finished_lot_id+item_id)
    * ``skipped_not_found``    — material_code not in Item table
    * ``skipped_invalid``      — entry malformed or violates a contract rule
                                 (missing material_code, missing qty_consumed,
                                 non-numeric qty, negative qty). Reason in ``reason``.
    """
    finished_lot_id: str = payload["finished_lot_id"]
    finished_lot_number: str = payload.get("finished_lot_number", "")
    consumed_at = datetime.fromisoformat(payload["released_at"].rstrip("Z"))
    results: list[dict] = []

    for mat in payload.get("consumed_materials", []):
        # ── P0-3: missing material_code must not crash the batch ───────
        material_code = (mat.get("material_code") or "").strip()
        if not material_code:
            logger.warning("consumption: entry missing material_code — skipping (%r)", mat)
            results.append({
                "material_code": None,
                "status": "skipped_invalid",
                "reason": "missing material_code",
            })
            continue

        # ── P0-2: negative or non-numeric qty must not invert the
        #          stock subtraction. Reject the entry, do not touch stock.
        raw_qty = mat.get("qty_consumed")
        if raw_qty is None:
            logger.warning("consumption: %s missing qty_consumed — skipping", material_code)
            results.append({
                "material_code": material_code,
                "status": "skipped_invalid",
                "reason": "missing qty_consumed",
            })
            continue
        try:
            qty = float(raw_qty)
        except (TypeError, ValueError):
            logger.warning(
                "consumption: %s non-numeric qty_consumed %r — skipping",
                material_code, raw_qty,
            )
            results.append({
                "material_code": material_code,
                "status": "skipped_invalid",
                "reason": "non-numeric qty_consumed",
            })
            continue
        if qty < 0:
            logger.warning(
                "consumption: %s rejected negative qty_consumed %g — skipping",
                material_code, qty,
            )
            results.append({
                "material_code": material_code,
                "status": "skipped_invalid",
                "reason": f"negative qty_consumed ({qty})",
            })
            continue

        item = session.exec(
            select(Item).where(Item.material_code == material_code)
        ).first()
        if item is None:
            logger.warning("consumption: material_code %s not found — skipping", material_code)
            results.append({"material_code": material_code, "status": "skipped_not_found"})
            continue

        existing = session.exec(
            select(MaterialConsumptionEvent)
            .where(MaterialConsumptionEvent.finished_lot_id == finished_lot_id)
            .where(MaterialConsumptionEvent.item_id == item.id)
        ).first()
        if existing is not None:
            results.append({"material_code": material_code, "status": "already_processed"})
            continue

        prev_stock = item.current_stock

        event = MaterialConsumptionEvent(
            item_id=item.id,
            qty_consumed=qty,
            finished_lot_id=finished_lot_id,
            finished_lot_number=finished_lot_number,
            supplier_lot_number=mat.get("supplier_lot_number"),
            packaging_lot_id=mat.get("packaging_lot_id"),
            consumed_at=consumed_at,
        )
        session.add(event)
        session.flush()  # event must be visible before rolling-average query

        item.current_stock = max(0.0, prev_stock - qty)
        item.daily_usage_rate = _recompute_daily_usage_rate(session, item.id)
        session.add(item)

        threshold = _threshold_crossed(item, prev_stock=prev_stock, new_stock=item.current_stock)
        results.append({
            "material_code": material_code,
            "status": "updated",
            "prev_stock": prev_stock,
            "new_stock": item.current_stock,
            "threshold_crossed": threshold,
            "item_id": item.id,
        })
        logger.info(
            "consumption: %s consumed %.0f of %s — %.0f → %.0f%s",
            finished_lot_id, qty, material_code, prev_stock, item.current_stock,
            f" [{threshold}]" if threshold else "",
        )

    session.commit()
    return {"processed": results}
