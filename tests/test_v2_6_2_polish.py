"""v2.7.1 polish tests (Receiving vNext):

1. ``build_zoho_receive_notes`` produces a clean, human-readable string
   (no duplicated ``[zoho-integration]`` / raw ``pack_track_*`` ids —
   the upstream service prepends its own trace, so PackTrack only
   contributes operator-facing prose).
2. The Receiving list page shows a "Start receive" vNext entry point
   when ``RECEIVING_VNEXT_ENABLED=true`` AND the PO is linked to a
   PackTrack PurchaseOrder AND is not fully received.
3. Same page does NOT show the entry point when the flag is off.
4. Same page does NOT show the entry point for fully-received POs.
5. The entry point links to the non-mutating
   ``GET /receive/v2/new?po_id=<internal_po_id>`` start page.
6. Legacy receiving list still renders end-to-end.

These tests live above the existing Stage 1/2 receiving tests; they do
not redundantly re-test materialization or push behavior.
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
from packtrack.services.receiving_v2_finalize import build_zoho_receive_notes

# ---------------------------------------------------------------------------
# Fixtures (shared shape with the existing v2_finalize tests)
# ---------------------------------------------------------------------------


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
def _restore_flag():
    original = settings.RECEIVING_VNEXT_ENABLED
    yield
    settings.RECEIVING_VNEXT_ENABLED = original


def _seed_user(session, role=Role.RECEIVING, *, name="Sahil Khatri"):
    user = User(
        id=1, email=f"{role.value}@example.com", name=name,
        role=role, password_hash="x", is_active=True,
    )
    session.add(user)
    session.commit()
    return user


def _client(session, engine, monkeypatch):
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
        return session.exec(select(User).order_by(User.id)).first()
    app.dependency_overrides[deps.require_user] = _force_user
    app.dependency_overrides[deps.current_user] = _force_user
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# 1) Zoho receive notes — human-readable, no duplicated metadata
# ---------------------------------------------------------------------------


def _seed_canary_world(session: Session) -> tuple[Receive, list[BoxReceipt]]:
    """Build a single-line case-first receive that's been finalized,
    so build_zoho_receive_notes has something to summarize."""
    user = _seed_user(session, Role.OWNER, name="Sahil Khatri")
    item = Item(
        id=42, name="Hyroxi Mit-A 12ct Variety Pack - 28mg - Display box [Packaging]",
        sku_code="SKU-X", material_code="PT-00042", zoho_item_id="z-42",
        unit="pcs", vendor="ACME", current_stock=0,
    )
    session.add(item)
    session.commit()
    po = PurchaseOrder(
        po_number="PO-00250", status=POStatus.DESIGN_APPROVED,
        created_by_id=user.id, created_at=datetime.utcnow(),
        zoho_po_id="po-z-canary",
    )
    session.add(po)
    session.commit()
    session.refresh(po)
    session.add(POLine(po_id=po.id, item_id=item.id, quantity=100))
    session.commit()
    rec = Receive(
        receive_number="R-2026-0001",
        purchase_order_id=po.id,
        delivery_date=date(2026, 6, 26),
        received_by_user_id=user.id,
        finalized_by_user_id=user.id,
        status=ReceiveStatus.FINALIZED,
        submission_id="dfe3809923ee45c89fb3e381b04cadb3",
        shipment_kind=ShipmentKind.PALLETIZED,
        notes="CANARY DRAFT — minimal vNext Stage 2 readiness check; do not finalize without operator approval.",
    )
    session.add(rec)
    session.commit()
    session.refresh(rec)
    case = ReceiveCase(receive_id=rec.id, vendor_case_number="CANARY-001", sequence=1)
    session.add(case)
    session.commit()
    session.refresh(case)
    line = ReceiveCaseLine(
        receive_case_id=case.id, purchase_order_id=po.id, item_id=item.id,
        declared_quantity=1.0, counted_quantity=None, accepted_quantity=1.0,
        unit_of_measure="pcs",
    )
    session.add(line)
    session.commit()

    box = BoxReceipt(
        packtrack_receipt_id="4a9905a3fda247c39f4d4431ebc05e8a",
        purchase_order_id=po.id, item_id=item.id,
        material_code=item.material_code, material_name=item.name[:240],
        supplier=item.vendor, supplier_lot_number=None,
        box_number="PT-4a9905a3fda247c39f4d4431ebc05e8a",
        submission_id=rec.submission_id, submission_line_index=1,
        declared_quantity=1.0, counted_quantity=None, accepted_quantity=1.0,
        unit_of_measure="pcs", confidence=Confidence.MEDIUM,
        received_by_user_id=user.id, received_at=datetime.utcnow(),
        luma_push_status=LumaPushStatus.PUSHED,
        receive_id=rec.id, receive_case_line_id=line.id,
    )
    session.add(box)
    session.commit()
    session.refresh(box)
    return rec, [box]


def test_build_zoho_receive_notes_is_human_readable(session):
    rec, boxes = _seed_canary_world(session)
    note = build_zoho_receive_notes(session, rec, boxes)

    # Human-readable header.
    assert note.splitlines()[0] == "Received via PackTrack"
    # Useful traceability for warehouse / ops.
    assert "Receive: R-2026-0001" in note
    assert "PO: PO-00250" in note
    assert "Case: CANARY-001" in note
    assert "Operator: Sahil Khatri" in note
    # Item summary with qty + unit.
    assert "Items (1):" in note
    assert "Display box" in note  # name substring
    assert "1 pcs" in note  # qty + unit
    # Operator-supplied receive notes preserved.
    assert "CANARY DRAFT" in note


def test_build_zoho_receive_notes_does_not_duplicate_machine_metadata(session):
    """Make sure PackTrack does NOT prepend ``[zoho-integration]`` or
    raw ``pack_track_*`` lines — the upstream service owns that trace."""
    rec, boxes = _seed_canary_world(session)
    note = build_zoho_receive_notes(session, rec, boxes)
    forbidden = [
        "[zoho-integration]",
        "pack_track_receipt_id=",
        "pack_track_operator_id=",
        "pack_track_workflow_session_id=",
    ]
    for needle in forbidden:
        assert needle not in note, f"PackTrack notes must not include {needle!r}"


def test_build_zoho_receive_notes_handles_multi_case_multi_line(session):
    rec, boxes = _seed_canary_world(session)
    # Add a second case + line + BoxReceipt.
    case2 = ReceiveCase(receive_id=rec.id, vendor_case_number="CANARY-002", sequence=2)
    session.add(case2)
    session.commit()
    session.refresh(case2)
    item2 = Item(
        name="Hyroxi Mit-A 12ct Variety Pack - 28mg - Label Sticker [Packaging]",
        sku_code="SKU-Y", material_code="PT-00173", zoho_item_id="z-173",
        unit="pcs", vendor="ACME", current_stock=0,
    )
    session.add(item2)
    session.commit()
    session.refresh(item2)
    box2 = BoxReceipt(
        packtrack_receipt_id="aaaabbbbccccddddaaaabbbbccccdddd",
        purchase_order_id=rec.purchase_order_id, item_id=item2.id,
        material_code=item2.material_code, material_name=item2.name[:240],
        supplier=item2.vendor, supplier_lot_number="L-42",
        box_number="PT-aaaabbbbccccddddaaaabbbbccccdddd",
        submission_id=rec.submission_id, submission_line_index=2,
        declared_quantity=3, counted_quantity=3, accepted_quantity=3,
        unit_of_measure="pcs", confidence=Confidence.HIGH,
        received_by_user_id=rec.received_by_user_id, received_at=datetime.utcnow(),
        luma_push_status=LumaPushStatus.PUSHED,
        receive_id=rec.id, receive_case_line_id=None,
    )
    session.add(box2)
    session.commit()

    note = build_zoho_receive_notes(session, rec, [*boxes, box2])
    assert "Case: CANARY-001; CANARY-002" in note
    assert "Items (2):" in note
    assert "Label Sticker" in note
    assert "Display box" in note


def test_build_zoho_receive_notes_caps_length(session):
    """Length cap protects against eating Zoho's ~2000-char field
    after the upstream trace + truncation marker land."""
    rec, boxes = _seed_canary_world(session)
    rec.notes = "X" * 5000
    session.add(rec)
    session.commit()
    note = build_zoho_receive_notes(session, rec, boxes)
    assert len(note) <= 1820  # 1800 cap + "\n[truncated]"


# ---------------------------------------------------------------------------
# 2-5) Start Receive UI entry point — flag-gated, links to GET start page
# ---------------------------------------------------------------------------


def _seed_receiving_world(session: Session, *, fully_received: bool = False) -> ZohoMirror:
    """One PO with its mirror, optionally fully-received."""
    user = _seed_user(session, Role.RECEIVING)
    item = Item(
        name="Test Item", sku_code="SKU-1", material_code="MC-1",
        zoho_item_id="z-1", unit="EACH", vendor="ACME", current_stock=0,
    )
    session.add(item)
    session.commit()
    po = PurchaseOrder(
        po_number="PO-START-1", status=POStatus.DESIGN_APPROVED,
        created_by_id=user.id, created_at=datetime.utcnow(),
        zoho_po_id="z-po-1",
    )
    session.add(po)
    session.commit()
    session.refresh(po)
    session.add(POLine(po_id=po.id, item_id=item.id, quantity=100))
    session.commit()
    mirror = ZohoMirror(
        zoho_purchaseorder_id=po.zoho_po_id,
        purchaseorder_number=po.po_number,
        vendor_name="ACME",
        line_items=[
            {
                "item_id": "z-1", "line_item_id": "li-1", "name": item.name,
                "quantity": 100,
                "quantity_received": 100 if fully_received else 0,
            }
        ],
    )
    session.add(mirror)
    session.commit()
    return mirror


def test_start_receive_button_shown_when_flag_on(session, engine, monkeypatch):
    settings.RECEIVING_VNEXT_ENABLED = True
    _seed_receiving_world(session)
    client = _client(session, engine, monkeypatch)
    r = client.get("/receive")
    assert r.status_code == 200
    assert 'data-testid="start-receive-vnext"' in r.text
    # Links to the NON-mutating GET start page with internal po_id.
    assert "/receive/v2/new?po_id=1" in r.text


def test_start_receive_button_hidden_when_flag_off(session, engine, monkeypatch):
    settings.RECEIVING_VNEXT_ENABLED = False
    _seed_receiving_world(session)
    client = _client(session, engine, monkeypatch)
    r = client.get("/receive")
    assert r.status_code == 200
    assert 'data-testid="start-receive-vnext"' not in r.text
    assert "/receive/v2/new" not in r.text


def test_start_receive_button_hidden_when_fully_received(session, engine, monkeypatch):
    settings.RECEIVING_VNEXT_ENABLED = True
    _seed_receiving_world(session, fully_received=True)
    client = _client(session, engine, monkeypatch)
    r = client.get("/receive")
    assert r.status_code == 200
    # Card still renders in the "Fully received" section…
    assert "PO-START-1" in r.text
    # …but the vNext start button is NOT shown for fully-received POs.
    assert 'data-testid="start-receive-vnext"' not in r.text


def test_start_receive_button_only_for_linked_pos(session, engine, monkeypatch):
    """If a Zoho mirror exists but no PackTrack PurchaseOrder has been
    adopted yet (po_id unknown), we cannot point at /receive/v2/new?po_id=
    cleanly. The button is hidden in that case; operator goes through the
    legacy flow first (which adopts the PO) and the button appears next time."""
    settings.RECEIVING_VNEXT_ENABLED = True
    _seed_user(session, Role.RECEIVING)
    # Add a mirror with NO matching PurchaseOrder.
    mirror = ZohoMirror(
        zoho_purchaseorder_id="z-orphan",
        purchaseorder_number="PO-ORPHAN",
        vendor_name="ACME",
        line_items=[{"item_id": "z-9", "name": "x", "quantity": 5, "quantity_received": 0}],
    )
    session.add(mirror)
    session.commit()
    client = _client(session, engine, monkeypatch)
    r = client.get("/receive")
    assert r.status_code == 200
    assert "PO-ORPHAN" in r.text
    assert 'data-testid="start-receive-vnext"' not in r.text


def test_receiving_list_still_renders_with_flag_off(session, engine, monkeypatch):
    """Regression guard: existing /receive page works regardless of flag."""
    settings.RECEIVING_VNEXT_ENABLED = False
    _seed_receiving_world(session)
    client = _client(session, engine, monkeypatch)
    r = client.get("/receive")
    assert r.status_code == 200
    assert "PO-START-1" in r.text
    # Legacy whole-card link still exists.
    assert 'href="/receive/z-po-1"' in r.text
