"""P0-1: durable double-submit dedup for the receiving flow.

The receiving POST handler requires a ``submission_id`` from the form and
short-circuits when the same id already produced BoxReceipts on this PO.
Durable backstop is the existing UNIQUE constraint
``uq_box_receipts_po_box`` — even if the in-flight check missed a row,
the second insert would violate the constraint.

These tests cover the lookup helper and the form-level integration:

  * exact prefix match returns the prior rows
  * mismatched id returns nothing
  * empty submission_id never matches
  * GET /receive/{po} embeds a submission_id in the rendered HTML
  * two GETs produce different submission_ids
  * POST without submission_id returns 400
  * Two POSTs with the same submission_id only create rows once and
    only call Luma once.
  * Two POSTs with different submission_ids both go through.
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("PACKTRACK_SECRET_KEY", "test-secret")

from datetime import datetime

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

from packtrack.models import (
    BoxReceipt,
    Confidence,
    Item,
    LumaPushStatus,
    Role,
    User,
    ZohoMirror,
)

# ---------------------------------------------------------------------------
# Helper-level coverage (pure SQL prefix lookup).
# ---------------------------------------------------------------------------


@pytest.fixture(name="engine")
def engine_fixture():
    """A single in-memory SQLite engine. Patched into packtrack.main and
    packtrack.db so the http middleware (which opens its own Session against
    the module-level engine) hits the same DB as the test session.

    StaticPool keeps all connections on the same in-memory DB — without it,
    each connection opens a fresh empty SQLite file and any SQLAlchemy
    attribute refresh after commit fails with 'no such table'.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    for name in (
        "users", "items", "zoho_mirror", "purchase_orders", "po_lines",
        "po_events", "box_receipts", "app_settings",
    ):
        Item.metadata.tables[name].create(bind=engine)
    return engine


@pytest.fixture(name="session")
def session_fixture(engine):
    with Session(engine) as session:
        yield session


def _seed_box(session: Session, *, po_id: int, item_id: int, box_number: str, receipt_id: str) -> BoxReceipt:
    box = BoxReceipt(
        packtrack_receipt_id=receipt_id,
        purchase_order_id=po_id,
        item_id=item_id,
        material_code="PT-1",
        material_name="X",
        supplier="Acme",
        box_number=box_number,
        declared_quantity=10.0,
        counted_quantity=10.0,
        accepted_quantity=10.0,
        unit_of_measure="each",
        confidence=Confidence.HIGH,
        received_by_user_id=1,
        received_at=datetime.utcnow(),
        luma_push_status=LumaPushStatus.PUSHED,
        luma_pushed_at=datetime.utcnow(),
    )
    session.add(box)
    session.commit()
    return box


def test_helper_finds_boxes_for_matching_submission(session: Session):
    from packtrack.routes.receiving import (
        _existing_boxes_for_submission,
        _submission_box_prefix,
    )
    sub = "abcdef1234567890" * 2  # 32 hex chars
    box_number = f"{_submission_box_prefix(sub)}1"
    _seed_box(session, po_id=1, item_id=10, box_number=box_number, receipt_id="r-1")
    out = _existing_boxes_for_submission(session, 1, sub)
    assert len(out) == 1
    assert out[0].box_number == box_number


def test_helper_returns_empty_for_unmatched_submission(session: Session):
    from packtrack.routes.receiving import (
        _existing_boxes_for_submission,
        _submission_box_prefix,
    )
    sub_old = "a" * 32
    sub_new = "b" * 32
    _seed_box(session, po_id=1, item_id=10,
              box_number=f"{_submission_box_prefix(sub_old)}1", receipt_id="r-1")
    assert _existing_boxes_for_submission(session, 1, sub_new) == []
    # And limited by po_id:
    assert _existing_boxes_for_submission(session, 999, sub_old) == []


def test_helper_returns_empty_for_blank_submission(session: Session):
    from packtrack.routes.receiving import _existing_boxes_for_submission
    assert _existing_boxes_for_submission(session, 1, "") == []


# ---------------------------------------------------------------------------
# Route-level coverage with FastAPI TestClient + SQLite + stubbed externals.
# ---------------------------------------------------------------------------


