"""Pagination + lazy-image coverage for /inventory.

Covers:
  * service: limit/offset slicing on filter_inventory_items
  * service: count_inventory_items matches filter
  * route: /inventory returns first page + pagination metadata
  * route: /inventory?page=2 returns the next slice
  * route: /inventory?stock_status=critical paginates within the filter
  * route: prev/next links carry through existing query params
  * template: <img> rendering uses loading="lazy" on item thumbnails
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("PACKTRACK_SECRET_KEY", "test-secret")

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from packtrack.models import Item, Role, User
from packtrack.services.inventory import (
    count_inventory_items,
    filter_inventory_items,
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
    """Route tests below patch app.dependency_overrides for require_user /
    current_user. Without this teardown those overrides leak into later
    test modules and break their auth-routing assumptions."""
    yield
    from packtrack.main import app
    app.dependency_overrides.clear()


def _seed_items(session: Session, n: int, *, prefix: str = "Item", with_code: bool = True) -> None:
    for i in range(n):
        session.add(Item(
            name=f"{prefix} {i:03d}",
            current_stock=10.0,
            material_code=(f"MC-{i}" if with_code else None),
        ))
    session.commit()


# ---------------------------------------------------------------------------
# Service-layer pagination
# ---------------------------------------------------------------------------


def test_filter_inventory_items_respects_limit_and_offset(session: Session):
    _seed_items(session, 5)
    page1 = filter_inventory_items(session, limit=2, offset=0)
    page2 = filter_inventory_items(session, limit=2, offset=2)
    page3 = filter_inventory_items(session, limit=2, offset=4)
    assert [it.name for it in page1] == ["Item 000", "Item 001"]
    assert [it.name for it in page2] == ["Item 002", "Item 003"]
    assert [it.name for it in page3] == ["Item 004"]


def test_filter_inventory_items_without_limit_returns_full_list(session: Session):
    """Backward-compat: existing call sites that omit limit get all rows."""
    _seed_items(session, 3)
    rows = filter_inventory_items(session)
    assert len(rows) == 3


def test_count_inventory_items_matches_filter_set(session: Session):
    _seed_items(session, 5)
    # +1 critical-stock item, with material_code, so it shows under default OR critical filter
    session.add(Item(
        name="Critical", current_stock=1, critical_point=10, material_code="C-1",
    ))
    session.commit()
    assert count_inventory_items(session) == 6
    assert count_inventory_items(session, stock_status="critical") == 1


def test_count_matches_filter_when_paginated(session: Session):
    _seed_items(session, 30)
    # Sanity: total is 30, a 10-row page still reports 30 total.
    assert count_inventory_items(session) == 30
    page = filter_inventory_items(session, limit=10, offset=0)
    assert len(page) == 10


# ---------------------------------------------------------------------------
# Route-layer pagination
# ---------------------------------------------------------------------------


def _client_with(session: Session, engine, monkeypatch: pytest.MonkeyPatch):
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
        return session.exec(select(User).where(User.role == Role.OWNER)).first()
    app.dependency_overrides[deps.require_user] = _force_user
    app.dependency_overrides[deps.current_user] = _force_user

    return TestClient(app, raise_server_exceptions=False)


def _seed_world(session: Session, n_items: int) -> None:
    session.add(User(
        id=1, email="o@example.com", name="Owner",
        role=Role.OWNER, password_hash="x", is_active=True,
    ))
    _seed_items(session, n_items)


def test_inventory_route_returns_first_page(
    session: Session, engine, monkeypatch: pytest.MonkeyPatch,
):
    _seed_world(session, 75)
    client = _client_with(session, engine, monkeypatch)
    r = client.get("/inventory")
    assert r.status_code == 200
    # Default PAGE_SIZE is 50; with 75 items we expect Page 1 of 2 in the UI.
    assert "Page 1 of 2" in r.text
    assert "75 item" in r.text


def test_inventory_route_page_2_returns_next_slice(
    session: Session, engine, monkeypatch: pytest.MonkeyPatch,
):
    _seed_world(session, 75)
    client = _client_with(session, engine, monkeypatch)
    r = client.get("/inventory?page=2")
    assert r.status_code == 200
    assert "Page 2 of 2" in r.text
    # Item 050 onwards is page 2.
    assert "Item 050" in r.text
    assert "Item 000" not in r.text


def test_inventory_route_critical_filter_still_paginates(
    session: Session, engine, monkeypatch: pytest.MonkeyPatch,
):
    """Home links to /inventory?stock_status=critical — must keep working."""
    _seed_world(session, 5)
    for i in range(3):
        session.add(Item(
            name=f"Critical {i:03d}", current_stock=1, critical_point=10, material_code=f"CC-{i}",
        ))
    session.commit()
    client = _client_with(session, engine, monkeypatch)
    r = client.get("/inventory?stock_status=critical")
    assert r.status_code == 200
    assert "Critical 000" in r.text
    # Only 3 critical items — fewer than PAGE_SIZE, so no pagination block.
    assert "Page 1 of" not in r.text


def test_inventory_route_pagination_links_preserve_query_params(
    session: Session, engine, monkeypatch: pytest.MonkeyPatch,
):
    """Prev/Next must keep filters across navigation."""
    _seed_world(session, 5)
    for i in range(60):
        session.add(Item(
            name=f"Critical {i:03d}", current_stock=1, critical_point=10, material_code=f"CC-{i}",
        ))
    session.commit()
    client = _client_with(session, engine, monkeypatch)
    r = client.get("/inventory?stock_status=critical")
    assert r.status_code == 200
    # The Next link goes to page=2 AND carries stock_status=critical through.
    assert 'href="?page=2&amp;stock_status=critical"' in r.text


def test_inventory_route_lazy_loads_item_thumbs(
    session: Session, engine, monkeypatch: pytest.MonkeyPatch,
):
    """Item thumbnails must render with loading=\"lazy\" to avoid all-at-once
    image-fetch storms that historically pushed the response past proxy limits."""
    session.add(User(
        id=1, email="o@example.com", name="Owner",
        role=Role.OWNER, password_hash="x", is_active=True,
    ))
    session.add(Item(
        name="With image", current_stock=10, material_code="MC-IMG",
        image_path="some-image.jpg",
    ))
    session.commit()
    client = _client_with(session, engine, monkeypatch)
    r = client.get("/inventory")
    assert r.status_code == 200
    assert '/uploads/items/some-image.jpg' in r.text
    assert 'loading="lazy"' in r.text
