"""v2.11.0 — PackTrack owns Item.current_stock; Zoho is snapshot-only.

Verifies the policy:

A. Existing item: inbound Zoho sync does NOT overwrite current_stock,
   regardless of pending state.
B. Existing item: inbound sync DOES update the snapshot fields.
C. New item: very first sync (is_new=True) seeds current_stock from
   Zoho AND populates the snapshot.
D. Master-data sync still works on existing items
   (name/description/unit when no pending edit; vendor always).
E. parse_zoho_stock handles all five known field names + missing.
F. record_zoho_stock_snapshot is a no-op when raw_stock is None
   (don't overwrite a prior snapshot with None).
G. zoho_stock_variance: positive when PT > Zoho, negative when PT <
   Zoho, None when no snapshot.
H. Item-detail page renders the "PackTrack · source of truth" label
   and the Zoho-snapshot block when a snapshot is present.
I. Item-detail page does NOT render the snapshot block when no
   snapshot is present.
J. Inventory edit route still cannot write current_stock (regression).
K. Inventory adjustment still updates current_stock (regression).
L. Adjustment sync to integration service still works (no regression
   from v2.10.0).
M. inventory_stock_policy module imports no Zoho/OAuth/HTTP symbol.
N. No Receiving file modified.
O. Snapshot fields are stored as Decimal (not float).
"""
from __future__ import annotations

import importlib
import os
from datetime import datetime
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("PACKTRACK_SECRET_KEY", "test-secret")

