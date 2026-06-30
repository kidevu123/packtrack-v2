"""v2.16.2: input-affordance polish on /inventory/{id}.

Editable inputs across the master-data editor must be visually obvious:
explicit border, white background, hover/focus state. Read-only/disabled
controls keep stone-50 + muted text so they cannot be confused with editable
ones. The data-pt-editable attribute is the test hook.

Assertions are class- and attribute-level — we don't render real CSS — so the
class names below MUST match `inventory_detail.html` exactly. If you rename
the affordance utility classes there, update the EDITABLE_AFFORDANCES /
READONLY_AFFORDANCES tuples below.
"""
from __future__ import annotations

import os
import re

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
)
from tests.test_inventory_item_detail_extended import _ITEM
from tests.test_inventory_masterdata_editor import _CATS, _MD

EDITABLE_AFFORDANCES = (
    "border",
    "bg-white",
    "border-stone-300",
    "hover:border-stone-400",
    "focus:border-stone-500",
    "focus:ring-2",
)
READONLY_AFFORDANCES = (
    "bg-stone-50",
    "border-stone-200",
    "text-stone-500",
    "cursor-not-allowed",
)


# --- fixtures (mirror test_inventory_masterdata_editor.py) ----------------


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


def _seed(session, role: Role = Role.OWNER, *, zoho_id: str | None = "z-100") -> Item:
    session.add(User(
        email=f"{role.value}-aff@example.com", name=role.value.title(),
        role=role, password_hash="x", is_active=True,
    ))
    it = Item(
        name="FIX 5ct Master Case Box [Packaging]",
        material_code="MC-1", vendor="Acme", unit="Box", description="local desc",
        current_stock=42.0, reorder_point=10.0, critical_point=5.0,
        product_line=derive_product_line("FIX 5ct Master Case Box [Packaging]"),
        zoho_item_id=zoho_id,
    )
    session.add(it)
    session.commit()
    session.refresh(it)
    return it


def _extended() -> ExtendedItemDetail:
    item = dict(_ITEM)
    return ExtendedItemDetail(
        available=True,
        metadata_available=True,
        item=item,
        custom_fields=build_custom_field_rows(item, _MD),
        categories=_CATS,
        field_policy=_MD["metadata"]["field_policy"],
    )


def _patch_render(monkeypatch, ext: ExtendedItemDetail):
    import packtrack.routes.inventory as inv

    monkeypatch.setattr(inv, "build_extended_detail", lambda _zid: ext)


# --- the only regex we need: pull every <input|select|textarea> tag --------

_TAG_RE = re.compile(r"<(input|select|textarea)\b[^>]*>", re.IGNORECASE)


def _form_tags(body: str) -> list[str]:
    return _TAG_RE.findall(body) and [m.group(0) for m in _TAG_RE.finditer(body)]


# --- tests -----------------------------------------------------------------


def test_owner_editable_inputs_have_visible_affordance(session, engine, monkeypatch):
    """Brand, manufacturer, description, custom fields all carry the editable
    affordance utilities (border, white bg, hover, focus ring)."""
    it = _seed(session, Role.OWNER)
    _patch_render(monkeypatch, _extended())
    client = _client(session, engine, monkeypatch, Role.OWNER)
    body = client.get(f"/inventory/{it.id}").text

    editable_tags = [t for t in _form_tags(body) if 'data-pt-editable="true"' in t]
    # The editor has at least: name, unit, description (textarea), brand,
    # manufacturer, category_id, plus 4 custom fields, plus operational inputs
    # (material_code, daily_usage_rate, reorder, critical, sea_lead, express_lead).
    # 16+ editable controls — concretely assert > 10 so trivial regressions fail.
    assert len(editable_tags) > 10, f"only {len(editable_tags)} editable inputs"
    for tag in editable_tags:
        for cls in EDITABLE_AFFORDANCES:
            assert cls in tag, f"editable input missing {cls!r}: {tag}"


def test_owner_editable_inputs_cover_each_input_kind(session, engine, monkeypatch):
    """At least one <input>, one <select>, and one <textarea> must carry the
    editable affordance — spec calls out text/select/textarea/numeric/empty."""
    it = _seed(session, Role.OWNER)
    _patch_render(monkeypatch, _extended())
    client = _client(session, engine, monkeypatch, Role.OWNER)
    body = client.get(f"/inventory/{it.id}").text

    editable = [t for t in _form_tags(body) if 'data-pt-editable="true"' in t]
    kinds = {t.split()[0].lstrip("<").lower() for t in editable}
    assert "input" in kinds and "select" in kinds and "textarea" in kinds, kinds


