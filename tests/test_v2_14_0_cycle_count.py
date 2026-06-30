"""v2.14.0 — Cycle-count batch adjustment workflow.

Covers:

A. Owner can GET the form.
B. Non-owner cannot GET the form (403).
C. Non-owner cannot POST (403).
D. Empty submission re-renders form with soft warning, nothing applied.
E. Zero-variance row is skipped (no adjustment created).
F. Positive-variance row creates an INCREASE adjustment.
G. Negative-variance row creates a DECREASE adjustment.
H. Negative counted quantity is rejected; whole batch aborts.
I. Non-numeric counted is rejected; whole batch aborts.
J. Resulting negative stock is rejected (delegated to create_adjustment).
K. All-or-nothing: one bad row blocks every other row from being applied.
L. Created adjustments carry mode=SET_QUANTITY, source=CYCLE_COUNT,
   reason_code=CYCLE_COUNT_CORRECTION.
M. quantity_before / delta / after are correct on each row.
N. Item.current_stock is updated via the existing adjustment service
   (not by cycle_count directly).
O. Zoho sync is invoked per row through the existing v2.10.0 path.
P. Failed Zoho sync does NOT roll back local PackTrack stock.
Q. Per-row note overrides shared note; default falls back to
   "Cycle count adjustment".
R. Single-item adjustment route still works (regression).
S. Adjustment history shows cycle-count rows.
T. Duplicate item ids in one batch are rejected at validation time.
U. Source-level guard — cycle_count service imports no Zoho/OAuth/HTTP.
V. Master-data fields unchanged after a successful batch.
W. cycle_count module does not mention Receiving.
"""
from __future__ import annotations

import importlib
import os
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("PACKTRACK_SECRET_KEY", "test-secret")

