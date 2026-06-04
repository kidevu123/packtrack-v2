"""Insert a handful of demo items + POs across pipeline stages so the
operator dashboard and board show real cards before Zoho is connected.

Idempotent: if any POs already exist, this script no-ops. To re-seed,
truncate purchase_orders first.

    sudo -u packtrack bash -lc 'cd /opt/packtrack/app && . .venv/bin/activate && set -a && source /etc/packtrack/packtrack.env && set +a && python scripts/seed_demo.py'
"""
import os
from datetime import date, datetime, timedelta

from sqlmodel import Session, select

from packtrack.config import settings
from packtrack.db import engine
from packtrack.models import (
    Item,
    POEvent,
    POLine,
    POStatus,
    PurchaseOrder,
    Role,
    Shipment,
    ShipMethod,
    ShipStatus,
    Urgency,
    User,
)

# (name, sku, vendor, stock, unit, daily, reorder, crit, sea_lead, exp_lead, accent_hex, last_cost)
DEMO_ITEMS = [
    {"name": "Bottle 500ml — clear", "sku_code": "BTL-500-CLR", "vendor": "Helen", "current_stock": 240, "unit": "units", "daily_usage_rate": 30, "reorder_point": 800, "critical_point": 300, "sea_lead_days": 45, "express_lead_days": 7, "accent": "#0ea5e9", "last_unit_cost": 0.42},
    {"name": "Display box — Mango Splash", "sku_code": "DISP-MNG-12", "vendor": "Helen", "current_stock": 180, "unit": "boxes", "daily_usage_rate": 6, "reorder_point": 200, "critical_point": 80, "sea_lead_days": 50, "express_lead_days": 8, "accent": "#f59e0b", "last_unit_cost": 1.85},
    {"name": "Master case — 12pk", "sku_code": "MC-12", "vendor": "Helen", "current_stock": 12, "unit": "cases", "daily_usage_rate": 1.5, "reorder_point": 30, "critical_point": 10, "sea_lead_days": 50, "express_lead_days": 8, "accent": "#6366f1", "last_unit_cost": 3.20},
    {"name": "Shrink wrap — clear roll", "sku_code": "SHR-CLR", "vendor": "Helen", "current_stock": 4, "unit": "rolls", "daily_usage_rate": 0.2, "reorder_point": 6, "critical_point": 3, "sea_lead_days": 40, "express_lead_days": 7, "accent": "#10b981", "last_unit_cost": 18.00},
    {"name": "Cap — flip-top white", "sku_code": "CAP-FT-WHT", "vendor": "Helen", "current_stock": 5200, "unit": "units", "daily_usage_rate": 50, "reorder_point": 2000, "critical_point": 800, "sea_lead_days": 45, "express_lead_days": 7, "accent": "#ef4444", "last_unit_cost": 0.07},
]


def _initials(name: str) -> str:
    parts = [p for p in name.split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][:1] + parts[1][:1]).upper()


def _write_placeholder_image(item: Item, accent: str) -> None:
    """Write a simple SVG placeholder so demo items aren't blank rectangles.

    SVG is fine here — browsers serve it as an image when the <img src> ends
    in .svg. No image lib needed; no Zoho call needed.
    """
    safe_id = f"demo-{item.id or item.sku_code or _initials(item.name)}".replace("/", "_")
    fname = f"{safe_id}.svg"
    out_dir = os.path.join(str(settings.UPLOAD_DIR), "items")
    os.makedirs(out_dir, exist_ok=True)
    initials = _initials(item.name)
    svg = f"""<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'>
  <defs>
    <linearGradient id='g' x1='0' y1='0' x2='1' y2='1'>
      <stop offset='0' stop-color='{accent}' stop-opacity='0.95'/>
      <stop offset='1' stop-color='{accent}' stop-opacity='0.55'/>
    </linearGradient>
  </defs>
  <rect width='100' height='100' fill='url(#g)' rx='8' ry='8'/>
  <text x='50' y='62' text-anchor='middle' font-family='ui-sans-serif,system-ui' font-weight='600' font-size='42' fill='white'>{initials}</text>
</svg>"""
    with open(os.path.join(out_dir, fname), "w", encoding="utf-8") as f:
        f.write(svg)
    item.image_path = fname

