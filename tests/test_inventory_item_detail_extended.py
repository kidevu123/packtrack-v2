"""v2.6.0 richer item detail: read-only extended Zoho detail + metadata.

Covers the read-only display path (no Zoho writes in this phase):

* extended detail renders when the service succeeds (dropdowns, accounting)
* the page still renders when the extended fetch fails (graceful degradation)
* metadata produces a *disabled* dropdown for ``cf_product_line``
* PackTrack's derived ``product_line`` is never overwritten by Zoho's
  ``cf_product_line``
* read-only / unknown form fields cannot mutate the item
* the new client module never imports/calls Zoho directly
* no custom-field keys are ever included in the outbound PATCH payload
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("PACKTRACK_SECRET_KEY", "test-secret")

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from packtrack.models import Item, Role, User
from packtrack.services import zoho_item_detail as zid
from packtrack.services.product_line import derive_product_line
from packtrack.services.zoho_item_detail import (
    ExtendedItemDetail,
    build_custom_field_rows,
    build_extended_detail,
    fetch_item_detail,
    fetch_metadata,
)

# --- sample service payloads (mirror live v1.31.0 shapes) -------------------

_METADATA = {
    "ok": True,
    "brand": "haute_brands",
    "metadata": {
        "custom_fields": [
            {
                "api_name": "cf_item_type", "label": "Item Type",
                "field_type": "string", "is_dropdown": False,
                "has_options": False, "options": None,
            },
            {
                "api_name": "cf_product_line", "label": "Product Line",
                "field_type": "dropdown", "is_dropdown": True,
                "has_options": True,
                "options": [
                    {"value_id": "1", "name": "7OH", "is_active": True},
                    {"value_id": "2", "name": "MIT A", "is_active": True},
                    {"value_id": "3", "name": "MIT B", "is_active": True},
                ],
            },
            {
                "api_name": "cf_description", "label": "Description",
                "field_type": "multiline", "is_dropdown": False,
            },
        ],
        "categories": [],
        "reporting_tags": [],
        "units": None,
        "field_policy": {"name": "writable", "selling_price": "read_only"},
    },
    "meta": {
        "cache_ttl_seconds": 3600,
        "warnings": [{"label": "units", "source": "/settings/units", "status": 404}],
    },
}

_ITEM = {
    "item_id": "z-100",
    "name": "FIX 5ct Master Case Box [Packaging]",
    "sku": "FIX-5CT",
    "unit": "Box",
    "description": "Zoho standard description here",
    "brand": "FIX",
    "manufacturer": "Haute",
    "category": {"category_id": "c1", "category_name": "Packaging", "parent_category_id": "-1"},
    "preferred_vendor": "Acme Co",
    "selling_price": 0.0,
    "cost_price": 1.25,
    "sales_account": {"account_id": "1", "account_name": "Sales"},
    "purchase_account": {"account_id": "2", "account_name": "Cost of Goods Sold"},
    "inventory_account": {"account_id": "3", "account_name": "Raw Materials"},
    "valuation_method": "fifo",
    "current_stock": -10.0,
    "available_stock": -10.0,
    "reorder_point": None,
    "reporting_tags": [],
    "image": {"image_id": None, "image_name": "box.png", "image_type": "png"},
    "custom_fields": {
        "cf_item_type": {
            "api_name": "cf_item_type", "label": "Item Type",
            "value": "Packaging", "is_dropdown": False,
        },
    },
}


# --- fixtures / harness (mirrors test_inventory_detail.py) ------------------


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


def _seed(session, role: Role = Role.OWNER, zoho_item_id: str | None = "z-100") -> Item:
    session.add(User(
        email=f"{role.value}@example.com", name=role.value.title(),
        role=role, password_hash="x", is_active=True,
    ))
    it = Item(
        name="FIX 5ct Master Case Box [Packaging]",
        material_code="MC-1", vendor="Acme", unit="Box",
        current_stock=42.0, reorder_point=10.0, critical_point=5.0,
        product_line=derive_product_line("FIX 5ct Master Case Box [Packaging]"),
        zoho_item_id=zoho_item_id,
    )
    session.add(it)
    session.commit()
    session.refresh(it)
    return it


def _full_extended() -> ExtendedItemDetail:
    return ExtendedItemDetail(
        available=True,
        metadata_available=True,
        item=_ITEM,
        custom_fields=build_custom_field_rows(_ITEM, _METADATA),
        warnings=["Zoho metadata: “units” unavailable"],
    )


# --- route rendering -------------------------------------------------------


def test_detail_renders_with_extended_success(session, engine, monkeypatch):
    it = _seed(session, Role.OWNER)
    import packtrack.routes.inventory as inv
    monkeypatch.setattr(inv, "build_extended_detail", lambda _zid: _full_extended())
    client = _client(session, engine, monkeypatch, Role.OWNER)
    r = client.get(f"/inventory/{it.id}")
    assert r.status_code == 200
    body = r.text
    assert "Primary details" in body
    assert "Packaging &amp; custom fields" in body
    assert "Zoho accounting &amp; inventory" in body
    # Standard description vs custom-field description are labeled distinctly.
    assert "Standard description" in body
    # Accounting values are present and read-only.
    assert "Cost of Goods Sold" in body
    assert "FIFO" in body


def test_extended_dropdown_is_disabled_with_zoho_options(session, engine, monkeypatch):
    it = _seed(session, Role.OWNER)
    import packtrack.routes.inventory as inv
    monkeypatch.setattr(inv, "build_extended_detail", lambda _zid: _full_extended())
    client = _client(session, engine, monkeypatch, Role.OWNER)
    body = client.get(f"/inventory/{it.id}").text
    # cf_product_line renders as a disabled <select> exposing the Zoho options.
    assert "<select disabled" in body
    assert "Zoho Product Line" in body
    for opt in ("7OH", "MIT A", "MIT B"):
        assert opt in body


def test_detail_still_renders_when_extended_fails(session, engine, monkeypatch):
    it = _seed(session, Role.OWNER)
    import packtrack.routes.inventory as inv
    failed = ExtendedItemDetail(
        available=False, warnings=["Zoho extended details unavailable."]
    )
    monkeypatch.setattr(inv, "build_extended_detail", lambda _zid: failed)
    client = _client(session, engine, monkeypatch, Role.OWNER)
    r = client.get(f"/inventory/{it.id}")
    assert r.status_code == 200
    body = r.text
    # Local page still renders, with a small operator note (not a hard error).
    assert "Zoho extended details unavailable." in body
    assert "Edit item" in body  # local edit form unaffected
    assert "Primary details" not in body  # no extended section when unavailable


def test_packtrack_product_line_not_overwritten_by_zoho_cf(session, engine, monkeypatch):
    it = _seed(session, Role.OWNER)
    assert it.product_line == "FIX"
    import packtrack.routes.inventory as inv
    monkeypatch.setattr(inv, "build_extended_detail", lambda _zid: _full_extended())
    client = _client(session, engine, monkeypatch, Role.OWNER)
    body = client.get(f"/inventory/{it.id}").text
    # The derived browsing group is shown distinctly from Zoho's custom field...
    assert "Browsing group" in body
    assert "Zoho Product Line" in body
    # ...and the DB value is untouched by viewing the extended detail.
    session.expire_all()
    assert session.get(Item, it.id).product_line == "FIX"


def test_readonly_and_unknown_fields_cannot_mutate_item(session, engine, monkeypatch):
    """Posting accounting / custom-field keys must not change the item — the
    update route only accepts name/description/unit + PackTrack-owned fields."""
    it = _seed(session, Role.OWNER)
    client = _client(session, engine, monkeypatch, Role.OWNER)
    r = client.post(
        f"/inventory/{it.id}",
        data={
            "name": it.name,
            "description": "",
            "material_code": it.material_code,
            "unit": it.unit,
            "daily_usage_rate": "0",
            "reorder_point": "10",
            "critical_point": "5",
            "sea_lead_days": "0",
            "express_lead_days": "0",
            # Attempted read-only / custom-field writes — all must be ignored.
            "selling_price": "999",
            "cost_price": "999",
            "cf_product_line": "MIT A",
            "valuation_method": "lifo",
            "current_stock": "9999",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    session.expire_all()
    fresh = session.get(Item, it.id)
    assert fresh.current_stock == 42.0  # stock never editable
    assert fresh.product_line == "FIX"  # not touched by cf_product_line
    # No Zoho-owned field changed → no pending push created.
    assert fresh.zoho_push_status is None


# --- service builder unit tests --------------------------------------------


def test_build_custom_field_rows_merges_defs_and_values():
    rows = build_custom_field_rows(_ITEM, _METADATA)
    by_name = {r.api_name: r for r in rows}
    # All three metadata-defined fields appear even though only one is set.
    assert set(by_name) == {"cf_item_type", "cf_product_line", "cf_description"}
    assert by_name["cf_item_type"].value == "Packaging"
    assert by_name["cf_item_type"].is_set is True
    pl = by_name["cf_product_line"]
    assert pl.is_dropdown is True
    assert pl.options == ["7OH", "MIT A", "MIT B"]
    assert pl.value is None and pl.is_set is False
    # cf_product_line is ordered before cf_description (per display order).
    names = [r.api_name for r in rows]
    assert names.index("cf_product_line") < names.index("cf_description")


def test_build_custom_field_rows_flags_value_not_in_options():
    item = dict(_ITEM)
    item["custom_fields"] = {
        "cf_product_line": {"api_name": "cf_product_line", "value": "LEGACY"},
    }
    rows = {r.api_name: r for r in build_custom_field_rows(item, _METADATA)}
    pl = rows["cf_product_line"]
    assert pl.value == "LEGACY"
    assert pl.value_in_options is False


def test_build_custom_field_rows_without_metadata_uses_item_only():
    rows = build_custom_field_rows(_ITEM, None)
    # Falls back to fields actually present on the item, as plain rows.
    assert [r.api_name for r in rows] == ["cf_item_type"]
    assert rows[0].is_dropdown is False


# --- graceful failure + caching (MockTransport, no real network) -----------


def _configure(monkeypatch):
    monkeypatch.setattr(zid.settings, "ZOHO_INTEGRATION_BASE_URL", "https://svc.example")
    monkeypatch.setattr(zid.settings, "ZOHO_INTEGRATION_APP_TOKEN", "tok")
    monkeypatch.setattr(zid.settings, "ZOHO_INTEGRATION_BRAND", "haute_brands")


def test_fetch_item_detail_returns_none_on_error(monkeypatch):
    _configure(monkeypatch)

    def handler(request):
        return httpx.Response(500, json={"ok": False})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert fetch_item_detail("z-100", client=client) is None


def test_fetch_item_detail_returns_item_on_success(monkeypatch):
    _configure(monkeypatch)

    def handler(request):
        assert request.headers["Authorization"] == "Bearer tok"
        assert request.headers["X-Brand"] == "haute_brands"
        return httpx.Response(200, json={"ok": True, "item": _ITEM})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    item = fetch_item_detail("z-100", client=client)
    assert item is not None and item["item_id"] == "z-100"


def test_fetch_metadata_caches_within_ttl(monkeypatch):
    _configure(monkeypatch)
    zid.reset_metadata_cache()
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json=_METADATA)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    first = fetch_metadata(client=client)
    second = fetch_metadata(client=client)
    assert first is not None and second is not None
    assert calls["n"] == 1  # second call served from cache
    zid.reset_metadata_cache()


def test_build_extended_detail_unavailable_when_item_fetch_fails(monkeypatch):
    _configure(monkeypatch)
    zid.reset_metadata_cache()
    monkeypatch.setattr(zid, "fetch_item_detail", lambda *a, **k: None)
    monkeypatch.setattr(zid, "fetch_metadata", lambda *a, **k: None)
    out = build_extended_detail("z-100")
    assert out.available is False
    assert "Zoho extended details unavailable." in out.warnings


# --- boundary guard: no direct Zoho calls in this phase --------------------


def test_no_direct_zoho_calls_in_detail_client():
    src = Path(zid.__file__).read_text(encoding="utf-8")
    assert "zohoapis" not in src
    assert "from packtrack.zoho" not in src
    assert "import packtrack.zoho" not in src
    # Uses the integration-service config, not Zoho creds directly.
    assert "ZOHO_INTEGRATION_BASE_URL" in src


def test_outbound_patch_payload_has_no_custom_fields():
    from packtrack.services.zoho_item_sync import _outbound_payload

    it = Item(name="X", unit="ea", description=None)
    payload = _outbound_payload(it)
    assert set(payload.keys()) == {"name", "description", "unit"}
    assert not any(k.startswith("cf_") for k in payload)