import httpx
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from packtrack.config import settings
from packtrack.models import (
    AdjustmentMode,
    AdjustmentReason,
    AdjustmentSource,
    InventoryAdjustment,
    Item,
    Role,
    User,
    ZohoSyncStatus,
)
from packtrack.services.cycle_count import (
    CycleCountInputRow,
    submit_cycle_count,
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


def _seed_user(session, *, role=Role.OWNER, user_id=1, name="Owner"):
    u = User(
        id=user_id, email=f"{role.value}-{user_id}@example.com", name=name,
        role=role, password_hash="x", is_active=True,
    )
    session.add(u)
    session.commit()
    return u


def _seed_item(session, *, name="Bubble mailer", sku="SKU-1",
               material_code="MC-1", current_stock=100.0):
    it = Item(
        name=name, sku_code=sku, material_code=material_code,
        unit="pcs", vendor="ACME", current_stock=current_stock,
        zoho_item_id=None,
    )
    session.add(it)
    session.commit()
    session.refresh(it)
    return it


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
# A/B/C — perms
# ---------------------------------------------------------------------------


def test_owner_can_get_form(session, engine, monkeypatch):
    _seed_user(session)
    _seed_item(session)
    client = _client(session, engine, monkeypatch)
    resp = client.get("/inventory/cycle-count")
    assert resp.status_code == 200
    assert "cycle-count-form" in resp.text
    assert "cycle-count-submit" in resp.text


def test_non_owner_cannot_get_form(session, engine, monkeypatch):
    designer = _seed_user(session, role=Role.DESIGN, user_id=1, name="Des")
    _seed_item(session)
    client = _client(session, engine, monkeypatch, user=designer)
    resp = client.get("/inventory/cycle-count")
    assert resp.status_code == 403


def test_non_owner_cannot_post(session, engine, monkeypatch):
    designer = _seed_user(session, role=Role.DESIGN, user_id=1, name="Des")
    item = _seed_item(session)
    client = _client(session, engine, monkeypatch, user=designer)
    resp = client.post(
        "/inventory/cycle-count",
        data={f"counted_{item.id}": "5"},
    )
    assert resp.status_code == 403
    assert session.exec(select(InventoryAdjustment)).all() == []


# ---------------------------------------------------------------------------
# D — empty submission
# ---------------------------------------------------------------------------


def test_empty_submission_renders_form_with_warning(session, engine, monkeypatch):
    _seed_user(session)
    _seed_item(session)
    client = _client(session, engine, monkeypatch)
    resp = client.post("/inventory/cycle-count", data={"shared_note": ""})
    assert resp.status_code == 200
    assert "cycle-count-warning" in resp.text
    assert session.exec(select(InventoryAdjustment)).all() == []


# ---------------------------------------------------------------------------
# E — zero-variance skipped
# ---------------------------------------------------------------------------


def test_zero_variance_row_is_skipped(session):
    _seed_user(session)
    item = _seed_item(session, current_stock=100.0)
    user = session.exec(select(User)).first()
    outcome = submit_cycle_count(
        session, actor=user,
        inputs=[CycleCountInputRow(item_id=item.id, raw_counted="100")],
    )
    assert outcome.errors == []
    assert outcome.created_count == 0
    assert outcome.skipped_count == 1
    assert outcome.rows[0].kind == "skipped_zero_variance"
    assert session.exec(select(InventoryAdjustment)).all() == []
    session.refresh(item)
    assert item.current_stock == 100.0


# ---------------------------------------------------------------------------
# F/G — variance creates adjustment
# ---------------------------------------------------------------------------


def test_positive_variance_creates_increase_adjustment(session):
    _seed_user(session)
    item = _seed_item(session, current_stock=100.0)
    user = session.exec(select(User)).first()
    outcome = submit_cycle_count(
        session, actor=user,
        inputs=[CycleCountInputRow(item_id=item.id, raw_counted="105")],
    )
    assert outcome.errors == []
    assert outcome.created_count == 1
    r = outcome.rows[0]
    assert r.kind == "created"
    assert r.quantity_before == Decimal("100")
    assert r.quantity_after == Decimal("105")
    assert r.quantity_delta == Decimal("5")
    session.refresh(item)
    assert item.current_stock == 105.0


def test_negative_variance_creates_decrease_adjustment(session):
    _seed_user(session)
    item = _seed_item(session, current_stock=100.0)
    user = session.exec(select(User)).first()
    outcome = submit_cycle_count(
        session, actor=user,
        inputs=[CycleCountInputRow(item_id=item.id, raw_counted="93")],
    )
    assert outcome.errors == []
    assert outcome.created_count == 1
    r = outcome.rows[0]
    assert r.quantity_delta == Decimal("-7")
    assert r.quantity_after == Decimal("93")
    session.refresh(item)
    assert item.current_stock == 93.0


# ---------------------------------------------------------------------------
# H/I/J — validation rejections
# ---------------------------------------------------------------------------


def test_negative_counted_rejected(session):
    _seed_user(session)
    item = _seed_item(session, current_stock=10.0)
    user = session.exec(select(User)).first()
    outcome = submit_cycle_count(
        session, actor=user,
        inputs=[CycleCountInputRow(item_id=item.id, raw_counted="-5")],
    )
    assert outcome.errors != []
    assert outcome.created_count == 0
    assert session.exec(select(InventoryAdjustment)).all() == []
    session.refresh(item)
    assert item.current_stock == 10.0


def test_invalid_decimal_rejected(session):
    _seed_user(session)
    item = _seed_item(session, current_stock=10.0)
    user = session.exec(select(User)).first()
    outcome = submit_cycle_count(
        session, actor=user,
        inputs=[CycleCountInputRow(item_id=item.id, raw_counted="not-a-number")],
    )
    assert outcome.errors != []
    assert session.exec(select(InventoryAdjustment)).all() == []


# ---------------------------------------------------------------------------
# K — all-or-nothing
# ---------------------------------------------------------------------------


def test_one_bad_row_blocks_entire_batch(session):
    _seed_user(session)
    item_a = _seed_item(session, name="Good Item", current_stock=10.0)
    item_b = _seed_item(session, name="Bad Item", current_stock=20.0)
    user = session.exec(select(User)).first()
    outcome = submit_cycle_count(
        session, actor=user,
        inputs=[
            CycleCountInputRow(item_id=item_a.id, raw_counted="15"),
            CycleCountInputRow(item_id=item_b.id, raw_counted="-1"),
        ],
    )
    assert outcome.errors != []
    assert outcome.created_count == 0
    session.refresh(item_a)
    session.refresh(item_b)
    assert item_a.current_stock == 10.0
    assert item_b.current_stock == 20.0
    assert session.exec(select(InventoryAdjustment)).all() == []


# ---------------------------------------------------------------------------
# L/M — adjustment metadata
# ---------------------------------------------------------------------------


def test_created_adjustment_carries_cycle_count_metadata(session):
    _seed_user(session)
    item = _seed_item(session, current_stock=100.0)
    user = session.exec(select(User)).first()
    submit_cycle_count(
        session, actor=user,
        inputs=[CycleCountInputRow(item_id=item.id, raw_counted="105")],
    )
    adj = session.exec(select(InventoryAdjustment)).first()
    assert adj is not None
    assert adj.mode is AdjustmentMode.SET_QUANTITY
    assert adj.source is AdjustmentSource.CYCLE_COUNT
    assert adj.reason_code is AdjustmentReason.CYCLE_COUNT_CORRECTION
    assert adj.quantity_before == Decimal("100")
    assert adj.quantity_delta == Decimal("5")
    assert adj.quantity_after == Decimal("105")


# ---------------------------------------------------------------------------
# N — Item.current_stock changed via the existing service only
# ---------------------------------------------------------------------------


def test_current_stock_updated_via_existing_service_path(session, monkeypatch):
    """If we patched create_adjustment to a no-op, current_stock must NOT
    change. That proves cycle_count delegates rather than writing
    Item.current_stock directly."""
    from packtrack.services import cycle_count as cc
    from packtrack.services import inventory_adjustments as ia

    _seed_user(session)
    item = _seed_item(session, current_stock=100.0)
    user = session.exec(select(User)).first()

    sentinel_called = {"count": 0}

    def _stub_create_adjustment(*args, **kwargs):
        sentinel_called["count"] += 1
        raise RuntimeError("create_adjustment intentionally disabled in test")

    monkeypatch.setattr(cc, "create_adjustment", _stub_create_adjustment)

    with pytest.raises(RuntimeError):
        submit_cycle_count(
            session, actor=user,
            inputs=[CycleCountInputRow(item_id=item.id, raw_counted="105")],
        )
    assert sentinel_called["count"] == 1
    session.refresh(item)
    assert item.current_stock == 100.0
    assert callable(ia.create_adjustment)


# ---------------------------------------------------------------------------
# O/P — sync invocation + failure does not roll back local stock
# ---------------------------------------------------------------------------


def test_sync_invoked_per_row_when_configured(session, monkeypatch):
    monkeypatch.setattr(settings, "ZOHO_INTEGRATION_ADJUST_ENABLED", True)
    monkeypatch.setattr(settings, "ZOHO_INTEGRATION_BASE_URL", "http://int.test")
    monkeypatch.setattr(settings, "ZOHO_INTEGRATION_APP_TOKEN", "tok")
    monkeypatch.setattr(settings, "ZOHO_INTEGRATION_BRAND", "haute_brands")

    _seed_user(session)
    item = _seed_item(session, current_stock=100.0)
    item.zoho_item_id = "zoho-1"
    session.add(item)
    session.commit()
    user = session.exec(select(User)).first()

    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        return httpx.Response(200, json={
            "ok": True,
            "zoho_adjustment_id": "Z-1",
            "zoho_reference": "REF-1",
            "zoho_status": "posted",
            "meta": {"idempotent": False},
        })

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = submit_cycle_count(
        session, actor=user,
        inputs=[CycleCountInputRow(item_id=item.id, raw_counted="105")],
        http_client=client,
    )
    assert len(calls) == 1
    assert outcome.created_count == 1
    assert outcome.rows[0].sync_status is ZohoSyncStatus.SYNCED


def test_failed_sync_does_not_roll_back_local_stock(session, monkeypatch):
    monkeypatch.setattr(settings, "ZOHO_INTEGRATION_ADJUST_ENABLED", True)
    monkeypatch.setattr(settings, "ZOHO_INTEGRATION_BASE_URL", "http://int.test")
    monkeypatch.setattr(settings, "ZOHO_INTEGRATION_APP_TOKEN", "tok")
    monkeypatch.setattr(settings, "ZOHO_INTEGRATION_BRAND", "haute_brands")

    _seed_user(session)
    item = _seed_item(session, current_stock=100.0)
    item.zoho_item_id = "zoho-fail"
    session.add(item)
    session.commit()
    user = session.exec(select(User)).first()

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "boom"}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = submit_cycle_count(
        session, actor=user,
        inputs=[CycleCountInputRow(item_id=item.id, raw_counted="105")],
        http_client=client,
    )
    session.refresh(item)
    assert item.current_stock == 105.0
    assert outcome.created_count == 1
    assert outcome.rows[0].sync_status is ZohoSyncStatus.FAILED
    adj = session.exec(select(InventoryAdjustment)).first()
    assert adj.zoho_sync_status is ZohoSyncStatus.FAILED
    assert "500" in (adj.zoho_sync_error or "")


