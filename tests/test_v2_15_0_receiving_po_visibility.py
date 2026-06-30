"""v2.15.0 — Receiving PO visibility diagnostic.

Covers:

A. Diagnostic page requires auth (RECEIVING or OWNER).
B. Non-receiving roles get 403.
C. Visible PO with linked PT + remaining qty shows
   appears_on_receiving=true + start_receive_available=true.
D. Fully-received PO shows hidden reason + no Start receive button.
E. Mirror without linked PT shows the not-linked reason.
F. vnext flag off → start_receive_available=false with flag-off reason
   even when a linked PT exists.
G. Search query filters by PO number, vendor, zoho id, item name.
H. Missing material code count surfaces but doesn't block.
I. GET is non-mutating (no Receive rows created).
J. /receive shows Find-a-PO link.
K. /receive/v2/new?po_id=... remains non-mutating.
L. Legacy /receive/{zoho_po_id} still routes (search-helper for
   the static-vs-dynamic resolution order).
M. Diagnostic service imports no Zoho/OAuth/HTTP symbol.
N. Diagnostic page never mentions Luma calls / never imports a
   Luma client.
O. Empty mirror table renders an empty-state without crashing.
P. counts dict reflects bucket distribution accurately.
"""
from __future__ import annotations

import os
from datetime import datetime

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
    Role,
    User,
    ZohoMirror,
)
from packtrack.services.receiving_po_visibility import (
    build_visibility_report,
    diagnose_mirror,
)


@pytest.fixture(name="engine")
def engine_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
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
def _vnext_on():
    original = settings.RECEIVING_VNEXT_ENABLED
    settings.RECEIVING_VNEXT_ENABLED = True
    yield
    settings.RECEIVING_VNEXT_ENABLED = original


def _seed_user(session, *, role=Role.OWNER, user_id=1, name="Owner"):
    u = User(
        id=user_id, email=f"{role.value}-{user_id}@example.com", name=name,
        role=role, password_hash="x", is_active=True,
    )
    session.add(u)
    session.commit()
    return u


def _seed_item(session, *, name="Mailer", material_code="MC-1"):
    it = Item(
        name=name, sku_code=f"SKU-{name}", material_code=material_code,
        zoho_item_id=f"z-{name.lower().replace(' ', '-')}", unit="pcs",
        vendor="ACME", current_stock=0,
    )
    session.add(it)
    session.commit()
    session.refresh(it)
    return it


def _seed_po(session, *, items, zoho_id="po-z-1", po_number="PO-001"):
    owner = session.exec(select(User)).first() or _seed_user(session)
    po = PurchaseOrder(
        po_number=po_number, status=POStatus.DESIGN_APPROVED,
        created_by_id=owner.id, created_at=datetime.utcnow(),
        zoho_po_id=zoho_id,
    )
    session.add(po)
    session.commit()
    session.refresh(po)
    for it in items:
        session.add(POLine(po_id=po.id, item_id=it.id, quantity=100))
    session.commit()
    return po


def _seed_mirror(
    session, *, zoho_id="po-z-1", po_number="PO-001", vendor="ACME",
    line_items=None,
):
    """Default mirror: 2 items at 100 each, 0 received → pending."""
    if line_items is None:
        line_items = [
            {"item_id": "z-mailer", "line_item_id": "li-1",
             "name": "Mailer", "quantity": 100, "quantity_received": 0},
            {"item_id": "z-sticker", "line_item_id": "li-2",
             "name": "Sticker pack", "quantity": 100, "quantity_received": 0},
        ]
    m = ZohoMirror(
        zoho_purchaseorder_id=zoho_id,
        purchaseorder_number=po_number,
        vendor_name=vendor,
        status="open",
        date="2026-06-30",
        line_items=line_items,
    )
    session.add(m)
    session.commit()
    session.refresh(m)
    return m


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
# A/B — auth
# ---------------------------------------------------------------------------


def test_owner_can_open_find(session, engine, monkeypatch):
    _seed_user(session)
    client = _client(session, engine, monkeypatch)
    resp = client.get("/receive/find")
    assert resp.status_code == 200
    assert "receive-find-form" in resp.text


