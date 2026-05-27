"""
Phase D — Logistics forecasting service.

compute_forecast(session) -> list[ForecastRow]

Returns one ForecastRow per Item that has a material_code, sorted:
  1. reorder_by_sea ASC (most urgent first)
  2. Items with no sales velocity at the end (days_of_stock = ∞)

Cached: BOM fetch from Luma is cached for 1 hour (module-level dict).
Never raises — logs warnings and skips unmapped items.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import httpx
from sqlmodel import Session, func, select

from packtrack.config import settings
from packtrack.models import Item, MaterialConsumptionEvent, SalesEvent

logger = logging.getLogger("packtrack.forecast")

# ---------------------------------------------------------------------------
# BOM cache — {product_sku: {material_code: qty_per_unit}}, refreshed every hour
# ---------------------------------------------------------------------------

_BOM_CACHE: dict[str, dict[str, float]] = {}
_BOM_FETCHED_AT: float = 0.0
_BOM_TTL: float = 3600.0  # seconds


def _fetch_bom() -> dict[str, dict[str, float]]:
    """Fetch BOM from Luma. Returns {} on failure (logged)."""
    if not settings.LUMA_URL or not settings.LUMA_PACKTRACK_SECRET:
        logger.warning("LUMA_URL or LUMA_PACKTRACK_SECRET not configured — forecast BOM empty")
        return {}
    url = f"{settings.LUMA_URL.rstrip('/')}/api/internal/product-packaging-specs"
    try:
        resp = httpx.get(
            url,
            headers={"X-Luma-PackTrack-Secret": settings.LUMA_PACKTRACK_SECRET},
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
        bom: dict[str, dict[str, float]] = {}
        for entry in data:
            sku = entry.get("product_sku", "")
            comps: dict[str, float] = {}
            for c in entry.get("components", []):
                mc = c.get("material_code", "")
                qty = float(c.get("qty_per_unit", 0))
                if mc and qty > 0:
                    comps[mc] = qty
            if sku and comps:
                bom[sku] = comps
        return bom
    except Exception:
        logger.exception("Failed to fetch BOM from Luma at %s", url)
        return {}


def _get_bom() -> dict[str, dict[str, float]]:
    global _BOM_CACHE, _BOM_FETCHED_AT
    now = time.monotonic()
    if now - _BOM_FETCHED_AT > _BOM_TTL:
        _BOM_CACHE = _fetch_bom()
        _BOM_FETCHED_AT = now
    return _BOM_CACHE


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class ForecastRow:
    item: Item
    daily_demand: float          # units/day from sales velocity × BOM
    days_of_stock: float         # current_stock / daily_demand (inf if demand == 0)
    reorder_by_sea: date | None  # today + days_of_stock - sea_lead_days (None if inf)
    suggested_qty: float         # (sea_lead_days + 30) × daily_demand - current_stock
    panel: str                   # "order_now" | "watch" | "ok" | "no_velocity"
    sales_drivers: list[tuple[str, float]] = field(default_factory=list)  # [(sku, daily_qty)]


# ---------------------------------------------------------------------------
# Main compute function
# ---------------------------------------------------------------------------

def compute_forecast(session: Session) -> list[ForecastRow]:
    today = date.today()
    cutoff = datetime.utcnow() - timedelta(days=60)

    # 1. Sales velocity per product_sku: avg daily qty, rolling 60 days
    sales_rows = session.exec(
        select(
            SalesEvent.product_sku,
            func.coalesce(func.sum(SalesEvent.qty_sold), 0).label("total_sold"),
        )
        .where(SalesEvent.sold_at >= cutoff)
        .group_by(SalesEvent.product_sku)
    ).all()
    sales_velocity: dict[str, float] = {
        row.product_sku: float(row.total_sold) / 60.0
        for row in sales_rows
    }

    # 2. BOM from Luma (cached)
    bom = _get_bom()

    # 3. Compute daily_demand per material_code
    #    daily_demand[mc] = Σ (sales_velocity[P] × bom[P][mc]) for all products P
    daily_demand_by_code: dict[str, float] = {}
    sales_drivers_by_code: dict[str, list[tuple[str, float]]] = {}
    for product_sku, velocity in sales_velocity.items():
        if velocity <= 0:
            continue
        for mc, qty_per_unit in bom.get(product_sku, {}).items():
            contribution = velocity * qty_per_unit
            daily_demand_by_code[mc] = daily_demand_by_code.get(mc, 0.0) + contribution
            if mc not in sales_drivers_by_code:
                sales_drivers_by_code[mc] = []
            sales_drivers_by_code[mc].append((product_sku, round(contribution, 2)))

    # 4. Load all items with material_code (PackTrack packaging items)
    items = session.exec(
        select(Item).where(Item.material_code.is_not(None)).order_by(Item.name)
    ).all()

    # 5. Build ForecastRow per item
    rows: list[ForecastRow] = []
    for item in items:
        mc = item.material_code
        demand = daily_demand_by_code.get(mc, 0.0)
        stock = float(item.current_stock)

        if demand <= 0:
            rows.append(ForecastRow(
                item=item,
                daily_demand=0.0,
                days_of_stock=float("inf"),
                reorder_by_sea=None,
                suggested_qty=0.0,
                panel="no_velocity",
                sales_drivers=[],
            ))
            continue

        days_of_stock = stock / demand
        reorder_by_sea = today + timedelta(days=days_of_stock) - timedelta(days=item.sea_lead_days)
        suggested_qty = max(0.0, (item.sea_lead_days + 30) * demand - stock)
        days_until_reorder = (reorder_by_sea - today).days

        if days_until_reorder <= 7:
            panel = "order_now"
        elif days_until_reorder <= 30:
            panel = "watch"
        else:
            panel = "ok"

        rows.append(ForecastRow(
            item=item,
            daily_demand=round(demand, 2),
            days_of_stock=round(days_of_stock, 1),
            reorder_by_sea=reorder_by_sea,
            suggested_qty=round(suggested_qty),
            panel=panel,
            sales_drivers=sorted(
                sales_drivers_by_code.get(mc, []),
                key=lambda x: x[1],
                reverse=True,
            ),
        ))

    # Sort: order_now first, then watch, then ok, then no_velocity; within each group by reorder_by_sea
    panel_order = {"order_now": 0, "watch": 1, "ok": 2, "no_velocity": 3}
    rows.sort(key=lambda r: (
        panel_order[r.panel],
        r.reorder_by_sea or date(9999, 12, 31),
    ))

    return rows
