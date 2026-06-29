"""v2.7.4 Receiving vNext polish — focused tests.

Covers:

A. Canary/test banner now renders on index + review (previously only on
   result.html).
B. `po_item_choices` label includes remaining quantity + unit, and
   prefers Zoho mirror `quantity_received` over POLine.received_quantity.
C. /receive vendor fallback: mirror -> linked PO's item.vendor ->
   "Vendor unknown".
D. POLine docstring documents the vNext semantics.
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
from packtrack.services.receiving_v2 import (
    is_test_receive,
    po_item_choices,
)

# Aliased: pytest would otherwise treat the imported helper as a test
# function because it starts with ``test_``.
from packtrack.services.receiving_v2 import test_receive_marker_text as _marker_text


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


def _seed_user(session, role=Role.OWNER, name="Owner"):
    u = User(
        id=1, email=f"{role.value}@example.com", name=name,
        role=role, password_hash="x", is_active=True,
    )
    session.add(u)
    session.commit()
    return u


def _seed_po(session, *, with_zoho=True, vendor=None, with_mirror=True,
             mirror_vendor=None, line_receiveds=None):
    """Returns (po, items[], mirror|None)."""
    owner = session.exec(select(User)).first() or _seed_user(session, Role.OWNER)
    item_a = Item(
        name="Item A", sku_code="SKU-A", material_code="MC-A",
        zoho_item_id="z-a", unit="pcs", vendor=vendor, current_stock=0,
    )
    item_b = Item(
        name="Item B", sku_code="SKU-B", material_code="MC-B",
        zoho_item_id="z-b", unit="pcs", vendor=vendor, current_stock=0,
    )
    session.add_all([item_a, item_b])
    session.commit()
    po = PurchaseOrder(
        po_number="PO-POLISH-1", status=POStatus.DESIGN_APPROVED,
        created_by_id=owner.id, created_at=datetime.utcnow(),
        zoho_po_id="po-z-polish-1" if with_zoho else None,
    )
    session.add(po)
    session.commit()
    session.refresh(po)
    session.add_all([
        POLine(po_id=po.id, item_id=item_a.id, quantity=100),
        POLine(po_id=po.id, item_id=item_b.id, quantity=50),
    ])
    session.commit()

    mirror = None
    if with_mirror and with_zoho:
        recv_a, recv_b = line_receiveds or (0, 0)
        mirror = ZohoMirror(
            zoho_purchaseorder_id=po.zoho_po_id, purchaseorder_number=po.po_number,
            vendor_name=mirror_vendor,
            line_items=[
                {"item_id": "z-a", "line_item_id": "li-a", "name": item_a.name,
                 "quantity": 100, "quantity_received": recv_a},
                {"item_id": "z-b", "line_item_id": "li-b", "name": item_b.name,
                 "quantity": 50, "quantity_received": recv_b},
            ],
        )
        session.add(mirror)
        session.commit()
    return po, [item_a, item_b], mirror


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


# ---------------------------------------------------------------------------
# A. Canary/test banner on index + review
# ---------------------------------------------------------------------------


def _seed_receive(session, po, *, notes=None):
    user = session.exec(select(User)).first() or _seed_user(session, Role.OWNER)
    rec = Receive(
        receive_number="R-2026-POL", purchase_order_id=po.id,
        delivery_date=date(2026, 6, 26), received_by_user_id=user.id,
        status=ReceiveStatus.PUSHED_OK, submission_id="cafe" * 8,
        shipment_kind=ShipmentKind.PALLETIZED, notes=notes,
    )
    session.add(rec)
    session.commit()
    session.refresh(rec)
    return rec


def test_is_test_receive_helper():
    """Helper detects the v2.7.2 marker prefix anywhere in notes."""
    fake = Receive(
        receive_number="x", purchase_order_id=None,
        delivery_date=date(2026, 6, 26), received_by_user_id=1,
        notes="some operator note\n[Marked as TEST/CANARY by X at 2026-06-26T20:10:38Z] — reason",
    )
    assert is_test_receive(fake) is True

    fake2 = Receive(
        receive_number="y", purchase_order_id=None,
        delivery_date=date(2026, 6, 26), received_by_user_id=1,
        notes="nothing special",
    )
    assert is_test_receive(fake2) is False
    assert is_test_receive(None) is False


def test_marker_text_helper_returns_just_the_marker_line():
    fake = Receive(
        receive_number="x", purchase_order_id=None,
        delivery_date=date(2026, 6, 26), received_by_user_id=1,
        notes="ORIGINAL OP NOTE\n\n[Marked as TEST/CANARY by Sahil at 2026-06-26T20:10:38Z] — vNext canary",
    )
    text = _marker_text(fake)
    assert text is not None
    assert text.startswith("[Marked as TEST/CANARY by Sahil")
    assert "vNext canary" in text


def test_index_page_shows_test_banner_when_marked(session, engine, monkeypatch):
    po, _, _ = _seed_po(session, with_mirror=True)
    rec = _seed_receive(
        session, po,
        notes="orig\n\n[Marked as TEST/CANARY by Owner at 2026-06-26T20:10:38Z] — canary",
    )
    client = _client(session, engine, monkeypatch)
    page = client.get(f"/receive/v2/{rec.id}")
    assert page.status_code == 200
    assert "Test / canary receive" in page.text
    assert "Marked as TEST/CANARY" in page.text  # marker line shown


def test_index_page_no_banner_when_unmarked(session, engine, monkeypatch):
    po, _, _ = _seed_po(session, with_mirror=True)
    rec = _seed_receive(session, po, notes="normal receive")
    client = _client(session, engine, monkeypatch)
    page = client.get(f"/receive/v2/{rec.id}")
    assert page.status_code == 200
    assert "Test / canary receive" not in page.text


def test_review_page_shows_test_banner_when_marked(session, engine, monkeypatch):
    po, _, _ = _seed_po(session, with_mirror=True)
    rec = _seed_receive(
        session, po,
        notes="orig\n\n[Marked as TEST/CANARY by Owner at 2026-06-26T20:10:38Z] — canary",
    )
    client = _client(session, engine, monkeypatch)
    page = client.get(f"/receive/v2/{rec.id}/review")
    assert page.status_code == 200
    assert "Test / canary receive" in page.text


# ---------------------------------------------------------------------------
# B. po_item_choices remaining-qty label
# ---------------------------------------------------------------------------


def test_po_item_choices_shows_remaining_with_unit_from_mirror(session):
    """Mirror's quantity_received is the source of truth for remaining."""
    po, items, _ = _seed_po(session, with_mirror=True, line_receiveds=(40, 10))
    choices = po_item_choices(session, po.id)
    labels = {c.item_id: c.label for c in choices}
    # Item A: ordered 100, mirror received 40, remaining 60 pcs
    assert "60 pcs remaining" in labels[items[0].id]
    assert items[0].name in labels[items[0].id]
    assert items[0].material_code in labels[items[0].id]
    # Item B: ordered 50, mirror received 10, remaining 40 pcs
    assert "40 pcs remaining" in labels[items[1].id]