def test_receiving_role_can_open_find(session, engine, monkeypatch):
    receiver = _seed_user(session, role=Role.RECEIVING, name="R")
    client = _client(session, engine, monkeypatch, user=receiver)
    resp = client.get("/receive/find")
    assert resp.status_code == 200


def test_design_role_forbidden(session, engine, monkeypatch):
    designer = _seed_user(session, role=Role.DESIGN, name="D")
    client = _client(session, engine, monkeypatch, user=designer)
    resp = client.get("/receive/find")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# C — visible PO with Start receive available
# ---------------------------------------------------------------------------


def test_pending_linked_po_shows_actionable(session):
    _seed_user(session)
    items = [_seed_item(session, name="Mailer"), _seed_item(session, name="Sticker pack")]
    po = _seed_po(session, items=items)
    _seed_mirror(session)
    report = build_visibility_report(session)
    assert len(report.diagnostics) == 1
    d = report.diagnostics[0]
    assert d.appears_on_receive is True
    assert d.start_receive_available is True
    assert d.bucket == "pending"
    assert d.pt_po_id == po.id
    assert d.start_receive_url == f"/receive/v2/new?po_id={po.id}"


# ---------------------------------------------------------------------------
# D — fully received hides Start receive
# ---------------------------------------------------------------------------


def test_fully_received_shows_clear_reason(session):
    _seed_user(session)
    items = [_seed_item(session, name="Mailer")]
    _seed_po(session, items=items)
    _seed_mirror(session, line_items=[
        {"item_id": "z-mailer", "line_item_id": "li-1",
         "name": "Mailer", "quantity": 100, "quantity_received": 100},
    ])
    report = build_visibility_report(session)
    d = report.diagnostics[0]
    assert d.bucket == "fully_received"
    assert d.start_receive_available is False
    assert d.start_receive_url is None
    assert "fully received" in (d.start_receive_reason or "").lower()


# ---------------------------------------------------------------------------
# E — unlinked mirror surfaces the not-linked reason
# ---------------------------------------------------------------------------


def test_unlinked_mirror_shows_not_linked_reason(session):
    _seed_user(session)
    _seed_mirror(session, zoho_id="po-z-unlinked", po_number="PO-UNLINKED")
    report = build_visibility_report(session)
    d = report.diagnostics[0]
    assert d.pt_po_id is None
    assert d.start_receive_available is False
    reason = (d.start_receive_reason or "").lower()
    assert "linked" in reason
    assert "packtrack" in reason  # service phrasing is "no PackTrack PurchaseOrder ... linked yet"


# ---------------------------------------------------------------------------
# F — vnext flag off
# ---------------------------------------------------------------------------


def test_vnext_off_blocks_start_even_when_linked(session):
    _seed_user(session)
    items = [_seed_item(session)]
    _seed_po(session, items=items)
    _seed_mirror(session)
    settings.RECEIVING_VNEXT_ENABLED = False
    try:
        report = build_visibility_report(session)
    finally:
        settings.RECEIVING_VNEXT_ENABLED = True
    d = report.diagnostics[0]
    assert d.start_receive_available is False
    assert "vnext" in (d.start_receive_reason or "").lower() or \
           "flag" in (d.start_receive_reason or "").lower()


# ---------------------------------------------------------------------------
# G — search query
# ---------------------------------------------------------------------------


def test_search_by_po_number(session):
    _seed_user(session)
    _seed_mirror(session, zoho_id="po-z-1", po_number="PO-AAA")
    _seed_mirror(session, zoho_id="po-z-2", po_number="PO-BBB")
    report = build_visibility_report(session, query="AAA")
    assert len(report.diagnostics) == 1
    assert report.diagnostics[0].purchaseorder_number == "PO-AAA"


def test_search_by_vendor(session):
    _seed_user(session)
    _seed_mirror(session, zoho_id="po-z-1", po_number="PO-1", vendor="ACME Corp")
    _seed_mirror(session, zoho_id="po-z-2", po_number="PO-2", vendor="Beta LLC")
    report = build_visibility_report(session, query="beta")
    assert len(report.diagnostics) == 1
    assert report.diagnostics[0].vendor_name == "Beta LLC"