def _client(session: Session, engine, monkeypatch: pytest.MonkeyPatch):  # -> TestClient
    """Spin up the app against the in-memory SQLite session and stub every
    external call the receiving flow makes (Luma + Zoho integration service +
    notifications). Keeps tests pure."""
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"

    from fastapi.testclient import TestClient

    import packtrack.db
    import packtrack.main
    from packtrack import deps
    from packtrack.db import get_session
    from packtrack.main import app
    from packtrack.services import receiving as recv_svc
    # The /healthz handler + the vendor_scope middleware open Sessions
    # directly against the module-level engine. Point both at our test DB.
    monkeypatch.setattr(packtrack.db, "engine", engine)
    monkeypatch.setattr(packtrack.main, "engine", engine)

    # Stub external calls so we can assert how many times they fire.
    calls = {"luma_push": 0, "luma_register": 0, "zoho_submit": 0}

    def _no_push(*_a, **_kw):
        calls["luma_push"] += 1
        return (True, None, {"ok": True})

    def _no_register(*_a, **_kw):
        calls["luma_register"] += 1
        from packtrack.services.receiving import (
            LumaRegistrationOutcome,
            LumaRegistrationResult,
        )
        return LumaRegistrationResult(outcome=LumaRegistrationOutcome.ALREADY_MAPPED)

    def _no_register_material(*_a, **_kw):
        calls["luma_register"] += 1
        return True, None

    def _no_zoho(*_a, **_kw):
        calls["zoho_submit"] += 1
        return []

    monkeypatch.setattr(recv_svc, "push_luma_receipt", _no_push)
    monkeypatch.setattr(recv_svc, "register_item_with_luma", _no_register)
    monkeypatch.setattr(recv_svc, "register_material_with_luma", _no_register_material)
    monkeypatch.setattr(recv_svc, "submit_zoho_receives", _no_zoho)
    # Patch the imports already resolved inside the route module too.
    from packtrack.routes import receiving as recv_route
    monkeypatch.setattr(recv_route, "push_luma_receipt", _no_push)
    monkeypatch.setattr(recv_route, "register_material_with_luma", _no_register_material)
    monkeypatch.setattr(recv_route, "submit_zoho_receives", _no_zoho)

    # Stub Luma env so the route enters the Luma-push branch.
    from packtrack.config import settings
    monkeypatch.setattr(settings, "LUMA_RECEIPT_WEBHOOK_URL", "http://luma.test/r")
    monkeypatch.setattr(settings, "LUMA_PACKTRACK_SECRET", "x")

    app.dependency_overrides[get_session] = lambda: session

    # Force the require_user / current_user dependencies to return our seed user.
    def _force_user():
        return session.exec(select(User).where(User.role == Role.OWNER)).first()
    app.dependency_overrides[deps.require_user] = _force_user
    app.dependency_overrides[deps.current_user] = _force_user

    return TestClient(app, raise_server_exceptions=False), calls


def _seed_world(session: Session) -> tuple[ZohoMirror, Item, User]:
    user = User(
        id=1, email="o@example.com", name="Owner", role=Role.OWNER,
        password_hash="x", is_active=True,
    )
    session.add(user)
    item = Item(
        id=42, zoho_item_id="z-1", name="Card", unit="each",
        current_stock=0.0, material_code="PT-1",
    )
    session.add(item)
    mirror = ZohoMirror(
        zoho_purchaseorder_id="po-z-1",
        purchaseorder_number="PO-001",
        line_items=[{
            "name": "Card",
            "quantity": 100.0,
            "quantity_received": 0.0,
            "item_id": "z-1",
            "line_item_id": "li-1",
        }],
    )
    session.add(mirror)
    session.commit()
    return mirror, item, user


def test_get_form_embeds_submission_id(session: Session, engine, monkeypatch: pytest.MonkeyPatch):
    _seed_world(session)
    client, _ = _client(session, engine, monkeypatch)
    r = client.get("/receive/po-z-1")
    assert r.status_code == 200
    assert 'name="submission_id"' in r.text


def test_get_form_returns_different_submission_id_per_render(
    session: Session, engine, monkeypatch: pytest.MonkeyPatch,
):
    _seed_world(session)
    client, _ = _client(session, engine, monkeypatch)

    import re

    def _id_in(html: str) -> str:
        m = re.search(r'name="submission_id" value="([0-9a-f]+)"', html)
        assert m, "submission_id input not found"
        return m.group(1)

    a = _id_in(client.get("/receive/po-z-1").text)
    b = _id_in(client.get("/receive/po-z-1").text)
    assert a != b


def test_post_without_submission_id_returns_400(
    session: Session, engine, monkeypatch: pytest.MonkeyPatch,
):
    _seed_world(session)
    client, _ = _client(session, engine, monkeypatch)
    r = client.post(
        "/receive/po-z-1",
        data={
            "zoho_item_id[]": "z-1",
            "zoho_line_item_id[]": "li-1",
            "qty_z-1": "50",
        },
    )
    assert r.status_code == 400


def test_double_post_with_same_submission_id_only_creates_once(
    session: Session, engine, monkeypatch: pytest.MonkeyPatch,
):
    _seed_world(session)
    client, calls = _client(session, engine, monkeypatch)

    sub = "deadbeef" * 4  # 32 hex chars
    payload = {
        "submission_id": sub,
        "zoho_item_id[]": "z-1",
        "zoho_line_item_id[]": "li-1",
        "qty_z-1": "100",
    }

    r1 = client.post("/receive/po-z-1", data=payload)
    assert r1.status_code == 200

    boxes_after_first = session.exec(select(BoxReceipt)).all()
    assert len(boxes_after_first) == 1
    luma_push_after_first = calls["luma_push"]
    zoho_submit_after_first = calls["zoho_submit"]

    r2 = client.post("/receive/po-z-1", data=payload)
    assert r2.status_code == 200
    assert "already processed" in r2.text.lower()

    boxes_after_second = session.exec(select(BoxReceipt)).all()
    assert len(boxes_after_second) == 1, "second submit must not create more rows"
    assert calls["luma_push"] == luma_push_after_first, "must not push to Luma again"
    assert calls["zoho_submit"] == zoho_submit_after_first, "must not call Zoho again"


def test_distinct_submission_ids_both_create_rows(
    session: Session, engine, monkeypatch: pytest.MonkeyPatch,
):
    _seed_world(session)
    client, _ = _client(session, engine, monkeypatch)

    base = {
        "zoho_item_id[]": "z-1",
        "zoho_line_item_id[]": "li-1",
        "qty_z-1": "20",
    }
    r1 = client.post("/receive/po-z-1", data={**base, "submission_id": "1" * 32})
    r2 = client.post("/receive/po-z-1", data={**base, "submission_id": "2" * 32})

    assert r1.status_code == 200 and r2.status_code == 200
    boxes = session.exec(select(BoxReceipt)).all()
    assert len(boxes) == 2
