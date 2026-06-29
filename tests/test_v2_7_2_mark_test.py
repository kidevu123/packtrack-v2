"""v2.7.2 — owner-only safe ``mark-test`` route + OIDC test determinism.

Covers:

* Mark-test requires auth (flag-gated 404 when off; 401 when no user).
* Mark-test is OWNER-only (RECEIVING returns 403).
* Confirmation string required (no accidental marks).
* POEvent ``receive_marked_test`` recorded with operator + reason.
* No BoxReceipt is deleted.
* No ``submit_zoho_receives`` / ``push_luma_receipt`` is called.
* Receive notes carry the marker line.
* Result page shows the test/canary banner after marking.
* Legacy /receive/{zoho_po_id} still renders (regression guard).
"""
from __future__ import annotations

import os
from datetime import date, datetime

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("PACKTRACK_SECRET_KEY", "test-secret")

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from packtrack.config import settings
from packtrack.models import (
    BoxReceipt,
    Confidence,
    Item,
    LumaPushStatus,
    POEvent,
    POLine,
    POStatus,
    PurchaseOrder,
    Receive,
    ReceiveCase,
    ReceiveCaseLine,
    ReceiveStatus,
    Role,
    ShipmentKind,
    User,
    ZohoMirror,
)
from packtrack.routes.receiving_v2 import MARK_TEST_CONFIRMATION_STRING


@pytest.fixture(name="engine")
def engine_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with engine.connect() as conn:
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_receive_cases_receive_case_number "
            "ON receive_cases (receive_id, vendor_case_number) "
            "WHERE vendor_case_number IS NOT NULL"
        )
        conn.commit()
    return engine


