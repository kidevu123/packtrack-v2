"""Drop one fat multi-line PO to stress-test the rendering."""
from datetime import date, datetime, timedelta

from sqlmodel import Session, select

from packtrack.db import engine
from packtrack.models import (
    Item,
    POEvent,
    POLine,
    POStatus,
    PurchaseOrder,
    Role,
    Urgency,
    User,
)


def main() -> None:
    with Session(engine) as s:
        owner = s.exec(select(User).where(User.role == Role.OWNER)).first()
        if owner is None:
            raise SystemExit("Seed an owner first.")
        # Look items up by SKU — DON'T rely on insertion order or name-sort,
        # since alphabetical sort will reorder Cap before Display box, etc.
        by_sku = {it.sku_code: it for it in s.exec(select(Item)).all()}
        for needed in ("BTL-500-CLR", "DISP-MNG-12", "MC-12", "SHR-CLR", "CAP-FT-WHT"):
            if needed not in by_sku:
                raise SystemExit(f"Demo item {needed} missing — run scripts/seed_demo.py first.")
        existing = s.exec(
            select(PurchaseOrder).where(PurchaseOrder.po_number == "PT-202605-0099")
        ).first()
        if existing is not None:
            print("PT-202605-0099 already exists — skipping.")
            return
        po = PurchaseOrder(
            po_number="PT-202605-0099",
            status=POStatus.DESIGN_REVIEW,
            urgency=Urgency.HIGH,
            notes="Full product launch — Mango Splash 12-pack production run",
            currency="USD",
            created_by_id=owner.id,
            created_at=datetime.utcnow() - timedelta(days=2),
            updated_at=datetime.utcnow() - timedelta(days=2),
        )
        s.add(po)
        s.flush()
        # 8 lines: each line is (sku, qty, unit_price, optional_note)
        plan = [
            ("BTL-500-CLR", 12000, 0.42, "Lot A — November run"),
            ("DISP-MNG-12", 1000,  1.85, "Standard artwork"),
            ("MC-12",       84,    3.20, None),
            ("SHR-CLR",     30,    18.00, None),
            ("CAP-FT-WHT",  12000, 0.07, "White"),
            ("BTL-500-CLR", 6000,  0.40, "Lot B — December run"),
            ("DISP-MNG-12", 500,   1.80, "Promo artwork"),
            ("CAP-FT-WHT",  6000,  0.065, "Cream"),
        ]
        for sku, qty, price, note in plan:
            s.add(POLine(
                po_id=po.id,
                item_id=by_sku[sku].id,
                quantity=qty,
                unit_price=price,
                line_notes=note,
                target_arrival=date.today() + timedelta(days=45),
            ))
        s.add(POEvent(
            po_id=po.id, kind="status_change",
            message="Created in design review",
            actor_id=owner.id,
        ))
        s.commit()
        total = sum(q * p for _, q, p, _note in plan)
        print(f"PT-202605-0099: {len(plan)} lines, ${total:,.2f}")


if __name__ == "__main__":
    main()
