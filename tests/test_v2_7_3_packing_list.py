"""v2.7.3 — packing-list attachment for Receiving vNext.

Test contract (per Phase 5 spec):

1. Flag OFF blocks packing-list upload route.
2. Flag ON allows OWNER/RECEIVING to upload.
3. Upload creates Attachment kind PACKING_LIST.
4. Receive.packing_list_attachment_id is set.
5. Receive page renders attached packing-list filename/link.
6. Replacing packing list updates the primary attachment pointer (old
   attachment row preserved for audit; pointer swaps to the new one).
7. Upload does not trigger finalize.
8. Upload does not create BoxReceipts.
9. Upload does not call Zoho/Luma.
10. Legacy receive still works.
11. Existing Receiving vNext tests still pass (covered by the rest of the suite).
12. Mark-test route still works (smoke regression).
"""
from __future__ import annotations

import io
import os
from datetime import date, datetime

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("PACKTRACK_SECRET_KEY", "test-secret")

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from packtrack.config import settings
from packtrack.models import (
    Attachment,
    AttachmentKind,
    BoxReceipt,
    Item,
    POLine,
    POStatus,
    PurchaseOrder,
    Receive,
    ReceiveStatus,
    Role,
    ShipmentKind,
    User,
    ZohoMirror,
)


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
    original = settings.RECEIVING_VNEXT_ENABLED
    settings.RECEIVING_VNEXT_ENABLED = True
    yield
    settings.RECEIVING_VNEXT_ENABLED = original


@pytest.fixture(autouse=True)
def _isolated_upload_dir(tmp_path, monkeypatch):
    """Redirect uploads to a temp dir so tests never write into the
    real ./uploads tree. Pre-create the directory because the
    ``/uploads`` StaticFiles mount in main.py requires it to exist."""
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "UPLOAD_DIR", upload_dir)
    yield


def _seed_user(session, role=Role.OWNER, name="Owner", uid=1):
    u = User(
        id=uid, email=f"{role.value}{uid}@example.com", name=name,
        role=role, password_hash="x", is_active=True,
    )
    session.add(u)
    session.commit()
    return u


def _seed_world(session, *, with_zoho=True):
    user = _seed_user(session)
    item = Item(
        id=11, name="Test Item", sku_code="SKU-1", material_code="MC-1",
        zoho_item_id="z-1", unit="EACH", vendor="ACME", current_stock=0,
    )
    session.add(item)
    session.commit()
    po = PurchaseOrder(
        po_number="PO-PL-1", status=POStatus.DESIGN_APPROVED,
        created_by_id=user.id, created_at=datetime.utcnow(),
        zoho_po_id="po-z-pl-1" if with_zoho else None,
    )
    session.add(po)
    session.commit()
    session.refresh(po)
    session.add(POLine(po_id=po.id, item_id=item.id, quantity=100))
    session.commit()
    if with_zoho:
        session.add(ZohoMirror(
            zoho_purchaseorder_id=po.zoho_po_id, purchaseorder_number=po.po_number,
            line_items=[{"item_id": "z-1", "line_item_id": "li-1",
                         "name": item.name, "quantity": 100, "quantity_received": 0}],
        ))
        session.commit()
    rec = Receive(
        receive_number="R-2026-1111",
        purchase_order_id=po.id,
        delivery_date=date(2026, 6, 26),
        received_by_user_id=user.id,
        status=ReceiveStatus.COUNTING,
        submission_id="cafe" * 8,
        shipment_kind=ShipmentKind.PALLETIZED,
    )
    session.add(rec)
    session.commit()
    session.refresh(rec)
    return rec, po, user


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
    """Spies that count any external call. Must stay at zero across
    every assertion in this file."""
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


def _pdf_bytes() -> bytes:
    """A minimal valid PDF header — enough that the upload handler
    treats it as a real file. We don't parse it."""
    return b"%PDF-1.4\n%canary-test\n1 0 obj\n<<>>\nendobj\nxref\n0 1\ntrailer\n<<>>\n%%EOF\n"


def _upload(client, receive_id, *, filename="packing.pdf", content=None):
    content = content or _pdf_bytes()
    return client.post(
        f"/receive/v2/{receive_id}/packing-list",
        files={"file": (filename, io.BytesIO(content), "application/pdf")},
        follow_redirects=False,
    )


# ---------------------------------------------------------------------------
# 1. Flag OFF blocks the route
# ---------------------------------------------------------------------------


def test_packing_list_upload_blocked_when_flag_off(session, engine, monkeypatch):
    settings.RECEIVING_VNEXT_ENABLED = False
    rec, _, _ = _seed_world(session)
    client = _client(session, engine, monkeypatch)
    r = _upload(client, rec.id)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 2 + 3 + 4 + 7 + 8 + 9. Happy path
# ---------------------------------------------------------------------------


def test_owner_upload_creates_packing_list_attachment_and_sets_pointer(
    session, engine, monkeypatch,
):
    calls = _stub_externals(monkeypatch)
    rec, po, user = _seed_world(session)
    box_count_before = session.scalar(
        select(__import__("sqlmodel").func.count()).select_from(BoxReceipt)
    )
    client = _client(session, engine, monkeypatch)

    r = _upload(client, rec.id, filename="sweettrip_packing_list.pdf")
    assert r.status_code == 303, r.text
    assert r.headers["Location"] == f"/receive/v2/{rec.id}"

    # Attachment row created with kind=PACKING_LIST.
    att = session.exec(
        select(Attachment).where(Attachment.kind == AttachmentKind.PACKING_LIST)
    ).first()
    assert att is not None
    assert att.po_id == po.id
    assert att.filename == "sweettrip_packing_list.pdf"
    assert att.file_path and att.file_path.startswith(AttachmentKind.PACKING_LIST.value + "/")
    assert att.version == 1
    assert att.uploaded_by_id == user.id
    assert att.source == "web"

    # Receive.packing_list_attachment_id set.
    session.refresh(rec)
    assert rec.packing_list_attachment_id == att.id
    assert rec.status == ReceiveStatus.COUNTING  # NOT finalized
    assert rec.finalized_at is None

    # No external calls. No BoxReceipts created.
    assert calls == {"luma_push": 0, "zoho_submit": 0,
                     "luma_register_material": 0, "ensure_material_code": 0}
    box_count_after = session.scalar(
        select(__import__("sqlmodel").func.count()).select_from(BoxReceipt)
    )
    assert box_count_after == box_count_before


