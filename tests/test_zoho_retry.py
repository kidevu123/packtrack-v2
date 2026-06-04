from datetime import datetime

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from packtrack import zoho
from packtrack.models import Item, POLine, POStatus, PurchaseOrder, User


def _session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_retry_unpushed_skips_cancelled_and_records_failures(monkeypatch):
    session = _session()
    user = User(email="owner@example.com", name="Owner", role="owner", password_hash="x")
    item = Item(name="Mailer", zoho_item_id="z1")
    session.add_all([user, item])
    session.commit()
    session.refresh(user)
    session.refresh(item)
    open_po = PurchaseOrder(
        po_number="PT-open",
        status=POStatus.DESIGN_REVIEW,
        created_by_id=user.id,
        created_at=datetime.utcnow(),
    )
    cancelled = PurchaseOrder(
        po_number="PT-cancelled",
        status=POStatus.CANCELLED,
        created_by_id=user.id,
        created_at=datetime.utcnow(),
    )
    session.add_all([open_po, cancelled])
    session.commit()
    session.refresh(open_po)
    session.add(POLine(po_id=open_po.id, item_id=item.id, quantity=10))
    session.commit()

    def fail_push(session_arg, po):
        po.push_status = "failed"
        po.push_error = "network down"
        session_arg.commit()
        return False, None, "network down"

    monkeypatch.setattr(zoho, "push_po", fail_push)

    assert zoho.retry_unpushed(session) == {"tried": 1, "ok": 0, "failed": 1}
    session.refresh(open_po)
    assert open_po.push_status == "failed"
    assert cancelled.push_status is None