def test_search_by_item_name(session):
    _seed_user(session)
    _seed_mirror(session, zoho_id="po-z-1", po_number="PO-1", line_items=[
        {"item_id": "z-a", "line_item_id": "li-1",
         "name": "Custom 8oz Bottle", "quantity": 50, "quantity_received": 0},
    ])
    _seed_mirror(session, zoho_id="po-z-2", po_number="PO-2", line_items=[
        {"item_id": "z-b", "line_item_id": "li-2",
         "name": "Sticker Pack", "quantity": 100, "quantity_received": 0},
    ])
    report = build_visibility_report(session, query="bottle")
    assert len(report.diagnostics) == 1
    assert report.diagnostics[0].purchaseorder_number == "PO-1"


def test_search_by_zoho_id(session):
    _seed_user(session)
    _seed_mirror(session, zoho_id="po-z-12345", po_number="PO-X")
    _seed_mirror(session, zoho_id="po-z-99999", po_number="PO-Y")
    report = build_visibility_report(session, query="12345")
    assert len(report.diagnostics) == 1


def test_search_with_no_match_returns_empty(session):
    _seed_user(session)
    _seed_mirror(session, zoho_id="po-z-1", po_number="PO-1")
    report = build_visibility_report(session, query="nothing-matches-xyz")
    assert report.diagnostics == []


# ---------------------------------------------------------------------------
# H — missing material code count
# ---------------------------------------------------------------------------


def test_missing_material_code_surfaces(session):
    _seed_user(session)
    items = [
        _seed_item(session, name="WithCode", material_code="MC-1"),
        _seed_item(session, name="NoCode1", material_code=""),
        _seed_item(session, name="NoCode2", material_code=""),
    ]
    _seed_po(session, items=items)
    _seed_mirror(session)
    report = build_visibility_report(session)
    d = report.diagnostics[0]
    assert d.missing_material_code_count == 2
    assert d.start_receive_available is True  # readiness is informational


# ---------------------------------------------------------------------------
# I — GET is non-mutating
# ---------------------------------------------------------------------------


def test_get_does_not_create_receive_rows(session, engine, monkeypatch):
    _seed_user(session)
    items = [_seed_item(session)]
    _seed_po(session, items=items)
    _seed_mirror(session)
    client = _client(session, engine, monkeypatch)
    client.get("/receive/find")
    client.get("/receive/find?q=PO")
    assert session.exec(select(Receive)).all() == []


def test_get_does_not_create_po_or_mirror_rows(session, engine, monkeypatch):
    _seed_user(session)
    items = [_seed_item(session)]
    _seed_po(session, items=items)
    _seed_mirror(session)
    client = _client(session, engine, monkeypatch)
    pre_po_count = len(session.exec(select(PurchaseOrder)).all())
    pre_mirror_count = len(session.exec(select(ZohoMirror)).all())
    client.get("/receive/find?q=PO")
    assert len(session.exec(select(PurchaseOrder)).all()) == pre_po_count
    assert len(session.exec(select(ZohoMirror)).all()) == pre_mirror_count


# ---------------------------------------------------------------------------
# J — /receive shows Find-a-PO link
# ---------------------------------------------------------------------------


def test_receive_page_shows_find_link(session, engine, monkeypatch):
    _seed_user(session, role=Role.RECEIVING)
    _seed_mirror(session)
    client = _client(session, engine, monkeypatch)
    page = client.get("/receive")
    assert page.status_code == 200
    assert "receiving-find-link" in page.text
    assert "/receive/find" in page.text


# ---------------------------------------------------------------------------
# K — /receive/v2/new?po_id is non-mutating
# ---------------------------------------------------------------------------


def test_vnext_new_get_is_non_mutating(session, engine, monkeypatch):
    """v2.5.0 spec: GET /receive/v2/new is the confirmation page; only
    POST creates a Receive. Regression check after the v2.15.0 link
    points operators there from the find page."""
    _seed_user(session)
    items = [_seed_item(session)]
    po = _seed_po(session, items=items)
    client = _client(session, engine, monkeypatch)
    resp = client.get(f"/receive/v2/new?po_id={po.id}")
    assert resp.status_code == 200
    assert session.exec(select(Receive)).all() == []