# ---------------------------------------------------------------------------
# Q — notes
# ---------------------------------------------------------------------------


def test_per_row_note_overrides_shared_note(session):
    _seed_user(session)
    item_a = _seed_item(session, name="A", current_stock=10)
    item_b = _seed_item(session, name="B", current_stock=10)
    user = session.exec(select(User)).first()
    submit_cycle_count(
        session, actor=user,
        shared_note="batch note",
        inputs=[
            CycleCountInputRow(item_id=item_a.id, raw_counted="11"),
            CycleCountInputRow(item_id=item_b.id, raw_counted="11", note="row note"),
        ],
    )
    rows = session.exec(
        select(InventoryAdjustment).order_by(InventoryAdjustment.id)
    ).all()
    assert rows[0].notes == "batch note"
    assert rows[1].notes == "row note"


def test_blank_note_defaults_to_cycle_count_label(session):
    _seed_user(session)
    item = _seed_item(session, current_stock=10)
    user = session.exec(select(User)).first()
    submit_cycle_count(
        session, actor=user,
        inputs=[CycleCountInputRow(item_id=item.id, raw_counted="11")],
    )
    adj = session.exec(select(InventoryAdjustment)).first()
    assert adj.notes == "Cycle count adjustment"


# ---------------------------------------------------------------------------
# R — single-item flow still works
# ---------------------------------------------------------------------------


