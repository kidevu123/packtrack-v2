"""v2.8.0: full metadata-driven Zoho item master-data editor.

Covers the editor end-to-end at the route level:

* owner sees editable brand/manufacturer/category/custom fields; non-owners see
  disabled controls and cannot POST
* metadata unavailable → read-only fallback
* server-side validation (category, dropdown options, numeric) before any call
* change detection: only changed fields are sent, unchanged → no call, multiple
  changes → one combined PATCH
* clearing custom fields sends ``""``; name/unit can't be cleared; category can't
  be cleared
* read-only / unknown fields are never sent
* PackTrack's derived ``product_line`` browsing group is never touched
* no direct Zoho/OAuth calls and no option-creation path

Outbound payload shape + sync-state transitions live in
``test_zoho_item_sync.py``; here we assert *what* the route decides to send (the
``payload`` handed to ``push_item_update``).
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
from packtrack.services.zoho_item_detail import (
    ExtendedItemDetail,
    build_custom_field_rows,
    resolve_master_data_changes,
)
from packtrack.services.zoho_item_sync import ItemPushResult

# Live-shaped item from the extended-detail test module (brand=FIX,
# manufacturer=Haute, category c1, custom cf_item_type=Packaging).
from tests.test_inventory_item_detail_extended import _ITEM

# --- metadata used for POST validation + editor rendering ------------------

_CUSTOM_DEFS = [
    {"api_name": "cf_item_type", "label": "Item Type", "field_type": "string",
     "is_dropdown": False, "policy": "writable"},
    {"api_name": "cf_product_line", "label": "Product Line", "field_type": "dropdown",
     "is_dropdown": True, "policy": "writable", "has_options": True,
     "options": [
         {"name": "7OH", "value_id": "1", "is_active": True},
         {"name": "MIT A", "value_id": "2", "is_active": True},
         {"name": "MIT B", "value_id": "3", "is_active": True},
     ]},
    {"api_name": "cf_description", "label": "Description", "field_type": "multiline",
     "is_dropdown": False, "policy": "writable"},
    {"api_name": "cf_unit_size", "label": "Unit Size", "field_type": "number",
     "is_dropdown": False, "policy": "writable"},
]
_CATS = [
    {"category_id": "c1", "name": "Packaging", "parent_category_id": "-1", "depth": 0},
    {"category_id": "c2", "name": "Master Case", "parent_category_id": "c1", "depth": 1},
]
_MD = {"ok": True, "metadata": {
    "custom_fields": _CUSTOM_DEFS, "categories": _CATS,
    "field_policy": {"name": "writable", "unit": "writable", "brand": "writable",
                     "manufacturer": "writable", "category_id": "writable"},
}}


# --- harness ---------------------------------------------------------------


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


def _seed(session, role: Role = Role.OWNER) -> Item:
    session.add(User(
        email=f"{role.value}@example.com", name=role.value.title(),
        role=role, password_hash="x", is_active=True,
    ))
    it = Item(
        name="FIX 5ct Master Case Box [Packaging]",
        material_code="MC-1", vendor="Acme", unit="Box", description="local desc",
        current_stock=42.0, reorder_point=10.0, critical_point=5.0,
        product_line=derive_product_line("FIX 5ct Master Case Box [Packaging]"),
        zoho_item_id="z-100",
    )
    session.add(it)
    session.commit()
    session.refresh(it)
    return it


def _extended(*, metadata_available: bool = True) -> ExtendedItemDetail:
    item = dict(_ITEM)
    md = _MD if metadata_available else None
    return ExtendedItemDetail(
        available=True,
        metadata_available=metadata_available,
        item=item,
        custom_fields=build_custom_field_rows(item, md),
        categories=_CATS if metadata_available else [],
        field_policy=_MD["metadata"]["field_policy"] if metadata_available else {},
    )


def _patch_render(monkeypatch, ext: ExtendedItemDetail):
    import packtrack.routes.inventory as inv

    monkeypatch.setattr(inv, "build_extended_detail", lambda _zid: ext)


def _patch_post(monkeypatch, *, ext: ExtendedItemDetail | None = None, metadata=_MD):
    """Wire the POST route's metadata + extended sources and capture pushes."""
    import packtrack.routes.inventory as inv

    ext = ext if ext is not None else _extended()
    monkeypatch.setattr(inv, "build_extended_detail", lambda _zid: ext)
    monkeypatch.setattr(inv, "fetch_metadata", lambda *a, **k: metadata)
    calls: list[dict] = []

    def _fake(session, item, *, payload, client=None):
        calls.append(payload)
        return ItemPushResult("synced")

    monkeypatch.setattr(inv, "push_item_update", _fake)
    return calls