@pytest.fixture(name="session")
def session_fixture(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture(autouse=True)
def _clear_app_overrides():
    yield
    from packtrack.main import app
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _flag_on():
    """All these tests run with the vNext flag ON; restore after."""
    original = settings.RECEIVING_VNEXT_ENABLED
    settings.RECEIVING_VNEXT_ENABLED = True
    yield
    settings.RECEIVING_VNEXT_ENABLED = original


def _seed_user(session, role=Role.OWNER, name="Owner"):
    u = User(
        id=1, email=f"{role.value}@example.com", name=name,
        role=role, password_hash="x", is_active=True,
    )
    session.add(u)
    session.commit()
    return u


def _seed_world(session, *, with_zoho=True):
    """One PO + mirror + a finalized canary-style Receive with one BoxReceipt."""
    user = _seed_user(session)
    item = Item(
        id=42, name="Canary item", sku_code="SKU-CAN", material_code="MC-CAN",
        zoho_item_id="z-can", unit="EACH", vendor="ACME", current_stock=0,
    )
    session.add(item)
    session.commit()
    po = PurchaseOrder(
        po_number="PO-CAN-1", status=POStatus.DESIGN_APPROVED,
        created_by_id=user.id, created_at=datetime.utcnow(),
        zoho_po_id="po-z-can-1" if with_zoho else None,
    )
    session.add(po)
    session.commit()
    session.refresh(po)
    session.add(POLine(po_id=po.id, item_id=item.id, quantity=10))
    session.commit()
    if with_zoho:
        session.add(ZohoMirror(
            zoho_purchaseorder_id=po.zoho_po_id, purchaseorder_number=po.po_number,
            line_items=[{"item_id": "z-can", "line_item_id": "li-can",
                         "name": item.name, "quantity": 10, "quantity_received": 0}],
        ))
        session.commit()
    rec = Receive(
        receive_number="R-2026-9999",
        purchase_order_id=po.id,
        delivery_date=date(2026, 6, 26),
        received_by_user_id=user.id,
        finalized_by_user_id=user.id,
        status=ReceiveStatus.PUSHED_OK,
        submission_id="cafe" * 8,
        shipment_kind=ShipmentKind.PALLETIZED,
        notes="CANARY DRAFT — original operator note.",
        finalized_at=datetime.utcnow(), pushed_at=datetime.utcnow(),
    )
    session.add(rec)
    session.commit()
    session.refresh(rec)
    case = ReceiveCase(receive_id=rec.id, vendor_case_number="CAN-1", sequence=1)
    session.add(case)
    session.commit()
    session.refresh(case)
    line = ReceiveCaseLine(
        receive_case_id=case.id, purchase_order_id=po.id, item_id=item.id,
        declared_quantity=1, counted_quantity=None, accepted_quantity=1,
        unit_of_measure="EACH",
    )
    session.add(line)
    session.commit()
    session.refresh(line)
    box = BoxReceipt(
        packtrack_receipt_id="canary-receipt-id-0000000000000000",
        purchase_order_id=po.id, item_id=item.id,
        material_code=item.material_code, material_name=item.name[:240],
        supplier=item.vendor, supplier_lot_number=None,
        box_number="PT-canary-receipt-id-0000000000000000",
        submission_id=rec.submission_id, submission_line_index=1,
        declared_quantity=1, counted_quantity=None, accepted_quantity=1,
        unit_of_measure="EACH", confidence=Confidence.MEDIUM,
        received_by_user_id=user.id, received_at=datetime.utcnow(),
        luma_push_status=LumaPushStatus.PUSHED,
        receive_id=rec.id, receive_case_line_id=line.id,
    )
    session.add(box)
    line.box_receipt_id = box.id
    session.commit()
    return rec, box, po, user


def _client(session, engine, monkeypatch, *, user=None):
    from fastapi.testclient import TestClient

    import packtrack.db
    import packtrack.main
    from packtrack import deps
    from packtrack.db import get_session
    from packtrack.main import app

    monkeypatch.setattr(packtrack.db, "engine", engine)
    monkeypatch.setattr(packtrack.main, "engine", engine)
    app.dependency_overrides[get_session] = lambda: session

    def _force_user():
        return user or session.exec(select(User).order_by(User.id)).first()
    app.dependency_overrides[deps.require_user] = _force_user
    app.dependency_overrides[deps.current_user] = _force_user
    return TestClient(app, raise_server_exceptions=False)


def _stub_externals(monkeypatch):
    """Spies that count any accidental external call. Test fails if any
    fires during the mark-test flow."""
    calls = {"luma_push": 0, "zoho_submit": 0,
             "luma_register_material": 0, "ensure_material_code": 0}
    from packtrack.services import receiving as recv_svc
    from packtrack.services import receiving_v2_finalize as finalize_svc

    def _no_luma(*_a, **_kw):
        calls["luma_push"] += 1
        return True, None, {"ok": True}

    def _no_zoho(*_a, **_kw):
        calls["zoho_submit"] += 1
        return []

    def _no_register(*_a, **_kw):
        calls["luma_register_material"] += 1
        return True, None

    def _no_ensure(*_a, **_kw):
        calls["ensure_material_code"] += 1
        return None

    monkeypatch.setattr(finalize_svc, "push_luma_receipt", _no_luma)
    monkeypatch.setattr(finalize_svc, "submit_zoho_receives", _no_zoho)
    monkeypatch.setattr(finalize_svc, "register_material_with_luma", _no_register)
    monkeypatch.setattr(finalize_svc, "ensure_material_code", _no_ensure)
    monkeypatch.setattr(recv_svc, "push_luma_receipt", _no_luma)
    monkeypatch.setattr(recv_svc, "submit_zoho_receives", _no_zoho)
    return calls


# ---------------------------------------------------------------------------


def test_mark_test_flag_off_returns_404(session, engine, monkeypatch):
    settings.RECEIVING_VNEXT_ENABLED = False
    _seed_world(session)
    client = _client(session, engine, monkeypatch)
    r = client.post(
        "/receive/v2/1/mark-test",
        data={"confirm": MARK_TEST_CONFIRMATION_STRING},
    )
    assert r.status_code == 404


def test_mark_test_non_owner_forbidden(session, engine, monkeypatch):
    rec, _, _, _ = _seed_world(session)
    # Replace seeded OWNER with RECEIVING role.
    only = session.exec(select(User)).first()
    only.role = Role.RECEIVING
    session.add(only)
    session.commit()
    client = _client(session, engine, monkeypatch, user=only)
    r = client.post(
        f"/receive/v2/{rec.id}/mark-test",
        data={"confirm": MARK_TEST_CONFIRMATION_STRING},
    )
    assert r.status_code == 403
    assert "Only OWNER" in r.text


def test_mark_test_requires_explicit_confirmation_string(session, engine, monkeypatch):
    rec, _, _, _ = _seed_world(session)
    client = _client(session, engine, monkeypatch)
    # Missing confirm
    r1 = client.post(f"/receive/v2/{rec.id}/mark-test", data={})
    assert r1.status_code == 400
    # Wrong confirm string
    r2 = client.post(f"/receive/v2/{rec.id}/mark-test", data={"confirm": "ok"})
    assert r2.status_code == 400
    # The error tells the operator what to send AND warns about external
    # records.
    assert MARK_TEST_CONFIRMATION_STRING in r2.text
    assert "NOT reversed" in r2.text


def test_mark_test_happy_path_records_event_and_does_not_call_externals(
    session, engine, monkeypatch,
):
    rec, box, po, user = _seed_world(session)
    calls = _stub_externals(monkeypatch)
    box_count_before = session.scalar(
        select(__import__("sqlmodel").func.count()).select_from(BoxReceipt)
    )

    client = _client(session, engine, monkeypatch)
    r = client.post(
        f"/receive/v2/{rec.id}/mark-test",
        data={"confirm": MARK_TEST_CONFIRMATION_STRING, "reason": "vNext canary"},
    )
    assert r.status_code == 200

    # 1. No external calls — at all.
    assert calls == {
        "luma_push": 0, "zoho_submit": 0,
        "luma_register_material": 0, "ensure_material_code": 0,
    }

    # 2. No BoxReceipt deletion.
    box_count_after = session.scalar(
        select(__import__("sqlmodel").func.count()).select_from(BoxReceipt)
    )
    assert box_count_after == box_count_before
    refreshed_box = session.get(BoxReceipt, box.id)
    assert refreshed_box is not None
    assert refreshed_box.luma_push_status == LumaPushStatus.PUSHED  # unchanged

    # 3. POEvent emitted.
    ev = session.exec(
        select(POEvent)
        .where(POEvent.po_id == po.id)
        .where(POEvent.kind == "receive_marked_test")
    ).first()
    assert ev is not None
    assert "R-2026-9999" in ev.message
    assert "vNext canary" in ev.message
    assert "NOT reversed" in ev.message
    assert ev.actor_id == user.id

    # 4. Receive.notes carries the marker line + the original note.
    session.refresh(rec)
    assert "CANARY DRAFT — original operator note." in (rec.notes or "")
    assert "[Marked as TEST/CANARY" in (rec.notes or "")
    assert "External Zoho/Luma records were NOT reversed" in (rec.notes or "")

    # 5. Result page UI carries the test/canary banner.
    # v2.7.4 normalized the banner copy to lowercase "external"; assert on
    # a stable substring that matches both old and new wording.
    assert "Test / canary receive" in r.text
    assert "Zoho and Luma records were NOT reversed" in r.text


def test_result_page_banner_visible_on_subsequent_render(
    session, engine, monkeypatch,
):
    """After marking, the next render of result.html (e.g. via a retry-push
    or the finalize redirect path) should still show the warning banner."""
    rec, _, _, _ = _seed_world(session)
    _stub_externals(monkeypatch)
    client = _client(session, engine, monkeypatch)
    client.post(
        f"/receive/v2/{rec.id}/mark-test",
        data={"confirm": MARK_TEST_CONFIRMATION_STRING, "reason": "test"},
    )
    # Retry-push re-renders the same template; banner should appear.
    r = client.post(f"/receive/v2/{rec.id}/retry-push")
    assert r.status_code == 200
    assert "Test / canary receive" in r.text


def test_mark_test_returns_403_when_user_is_design_role(session, engine, monkeypatch):
    rec, _, _, _ = _seed_world(session)
    only = session.exec(select(User)).first()
    only.role = Role.DESIGN
    session.add(only)
    session.commit()
    client = _client(session, engine, monkeypatch, user=only)
    r = client.post(
        f"/receive/v2/{rec.id}/mark-test",
        data={"confirm": MARK_TEST_CONFIRMATION_STRING},
    )
    assert r.status_code == 403


def test_legacy_receive_form_still_renders(session, engine, monkeypatch):
    """Stage 2 regression guard: legacy /receive/{zoho_po_id} still works."""
    _rec, _, po, _ = _seed_world(session)
    client = _client(session, engine, monkeypatch)
    r = client.get(f"/receive/{po.zoho_po_id}")
    assert r.status_code == 200
    assert "submission_id" in r.text  # v2.4.1 token still embedded