def test_receiving_user_can_upload(session, engine, monkeypatch):
    _stub_externals(monkeypatch)
    rec, _, _ = _seed_world(session)
    seeded = session.exec(select(User)).first()
    seeded.role = Role.RECEIVING
    session.add(seeded)
    session.commit()
    client = _client(session, engine, monkeypatch, user=seeded)
    r = _upload(client, rec.id)
    assert r.status_code == 303


def test_design_role_forbidden(session, engine, monkeypatch):
    _stub_externals(monkeypatch)
    rec, _, _ = _seed_world(session)
    seeded = session.exec(select(User)).first()
    seeded.role = Role.DESIGN
    session.add(seeded)
    session.commit()
    client = _client(session, engine, monkeypatch, user=seeded)
    r = _upload(client, rec.id)
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# 5. Receive page renders attached filename + link
# ---------------------------------------------------------------------------


def test_receive_page_renders_attached_packing_list(session, engine, monkeypatch):
    _stub_externals(monkeypatch)
    rec, _, _ = _seed_world(session)
    client = _client(session, engine, monkeypatch)
    _upload(client, rec.id, filename="vendor_pl.pdf")

    page = client.get(f"/receive/v2/{rec.id}")
    assert page.status_code == 200
    assert "vendor_pl.pdf" in page.text
    assert 'data-testid="packing-list-link"' in page.text
    assert 'data-testid="packing-list-replace-form"' in page.text
    assert 'data-testid="packing-list-upload-form"' not in page.text


def test_receive_page_renders_empty_state_when_no_packing_list(
    session, engine, monkeypatch,
):
    rec, _, _ = _seed_world(session)
    client = _client(session, engine, monkeypatch)
    page = client.get(f"/receive/v2/{rec.id}")
    assert page.status_code == 200
    assert "No packing list attached" in page.text
    assert 'data-testid="packing-list-upload-form"' in page.text


# ---------------------------------------------------------------------------
# 6. Replace updates the pointer and bumps version
# ---------------------------------------------------------------------------


def test_replace_swaps_pointer_and_keeps_old_attachment(session, engine, monkeypatch):
    _stub_externals(monkeypatch)
    rec, _, _ = _seed_world(session)
    client = _client(session, engine, monkeypatch)

    _upload(client, rec.id, filename="v1.pdf")
    session.refresh(rec)
    first_id = rec.packing_list_attachment_id
    assert first_id is not None

    _upload(client, rec.id, filename="v2.pdf")
    session.refresh(rec)
    second_id = rec.packing_list_attachment_id
    assert second_id is not None
    assert second_id != first_id

    rows = session.exec(
        select(Attachment).where(Attachment.kind == AttachmentKind.PACKING_LIST)
    ).all()
    assert {a.id for a in rows} == {first_id, second_id}
    versions = sorted(a.version for a in rows)
    assert versions == [1, 2]


# ---------------------------------------------------------------------------
# Extension allow-list + bad inputs
# ---------------------------------------------------------------------------


def test_upload_rejects_disallowed_extension(session, engine, monkeypatch):
    _stub_externals(monkeypatch)
    rec, _, _ = _seed_world(session)
    client = _client(session, engine, monkeypatch)
    r = _upload(client, rec.id, filename="malware.exe", content=b"MZbad")
    assert r.status_code == 400
    assert "Allowed packing-list types" in r.text


def test_upload_rejects_when_receive_has_no_po(session, engine, monkeypatch):
    _stub_externals(monkeypatch)
    user = _seed_user(session)
    rec = Receive(
        receive_number="R-2026-9999",
        purchase_order_id=None,
        delivery_date=date(2026, 6, 26),
        received_by_user_id=user.id,
        status=ReceiveStatus.DRAFT,
        submission_id="abcd" * 8,
    )
    session.add(rec)
    session.commit()
    session.refresh(rec)
    client = _client(session, engine, monkeypatch)
    r = _upload(client, rec.id)
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# 10. Legacy receive still works (regression guard)
# ---------------------------------------------------------------------------


def test_legacy_receive_form_still_renders(session, engine, monkeypatch):
    _, po, _ = _seed_world(session)
    client = _client(session, engine, monkeypatch)
    r = client.get(f"/receive/{po.zoho_po_id}")
    assert r.status_code == 200
    assert "submission_id" in r.text


# ---------------------------------------------------------------------------
# 12. Mark-test smoke
# ---------------------------------------------------------------------------


def test_mark_test_route_still_reachable(session, engine, monkeypatch):
    from packtrack.routes.receiving_v2 import MARK_TEST_CONFIRMATION_STRING

    _stub_externals(monkeypatch)
    rec, _, _ = _seed_world(session)
    client = _client(session, engine, monkeypatch)
    r = client.post(
        f"/receive/v2/{rec.id}/mark-test",
        data={"confirm": MARK_TEST_CONFIRMATION_STRING, "reason": "test"},
    )
    assert r.status_code == 200
    assert "Test / canary receive" in r.text