def _baseline(it: Item) -> dict:
    """Form where every editable field equals its hidden original (no change)."""
    return {
        "name": it.name, "name__orig": it.name,
        "unit": it.unit, "unit__orig": it.unit,
        "description": it.description or "", "description__orig": it.description or "",
        "brand": "FIX", "brand__orig": "FIX",
        "manufacturer": "Haute", "manufacturer__orig": "Haute",
        "category_id": "c1", "category_id__orig": "c1",
        "cf_item_type": "Packaging", "cf_item_type__orig": "Packaging",
        "cf_product_line": "", "cf_product_line__orig": "",
        "cf_description": "", "cf_description__orig": "",
        "cf_unit_size": "", "cf_unit_size__orig": "",
        "material_code": it.material_code or "",
        "daily_usage_rate": "0", "reorder_point": "10", "critical_point": "5",
        "sea_lead_days": "0", "express_lead_days": "0",
    }


# --- rendering -------------------------------------------------------------


def test_owner_sees_editable_master_data_fields(session, engine, monkeypatch):
    it = _seed(session, Role.OWNER)
    _patch_render(monkeypatch, _extended())
    client = _client(session, engine, monkeypatch, Role.OWNER)
    body = client.get(f"/inventory/{it.id}").text
    # Standard editable controls + change-detection hidden originals.
    for key in ("name", "unit", "brand", "manufacturer", "category_id"):
        assert f'name="{key}"' in body
        assert f'name="{key}__orig"' in body
    # Category dropdown carries metadata categories, custom dropdown its options.
    assert "Master Case" in body
    assert '<select name="cf_product_line"' in body
    for opt in ("7OH", "MIT A", "MIT B"):
        assert opt in body


def test_non_owner_sees_disabled_fields(session, engine, monkeypatch):
    it = _seed(session, Role.AGENT)
    _patch_render(monkeypatch, _extended())
    client = _client(session, engine, monkeypatch, Role.AGENT)
    body = client.get(f"/inventory/{it.id}").text
    assert "disabled" in body
    assert "Read-only" in body
    # No hidden originals (nothing is submittable for a non-owner).
    assert 'name="brand__orig"' not in body
    assert "Save changes" not in body


def test_metadata_unavailable_renders_readonly_custom_fields(session, engine, monkeypatch):
    it = _seed(session, Role.OWNER)
    _patch_render(monkeypatch, _extended(metadata_available=False))
    client = _client(session, engine, monkeypatch, Role.OWNER)
    body = client.get(f"/inventory/{it.id}").text
    assert "metadata is unavailable" in body
    # The custom fields present on the item are shown but not editable.
    assert "cf_item_type__orig" not in body


# --- POST: change detection, validation, dispatch --------------------------


def test_no_change_avoids_service_call(session, engine, monkeypatch):
    it = _seed(session, Role.OWNER)
    calls = _patch_post(monkeypatch)
    client = _client(session, engine, monkeypatch, Role.OWNER)
    r = client.post(f"/inventory/{it.id}", data=_baseline(it), follow_redirects=False)
    assert r.status_code == 303
    assert calls == []


def test_brand_sent_only_when_changed(session, engine, monkeypatch):
    it = _seed(session, Role.OWNER)
    calls = _patch_post(monkeypatch)
    client = _client(session, engine, monkeypatch, Role.OWNER)
    client.post(f"/inventory/{it.id}",
                data={**_baseline(it), "brand": "Haute Brands"}, follow_redirects=False)
    assert calls == [{"brand": "Haute Brands"}]


def test_category_dropdown_sends_category_id(session, engine, monkeypatch):
    it = _seed(session, Role.OWNER)
    calls = _patch_post(monkeypatch)
    client = _client(session, engine, monkeypatch, Role.OWNER)
    client.post(f"/inventory/{it.id}",
                data={**_baseline(it), "category_id": "c2"}, follow_redirects=False)
    assert calls == [{"category_id": "c2"}]