DEMO_POS = [
    # (po_number_hint, status, urgency, age_days, lines [(item_idx, qty, unit_price)])
    ("PT-202605-0001", POStatus.DRAFT,           Urgency.NORMAL,   1,  [(0, 5000, 0.42)]),
    ("PT-202605-0002", POStatus.DESIGN_REVIEW,   Urgency.HIGH,     2,  [(1, 600, 1.85), (2, 80, 3.20)]),
    ("PT-202605-0003", POStatus.DESIGN_APPROVED, Urgency.NORMAL,   4,  [(2, 100, 3.20)]),
    ("PT-202605-0004", POStatus.PI_RECEIVED,     Urgency.NORMAL,   1,  [(0, 4000, 0.42), (4, 6000, 0.07)]),
    ("PT-202605-0005", POStatus.PRODUCTION,      Urgency.NORMAL,   18, [(1, 1200, 1.85)]),
    ("PT-202605-0006", POStatus.SHIPPED,         Urgency.CRITICAL, 5,  [(3, 30, 18.00)]),
    ("PT-202605-0007", POStatus.DESIGN_REVIEW,   Urgency.NORMAL,   8,  [(4, 8000, 0.07)]),
]


def main() -> None:
    with Session(engine) as session:
        owner = session.exec(select(User).where(User.role == Role.OWNER)).first()
        if owner is None:
            raise SystemExit("Seed an owner first (scripts/seed_owner.py).")

        if session.exec(select(PurchaseOrder).limit(1)).first() is not None:
            print("Demo data already seeded (POs exist) — skipping.")
            return

        # Items: insert if missing + write a per-item placeholder SVG so the
        # UI shows real visuals before Zoho images sync in.
        item_rows: list[Item] = []
        for spec in DEMO_ITEMS:
            accent = spec.pop("accent", "#1c1917")
            existing = session.exec(select(Item).where(Item.sku_code == spec["sku_code"])).first()
            if existing is None:
                row = Item(**spec)
                session.add(row)
                session.flush()
                item_rows.append(row)
            else:
                item_rows.append(existing)
            _write_placeholder_image(item_rows[-1], accent)
        session.commit()

        now = datetime.utcnow()
        for i, (hint, status, urgency, age_days, lines) in enumerate(DEMO_POS):
            # Stagger timestamps within the day so the activity feed reads
            # naturally instead of stacking everything at one timestamp.
            ts = now - timedelta(days=age_days, minutes=i * 7)
            po = PurchaseOrder(
                po_number=hint,
                status=status,
                urgency=urgency,
                notes=None,
                created_by_id=owner.id,
                created_at=ts,
                updated_at=ts,
            )
            session.add(po)
            session.flush()
            for item_idx, qty, price in lines:
                session.add(POLine(
                    po_id=po.id,
                    item_id=item_rows[item_idx].id,
                    quantity=qty,
                    unit_price=price,
                    target_arrival=date.today() + timedelta(days=30),
                ))
            # ONE event per PO — anchors the days-in-stage clock and shows
            # cleanly in the activity feed.
            session.add(POEvent(
                po_id=po.id, kind="status_change",
                message=f"Created in {status.value.replace('_', ' ')}",
                actor_id=owner.id, created_at=ts,
            ))

            # For shipped POs, add real Shipment rows with realistic ETAs so
            # the dashboard "Landing this week" panel has data.
            if status == POStatus.SHIPPED:
                for item_idx, qty, _price in lines:
                    eta_days = 4 if item_idx == 3 else 12  # Shrink wrap arrives soon
                    session.add(Shipment(
                        po_id=po.id,
                        item_id=item_rows[item_idx].id,
                        method=ShipMethod.EXPRESS if eta_days < 7 else ShipMethod.SEA,
                        quantity=qty,
                        shipped_date=ts.date(),
                        eta=date.today() + timedelta(days=eta_days),
                        carrier="DHL" if eta_days < 7 else "Maersk",
                        tracking_number=f"TRK{po.id:04d}",
                        status=ShipStatus.IN_TRANSIT,
                    ))

        session.commit()
        print(f"Seeded {len(item_rows)} items and {len(DEMO_POS)} demo POs.")


if __name__ == "__main__":
    main()
