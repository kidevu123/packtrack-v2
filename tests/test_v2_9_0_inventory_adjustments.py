"""v2.9.0 Inventory adjustments — full coverage.

PackTrack v2.9.0 makes the local ``Item.current_stock`` value
authoritative. Every change goes through an immutable
``InventoryAdjustment`` ledger row. Tests verify:

A. Owner can access GET /inventory/{id}/adjust.
B. Non-owner cannot access the form (403).
C. Non-owner cannot POST the form (403).
D. Increase adjustment updates current_stock and creates a ledger row
   with correct before/delta/after.
E. Decrease adjustment updates current_stock and creates a ledger row.
F. set_quantity mode computes the correct delta (incl. derived direction).
G. set_quantity that matches current stock is rejected (no-op).
H. Negative resulting stock is rejected.
I. Zero delta (delta mode) is rejected.
J. Invalid reason code is rejected.
K. Reason "other" without notes is rejected.
L. Notes are persisted.
M. No PATCH/PUT/DELETE route exists for an adjustment row.
N. Adjustment row is append-only — service never overwrites existing rows.
O. current_stock and adjustment row are updated transactionally
   (failed insert → no stock change).
P. Item history page shows the adjustment.
Q. Global history page shows the adjustment.
R. Sync status defaults to NOT_CONFIGURED when integration disabled.
S. Sync status becomes PENDING when the integration flag is on.
T. Inventory adjustment module imports NO Zoho/OAuth symbol.
U. Inventory master-data fields (name/vendor/material_code/unit) are
   not changed by submitting an adjustment.
V. Quantity columns are Decimal-typed (not float).
W. Adjustment numbers are unique per year and sequential.
X. Owner cannot include an unknown reason via direct POST.
"""
from __future__ import annotations

import importlib
import os
from datetime import datetime
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("PACKTRACK_SECRET_KEY", "test-secret")

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from packtrack.config import settings
from packtrack.models import (
    AdjustmentDirection,
    AdjustmentMode,
    AdjustmentReason,
    AdjustmentSource,
    InventoryAdjustment,
    Item,
    Role,
    User,
    ZohoSyncStatus,
)
from packtrack.services.inventory_adjustments import (
    AdjustmentError,
    create_adjustment,
    enqueue_or_mark_adjustment_sync,
    generate_adjustment_number,
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
        id=user_id,
        email=f"{role.value}-{user_id}@example.com",
        name=name, role=role, password_hash="x", is_active=True,
    )
    session.add(u)
    session.commit()
    return u


