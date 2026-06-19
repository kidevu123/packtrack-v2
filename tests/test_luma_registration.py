"""Tests for register_item_with_luma + the typed-outcome contract.

Locks the payload shape (always includes ``zoho_item_id`` when set on
the item), the skip/conflict/failure outcome semantics, and the
back-compat tuple shim used by the receive routes.

httpx.MockTransport keeps the test pure — no Luma, no DB.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from packtrack.config import settings
from packtrack.models import Item
from packtrack.services.receiving import (
    LumaRegistrationOutcome,
    register_item_with_luma,
    register_material_with_luma,
)

_WEBHOOK = "http://luma.test/api/integrations/packtrack/receipts"
_SECRET = "test-packtrack-secret"
_EXPECTED_URL = "http://luma.test/api/integrations/packtrack/items"


@pytest.fixture(autouse=True)
def _configure_luma():
    saved = {
        "LUMA_RECEIPT_WEBHOOK_URL": settings.LUMA_RECEIPT_WEBHOOK_URL,
        "LUMA_PACKTRACK_SECRET": settings.LUMA_PACKTRACK_SECRET,
    }
    settings.LUMA_RECEIPT_WEBHOOK_URL = _WEBHOOK
    settings.LUMA_PACKTRACK_SECRET = _SECRET
    yield
    for k, v in saved.items():
        setattr(settings, k, v)


def _item(**overrides: Any) -> Item:
    base: dict[str, Any] = {
        "id": 42,
        "zoho_item_id": "ZHO-9001",
        "name": "Hyroxi MIT-B 4ct Sweet Trip - 100mg - Blister Card",
        "sku_code": "HMB-ST-4-BC",
        "material_code": "PT-00095",
        "unit": "each",
    }
    base.update(overrides)
    return Item(**base)


def _mock_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://ignored")


# ---------------------------------------------------------------------------
# Payload always includes zoho_item_id when present
# ---------------------------------------------------------------------------


def test_payload_includes_zoho_item_id_when_present():
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["headers"] = dict(req.headers)
        captured["body"] = json.loads(req.content)
        return httpx.Response(
            201,
            json={"ok": True, "outcome": "REGISTERED", "created": True,
                  "luma_material_id": "uuid-pm-1"},
        )

    with _mock_client(handler) as client:
        result = register_item_with_luma(_item(), client=client)

    assert captured["url"] == _EXPECTED_URL
    assert captured["headers"]["x-packtrack-secret"] == _SECRET
    assert captured["body"]["material_code"] == "PT-00095"
    assert captured["body"]["zoho_item_id"] == "ZHO-9001"
    assert captured["body"]["kind"] == "BLISTER_CARD"
    assert captured["body"]["unit_of_measure"] == "each"
    assert result.outcome is LumaRegistrationOutcome.REGISTERED
    assert result.ok is True
    assert result.luma_material_id == "uuid-pm-1"


def test_payload_omits_zoho_item_id_when_blank():
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={"ok": True, "outcome": "ALREADY_MAPPED",
                                         "luma_material_id": "uuid-pm-1"})

    with _mock_client(handler) as client:
        register_item_with_luma(_item(zoho_item_id=None), client=client)

    assert "zoho_item_id" not in captured["body"]


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


def test_updated_outcome_is_surfaced():
    """Luma's UPDATED response (zoho_item_id backfilled) returns a result the
    backfill script can act on — NOT folded into ALREADY_MAPPED."""
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "ok": True, "outcome": "UPDATED", "created": False,
            "luma_material_id": "uuid-pm-77",
        })

    with _mock_client(handler) as client:
        r = register_item_with_luma(_item(), client=client)
    assert r.outcome is LumaRegistrationOutcome.UPDATED
    assert r.ok is True
    assert r.luma_material_id == "uuid-pm-77"


def test_already_mapped_outcome():
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "outcome": "ALREADY_MAPPED",
                                         "luma_material_id": "uuid-pm-1"})
    with _mock_client(handler) as client:
        r = register_item_with_luma(_item(), client=client)
    assert r.outcome is LumaRegistrationOutcome.ALREADY_MAPPED
    assert r.ok is True


def test_conflict_outcome_is_flagged_not_treated_as_success():
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={
            "ok": False,
            "outcome": "ZOHO_ID_CONFLICT_REVIEW_REQUIRED",
            "error": "ZOHO_ID_CONFLICT_REVIEW_REQUIRED",
            "material_code": "PT-00095",
            "luma_material_id": "uuid-pm-77",
            "existing_zoho_item_id": "ZHO-OLD",
            "incoming_zoho_item_id": "ZHO-9001",
        })

    with _mock_client(handler) as client:
        r = register_item_with_luma(_item(), client=client)
    assert r.outcome is LumaRegistrationOutcome.CONFLICT
    assert r.ok is False
    assert r.needs_review is True
    assert r.existing_zoho_item_id == "ZHO-OLD"
    assert r.incoming_zoho_item_id == "ZHO-9001"


def test_500_returns_failed():
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="oops")
    with _mock_client(handler) as client:
        r = register_item_with_luma(_item(), client=client)
    assert r.outcome is LumaRegistrationOutcome.FAILED
    assert r.status_code == 500


def test_network_error_returns_failed():
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")
    with _mock_client(handler) as client:
        r = register_item_with_luma(_item(), client=client)
    assert r.outcome is LumaRegistrationOutcome.FAILED
    assert "network error" in (r.message or "")


# ---------------------------------------------------------------------------
# Skips (local short-circuits)
# ---------------------------------------------------------------------------


def test_skipped_no_material_code(monkeypatch: pytest.MonkeyPatch):
    called = False
    def handler(_req: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    with _mock_client(handler) as client:
        r = register_item_with_luma(_item(material_code=None), client=client)
    assert r.outcome is LumaRegistrationOutcome.SKIPPED_NO_MATERIAL_CODE
    assert called is False, "should not call Luma when material_code is missing"


def test_skipped_no_config(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "LUMA_RECEIPT_WEBHOOK_URL", "")
    r = register_item_with_luma(_item())
    assert r.outcome is LumaRegistrationOutcome.SKIPPED_NO_CONFIG


# ---------------------------------------------------------------------------
# Back-compat tuple shim
# ---------------------------------------------------------------------------


def test_register_material_with_luma_back_compat_tuple():
    """The receive routes pass the result through the tuple form. UPDATED
    must read as ok=True so the receipt push proceeds."""
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "outcome": "UPDATED",
                                         "luma_material_id": "uuid-pm-9"})
    # Patch httpx.Client used internally by the shim.
    with httpx.MockTransport(handler) as _:
        pass  # ensure context — actual call goes through default Client

    # Use a real Client-via-mock by patching httpx.Client through the helper.
    import httpx as _httpx
    real_client = _httpx.Client

    class _Stub(_httpx.Client):
        def __init__(self, *a, **kw):
            super().__init__(*a, transport=_httpx.MockTransport(handler), **kw)

    _httpx.Client = _Stub  # type: ignore[misc]
    try:
        ok, msg = register_material_with_luma(_item())
    finally:
        _httpx.Client = real_client  # type: ignore[misc]

    assert ok is True
    assert msg is None


def test_register_material_with_luma_tuple_returns_false_on_conflict():
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={
            "ok": False, "outcome": "ZOHO_ID_CONFLICT_REVIEW_REQUIRED",
            "error": "ZOHO_ID_CONFLICT_REVIEW_REQUIRED",
            "existing_zoho_item_id": "ZHO-OLD",
            "incoming_zoho_item_id": "ZHO-9001",
        })

    import httpx as _httpx
    real_client = _httpx.Client

    class _Stub(_httpx.Client):
        def __init__(self, *a, **kw):
            super().__init__(*a, transport=_httpx.MockTransport(handler), **kw)

    _httpx.Client = _Stub  # type: ignore[misc]
    try:
        ok, msg = register_material_with_luma(_item())
    finally:
        _httpx.Client = real_client  # type: ignore[misc]
    assert ok is False
    assert msg and "CONFLICT" in msg.upper()


# ---------------------------------------------------------------------------
# Identity rule: material_code, not name
# ---------------------------------------------------------------------------


def test_material_code_is_the_identity_key_not_name():
    """Renaming an item must not change the payload's material_code (which is
    Luma's stable key)."""
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={"ok": True, "outcome": "ALREADY_MAPPED"})

    renamed = _item(name="Renamed-on-the-Fly Box")
    with _mock_client(handler) as client:
        register_item_with_luma(renamed, client=client)

    assert captured["body"]["material_code"] == "PT-00095"  # unchanged
    assert captured["body"]["material_name"] == "Renamed-on-the-Fly Box"


# ---------------------------------------------------------------------------
# Receive routes call registration BEFORE pushing the receipt
# ---------------------------------------------------------------------------


def test_receive_route_source_calls_register_before_push():
    """Source-level contract: in packtrack/routes/receiving.py, both the
    submit and retry paths call ``register_material_with_luma`` BEFORE the
    matching ``push_luma_receipt`` for the same logical block. This is a
    cheap guard against a future refactor reordering the calls — a real
    behavioural test would need the full FastAPI app + DB up.
    """
    from pathlib import Path
    src = Path("packtrack/routes/receiving.py").read_text()
    register_positions = [
        i for i, line in enumerate(src.splitlines())
        if "register_material_with_luma(item)" in line
    ]
    push_positions = [
        i for i, line in enumerate(src.splitlines())
        if "push_luma_receipt(" in line and "from " not in line and "import" not in line
    ]
    assert register_positions, "register_material_with_luma must be called from the receive routes"
    assert push_positions, "push_luma_receipt must be called from the receive routes"
    # For each push call, there must be a register call earlier in the file.
    for pp in push_positions:
        assert any(rp < pp for rp in register_positions), (
            f"push_luma_receipt at line {pp+1} has no register_material_with_luma "
            f"call before it"
        )
