from datetime import datetime

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from packtrack.models import Item, POLine, POStatus, PurchaseOrder, User, ZohoMirror
from packtrack.services.inventory import (
    coverage_for_items,
    filter_inventory_items,
    suggested_reorder_qty,
)


def _session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _owner(session: Session) -> User:
    owner = User(
        email="owner@example.com",
        name="Owner",
        role="owner",
        password_hash="x",
    )
    session.add(owner)
    session.commit()
    session.refresh(owner)
    return owner


def test_suggested_reorder_qty_uses_usage_and_lead_time():
    item = Item(
        name="Mailer",
        current_stock=100,
        daily_usage_rate=10,
        sea_lead_days=45,
        reorder_point=250,
    )
    assert suggested_reorder_qty(item, buffer_days=14) == 490


def test_filter_inventory_items_handles_stock_and_missing_material_code():
    session = _session()
    session.add_all(
        [
            Item(name="Critical mailer", vendor="Helen", current_stock=5, critical_point=10),
            Item(name="Low carton", vendor="Helen", current_stock=15, reorder_point=20, material_code="MAT-1"),
            Item(name="Fine insert", vendor="Other", current_stock=100, reorder_point=20, material_code="MAT-2"),
        ]
    )
    session.commit()

    missing = filter_inventory_items(session, missing_material_code=True)
    assert [it.name for it in missing] == ["Critical mailer"]

    low = filter_inventory_items(session, stock_status="low")
    assert {it.name for it in low} == {"Critical mailer", "Low carton"}

    vendor = filter_inventory_items(session, vendor="hel")
    assert {it.name for it in vendor} == {"Critical mailer", "Low carton"}


def test_coverage_for_items_counts_packtrack_and_zoho_remaining_quantities():
    session = _session()
    owner = _owner(session)
    item_open = Item(name="Open", zoho_item_id="z-open")
    item_zoho = Item(name="Zoho", zoho_item_id="z-zoho")
    item_clear = Item(name="Clear", zoho_item_id="z-clear")
    session.add_all([item_open, item_zoho, item_clear])
    session.commit()
    for item in (item_open, item_zoho, item_clear):
        session.refresh(item)

    po = PurchaseOrder(
        po_number="PT-1",
        status=POStatus.DESIGN_APPROVED,
        created_by_id=owner.id,
        created_at=datetime.utcnow(),
    )
    session.add(po)
    session.commit()
    session.refresh(po)
    session.add(POLine(po_id=po.id, item_id=item_open.id, quantity=100))
    session.add(
        ZohoMirror(
            zoho_purchaseorder_id="zpo-1",
            purchaseorder_number="PO-1",
            status="issued",
            line_items=[
                {"item_id": "z-zoho", "quantity": 50, "quantity_received": 10},
                {"item_id": "z-clear", "quantity": 20, "quantity_received": 20},
            ],
        )
    )
    session.commit()

    coverage = coverage_for_items(session, [item_open, item_zoho, item_clear])
    assert coverage[item_open.id].packtrack_open_qty == 100
    assert coverage[item_zoho.id].zoho_open_qty == 40
    assert coverage[item_clear.id].is_covered is False