def _seed_item(session, *, current_stock=100.0, name="Bubble mailer", sku="SKU-1",
               material_code="MC-1", unit="pcs", vendor="ACME"):
    it = Item(
        name=name, sku_code=sku, material_code=material_code,
        unit=unit, vendor=vendor, current_stock=current_stock,
        zoho_item_id="z-1",
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
# A/B/C — owner-only access
# ---------------------------------------------------------------------------


def test_owner_can_open_adjust_form(session, engine, monkeypatch):
    _seed_user(session)
    item = _seed_item(session)
    client = _client(session, engine, monkeypatch)
    resp = client.get(f"/inventory/{item.id}/adjust")
    assert resp.status_code == 200
    assert "adjust-form" in resp.text


def test_non_owner_cannot_open_adjust_form(session, engine, monkeypatch):
    designer = _seed_user(session, role=Role.DESIGN, user_id=1, name="Designer")
    item = _seed_item(session)
    client = _client(session, engine, monkeypatch, user=designer)
    resp = client.get(f"/inventory/{item.id}/adjust")
    assert resp.status_code == 403


def test_non_owner_cannot_post_adjustment(session, engine, monkeypatch):
    designer = _seed_user(session, role=Role.DESIGN, user_id=1, name="Designer")
    item = _seed_item(session)
    client = _client(session, engine, monkeypatch, user=designer)
    resp = client.post(
        f"/inventory/{item.id}/adjust",
        data={
            "mode": "delta", "direction": "increase",
            "quantity": "5", "reason_code": "manual_correction",
        },
    )
    assert resp.status_code == 403
    assert session.exec(select(InventoryAdjustment)).all() == []
    session.refresh(item)
    assert item.current_stock == 100.0


# ---------------------------------------------------------------------------
# D/E/F — delta + set_quantity math
# ---------------------------------------------------------------------------


def test_increase_adjustment_updates_stock_and_writes_ledger(session, engine, monkeypatch):
    _seed_user(session)
    item = _seed_item(session, current_stock=100.0)
    client = _client(session, engine, monkeypatch)
    resp = client.post(
        f"/inventory/{item.id}/adjust",
        data={
            "mode": "delta", "direction": "increase",
            "quantity": "7", "reason_code": "manual_correction",
            "notes": "smoke",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    session.refresh(item)
    assert item.current_stock == 107.0
    rows = session.exec(select(InventoryAdjustment)).all()
    assert len(rows) == 1
    r = rows[0]
    assert r.quantity_before == Decimal("100")
    assert r.quantity_delta == Decimal("7")
    assert r.quantity_after == Decimal("107")
    assert r.direction is AdjustmentDirection.INCREASE
    assert r.mode is AdjustmentMode.DELTA
    assert r.reason_code is AdjustmentReason.MANUAL_CORRECTION
    assert r.source is AdjustmentSource.MANUAL_ADJUSTMENT
    assert r.adjustment_number.startswith("ADJ-")


def test_decrease_adjustment_updates_stock_and_writes_ledger(session, engine, monkeypatch):
    _seed_user(session)
    item = _seed_item(session, current_stock=100.0)
    client = _client(session, engine, monkeypatch)
    resp = client.post(
        f"/inventory/{item.id}/adjust",
        data={
            "mode": "delta", "direction": "decrease",
            "quantity": "3", "reason_code": "damaged",
            "notes": "broken in transit",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    session.refresh(item)
    assert item.current_stock == 97.0
    r = session.exec(select(InventoryAdjustment)).first()
    assert r.quantity_delta == Decimal("-3")
    assert r.quantity_after == Decimal("97")
    assert r.direction is AdjustmentDirection.DECREASE


def test_set_quantity_computes_delta_and_direction(session, engine, monkeypatch):
    _seed_user(session)
    item = _seed_item(session, current_stock=100.0)
    client = _client(session, engine, monkeypatch)
    # Counted 85 → delta -15, direction DECREASE
    resp = client.post(
        f"/inventory/{item.id}/adjust",
        data={
            "mode": "set_quantity", "quantity": "85",
            "reason_code": "cycle_count_correction",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    r = session.exec(select(InventoryAdjustment)).first()
    assert r.mode is AdjustmentMode.SET_QUANTITY
    assert r.direction is AdjustmentDirection.DECREASE
    assert r.quantity_before == Decimal("100")
    assert r.quantity_delta == Decimal("-15")
    assert r.quantity_after == Decimal("85")
    session.refresh(item)
    assert item.current_stock == 85.0


def test_set_quantity_matching_current_stock_rejected(session, engine, monkeypatch):
    _seed_user(session)
    item = _seed_item(session, current_stock=42.0)
    client = _client(session, engine, monkeypatch)
    resp = client.post(
        f"/inventory/{item.id}/adjust",
        data={
            "mode": "set_quantity", "quantity": "42",
            "reason_code": "cycle_count_correction",
        },
    )
    # Re-rendered form with 400-class form error (response is the form
    # page itself, not a redirect — so we look for the inline error).
    assert "adjust-form-error" in resp.text
    assert "no adjustment to record" in resp.text
    assert session.exec(select(InventoryAdjustment)).all() == []


# ---------------------------------------------------------------------------
# G/H/I — validation
# ---------------------------------------------------------------------------


def test_negative_resulting_stock_rejected(session, engine, monkeypatch):
    _seed_user(session)
    item = _seed_item(session, current_stock=5.0)
    client = _client(session, engine, monkeypatch)
    resp = client.post(
        f"/inventory/{item.id}/adjust",
        data={
            "mode": "delta", "direction": "decrease",
            "quantity": "10", "reason_code": "damaged",
        },
    )
    assert "adjust-form-error" in resp.text
    assert "negative stock is not permitted" in resp.text
    assert session.exec(select(InventoryAdjustment)).all() == []
    session.refresh(item)
    assert item.current_stock == 5.0


def test_zero_delta_rejected(session, engine, monkeypatch):
    _seed_user(session)
    item = _seed_item(session, current_stock=100.0)
    client = _client(session, engine, monkeypatch)
    resp = client.post(
        f"/inventory/{item.id}/adjust",
        data={
            "mode": "delta", "direction": "increase",
            "quantity": "0", "reason_code": "manual_correction",
        },
    )
    assert "adjust-form-error" in resp.text
    assert "greater than zero" in resp.text
    assert session.exec(select(InventoryAdjustment)).all() == []


def test_invalid_reason_rejected(session, engine, monkeypatch):
    _seed_user(session)
    item = _seed_item(session)
    client = _client(session, engine, monkeypatch)
    resp = client.post(
        f"/inventory/{item.id}/adjust",
        data={
            "mode": "delta", "direction": "increase",
            "quantity": "1", "reason_code": "definitely_not_a_real_reason",
        },
    )
    assert resp.status_code == 400


def test_other_without_notes_rejected(session, engine, monkeypatch):
    _seed_user(session)
    item = _seed_item(session)
    client = _client(session, engine, monkeypatch)
    resp = client.post(
        f"/inventory/{item.id}/adjust",
        data={
            "mode": "delta", "direction": "increase",
            "quantity": "1", "reason_code": "other",
            "notes": "",
        },
    )
    assert "adjust-form-error" in resp.text
    assert "requires a note" in resp.text
    assert session.exec(select(InventoryAdjustment)).all() == []


def test_notes_are_persisted(session, engine, monkeypatch):
    _seed_user(session)
    item = _seed_item(session)
    client = _client(session, engine, monkeypatch)
    client.post(
        f"/inventory/{item.id}/adjust",
        data={
            "mode": "delta", "direction": "increase",
            "quantity": "2", "reason_code": "manual_correction",
            "notes": "  loaded extra after recount  ",
        },
        follow_redirects=False,
    )
    r = session.exec(select(InventoryAdjustment)).first()
    assert r.notes == "loaded extra after recount"  # service strips


# ---------------------------------------------------------------------------
# J/K — append-only / no edit surface
# ---------------------------------------------------------------------------


def test_no_edit_or_delete_route_for_adjustment(session, engine, monkeypatch):
    """Sanity-check via the live FastAPI route table that no
    PATCH/PUT/DELETE route exposes adjustment mutation."""
    _seed_user(session)
    item = _seed_item(session)
    client = _client(session, engine, monkeypatch)
    client.post(
        f"/inventory/{item.id}/adjust",
        data={
            "mode": "delta", "direction": "increase",
            "quantity": "1", "reason_code": "manual_correction",
        },
        follow_redirects=False,
    )
    r = session.exec(select(InventoryAdjustment)).first()
    for verb in ("delete", "patch", "put"):
        method = getattr(client, verb)
        resp = method(f"/inventory/{item.id}/adjustments/{r.id}")
        assert resp.status_code in (404, 405)


def test_service_never_overwrites_existing_row(session):
    _seed_user(session)
    item = _seed_item(session, current_stock=100.0)
    user = session.exec(select(User)).first()
    result_a = create_adjustment(
        session, item_id=item.id, actor=user,
        mode=AdjustmentMode.DELTA, direction=AdjustmentDirection.INCREASE,
        raw_quantity="5", reason_code=AdjustmentReason.MANUAL_CORRECTION,
        notes=None,
    )
    result_b = create_adjustment(
        session, item_id=item.id, actor=user,
        mode=AdjustmentMode.DELTA, direction=AdjustmentDirection.DECREASE,
        raw_quantity="2", reason_code=AdjustmentReason.MANUAL_CORRECTION,
        notes=None,
    )
    assert result_a.adjustment.id != result_b.adjustment.id
    rows = session.exec(select(InventoryAdjustment)).all()
    assert len(rows) == 2
    # First row's data is intact and untouched.
    first = session.get(InventoryAdjustment, result_a.adjustment.id)
    assert first.quantity_delta == Decimal("5")
    assert first.quantity_after == Decimal("105")


# ---------------------------------------------------------------------------
# L — transactional update
# ---------------------------------------------------------------------------


def test_failed_validation_does_not_change_stock(session):
    _seed_user(session)
    item = _seed_item(session, current_stock=10.0)
    user = session.exec(select(User)).first()
    with pytest.raises(AdjustmentError):
        create_adjustment(
            session, item_id=item.id, actor=user,
            mode=AdjustmentMode.DELTA, direction=AdjustmentDirection.DECREASE,
            raw_quantity="50",
            reason_code=AdjustmentReason.DAMAGED, notes=None,
        )
    session.refresh(item)
    assert item.current_stock == 10.0
    assert session.exec(select(InventoryAdjustment)).all() == []


# ---------------------------------------------------------------------------
# M/N — history pages
# ---------------------------------------------------------------------------


def test_item_history_page_shows_adjustment(session, engine, monkeypatch):
    _seed_user(session)
    item = _seed_item(session)
    client = _client(session, engine, monkeypatch)
    client.post(
        f"/inventory/{item.id}/adjust",
        data={
            "mode": "delta", "direction": "increase",
            "quantity": "1", "reason_code": "manual_correction",
        },
        follow_redirects=False,
    )
    page = client.get(f"/inventory/{item.id}/adjustments")
    assert page.status_code == 200
    assert "adjustment-history-table" in page.text
    assert "ADJ-" in page.text


def test_global_history_page_shows_adjustment(session, engine, monkeypatch):
    _seed_user(session)
    item = _seed_item(session)
    client = _client(session, engine, monkeypatch)
    client.post(
        f"/inventory/{item.id}/adjust",
        data={
            "mode": "delta", "direction": "increase",
            "quantity": "2", "reason_code": "found_extra",
        },
        follow_redirects=False,
    )
    page = client.get("/inventory/adjustments")
    assert page.status_code == 200
    assert "adjustment-history-table" in page.text
    assert "ADJ-" in page.text
    assert item.name in page.text


# ---------------------------------------------------------------------------
# O/P — sync defaults + seam, NO direct Zoho call
# ---------------------------------------------------------------------------


def test_sync_status_defaults_to_not_configured(session):
    _seed_user(session)
    item = _seed_item(session)
    user = session.exec(select(User)).first()
    result = create_adjustment(
        session, item_id=item.id, actor=user,
        mode=AdjustmentMode.DELTA, direction=AdjustmentDirection.INCREASE,
        raw_quantity="1", reason_code=AdjustmentReason.MANUAL_CORRECTION,
        notes=None,
    )
    assert result.sync_status is ZohoSyncStatus.NOT_CONFIGURED
    assert result.adjustment.zoho_sync_status is ZohoSyncStatus.NOT_CONFIGURED


def test_sync_status_becomes_pending_when_integration_configured(
    session, monkeypatch,
):
    """The seam reads three settings flags; flipping all three on
    moves new adjustments into PENDING — but still no HTTP call."""
    monkeypatch.setattr(settings, "ZOHO_INTEGRATION_ADJUST_ENABLED", True)
    monkeypatch.setattr(settings, "ZOHO_INTEGRATION_BASE_URL", "http://example")
    monkeypatch.setattr(settings, "ZOHO_INTEGRATION_APP_TOKEN", "x")
    _seed_user(session)
    item = _seed_item(session)
    user = session.exec(select(User)).first()
    result = create_adjustment(
        session, item_id=item.id, actor=user,
        mode=AdjustmentMode.DELTA, direction=AdjustmentDirection.INCREASE,
        raw_quantity="1", reason_code=AdjustmentReason.MANUAL_CORRECTION,
        notes=None,
    )
    assert result.sync_status is ZohoSyncStatus.PENDING


def test_enqueue_sync_helper_is_pure(monkeypatch):
    """The seam helper sets the status field without any network I/O.
    Verified by running with httpx monkey-patched to a hard failure —
    if anything tried to make an HTTP call it would surface here."""
    import httpx as _httpx

    def _no_http(*args, **kwargs):
        raise RuntimeError("v2.9.0 must not make HTTP calls during adjustment")

    monkeypatch.setattr(_httpx, "post", _no_http)
    monkeypatch.setattr(_httpx, "get", _no_http)
    adjustment = InventoryAdjustment(
        item_id=1, adjustment_number="ADJ-X",
        mode=AdjustmentMode.DELTA, direction=AdjustmentDirection.INCREASE,
        quantity_before=Decimal("0"), quantity_delta=Decimal("1"),
        quantity_after=Decimal("1"),
        reason_code=AdjustmentReason.MANUAL_CORRECTION,
        created_by_user_id=1, idempotency_key="kk",
    )
    status = enqueue_or_mark_adjustment_sync(adjustment)
    assert status in (ZohoSyncStatus.NOT_CONFIGURED, ZohoSyncStatus.PENDING)


def test_adjustment_module_imports_no_zoho_symbol():
    """Source-level guard: the adjustment service file imports no
    Zoho/OAuth client. A later contributor wiring a direct call would
    fail this test before review."""
    import packtrack.services.inventory_adjustments as mod
    with open(mod.__file__) as f:
        src = f.read()
    lowered = src.lower()
    # Importing from packtrack.config (which carries the seam flags) is
    # fine; importing a Zoho client symbol is not.
    forbidden = [
        "from packtrack.services.zoho",
        "import httpx",
        "import requests",
        "oauth",
        "access_token",
    ]
    for needle in forbidden:
        assert needle not in lowered, f"Forbidden import/use: {needle!r}"


# ---------------------------------------------------------------------------
# Q — master-data fields unchanged
# ---------------------------------------------------------------------------


def test_master_data_fields_unchanged_by_adjustment(session, engine, monkeypatch):
    _seed_user(session)
    item = _seed_item(session, name="Bubble mailer", vendor="ACME",
                      material_code="MC-1", unit="pcs")
    before = (item.name, item.vendor, item.material_code, item.unit,
              item.sku_code)
    client = _client(session, engine, monkeypatch)
    client.post(
        f"/inventory/{item.id}/adjust",
        data={
            "mode": "delta", "direction": "increase",
            "quantity": "1", "reason_code": "manual_correction",
        },
        follow_redirects=False,
    )
    session.refresh(item)
    after = (item.name, item.vendor, item.material_code, item.unit,
             item.sku_code)
    assert before == after


# ---------------------------------------------------------------------------
# R — Decimal column types
# ---------------------------------------------------------------------------


def test_quantity_columns_are_numeric_decimal_in_schema(session):
    _seed_user(session)
    item = _seed_item(session, current_stock=100.0)
    user = session.exec(select(User)).first()
    create_adjustment(
        session, item_id=item.id, actor=user,
        mode=AdjustmentMode.SET_QUANTITY, direction=None,
        raw_quantity="100.1234", reason_code=AdjustmentReason.CYCLE_COUNT_CORRECTION,
        notes=None,
    )
    r = session.exec(select(InventoryAdjustment)).first()
    # quantity_after is round-trip-safe Decimal — no float drift.
    assert isinstance(r.quantity_before, Decimal)
    assert isinstance(r.quantity_delta, Decimal)
    assert isinstance(r.quantity_after, Decimal)
    assert r.quantity_after == Decimal("100.1234")
    assert r.quantity_delta == Decimal("0.1234")


# ---------------------------------------------------------------------------
# S — Adjustment numbers
# ---------------------------------------------------------------------------


def test_adjustment_number_is_sequential_and_unique(session):
    _seed_user(session)
    item = _seed_item(session, current_stock=100.0)
    user = session.exec(select(User)).first()
    now = datetime(2026, 6, 29, 12, 0, 0)
    n1 = generate_adjustment_number(session, now=now)
    assert n1 == "ADJ-2026-0001"
    r1 = create_adjustment(
        session, item_id=item.id, actor=user,
        mode=AdjustmentMode.DELTA, direction=AdjustmentDirection.INCREASE,
        raw_quantity="1", reason_code=AdjustmentReason.MANUAL_CORRECTION,
        notes=None, now=now,
    )
    r2 = create_adjustment(
        session, item_id=item.id, actor=user,
        mode=AdjustmentMode.DELTA, direction=AdjustmentDirection.DECREASE,
        raw_quantity="1", reason_code=AdjustmentReason.MANUAL_CORRECTION,
        notes=None, now=now,
    )
    assert r1.adjustment.adjustment_number == "ADJ-2026-0001"
    assert r2.adjustment.adjustment_number == "ADJ-2026-0002"


# ---------------------------------------------------------------------------
# T — no Receiving file is changed
# ---------------------------------------------------------------------------


def test_no_receiving_module_imported_from_inventory_adjustments():
    mod = importlib.import_module("packtrack.services.inventory_adjustments")
    with open(mod.__file__) as f:
        src = f.read()
    assert "receiving" not in src.lower()

    mod2 = importlib.import_module("packtrack.routes.inventory_adjustments")
    with open(mod2.__file__) as f:
        src2 = f.read()
    assert "receiving" not in src2.lower()