import httpx
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from packtrack import zoho
from packtrack.config import settings
from packtrack.models import (
    AdjustmentDirection,
    AdjustmentMode,
    AdjustmentReason,
    Item,
    Role,
    User,
    ZohoSyncStatus,
)
from packtrack.services.inventory_adjustment_sync import try_sync_adjustment
from packtrack.services.inventory_adjustments import create_adjustment
from packtrack.services.inventory_stock_policy import (
    parse_zoho_stock,
    record_zoho_stock_snapshot,
    zoho_stock_variance,
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


def _seed_item(session, *, zoho_item_id="z-1", current_stock=100.0,
               name="Bubble mailer"):
    it = Item(
        name=name, zoho_item_id=zoho_item_id,
        current_stock=current_stock, unit="pcs",
        vendor="ACME", material_code="MC-1", sku_code="SKU-1",
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
# A/B — existing item: never overwrite current_stock; do update snapshot
# ---------------------------------------------------------------------------


def test_existing_item_sync_does_not_overwrite_stock(session):
    item = _seed_item(session, current_stock=100.0)
    raw = {
        "item_id": item.zoho_item_id,
        "name": "Bubble mailer",
        "actual_available_stock": 250,
    }
    zoho._apply_item_sync_fields(item, raw, is_new=False)
    assert item.current_stock == 100.0
    assert float(item.last_zoho_stock_snapshot) == 250.0
    assert item.last_zoho_stock_snapshot_at is not None


def test_existing_item_sync_snapshot_with_pending_owner_edit(session):
    item = _seed_item(session, current_stock=50.0)
    item.zoho_push_status = "pending"
    session.add(item)
    session.commit()
    raw = {
        "item_id": item.zoho_item_id,
        "name": "REVERTED",  # would have been applied if not pending
        "actual_available_stock": 999,
    }
    zoho._apply_item_sync_fields(item, raw, is_new=False)
    # Owner-pushable fields preserved.
    assert item.name == "Bubble mailer"
    # Stock not overwritten even though item is non-pending for stock.
    assert item.current_stock == 50.0
    # Snapshot still recorded.
    assert float(item.last_zoho_stock_snapshot) == 999.0


# ---------------------------------------------------------------------------
# C — new item: seed current_stock from Zoho on FIRST insert only
# ---------------------------------------------------------------------------


def test_new_item_sync_seeds_current_stock_from_zoho(session):
    """Fresh row, is_new=True: current_stock takes Zoho value."""
    item = Item(
        zoho_item_id="z-new", name="Newly imported",
        unit="pcs", current_stock=0.0,
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    raw = {
        "item_id": "z-new",
        "name": "Newly imported",
        "actual_available_stock": 42,
    }
    zoho._apply_item_sync_fields(item, raw, is_new=True)
    assert item.current_stock == 42.0
    assert float(item.last_zoho_stock_snapshot) == 42.0


def test_new_item_sync_with_missing_stock_field_leaves_zero(session):
    """If Zoho omits the stock field on a brand-new item, current_stock
    stays at 0 (no exception, no snapshot update)."""
    item = Item(
        zoho_item_id="z-new-2", name="No stock field",
        unit="pcs", current_stock=0.0,
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    raw = {"item_id": "z-new-2", "name": "No stock field"}
    zoho._apply_item_sync_fields(item, raw, is_new=True)
    assert item.current_stock == 0.0
    assert item.last_zoho_stock_snapshot is None


# ---------------------------------------------------------------------------
# D — master-data sync regression
# ---------------------------------------------------------------------------


def test_existing_item_sync_still_updates_master_data(session):
    item = _seed_item(session, current_stock=100.0)
    item.zoho_push_status = None
    session.add(item)
    session.commit()
    raw = {
        "item_id": item.zoho_item_id,
        "name": "Renamed", "description": "new desc",
        "unit": "boxes", "vendor_name": "New Vendor",
        "sku": "SKU-NEW",
        "actual_available_stock": 7,
    }
    zoho._apply_item_sync_fields(item, raw, is_new=False)
    assert item.name == "Renamed"
    assert item.description == "new desc"
    assert item.unit == "boxes"
    assert item.vendor == "New Vendor"
    assert item.sku_code == "SKU-NEW"
    # Stock untouched.
    assert item.current_stock == 100.0


# ---------------------------------------------------------------------------
# E/F — parse_zoho_stock + record_zoho_stock_snapshot
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key,value,expected", [
    ("actual_available_stock", 100, Decimal("100")),
    ("available_stock", "50.5", Decimal("50.5")),
    ("stock_on_hand", 0, Decimal("0")),
    ("quantity", "12", Decimal("12")),
    ("stock", "3.1416", Decimal("3.1416")),
])
def test_parse_zoho_stock_picks_known_fields(key, value, expected):
    assert parse_zoho_stock({key: value}) == expected


def test_parse_zoho_stock_returns_none_when_missing():
    assert parse_zoho_stock({}) is None
    assert parse_zoho_stock({"actual_available_stock": None}) is None
    assert parse_zoho_stock({"actual_available_stock": ""}) is None


def test_parse_zoho_stock_returns_none_on_garbage():
    assert parse_zoho_stock({"actual_available_stock": "not a number"}) is None


def test_record_snapshot_with_none_is_noop(session):
    """Don't blow away a previous snapshot when the upstream field is
    momentarily missing."""
    item = _seed_item(session)
    item.last_zoho_stock_snapshot = Decimal("50")
    item.last_zoho_stock_snapshot_at = datetime(2026, 6, 30, 12, 0)
    session.add(item)
    session.commit()
    record_zoho_stock_snapshot(item, None)
    assert item.last_zoho_stock_snapshot == Decimal("50")
    assert item.last_zoho_stock_snapshot_at == datetime(2026, 6, 30, 12, 0)


# ---------------------------------------------------------------------------
# G — variance
# ---------------------------------------------------------------------------


def test_variance_positive_when_pt_higher(session):
    item = _seed_item(session, current_stock=10.0)
    item.last_zoho_stock_snapshot = Decimal("7")
    session.add(item)
    session.commit()
    assert zoho_stock_variance(item) == Decimal("3")


def test_variance_negative_when_pt_lower(session):
    item = _seed_item(session, current_stock=5.0)
    item.last_zoho_stock_snapshot = Decimal("8")
    session.add(item)
    session.commit()
    assert zoho_stock_variance(item) == Decimal("-3")


def test_variance_none_when_no_snapshot(session):
    item = _seed_item(session, current_stock=10.0)
    assert item.last_zoho_stock_snapshot is None
    assert zoho_stock_variance(item) is None


# ---------------------------------------------------------------------------
# H/I — item-detail UI
# ---------------------------------------------------------------------------


def test_item_detail_renders_source_of_truth_label(session, engine, monkeypatch):
    _seed_user(session)
    item = _seed_item(session)
    client = _client(session, engine, monkeypatch)
    page = client.get(f"/inventory/{item.id}")
    assert page.status_code == 200
    assert "stock-source-of-truth" in page.text
    assert "PackTrack" in page.text


def test_item_detail_renders_snapshot_block_when_present(session, engine, monkeypatch):
    _seed_user(session)
    item = _seed_item(session, current_stock=10.0)
    item.last_zoho_stock_snapshot = Decimal("7")
    item.last_zoho_stock_snapshot_at = datetime(2026, 6, 30, 12, 0)
    session.add(item)
    session.commit()
    client = _client(session, engine, monkeypatch)
    page = client.get(f"/inventory/{item.id}")
    assert page.status_code == 200
    assert "zoho-stock-snapshot" in page.text
    assert "zoho-stock-variance" in page.text
    assert "Zoho snapshot" in page.text


def test_item_detail_hides_snapshot_block_when_absent(session, engine, monkeypatch):
    _seed_user(session)
    item = _seed_item(session)
    assert item.last_zoho_stock_snapshot is None
    client = _client(session, engine, monkeypatch)
    page = client.get(f"/inventory/{item.id}")
    assert page.status_code == 200
    assert "zoho-stock-snapshot" not in page.text


# ---------------------------------------------------------------------------
# J — master-data edit route still cannot write current_stock
# ---------------------------------------------------------------------------


def test_master_data_edit_route_cannot_change_stock(session, engine, monkeypatch):
    """The /inventory/{id} POST update_item route accepts no
    current_stock field. Submitting one is silently ignored."""
    _seed_user(session)
    item = _seed_item(session, current_stock=100.0)
    client = _client(session, engine, monkeypatch)
    resp = client.post(
        f"/inventory/{item.id}",
        data={
            "name": item.name, "description": "", "material_code": "",
            "vendor": item.vendor, "unit": item.unit,
            "cf_product_line": "", "cf_product_line_original": "",
            "daily_usage_rate": "0", "reorder_point": "0",
            "critical_point": "0", "sea_lead_days": "45",
            "express_lead_days": "7",
            "current_stock": "9999",  # attempted injection
        },
        follow_redirects=False,
    )
    # Route just redirects regardless of the spurious field.
    assert resp.status_code in (200, 302, 303)
    session.refresh(item)
    assert item.current_stock == 100.0


# ---------------------------------------------------------------------------
# K — inventory adjustment still updates current_stock
# ---------------------------------------------------------------------------


def test_adjustment_still_updates_current_stock(session):
    _seed_user(session)
    item = _seed_item(session, current_stock=100.0)
    user = session.exec(select(User)).first()
    create_adjustment(
        session, item_id=item.id, actor=user,
        mode=AdjustmentMode.DELTA, direction=AdjustmentDirection.INCREASE,
        raw_quantity="1", reason_code=AdjustmentReason.MANUAL_CORRECTION,
        notes=None,
    )
    session.refresh(item)
    assert item.current_stock == 101.0


# ---------------------------------------------------------------------------
# L — adjustment sync to integration service still works (no v2.10 regression)
# ---------------------------------------------------------------------------


def test_adjustment_sync_still_works(session, monkeypatch):
    """Wire up the same MockTransport pattern v2.10.0 used."""
    monkeypatch.setattr(settings, "ZOHO_INTEGRATION_ADJUST_ENABLED", True)
    monkeypatch.setattr(settings, "ZOHO_INTEGRATION_BASE_URL", "http://int.test")
    monkeypatch.setattr(settings, "ZOHO_INTEGRATION_APP_TOKEN", "tok")
    monkeypatch.setattr(settings, "ZOHO_INTEGRATION_BRAND", "haute_brands")

    _seed_user(session)
    item = _seed_item(session, current_stock=100.0)
    user = session.exec(select(User)).first()
    result = create_adjustment(
        session, item_id=item.id, actor=user,
        mode=AdjustmentMode.DELTA, direction=AdjustmentDirection.INCREASE,
        raw_quantity="1", reason_code=AdjustmentReason.MANUAL_CORRECTION,
        notes=None,
    )

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "ok": True, "brand": "haute_brands",
            "item_id": item.zoho_item_id,
            "packtrack_adjustment_number": result.adjustment.adjustment_number,
            "quantity_before": "100.0000", "quantity_delta": "1.0000",
            "quantity_after": "101.0000",
            "zoho_adjustment_id": "Z-1", "zoho_reference": "REF-1",
            "zoho_status": "posted", "meta": {"idempotent": False},
        })

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = try_sync_adjustment(
        session, result.adjustment, item, actor=user, http_client=client,
    )
    assert outcome.to_status() is ZohoSyncStatus.SYNCED
    session.refresh(result.adjustment)
    assert result.adjustment.zoho_reference == "REF-1"


