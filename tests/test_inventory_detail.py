"""Item detail route: view permissions + owner edit workflow."""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("PACKTRACK_SECRET_KEY", "test-secret")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from packtrack.models import Item, Role, User
from packtrack.services.product_line import derive_product_line


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


def _client(session: Session, engine, monkeypatch, role: Role) -> TestClient:
    import packtrack.db
    import packtrack.main
    from packtrack import deps
    from packtrack.db import get_session
    from packtrack.main import app

    monkeypatch.setattr(packtrack.db, "engine", engine)
    monkeypatch.setattr(packtrack.main, "engine", engine)
    app.dependency_overrides[get_session] = lambda: session

    def _force_user():
        return session.exec(select(User).where(User.role == role)).first()

    app.dependency_overrides[deps.require_user] = _force_user
    app.dependency_overrides[deps.current_user] = _force_user
    return TestClient(app, raise_server_exceptions=False)


def _seed(session: Session, role: Role = Role.OWNER) -> Item:
    session.add(User(
        email=f"{role.value}@example.com", name=role.value.title(),
        role=role, password_hash="x", is_active=True,
    ))
    it = Item(
        name="FIX 15mg 12ct - Bottle Label",
        material_code="MC-1", vendor="Acme", unit="units",
        current_stock=42.0, reorder_point=10.0, critical_point=5.0,
        product_line=derive_product_line("FIX 15mg 12ct - Bottle Label"),
        zoho_item_id="z-100",
    )
    session.add(it)
    session.commit()
    session.refresh(it)
    return it


def test_detail_renders_for_owner(session, engine, monkeypatch):
    it = _seed(session, Role.OWNER)
    client = _client(session, engine, monkeypatch, Role.OWNER)
    r = client.get(f"/inventory/{it.id}")
    assert r.status_code == 200
    assert "Save changes" in r.text  # owner sees the edit form submit
    assert "FIX 15mg 12ct" in r.text


def test_detail_readonly_for_non_owner(session, engine, monkeypatch):
    it = _seed(session, Role.AGENT)
    client = _client(session, engine, monkeypatch, Role.AGENT)
    r = client.get(f"/inventory/{it.id}")
    assert r.status_code == 200
    assert "Save changes" not in r.text  # no submit for non-owners
    assert "Read-only" in r.text
    assert "Primary details" in r.text


def test_detail_404_for_missing(session, engine, monkeypatch):
    _seed(session, Role.OWNER)
    client = _client(session, engine, monkeypatch, Role.OWNER)
    assert client.get("/inventory/999999").status_code == 404


def test_forecast_path_not_shadowed_by_detail_route(session, engine, monkeypatch):
    """`/inventory/forecast` must not be captured by /inventory/{id:int}."""
    _seed(session, Role.OWNER)
    client = _client(session, engine, monkeypatch, Role.OWNER)
    r = client.get("/inventory/forecast")
    # Reaches the forecast route (200), not a 422 int-parse error.
    assert r.status_code == 200