def test_invalid_category_rejected_before_call(session, engine, monkeypatch):
    it = _seed(session, Role.OWNER)
    calls = _patch_post(monkeypatch)
    client = _client(session, engine, monkeypatch, Role.OWNER)
    r = client.post(f"/inventory/{it.id}",
                    data={**_baseline(it), "category_id": "zzz"}, follow_redirects=False)
    assert r.status_code == 200  # re-render with errors, no redirect
    assert calls == []
    assert "Choose a valid category" in r.text


def test_category_cannot_be_cleared(session, engine, monkeypatch):
    it = _seed(session, Role.OWNER)
    calls = _patch_post(monkeypatch)
    client = _client(session, engine, monkeypatch, Role.OWNER)
    r = client.post(f"/inventory/{it.id}",
                    data={**_baseline(it), "category_id": ""}, follow_redirects=False)
    assert r.status_code == 200
    assert calls == []
    assert "can&#39;t be cleared" in r.text or "can't be cleared" in r.text


def test_custom_freetext_sent(session, engine, monkeypatch):
    it = _seed(session, Role.OWNER)
    calls = _patch_post(monkeypatch)
    client = _client(session, engine, monkeypatch, Role.OWNER)
    client.post(f"/inventory/{it.id}",
                data={**_baseline(it), "cf_item_type": "Component"}, follow_redirects=False)
    assert calls == [{"custom_fields": {"cf_item_type": "Component"}}]


def test_custom_dropdown_sent(session, engine, monkeypatch):
    it = _seed(session, Role.OWNER)
    calls = _patch_post(monkeypatch)
    client = _client(session, engine, monkeypatch, Role.OWNER)
    client.post(f"/inventory/{it.id}",
                data={**_baseline(it), "cf_product_line": "MIT A"}, follow_redirects=False)
    assert calls == [{"custom_fields": {"cf_product_line": "MIT A"}}]


def test_custom_dropdown_invalid_option_rejected(session, engine, monkeypatch):
    it = _seed(session, Role.OWNER)
    calls = _patch_post(monkeypatch)
    client = _client(session, engine, monkeypatch, Role.OWNER)
    r = client.post(f"/inventory/{it.id}",
                    data={**_baseline(it), "cf_product_line": "BOGUS"}, follow_redirects=False)
    assert r.status_code == 200
    assert calls == []


def test_custom_dropdown_clear_sends_empty(session, engine, monkeypatch):
    it = _seed(session, Role.OWNER)
    calls = _patch_post(monkeypatch)
    client = _client(session, engine, monkeypatch, Role.OWNER)
    client.post(
        f"/inventory/{it.id}",
        data={**_baseline(it), "cf_product_line": "", "cf_product_line__orig": "MIT A"},
        follow_redirects=False,
    )
    assert calls == [{"custom_fields": {"cf_product_line": ""}}]


def test_numeric_custom_valid_sent(session, engine, monkeypatch):
    it = _seed(session, Role.OWNER)
    calls = _patch_post(monkeypatch)
    client = _client(session, engine, monkeypatch, Role.OWNER)
    client.post(f"/inventory/{it.id}",
                data={**_baseline(it), "cf_unit_size": "12.5"}, follow_redirects=False)
    assert calls == [{"custom_fields": {"cf_unit_size": "12.5"}}]


def test_numeric_custom_invalid_rejected(session, engine, monkeypatch):
    it = _seed(session, Role.OWNER)
    calls = _patch_post(monkeypatch)
    client = _client(session, engine, monkeypatch, Role.OWNER)
    r = client.post(f"/inventory/{it.id}",
                    data={**_baseline(it), "cf_unit_size": "abc"}, follow_redirects=False)
    assert r.status_code == 200
    assert calls == []
    assert "Must be a number" in r.text


def test_mixed_changes_produce_one_combined_patch(session, engine, monkeypatch):
    it = _seed(session, Role.OWNER)
    calls = _patch_post(monkeypatch)
    client = _client(session, engine, monkeypatch, Role.OWNER)
    client.post(
        f"/inventory/{it.id}",
        data={**_baseline(it), "brand": "B", "category_id": "c2",
              "cf_product_line": "MIT B", "cf_item_type": "Component"},
        follow_redirects=False,
    )
    assert len(calls) == 1
    assert calls[0] == {
        "brand": "B", "category_id": "c2",
        "custom_fields": {"cf_product_line": "MIT B", "cf_item_type": "Component"},
    }