def test_po_item_choices_falls_back_to_po_line_when_no_mirror(session):
    po, items, _ = _seed_po(session, with_zoho=False, with_mirror=False)
    # Bump POLine.received_quantity to simulate legacy receipt.
    po_lines = session.exec(select(POLine).where(POLine.po_id == po.id)).all()
    po_lines[0].received_quantity = 25
    session.add(po_lines[0])
    session.commit()
    choices = po_item_choices(session, po.id)
    labels = {c.item_id: c.label for c in choices}
    # Item A: ordered 100, POLine.received_quantity 25, remaining 75
    assert "75 pcs remaining" in labels[items[0].id]
    # Item B: ordered 50, POLine.received_quantity 0, remaining 50
    assert "50 pcs remaining" in labels[items[1].id]


def test_po_item_choices_shows_ordered_when_no_receive_info(session):
    """With no mirror AND POLine.received_quantity at default 0, the
    label shows "100 pcs remaining" (not "ordered") because
    received_quantity is a real number (0), not None."""
    po, items, _ = _seed_po(session, with_zoho=False, with_mirror=False)
    choices = po_item_choices(session, po.id)
    labels = {c.item_id: c.label for c in choices}
    assert "100 pcs remaining" in labels[items[0].id]


def test_po_item_choices_handles_zero_ordered_quantity(session):
    """Zero ordered → 'remaining unknown' fallback."""
    owner = _seed_user(session, Role.OWNER)
    item = Item(
        name="Zero Qty Item", sku_code="SKU-Z", material_code="MC-Z",
        zoho_item_id="z-z", unit="pcs", current_stock=0,
    )
    session.add(item)
    session.commit()
    po = PurchaseOrder(
        po_number="PO-ZQ", status=POStatus.DESIGN_APPROVED,
        created_by_id=owner.id, created_at=datetime.utcnow(),
    )
    session.add(po)
    session.commit()
    session.refresh(po)
    session.add(POLine(po_id=po.id, item_id=item.id, quantity=0))
    session.commit()
    choices = po_item_choices(session, po.id)
    assert len(choices) == 1
    assert "remaining unknown" in choices[0].label