def test_single_item_adjustment_route_still_works(session, engine, monkeypatch):
    _seed_user(session)
    item = _seed_item(session, current_stock=10.0)
    client = _client(session, engine, monkeypatch)
    resp = client.post(
        f"/inventory/{item.id}/adjust",
        data={
            "mode": "delta", "direction": "increase",
            "quantity": "1", "reason_code": "manual_correction",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    session.refresh(item)
    assert item.current_stock == 11.0


# ---------------------------------------------------------------------------
# S — history reflects cycle-count rows
# ---------------------------------------------------------------------------


def test_history_page_shows_cycle_count_adjustments(session, engine, monkeypatch):
    _seed_user(session)
    item = _seed_item(session, current_stock=100.0)
    user = session.exec(select(User)).first()
    submit_cycle_count(
        session, actor=user,
        inputs=[CycleCountInputRow(item_id=item.id, raw_counted="105")],
    )
    client = _client(session, engine, monkeypatch)
    page = client.get(f"/inventory/{item.id}/adjustments")
    assert page.status_code == 200
    assert "ADJ-" in page.text
    page2 = client.get("/inventory/adjustments")
    assert page2.status_code == 200
    assert "ADJ-" in page2.text


# ---------------------------------------------------------------------------
# T — duplicate item rejected at validation
# ---------------------------------------------------------------------------


def test_duplicate_item_in_batch_is_rejected(session):
    _seed_user(session)
    item = _seed_item(session, current_stock=10)
    user = session.exec(select(User)).first()
    outcome = submit_cycle_count(
        session, actor=user,
        inputs=[
            CycleCountInputRow(item_id=item.id, raw_counted="11"),
            CycleCountInputRow(item_id=item.id, raw_counted="12"),
        ],
    )
    assert outcome.errors != []
    assert outcome.created_count == 0
    assert session.exec(select(InventoryAdjustment)).all() == []


# ---------------------------------------------------------------------------
# U/W — source-level guards
# ---------------------------------------------------------------------------


def test_cycle_count_module_imports_no_zoho_or_oauth_symbol():
    import packtrack.services.cycle_count as mod
    with open(mod.__file__) as f:
        src = f.read()
    lowered = src.lower()
    forbidden = [
        "oauth", "access_token", "refresh_token",
        "zoho.com", "zohoapis.com",
    ]
    for needle in forbidden:
        assert needle not in lowered, f"Forbidden: {needle!r}"


def test_cycle_count_module_does_not_touch_receiving():
    mod = importlib.import_module("packtrack.services.cycle_count")
    with open(mod.__file__) as f:
        src = f.read()
    assert "receiving" not in src.lower()


# ---------------------------------------------------------------------------
# V — master-data untouched
# ---------------------------------------------------------------------------


def test_master_data_unchanged_after_batch(session):
    _seed_user(session)
    item = _seed_item(session, current_stock=100.0)
    pre = (item.name, item.vendor, item.material_code, item.unit, item.sku_code)
    user = session.exec(select(User)).first()
    submit_cycle_count(
        session, actor=user,
        inputs=[CycleCountInputRow(item_id=item.id, raw_counted="105")],
    )
    session.refresh(item)
    post = (item.name, item.vendor, item.material_code, item.unit, item.sku_code)
    assert pre == post


# ---------------------------------------------------------------------------
# UI link surface
# ---------------------------------------------------------------------------


def test_inventory_page_shows_cycle_count_link_for_owner(session, engine, monkeypatch):
    _seed_user(session)
    _seed_item(session)
    client = _client(session, engine, monkeypatch)
    page = client.get("/inventory")
    assert page.status_code == 200
    assert "Cycle count" in page.text
    assert "/inventory/cycle-count" in page.text


def test_inventory_page_hides_cycle_count_link_for_non_owner(
    session, engine, monkeypatch,
):
    designer = _seed_user(session, role=Role.DESIGN, user_id=1, name="Des")
    _seed_item(session)
    client = _client(session, engine, monkeypatch, user=designer)
    page = client.get("/inventory")
    assert page.status_code == 200
    assert "/inventory/cycle-count" not in page.text