def test_empty_custom_field_still_renders_visible_box(session, engine, monkeypatch):
    """The spec calls out empty editable custom fields. value="" but the
    editable affordance utilities must still be on the tag — that is what gives
    the empty field a visible box."""
    it = _seed(session, Role.OWNER)
    _patch_render(monkeypatch, _extended())
    client = _client(session, engine, monkeypatch, Role.OWNER)
    body = client.get(f"/inventory/{it.id}").text

    # cf_product_line/cf_description/cf_unit_size start empty on the seeded item.
    empty_keys = ("cf_description", "cf_unit_size")
    for key in empty_keys:
        tag = next(
            (t for t in _form_tags(body) if f'name="{key}"' in t), None,
        )
        assert tag is not None, f"missing editable input for {key}"
        assert 'data-pt-editable="true"' in tag
        assert 'value=""' in tag or ">" + "</textarea>" in body  # empty value
        for cls in EDITABLE_AFFORDANCES:
            assert cls in tag, f"empty {key} missing affordance {cls!r}"


def test_disabled_vendor_uses_readonly_affordance(session, engine, monkeypatch):
    """Vendor is read-only when item has a zoho_item_id — it must keep the
    visually-muted readonly styling so it doesn't look editable."""
    it = _seed(session, Role.OWNER, zoho_id="z-100")
    _patch_render(monkeypatch, _extended())
    client = _client(session, engine, monkeypatch, Role.OWNER)
    body = client.get(f"/inventory/{it.id}").text

    vendor_tag = next(
        (t for t in _form_tags(body)
         if 'value="Acme"' in t and "disabled" in t),
        None,
    )
    assert vendor_tag is not None, "disabled vendor input not found"
    assert 'data-pt-editable="false"' in vendor_tag
    for cls in READONLY_AFFORDANCES:
        assert cls in vendor_tag, f"readonly vendor missing {cls!r}"


def test_non_owner_all_controls_are_readonly(session, engine, monkeypatch):
    """A non-owner sees every editor control flagged false. The check is
    scoped to inputs that opt into data-pt-editable so layout chrome (nav
    search, etc.) doesn't contaminate the assertion."""
    it = _seed(session, Role.AGENT)
    _patch_render(monkeypatch, _extended())
    client = _client(session, engine, monkeypatch, Role.AGENT)
    body = client.get(f"/inventory/{it.id}").text

    editor_tags = [t for t in _form_tags(body) if "data-pt-editable=" in t]
    assert editor_tags, "no editor controls rendered for non-owner"
    for t in editor_tags:
        assert 'data-pt-editable="false"' in t, f"non-owner tag editable: {t}"
        # bg-white is the biggest visual cue an input is editable — must be
        # absent on every read-only editor control.
        assert "bg-white" not in t, f"non-owner tag has editable bg: {t}"


def test_readonly_zoho_accounting_section_uses_no_inputs(session, engine, monkeypatch):
    """The Zoho accounting block is a <dl>, never inputs. data-pt-editable
    only appears on inputs — its absence here keeps that section visually
    distinct from the editable card above."""
    it = _seed(session, Role.OWNER)
    _patch_render(monkeypatch, _extended())
    client = _client(session, engine, monkeypatch, Role.OWNER)
    body = client.get(f"/inventory/{it.id}").text

    # Section header anchors the block; from there, scan a window of body text
    # and confirm none of the dt/dd values were turned into editable inputs.
    idx = body.find("Zoho accounting")
    assert idx > 0, "Zoho accounting section missing"
    window = body[idx : idx + 4000]
    assert 'data-pt-editable="true"' not in window
    # Cost price / selling price / sales account stay as plain dd text.
    assert "<dt" in window and "<dd" in window


def test_operational_section_inputs_have_affordance(session, engine, monkeypatch):
    """The hand-written PackTrack operational inputs (reorder, critical, lead
    days, daily usage, material code) are not driven by the field_input macro,
    so verify they were brought onto the same affordance utilities."""
    it = _seed(session, Role.OWNER)
    _patch_render(monkeypatch, _extended())
    client = _client(session, engine, monkeypatch, Role.OWNER)
    body = client.get(f"/inventory/{it.id}").text

    for key in (
        "material_code", "daily_usage_rate", "reorder_point", "critical_point",
        "sea_lead_days", "express_lead_days",
    ):
        tag = next(
            (t for t in _form_tags(body) if f'name="{key}"' in t), None,
        )
        assert tag is not None, f"missing operational input {key}"
        assert 'data-pt-editable="true"' in tag, key
        for cls in EDITABLE_AFFORDANCES:
            assert cls in tag, f"{key} missing affordance {cls!r}"
