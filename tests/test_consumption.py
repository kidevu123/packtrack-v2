"""Tests for the Luma → PackTrack packaging consumption service."""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("PACKTRACK_SECRET_KEY", "test-secret")

from datetime import datetime, timedelta

import pytest
from sqlmodel import Session, create_engine, select

from packtrack.models import Item, MaterialConsumptionEvent, User
from packtrack.services.consumption import (
    _recompute_daily_usage_rate,
    _threshold_crossed,
    process_luma_consumption,
)


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    # Only create the tables required for consumption tests; other models use
    # PostgreSQL-only JSONB columns that are incompatible with SQLite.
    Item.metadata.tables["items"].create(bind=engine)
    MaterialConsumptionEvent.metadata.tables["material_consumption_events"].create(bind=engine)
    # users table is needed by notify_stock_alert (queried on threshold crossings).
    # No JSONB columns so it is safe to create under SQLite.
    User.metadata.tables["users"].create(bind=engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def item(session: Session) -> Item:
    it = Item(
        name="Test Blister Card",
        unit="each",
        current_stock=5000.0,
        reorder_point=1000.0,
        critical_point=200.0,
        daily_usage_rate=0.0,
        material_code="PT-00001",
    )
    session.add(it)
    session.commit()
    session.refresh(it)
    return it


PAYLOAD = {
    "source": "LUMA",
    "finished_lot_id": "lot-abc-123",
    "finished_lot_number": "FL-2024-001",
    "product_sku": "HN-001",
    "units_produced": 1000,
    "released_at": "2024-01-15T10:30:00Z",
    "consumed_materials": [
        {
            "material_code": "PT-00001",
            "qty_consumed": 1000,
            "packaging_lot_id": "pl-uuid-1",
            "supplier_lot_number": "SL-2024-001",
        }
    ],
}


def test_process_decrements_current_stock(session: Session, item: Item):
    process_luma_consumption(session, PAYLOAD)
    session.refresh(item)
    assert item.current_stock == 4000.0


def test_process_is_idempotent(session: Session, item: Item):
    process_luma_consumption(session, PAYLOAD)
    process_luma_consumption(session, PAYLOAD)
    session.refresh(item)
    assert item.current_stock == 4000.0
    events = session.exec(
        select(MaterialConsumptionEvent).where(
            MaterialConsumptionEvent.finished_lot_id == "lot-abc-123"
        )
    ).all()
    assert len(events) == 1


def test_process_skips_unknown_material(session: Session, item: Item):
    p = {**PAYLOAD, "consumed_materials": [{"material_code": "PT-99999", "qty_consumed": 100}]}
    result = process_luma_consumption(session, p)
    session.refresh(item)
    assert item.current_stock == 5000.0
    assert result["processed"][0]["status"] == "skipped_not_found"


def test_process_stock_floors_at_zero(session: Session, item: Item):
    p = {**PAYLOAD, "consumed_materials": [{"material_code": "PT-00001", "qty_consumed": 99999}]}
    process_luma_consumption(session, p)
    session.refresh(item)
    assert item.current_stock == 0.0


def test_threshold_crossed_reorder(item: Item):
    assert _threshold_crossed(item, prev_stock=1500.0, new_stock=900.0) == "reorder"


def test_threshold_crossed_critical(item: Item):
    assert _threshold_crossed(item, prev_stock=300.0, new_stock=100.0) == "critical"


def test_threshold_not_crossed_when_already_below(item: Item):
    assert _threshold_crossed(item, prev_stock=800.0, new_stock=600.0) is None


def test_recompute_daily_usage_rate(session: Session, item: Item):
    for i, qty in enumerate([100.0, 200.0, 300.0]):
        session.add(MaterialConsumptionEvent(
            item_id=item.id,
            qty_consumed=qty,
            finished_lot_id=f"lot-{i}",
            finished_lot_number=f"FL-{i}",
            consumed_at=datetime.utcnow() - timedelta(days=i * 5),
            received_at=datetime.utcnow(),
        ))
    session.commit()
    rate = _recompute_daily_usage_rate(session, item.id)
    assert abs(rate - 600.0 / 30.0) < 0.01


# ── Route tests ──────────────────────────────────────────────────────────────

# Lazy imports: packtrack.main → packtrack.db → engine uses settings.DATABASE_URL
# at module load time.  conftest.py sets DATABASE_URL to a postgres placeholder
# before any module is collected, so we must NOT import these at the top of this
# file.  Instead we import inside the helper so they are resolved at *call* time,
# after monkeypatch has had a chance to set the env var.


def _client(session: Session):  # -> TestClient
    import os
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    from fastapi.testclient import TestClient

    from packtrack.db import get_session
    from packtrack.main import app
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app, raise_server_exceptions=False)


def test_route_rejects_missing_secret(session: Session, item: Item):
    resp = _client(session).post("/api/internal/luma-consumption", json=PAYLOAD)
    assert resp.status_code == 401


def test_route_rejects_wrong_secret(session: Session, item: Item):
    resp = _client(session).post(
        "/api/internal/luma-consumption",
        json=PAYLOAD,
        headers={"x-luma-packtrack-secret": "wrong"},
    )
    assert resp.status_code == 401


def test_route_processes_valid_request(session: Session, item: Item, monkeypatch):
    monkeypatch.setenv("LUMA_PACKTRACK_SECRET", "test-secret")
    resp = _client(session).post(
        "/api/internal/luma-consumption",
        json=PAYLOAD,
        headers={"x-luma-packtrack-secret": "test-secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    session.refresh(item)
    assert item.current_stock == 4000.0


def test_route_returns_400_on_missing_fields(session: Session, monkeypatch):
    monkeypatch.setenv("LUMA_PACKTRACK_SECRET", "test-secret")
    resp = _client(session).post(
        "/api/internal/luma-consumption",
        json={"source": "LUMA"},
        headers={"x-luma-packtrack-secret": "test-secret"},
    )
    assert resp.status_code == 400