# ---------------------------------------------------------------------------
# C. /receive vendor fallback
# ---------------------------------------------------------------------------


def test_receive_page_uses_mirror_vendor_name(session, engine, monkeypatch):
    _po, _, _ = _seed_po(session, vendor="Helen",
                         mirror_vendor="Helen's Packaging Co.")
    client = _client(session, engine, monkeypatch)
    r = client.get("/receive")
    assert r.status_code == 200
    assert "Helen&#39;s Packaging Co." in r.text or "Helen's Packaging Co." in r.text


def test_receive_page_falls_back_to_item_vendor_when_mirror_vendor_blank(
    session, engine, monkeypatch,
):
    _po, _, _ = _seed_po(session, vendor="ACME Inc.",
                         mirror_vendor=None)
    client = _client(session, engine, monkeypatch)
    r = client.get("/receive")
    assert r.status_code == 200
    assert "ACME Inc." in r.text


def test_receive_page_shows_vendor_unknown_when_no_source_has_vendor(
    session, engine, monkeypatch,
):
    _po, _, _ = _seed_po(session, vendor=None, mirror_vendor=None)
    client = _client(session, engine, monkeypatch)
    r = client.get("/receive")
    assert r.status_code == 200
    assert "Vendor unknown" in r.text
    # And the bare em-dash should be gone for this PO.
    assert "<span>—</span>" not in r.text


def test_receive_page_renders_legacy_when_flag_off(session, engine, monkeypatch):
    """Flag off shouldn't break the receiving list."""
    settings.RECEIVING_VNEXT_ENABLED = False
    _seed_po(session, vendor="ACME")
    # Demote the seeded OWNER to RECEIVING so the role check on /receive
    # (which allows OWNER + RECEIVING) still passes.
    only = session.exec(select(User)).first()
    only.role = Role.RECEIVING
    session.add(only)
    session.commit()
    client = _client(session, engine, monkeypatch, user=only)
    r = client.get("/receive")
    assert r.status_code == 200
    assert "ACME" in r.text


# ---------------------------------------------------------------------------
# D. Documentation note (model-level docstring)
# ---------------------------------------------------------------------------


def test_poline_docstring_documents_vnext_semantics():
    """Lightweight check that the docstring carries the warning so a
    grep / future PR reviewer can find it."""
    from packtrack.models import POLine
    doc = POLine.__doc__ or ""
    assert "Receiving vNext" in doc
    assert "received_quantity" in doc
    assert "mirror" in doc.lower() or "BoxReceipt" in doc