# ---------------------------------------------------------------------------
# L — legacy /receive/{zoho_po_id} still routes
# ---------------------------------------------------------------------------


def test_legacy_receive_route_still_resolves(session, engine, monkeypatch):
    """Static /receive/find must NOT shadow the dynamic /receive/{id}.
    A request for a non-existent zoho_po_id should NOT match /receive/find
    (and produce a Find-page response); it should land on the legacy
    route. Since the legacy route does an adopt, asking for an unknown
    id returns either 404 or a render — both fine; what matters is it's
    NOT the Find page."""
    _seed_user(session, role=Role.RECEIVING)
    client = _client(session, engine, monkeypatch)
    resp = client.get("/receive/totally-not-find-or-anything-real")
    # Should not contain the Find-page form
    assert "receive-find-form" not in resp.text


# ---------------------------------------------------------------------------
# M — source-level guard
# ---------------------------------------------------------------------------


def test_diagnostic_service_imports_no_zoho_or_http_symbol():
    import packtrack.services.receiving_po_visibility as mod
    with open(mod.__file__) as f:
        src = f.read()
    lowered = src.lower()
    forbidden = [
        "import httpx", "import requests",
        "oauth", "access_token", "refresh_token",
        "zoho.com", "zohoapis.com",
        "import luma", "luma_url", "luma_secret",
    ]
    for needle in forbidden:
        assert needle not in lowered, f"Forbidden: {needle!r}"


# ---------------------------------------------------------------------------
# N — diagnostic route imports no Luma symbol
# ---------------------------------------------------------------------------


def test_diagnostic_route_imports_no_luma_symbol():
    import packtrack.routes.receiving_diagnostics as mod
    with open(mod.__file__) as f:
        src = f.read()
    assert "luma" not in src.lower()


# ---------------------------------------------------------------------------
# O — empty mirror table
# ---------------------------------------------------------------------------


def test_empty_mirror_renders_empty_state(session, engine, monkeypatch):
    _seed_user(session)
    client = _client(session, engine, monkeypatch)
    resp = client.get("/receive/find")
    assert resp.status_code == 200
    # Either "No mirrors yet" or "No mirrors match" should appear.
    assert "No mirrors" in resp.text


# ---------------------------------------------------------------------------
# P — counts accuracy
# ---------------------------------------------------------------------------


def test_counts_dict_matches_bucket_distribution(session):
    _seed_user(session)
    items = [_seed_item(session, name=n) for n in ("A", "B", "C")]
    # PO 1: pending (0 received)
    _seed_po(session, items=[items[0]], zoho_id="po-z-1", po_number="PO-1")
    _seed_mirror(session, zoho_id="po-z-1", po_number="PO-1")
    # PO 2: partial
    _seed_po(session, items=[items[1]], zoho_id="po-z-2", po_number="PO-2")
    _seed_mirror(session, zoho_id="po-z-2", po_number="PO-2", line_items=[
        {"item_id": "z-b", "line_item_id": "li", "name": "B",
         "quantity": 100, "quantity_received": 40},
    ])
    # PO 3: fully received
    _seed_po(session, items=[items[2]], zoho_id="po-z-3", po_number="PO-3")
    _seed_mirror(session, zoho_id="po-z-3", po_number="PO-3", line_items=[
        {"item_id": "z-c", "line_item_id": "li", "name": "C",
         "quantity": 100, "quantity_received": 100},
    ])
    report = build_visibility_report(session)
    assert report.counts["total_mirrors"] == 3
    assert report.counts["pending"] == 1
    assert report.counts["partial"] == 1
    assert report.counts["fully_received"] == 1
    assert report.counts["actionable_start_receive"] == 2  # pending + partial


# ---------------------------------------------------------------------------
# Unit: diagnose_mirror handles None linked_po and empty line_items
# ---------------------------------------------------------------------------


def test_diagnose_mirror_handles_empty_line_items(session):
    _seed_user(session)
    m = _seed_mirror(session, zoho_id="po-z-empty", po_number="PO-E", line_items=[])
    d = diagnose_mirror(session, m, linked_po=None, vnext_enabled=True)
    assert d.bucket == "no_line_items"
    assert d.line_count == 0
    assert d.start_receive_available is False
