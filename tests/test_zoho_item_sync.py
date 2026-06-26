"""Outbound item sync through the zoho-integration-service item endpoints.

Covers the v2.5.1 wiring: real PATCH for name/description/unit, vendor never
sent, success → synced, 4xx/5xx → failed (edit kept), unconfigured/no-id →
pending, and the boundary guarantee that no direct Zoho API/OAuth code is used.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("PACKTRACK_SECRET_KEY", "test-secret")

import httpx
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from packtrack.config import settings
from packtrack.models import Item
from packtrack.services import zoho_item_sync as zis


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _item(session: Session, **kw) -> Item:
    it = Item(
        name=kw.pop("name", "FIX 15mg - Bottle Label"),
        description=kw.pop("description", "orig desc"),
        unit=kw.pop("unit", "units"),
        vendor=kw.pop("vendor", "Acme"),
        zoho_item_id=kw.pop("zoho_item_id", "z-1"),
        current_stock=10.0,
        **kw,
    )
    session.add(it)
    session.commit()
    session.refresh(it)
    return it


def _configure(monkeypatch):
    monkeypatch.setattr(settings, "ZOHO_INTEGRATION_BASE_URL", "https://svc.example.com")
    monkeypatch.setattr(settings, "ZOHO_INTEGRATION_APP_TOKEN", "tok-123")
    monkeypatch.setattr(settings, "ZOHO_INTEGRATION_BRAND", "fix")


def _unconfigure(monkeypatch):
    monkeypatch.setattr(settings, "ZOHO_INTEGRATION_BASE_URL", "")
    monkeypatch.setattr(settings, "ZOHO_INTEGRATION_APP_TOKEN", "")
    monkeypatch.setattr(settings, "ZOHO_INTEGRATION_BRAND", "")


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


def test_patch_called_with_correct_url_headers_payload(session, monkeypatch):
    _configure(monkeypatch)
    it = _item(session, name="New Name", description="New Desc", unit="boxes")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"item": {"item_id": "z-1", "name": "New Name",
                           "description": "New Desc", "unit": "boxes"}},
        )

    result = zis.push_item_update(session, it, client=_client(handler))

    assert result.status == "synced"
    assert captured["method"] == "PATCH"
    assert captured["url"] == "https://svc.example.com/zoho/pack_track/items/z-1"
    # Bearer is the header that actually authenticates against the service.
    assert captured["headers"]["authorization"] == "Bearer tok-123"
    assert captured["headers"]["x-internal-token"] == "tok-123"
    assert captured["headers"]["x-brand"] == "fix"
    # Only the writable allowlist is sent.
    assert set(captured["body"].keys()) == {"name", "description", "unit"}
    assert captured["body"] == {
        "name": "New Name", "description": "New Desc", "unit": "boxes",
    }


def test_vendor_never_in_payload(session, monkeypatch):
    _configure(monkeypatch)
    it = _item(session, vendor="SecretVendor")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"item": {"name": it.name}})

    zis.push_item_update(session, it, client=_client(handler))
    assert "vendor" not in captured["body"]
    assert "SecretVendor" not in json.dumps(captured["body"])


def test_success_marks_synced_and_clears_error(session, monkeypatch):
    _configure(monkeypatch)
    it = _item(session)
    it.zoho_push_status = "failed"
    it.zoho_push_error = "old error"
    session.add(it)
    session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"item": {"name": it.name,
                                                  "description": it.description,
                                                  "unit": it.unit}})

    result = zis.push_item_update(session, it, client=_client(handler))
    assert result.status == "synced"
    session.refresh(it)
    assert it.zoho_push_status == "synced"
    assert it.zoho_push_error is None
    assert it.zoho_push_attempted_at is not None


def test_read_after_write_aligns_from_normalized_response(session, monkeypatch):
    """If the service normalizes a value, PackTrack aligns name/desc/unit."""
    _configure(monkeypatch)
    it = _item(session, name="lower name")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"item": {"name": "Normalized Name",
                                                  "description": "d", "unit": "ea"}})

    zis.push_item_update(session, it, client=_client(handler))
    session.refresh(it)
    assert it.name == "Normalized Name"
    assert it.unit == "ea"


def test_read_after_write_falls_back_to_get(session, monkeypatch):
    """Empty PATCH body triggers a verification GET; failures are ignored."""
    _configure(monkeypatch)
    it = _item(session, name="Keep Name")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "PATCH":
            return httpx.Response(200, json={})  # no usable item body
        return httpx.Response(200, json={"item": {"name": "Keep Name",
                                                  "description": "x", "unit": "units"}})

    result = zis.push_item_update(session, it, client=_client(handler))
    assert result.status == "synced"
    assert calls == ["PATCH", "GET"]


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status_code,body,needle",
    [
        (400, {"error": "BAD_REQUEST", "detail": "nope"}, "BAD_REQUEST"),
        (422, {"error": "VENDOR_UPDATE_NOT_SUPPORTED", "detail": "no vendor"},
         "VENDOR_UPDATE_NOT_SUPPORTED"),
        (500, {"detail": "boom"}, "500"),
    ],
)
def test_service_error_marks_failed_keeps_edit(session, monkeypatch, status_code, body, needle):
    _configure(monkeypatch)
    it = _item(session, name="Edited Name")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=body)

    result = zis.push_item_update(session, it, client=_client(handler))
    assert result.status == "failed"
    session.refresh(it)
    assert it.zoho_push_status == "failed"
    assert it.zoho_push_error and needle in it.zoho_push_error
    # Local edit is never rolled back.
    assert it.name == "Edited Name"


def test_network_error_marks_failed(session, monkeypatch):
    _configure(monkeypatch)
    it = _item(session)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    result = zis.push_item_update(session, it, client=_client(handler))
    assert result.status == "failed"
    session.refresh(it)
    assert it.zoho_push_status == "failed"
    assert "network error" in (it.zoho_push_error or "")


# ---------------------------------------------------------------------------
# Pending paths (no remote call)
# ---------------------------------------------------------------------------


def test_unconfigured_service_parks_pending_without_call(session, monkeypatch):
    _unconfigure(monkeypatch)
    it = _item(session)
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={})

    result = zis.push_item_update(session, it, client=_client(handler))
    assert result.status == "pending"
    assert called["n"] == 0
    session.refresh(it)
    assert it.zoho_push_status == "pending"
    assert it.zoho_push_error is None
    assert not zis.item_write_path_available()


def test_missing_zoho_item_id_does_not_call_service(session, monkeypatch):
    _configure(monkeypatch)
    it = _item(session, zoho_item_id=None)
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={})

    result = zis.push_item_update(session, it, client=_client(handler))
    assert result.status == "pending"
    assert called["n"] == 0
    session.refresh(it)
    assert it.zoho_push_status == "pending"


def test_configured_when_all_three_settings_present(monkeypatch):
    _configure(monkeypatch)
    assert zis.item_write_path_available() is True
    monkeypatch.setattr(settings, "ZOHO_INTEGRATION_BRAND", "")
    assert zis.item_write_path_available() is False


# ---------------------------------------------------------------------------
# cf_product_line custom-field write (v2.7.0)
# ---------------------------------------------------------------------------


def test_cf_product_line_included_in_payload_and_synced(session, monkeypatch):
    _configure(monkeypatch)
    it = _item(session, name="Keep", description="Keep", unit="ea")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"item": {"name": "Keep",
                                                  "description": "Keep", "unit": "ea"}})

    result = zis.push_item_update(
        session, it, cf_product_line="MIT A", client=_client(handler)
    )
    assert result.status == "synced"
    # Scalar allowlist plus exactly one custom field, by api_name + option name.
    assert captured["body"]["custom_fields"] == {"cf_product_line": "MIT A"}
    assert set(captured["body"].keys()) == {"name", "description", "unit", "custom_fields"}
    # No raw customfield id, no other custom field, no vendor.
    blob = json.dumps(captured["body"])
    assert "customfield_id" not in blob
    assert "vendor" not in blob


def test_cf_product_line_empty_string_clears(session, monkeypatch):
    _configure(monkeypatch)
    it = _item(session)

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"item": {"name": it.name}})

    captured = {}
    zis.push_item_update(session, it, cf_product_line="", client=_client(handler))
    assert captured["body"]["custom_fields"] == {"cf_product_line": ""}


def test_cf_product_line_not_sent_when_none(session, monkeypatch):
    _configure(monkeypatch)
    it = _item(session)

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"item": {"name": it.name}})

    captured = {}
    zis.push_item_update(session, it, client=_client(handler))  # cf_product_line=None
    assert "custom_fields" not in captured["body"]


def test_cf_product_line_failure_marks_failed_keeps_local(session, monkeypatch):
    _configure(monkeypatch)
    it = _item(session, name="Edited Name")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "INVALID_CUSTOM_FIELD_VALUE",
                                         "detail": "nope"})

    result = zis.push_item_update(
        session, it, cf_product_line="MIT A", client=_client(handler)
    )
    assert result.status == "failed"
    session.refresh(it)
    assert it.zoho_push_status == "failed"
    assert "INVALID_CUSTOM_FIELD_VALUE" in (it.zoho_push_error or "")
    assert it.name == "Edited Name"  # local scalar edit not rolled back


def test_outbound_payload_cf_only_added_when_provided():
    it = Item(name="X", unit="ea", description="d")
    assert "custom_fields" not in zis._outbound_payload(it)
    assert zis._outbound_payload(it, cf_product_line="MIT B")["custom_fields"] == {
        "cf_product_line": "MIT B"
    }
    # Empty string is a real (clearing) value, not "omit".
    assert zis._outbound_payload(it, cf_product_line="")["custom_fields"] == {
        "cf_product_line": ""
    }


# ---------------------------------------------------------------------------
# Boundary: no direct Zoho API / OAuth code in the item-sync module
# ---------------------------------------------------------------------------


def test_no_direct_zoho_api_or_oauth_in_item_sync():
    src = Path(zis.__file__).read_text().lower()
    for forbidden in ("zohoapis", "refresh_token", "oauth", "accounts.zoho",
                      "client_secret", "import packtrack.zoho"):
        assert forbidden not in src, f"unexpected direct-Zoho reference: {forbidden}"
