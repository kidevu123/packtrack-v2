"""v2.7.0: editing the Zoho ``cf_product_line`` dropdown from item detail.

This is the only editable Zoho custom field. Covers the route-level workflow:
owner vs non-owner rendering, metadata fallback, server-side validation,
change-detection, clearing, and the strict separation from PackTrack's derived
``product_line`` browsing group. Payload shape + sync state transitions are
covered in ``test_zoho_item_sync.py``; here we assert *what* the route decides
to send (the ``cf_product_line`` value passed to ``push_item_update``).
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("PACKTRACK_SECRET_KEY", "test-secret")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from packtrack.models import Item, Role, User
from packtrack.services import zoho_item_detail as zid
from packtrack.services.product_line import derive_product_line
from packtrack.services.zoho_item_detail import ExtendedItemDetail, build_custom_field_rows
from packtrack.services.zoho_item_sync import ItemPushResult

# Reuse the live-shaped service payloads from the extended-detail test module.
from tests.test_inventory_item_detail_extended import _ITEM, _METADATA

_OPTIONS = ["7OH", "MIT A", "MIT B"]


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
    zid.reset_metadata_cache()


def _client(session, engine, monkeypatch, role: Role) -> TestClient:
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


def _seed(session, role: Role = Role.OWNER, cf_value: str | None = None) -> Item:
    session.add(User(
        email=f"{role.value}@example.com", name=role.value.title(),
        role=role, password_hash="x", is_active=True,
    ))
    it = Item(
        name="FIX 5ct Master Case Box [Packaging]",
        material_code="MC-1", vendor="Acme", unit="Box",
        description="local desc",
        current_stock=42.0, reorder_point=10.0, critical_point=5.0,
        product_line=derive_product_line("FIX 5ct Master Case Box [Packaging]"),
        zoho_item_id="z-100",
    )
    session.add(it)
    session.commit()
    session.refresh(it)
    return it


def _extended(cf_value: str | None = None) -> ExtendedItemDetail:
    """Full extended detail with cf_product_line optionally set on the item."""
    item = dict(_ITEM)
    item["custom_fields"] = dict(_ITEM["custom_fields"])
    if cf_value is not None:
        item["custom_fields"]["cf_product_line"] = {
            "api_name": "cf_product_line", "value": cf_value,
        }
    return ExtendedItemDetail(
        available=True,
        metadata_available=True,
        item=item,
        custom_fields=build_custom_field_rows(item, _METADATA),
        warnings=[],
    )


def _patch_render(monkeypatch, ext: ExtendedItemDetail):
    import packtrack.routes.inventory as inv

    monkeypatch.setattr(inv, "build_extended_detail", lambda _zid: ext)


def _patch_options(monkeypatch, options):
    """Stub the server-side validation source (metadata options)."""
    import packtrack.routes.inventory as inv

    monkeypatch.setattr(inv, "product_line_options", lambda *a, **k: options)


def _capture_push(monkeypatch):
    """Capture the cf_product_line value the route hands to push_item_update."""
    import packtrack.routes.inventory as inv

    calls: list[dict] = []

    def _fake(session, item, *, cf_product_line=None, client=None):
        calls.append({"cf_product_line": cf_product_line, "item_id": item.id})
        return ItemPushResult("synced")

    monkeypatch.setattr(inv, "push_item_update", _fake)
    return calls


def _form(it: Item, **overrides) -> dict:
    data = {
        "name": it.name,
        "description": it.description or "",
        "material_code": it.material_code or "",
        "unit": it.unit,
        "daily_usage_rate": str(it.daily_usage_rate),
        "reorder_point": str(int(it.reorder_point)),
        "critical_point": str(int(it.critical_point)),
        "sea_lead_days": str(it.sea_lead_days),
        "express_lead_days": str(it.express_lead_days),
    }
    data.update(overrides)
    return data


# --- rendering -------------------------------------------------------------


def test_owner_sees_editable_dropdown_with_metadata_options(session, engine, monkeypatch):
    it = _seed(session, Role.OWNER)
    _patch_render(monkeypatch, _extended())
    client = _client(session, engine, monkeypatch, Role.OWNER)
    body = client.get(f"/inventory/{it.id}").text
    assert '<select name="cf_product_line"' in body
    assert 'name="cf_product_line_original"' in body
    assert "— Not set —" in body
    for opt in _OPTIONS:
        assert opt in body


def test_non_owner_sees_readonly_disabled_dropdown(session, engine, monkeypatch):
    it = _seed(session, Role.AGENT)
    _patch_render(monkeypatch, _extended(cf_value="MIT A"))
    client = _client(session, engine, monkeypatch, Role.AGENT)
    body = client.get(f"/inventory/{it.id}").text
    # No editable control for non-owners; the read-only mirror shows it disabled.
    assert '<select name="cf_product_line"' not in body
    assert "<select disabled" in body
    assert "MIT A" in body


def test_metadata_unavailable_renders_readonly_fallback(session, engine, monkeypatch):
    it = _seed(session, Role.OWNER)
    # Item fetched but metadata missing → cf row is a plain (non-dropdown) row.
    item = dict(_ITEM)
    item["custom_fields"] = {
        "cf_product_line": {"api_name": "cf_product_line", "value": "MIT B"},
    }
    ext = ExtendedItemDetail(
        available=True, metadata_available=False, item=item,
        custom_fields=build_custom_field_rows(item, None),
        warnings=["Zoho field metadata unavailable — labels and dropdown options are limited."],
    )
    _patch_render(monkeypatch, ext)
    client = _client(session, engine, monkeypatch, Role.OWNER)
    body = client.get(f"/inventory/{it.id}").text
    # No editable dropdown; current value shown read-only with a soft warning.
    assert '<select name="cf_product_line"' not in body
    assert "MIT B" in body
    assert "Zoho options are unavailable right now" in body


# --- POST: validation, change-detection, dispatch --------------------------


def test_valid_option_is_sent_as_cf_product_line(session, engine, monkeypatch):
    it = _seed(session, Role.OWNER)
    _patch_options(monkeypatch, _OPTIONS)
    calls = _capture_push(monkeypatch)
    client = _client(session, engine, monkeypatch, Role.OWNER)
    r = client.post(
        f"/inventory/{it.id}",
        data=_form(it, cf_product_line="MIT A", cf_product_line_original=""),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert calls == [{"cf_product_line": "MIT A", "item_id": it.id}]


def test_clearing_sends_empty_string(session, engine, monkeypatch):
    it = _seed(session, Role.OWNER)
    _patch_options(monkeypatch, _OPTIONS)
    calls = _capture_push(monkeypatch)
    client = _client(session, engine, monkeypatch, Role.OWNER)
    client.post(
        f"/inventory/{it.id}",
        data=_form(it, cf_product_line="", cf_product_line_original="MIT A"),
        follow_redirects=False,
    )
    assert calls == [{"cf_product_line": "", "item_id": it.id}]


def test_unchanged_value_is_not_sent(session, engine, monkeypatch):
    it = _seed(session, Role.OWNER)
    _patch_options(monkeypatch, _OPTIONS)
    calls = _capture_push(monkeypatch)
    client = _client(session, engine, monkeypatch, Role.OWNER)
    r = client.post(
        f"/inventory/{it.id}",
        data=_form(it, cf_product_line="MIT A", cf_product_line_original="MIT A"),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert calls == []  # nothing changed → no service call at all


def test_invalid_option_rejected_before_service_call(session, engine, monkeypatch):
    it = _seed(session, Role.OWNER)
    _patch_options(monkeypatch, _OPTIONS)
    calls = _capture_push(monkeypatch)
    client = _client(session, engine, monkeypatch, Role.OWNER)
    r = client.post(
        f"/inventory/{it.id}",
        data=_form(it, cf_product_line="BOGUS", cf_product_line_original=""),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].endswith("saved=cf_invalid")
    assert calls == []  # never sent to the service


def test_metadata_unavailable_post_does_not_send(session, engine, monkeypatch):
    it = _seed(session, Role.OWNER)
    _patch_options(monkeypatch, None)  # metadata unavailable → can't validate
    calls = _capture_push(monkeypatch)
    client = _client(session, engine, monkeypatch, Role.OWNER)
    r = client.post(
        f"/inventory/{it.id}",
        data=_form(it, cf_product_line="MIT A", cf_product_line_original=""),
        follow_redirects=False,
    )
    assert r.headers["location"].endswith("saved=cf_unavailable")
    assert calls == []


def test_scalar_change_still_pushes_without_cf(session, engine, monkeypatch):
    """Editing only name pushes scalars; cf_product_line stays None (not sent)."""
    it = _seed(session, Role.OWNER)
    _patch_options(monkeypatch, _OPTIONS)
    calls = _capture_push(monkeypatch)
    client = _client(session, engine, monkeypatch, Role.OWNER)
    client.post(
        f"/inventory/{it.id}",
        data=_form(it, name="New Name", cf_product_line="MIT A",
                   cf_product_line_original="MIT A"),
        follow_redirects=False,
    )
    assert calls == [{"cf_product_line": None, "item_id": it.id}]


def test_derived_product_line_unchanged_by_cf_edit(session, engine, monkeypatch):
    it = _seed(session, Role.OWNER)
    assert it.product_line == "FIX"
    _patch_options(monkeypatch, _OPTIONS)
    _capture_push(monkeypatch)
    client = _client(session, engine, monkeypatch, Role.OWNER)
    client.post(
        f"/inventory/{it.id}",
        data=_form(it, cf_product_line="MIT A", cf_product_line_original=""),
        follow_redirects=False,
    )
    session.expire_all()
    assert session.get(Item, it.id).product_line == "FIX"


def test_non_owner_cannot_edit_cf_product_line(session, engine, monkeypatch):
    it = _seed(session, Role.AGENT)
    _patch_options(monkeypatch, _OPTIONS)
    calls = _capture_push(monkeypatch)
    client = _client(session, engine, monkeypatch, Role.AGENT)
    r = client.post(
        f"/inventory/{it.id}",
        data=_form(it, cf_product_line="MIT A", cf_product_line_original=""),
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert calls == []


# --- boundary: no new direct Zoho / option-creation paths ------------------


def test_no_option_creation_or_direct_zoho_in_route_and_client():
    import packtrack.routes.inventory as inv

    route_src = __import__("pathlib").Path(inv.__file__).read_text(encoding="utf-8")
    sync_src = __import__("pathlib").Path(
        __import__("packtrack.services.zoho_item_sync", fromlist=["__file__"]).__file__
    ).read_text(encoding="utf-8")
    for src in (route_src, sync_src):
        # No direct Zoho API/OAuth usage was introduced.
        assert "zohoapis" not in src
        assert "refresh_token" not in src
        # No dropdown-option creation path (service owns options; we never add).
        assert "create_option" not in src
        assert "add_option" not in src
