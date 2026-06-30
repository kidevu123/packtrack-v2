"""v2.16.0 — Cycle-count form completeness (Zoho snapshot + live Δ/After).

Closes the two spec gaps left in v2.14.0:

  A. Form did not surface Zoho snapshot/variance per row.
     v2.16.0 adds a Zoho-snapshot column that reads the v2.11.0
     ``Item.last_zoho_stock_snapshot`` and shows variance vs PT
     current_stock (Δ +N amber, Δ -N red, "in sync" emerald).

  B. Form had no live variance preview as the operator typed.
     v2.16.0 adds Δ and After columns rewritten per-keystroke by a
     small inline JS handler. Server validation is unchanged — this
     is operator UX only.
"""
from __future__ import annotations

import os
from datetime import datetime
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("PACKTRACK_SECRET_KEY", "test-secret")

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from packtrack.models import Item, Role, User


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


def _seed_user(session, *, role=Role.OWNER, user_id=1, name="Owner"):
    u = User(
        id=user_id, email=f"{role.value}-{user_id}@example.com", name=name,
        role=role, password_hash="x", is_active=True,
    )
    session.add(u)
    session.commit()
    return u


def _seed_item(session, *, name="Mailer", current_stock=100.0,
               zoho_snapshot=None, snapshot_at=None):
    it = Item(
        name=name, sku_code="SKU-1", material_code="MC-1",
        unit="pcs", vendor="ACME", current_stock=current_stock,
    )
    if zoho_snapshot is not None:
        it.last_zoho_stock_snapshot = zoho_snapshot
        it.last_zoho_stock_snapshot_at = snapshot_at or datetime.utcnow()
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
# New header columns
# ---------------------------------------------------------------------------


def test_form_renders_zoho_snapshot_column(session, engine, monkeypatch):
    _seed_user(session)
    _seed_item(session)
    client = _client(session, engine, monkeypatch)
    resp = client.get("/inventory/cycle-count")
    assert resp.status_code == 200
    assert "Zoho snapshot" in resp.text
    assert ">Current (PT)<" in resp.text


def test_form_renders_variance_preview_columns(session, engine, monkeypatch):
    _seed_user(session)
    _seed_item(session)
    client = _client(session, engine, monkeypatch)
    resp = client.get("/inventory/cycle-count")
    assert resp.status_code == 200
    assert ">Δ<" in resp.text
    assert ">After<" in resp.text


# ---------------------------------------------------------------------------
# Per-row cells
# ---------------------------------------------------------------------------


def test_zoho_snapshot_cell_shows_value_when_present(session, engine, monkeypatch):
    _seed_user(session)
    item = _seed_item(session, current_stock=100.0, zoho_snapshot=Decimal("105"))
    client = _client(session, engine, monkeypatch)
    resp = client.get("/inventory/cycle-count")
    assert f'data-testid="cycle-count-zoho-{item.id}"' in resp.text
    # PT(100) - Zoho(105) = -5 → red Δ -5
    assert "Δ -5" in resp.text


def test_zoho_snapshot_cell_shows_in_sync_when_equal(session, engine, monkeypatch):
    _seed_user(session)
    item = _seed_item(session, current_stock=100.0, zoho_snapshot=Decimal("100"))
    client = _client(session, engine, monkeypatch)
    resp = client.get("/inventory/cycle-count")
    assert f'data-testid="cycle-count-zoho-{item.id}"' in resp.text
    assert "in sync" in resp.text


def test_zoho_snapshot_cell_renders_dash_when_absent(session, engine, monkeypatch):
    _seed_user(session)
    item = _seed_item(session, current_stock=100.0, zoho_snapshot=None)
    client = _client(session, engine, monkeypatch)
    resp = client.get("/inventory/cycle-count")
    assert f'data-testid="cycle-count-zoho-{item.id}"' in resp.text
    # No snapshot → no in-sync claim
    assert "in sync" not in resp.text


def test_form_carries_per_row_data_attributes_for_js(session, engine, monkeypatch):
    _seed_user(session)
    item = _seed_item(session, current_stock=42.0)
    client = _client(session, engine, monkeypatch)
    resp = client.get("/inventory/cycle-count")
    assert 'data-current="42' in resp.text
    assert f'data-cc-counted="{item.id}"' in resp.text
    assert f'data-cc-delta="{item.id}"' in resp.text
    assert f'data-cc-after="{item.id}"' in resp.text


def test_form_includes_live_preview_script(session, engine, monkeypatch):
    _seed_user(session)
    _seed_item(session)
    client = _client(session, engine, monkeypatch)
    resp = client.get("/inventory/cycle-count")
    assert "data-cc-counted" in resp.text
    assert "addEventListener" in resp.text


# ---------------------------------------------------------------------------
# Regression: v2.14.0 contract intact
# ---------------------------------------------------------------------------


def test_v2_14_0_counted_input_testid_unchanged(session, engine, monkeypatch):
    _seed_user(session)
    item = _seed_item(session)
    client = _client(session, engine, monkeypatch)
    resp = client.get("/inventory/cycle-count")
    assert f'data-testid="cycle-count-input-{item.id}"' in resp.text


def test_v2_14_0_submit_button_testid_unchanged(session, engine, monkeypatch):
    _seed_user(session)
    _seed_item(session)
    client = _client(session, engine, monkeypatch)
    resp = client.get("/inventory/cycle-count")
    assert 'data-testid="cycle-count-submit"' in resp.text


def test_non_owner_still_forbidden_after_form_change(session, engine, monkeypatch):
    designer = _seed_user(session, role=Role.DESIGN, name="Des")
    _seed_item(session)
    client = _client(session, engine, monkeypatch, user=designer)
    resp = client.get("/inventory/cycle-count")
    assert resp.status_code == 403