def test_owner_edit_updates_fields_and_parks_pending(session, engine, monkeypatch):
    it = _seed(session, Role.OWNER)
    client = _client(session, engine, monkeypatch, Role.OWNER)
    r = client.post(
        f"/inventory/{it.id}",
        data={
            "name": "FIX 15mg 12ct - Renamed", "name__orig": it.name,
            "description": "new desc", "description__orig": "",
            "material_code": "MC-2",
            "vendor": "NewVendor",
            "unit": "boxes", "unit__orig": it.unit,
            "daily_usage_rate": "2.5",
            "reorder_point": "20",
            "critical_point": "8",
            "sea_lead_days": "40",
            "express_lead_days": "6",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    # Name (Zoho-owned) changed → parked local/pending.
    assert "saved=local" in r.headers["location"]

    session.expire_all()
    fresh = session.get(Item, it.id)
    assert fresh.name == "FIX 15mg 12ct - Renamed"
    assert fresh.material_code == "MC-2"
    # Vendor is Zoho-read-only for synced items (has zoho_item_id) → unchanged.
    assert fresh.vendor == "Acme"
    assert fresh.unit == "boxes"
    assert fresh.daily_usage_rate == 2.5
    assert fresh.reorder_point == 20.0
    assert fresh.critical_point == 8.0
    assert fresh.sea_lead_days == 40
    assert fresh.express_lead_days == 6
    assert fresh.reorder_point_locked is True
    assert fresh.zoho_push_status == "pending"
    # product_line recomputed from the new name.
    assert fresh.product_line == "FIX"
    # current_stock is never editable from this form.
    assert fresh.current_stock == 42.0


def test_owner_edit_threshold_only_does_not_park_pending(session, engine, monkeypatch):
    it = _seed(session, Role.OWNER)
    client = _client(session, engine, monkeypatch, Role.OWNER)
    r = client.post(
        f"/inventory/{it.id}",
        data={
            "name": it.name, "name__orig": it.name,
            "description": "", "description__orig": "",
            "material_code": it.material_code,
            "vendor": it.vendor,
            "unit": it.unit, "unit__orig": it.unit,
            "daily_usage_rate": "0",
            "reorder_point": "30",
            "critical_point": "5",
            "sea_lead_days": "45",
            "express_lead_days": "7",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "saved=ok" in r.headers["location"]
    session.expire_all()
    fresh = session.get(Item, it.id)
    assert fresh.reorder_point == 30.0
    assert fresh.zoho_push_status is None  # no Zoho-owned field changed


def test_inline_edit_updates_operational_fields_only(session, engine, monkeypatch):
    """Inline row edit handles PackTrack-owned fields and must NOT touch the
    Zoho-owned vendor or mark Zoho sync pending (that path lives on the detail
    page via push_item_update)."""
    it = _seed(session, Role.OWNER)
    client = _client(session, engine, monkeypatch, Role.OWNER)
    r = client.post(
        f"/inventory/{it.id}/edit",
        data={
            "reorder_point": "25",
            "critical_point": "9",
            "daily_usage_rate": "1.5",
            "material_code": "MC-INLINE",
            # A stray vendor field must be ignored, not silently saved locally.
            "vendor": "ShouldBeIgnored",
        },
        headers={"hx-request": "true"},
    )
    assert r.status_code == 200
    session.expire_all()
    fresh = session.get(Item, it.id)
    assert fresh.reorder_point == 25.0
    assert fresh.critical_point == 9.0
    assert fresh.daily_usage_rate == 1.5
    assert fresh.material_code == "MC-INLINE"
    assert fresh.reorder_point_locked is True
    # Vendor (Zoho-owned) is untouched and no pending push was created.
    assert fresh.vendor == "Acme"
    assert fresh.zoho_push_status is None


def test_non_owner_cannot_edit(session, engine, monkeypatch):
    it = _seed(session, Role.AGENT)
    client = _client(session, engine, monkeypatch, Role.AGENT)
    r = client.post(
        f"/inventory/{it.id}",
        data={"name": "hacked", "unit": "units"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    session.expire_all()
    assert session.get(Item, it.id).name != "hacked"


def test_retry_sync_owner_redirects_with_status(session, engine, monkeypatch):
    """Owner retry re-runs the push; with the service unconfigured in tests it
    parks pending and redirects with saved=local."""
    it = _seed(session, Role.OWNER)
    client = _client(session, engine, monkeypatch, Role.OWNER)
    r = client.post(f"/inventory/{it.id}/sync/retry", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/inventory/{it.id}?saved=local"
    session.expire_all()
    assert session.get(Item, it.id).zoho_push_status == "pending"


def test_retry_sync_forbidden_for_non_owner(session, engine, monkeypatch):
    it = _seed(session, Role.AGENT)
    client = _client(session, engine, monkeypatch, Role.AGENT)
    r = client.post(f"/inventory/{it.id}/sync/retry", follow_redirects=False)
    assert r.status_code == 403