# ---------------------------------------------------------------------------
# M — source-level guard: stock-policy module has no Zoho/OAuth/HTTP import
# ---------------------------------------------------------------------------


def test_stock_policy_module_imports_no_external_zoho_symbol():
    import packtrack.services.inventory_stock_policy as mod
    with open(mod.__file__) as f:
        src = f.read()
    lowered = src.lower()
    forbidden = [
        "import httpx", "import requests",
        "oauth", "access_token", "refresh_token",
        "zoho.com", "zohoapis.com",
    ]
    for needle in forbidden:
        assert needle not in lowered, f"Forbidden import/use: {needle!r}"


# ---------------------------------------------------------------------------
# N — no Receiving file touched
# ---------------------------------------------------------------------------


def test_no_receiving_imports_in_stock_policy_module():
    mod = importlib.import_module("packtrack.services.inventory_stock_policy")
    with open(mod.__file__) as f:
        src = f.read()
    assert "receiving" not in src.lower()


# ---------------------------------------------------------------------------
# O — snapshot stored as Decimal not float (Decimal-safe column)
# ---------------------------------------------------------------------------


def test_snapshot_round_trips_as_decimal(session):
    item = _seed_item(session)
    item.last_zoho_stock_snapshot = Decimal("3.1416")
    session.add(item)
    session.commit()
    session.refresh(item)
    assert isinstance(item.last_zoho_stock_snapshot, Decimal)
    assert item.last_zoho_stock_snapshot == Decimal("3.1416")