def test_name_cannot_be_cleared(session, engine, monkeypatch):
    it = _seed(session, Role.OWNER)
    calls = _patch_post(monkeypatch)
    client = _client(session, engine, monkeypatch, Role.OWNER)
    r = client.post(f"/inventory/{it.id}",
                    data={**_baseline(it), "name": ""}, follow_redirects=False)
    assert r.status_code == 200
    assert calls == []
    session.expire_all()
    assert session.get(Item, it.id).name == it.name  # unchanged


def test_name_change_pushed_and_mirrored_locally(session, engine, monkeypatch):
    it = _seed(session, Role.OWNER)
    calls = _patch_post(monkeypatch)
    client = _client(session, engine, monkeypatch, Role.OWNER)
    client.post(f"/inventory/{it.id}",
                data={**_baseline(it), "name": "New Name [Packaging]"},
                follow_redirects=False)
    assert calls == [{"name": "New Name [Packaging]"}]
    session.expire_all()
    assert session.get(Item, it.id).name == "New Name [Packaging]"


def test_metadata_unavailable_rejects_custom_change(session, engine, monkeypatch):
    it = _seed(session, Role.OWNER)
    calls = _patch_post(monkeypatch, ext=_extended(metadata_available=False), metadata=None)
    client = _client(session, engine, monkeypatch, Role.OWNER)
    r = client.post(f"/inventory/{it.id}",
                    data={**_baseline(it), "cf_item_type": "Component"},
                    follow_redirects=False)
    assert r.status_code == 200
    assert calls == []


def test_derived_product_line_unchanged_by_cf_edit(session, engine, monkeypatch):
    it = _seed(session, Role.OWNER)
    assert it.product_line == "FIX"
    _patch_post(monkeypatch)
    client = _client(session, engine, monkeypatch, Role.OWNER)
    client.post(f"/inventory/{it.id}",
                data={**_baseline(it), "cf_product_line": "MIT A"}, follow_redirects=False)
    session.expire_all()
    assert session.get(Item, it.id).product_line == "FIX"


def test_non_owner_cannot_post(session, engine, monkeypatch):
    it = _seed(session, Role.AGENT)
    calls = _patch_post(monkeypatch)
    client = _client(session, engine, monkeypatch, Role.AGENT)
    r = client.post(f"/inventory/{it.id}",
                    data={**_baseline(it), "brand": "X"}, follow_redirects=False)
    assert r.status_code == 403
    assert calls == []


# --- resolver unit tests (pure) --------------------------------------------


def _resolve(submitted, originals, *, metadata=_MD, categories=_CATS):
    return resolve_master_data_changes(
        metadata=metadata, categories=categories,
        submitted=submitted, originals=originals,
    )


def test_resolver_only_changed_fields():
    res = _resolve(
        {"brand": "New", "name": "Same"}, {"brand": "Old", "name": "Same"},
    )
    assert res.payload == {"brand": "New"}
    assert res.errors == {}
    assert res.changed is True


def test_resolver_readonly_keys_absent_are_never_sent():
    # selling_price isn't a recognized key → it's simply not in submitted, so
    # it can never appear in the payload.
    res = _resolve({"brand": "New"}, {"brand": "Old"})
    assert "selling_price" not in res.payload
    assert set(res.payload) <= {"name", "unit", "description", "brand",
                                "manufacturer", "category_id", "custom_fields"}


def test_resolver_dropdown_case_insensitive_canonicalizes():
    res = _resolve({"cf_product_line": "mit a"}, {"cf_product_line": ""})
    assert res.payload == {"custom_fields": {"cf_product_line": "MIT A"}}


def test_resolver_numeric_rejects_non_number():
    res = _resolve({"cf_unit_size": "x"}, {"cf_unit_size": ""})
    assert res.payload == {}
    assert "cf_unit_size" in res.errors


# --- boundary: no direct Zoho / no option creation -------------------------


def test_no_option_creation_or_direct_zoho():
    import pathlib

    import packtrack.routes.inventory as inv
    import packtrack.services.zoho_item_sync as zis

    for mod in (inv, zis):
        src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
        assert "zohoapis" not in src
        assert "refresh_token" not in src
        assert "create_option" not in src
        assert "add_option" not in src
