"""Tests for the zoho-integration-service Pack Track receive client.

Uses ``httpx.MockTransport`` injected via the optional ``client`` kwarg on
``preview_receive`` / ``commit_receive`` — no monkeypatching, no real network,
no DB. Settings are mutated via a fixture that restores the originals so
tests are independent.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from packtrack.config import settings
from packtrack.services.zoho_integration import (
    ReceivePayload,
    ZohoIntegrationConfigError,
    ZohoIntegrationGatewayError,
    ZohoIntegrationIdempotencyConflictError,
    ZohoIntegrationLiveWriteDisabledError,
    ZohoIntegrationNotConfiguredError,
    ZohoIntegrationRateLimitedError,
    ZohoIntegrationValidationError,
    commit_receive,
    preview_receive,
)

# Any non-Zoho host is fine for the mock — the direct-Zoho-host guard below
# explicitly forbids ``zohoapis.com`` / ``accounts.zoho.com``.
_BASE = "http://zoho-integration.test"
_BRAND = "haute_brands"
_TOKEN = "test-bearer-token"


@pytest.fixture(autouse=True)
def _configure_settings():
    """Populate the integration settings for each test; restore on teardown."""
    keys = {
        "ZOHO_INTEGRATION_BASE_URL": _BASE,
        "ZOHO_INTEGRATION_APP_TOKEN": _TOKEN,
        "ZOHO_INTEGRATION_BRAND": _BRAND,
        "ZOHO_INTEGRATION_TIMEOUT_SECONDS": 5.0,
        "ZOHO_INTEGRATION_RECEIVE_ENABLED": True,
    }
    saved = {k: getattr(settings, k) for k in keys}
    for k, v in keys.items():
        setattr(settings, k, v)
    yield
    for k, v in saved.items():
        setattr(settings, k, v)


def _payload(**overrides: Any) -> ReceivePayload:
    base = {
        "pack_track_receipt_id": "PT-RCPT-9999",
        "purchaseorder_id": "5254-PO",
        "purchaseorder_line_item_id": "5254-PO-LINE",
        "item_id": "5254-ITEM",
        "received_quantity": 100.0,
        "received_date": "2026-06-18",
        "pack_track_operator_id": "op-7",
        "pack_track_workflow_session_id": "session-abc",
    }
    base.update(overrides)
    return ReceivePayload(**base)


def _mock_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://ignored")


# ---------------------------------------------------------------------------
# Configuration guard
# ---------------------------------------------------------------------------


def test_commit_raises_when_not_configured():
    settings.ZOHO_INTEGRATION_BASE_URL = ""
    with pytest.raises(ZohoIntegrationNotConfiguredError):
        commit_receive(_payload())


def test_preview_raises_when_not_configured():
    settings.ZOHO_INTEGRATION_APP_TOKEN = ""
    with pytest.raises(ZohoIntegrationNotConfiguredError):
        preview_receive(_payload())


# ---------------------------------------------------------------------------
# Preview happy + sad
# ---------------------------------------------------------------------------


def test_preview_success_sends_correct_headers_and_body():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"would_be_payload": {"line_items": []}, "preflight": "ok"})

    with _mock_client(handler) as client:
        result = preview_receive(_payload(), client=client)

    assert result == {"would_be_payload": {"line_items": []}, "preflight": "ok"}
    assert captured["url"] == f"{_BASE}/zoho/pack_track/receive/preview"
    assert captured["headers"]["authorization"] == f"Bearer {_TOKEN}"
    assert captured["headers"]["x-brand"] == _BRAND
    assert captured["headers"]["idempotency-key"] == "PACK_TRACK_RECEIVE_PT-RCPT-9999"
    assert captured["body"]["pack_track_receipt_id"] == "PT-RCPT-9999"
    assert captured["body"]["received_quantity"] == 100.0
    # Legacy header must not be sent.
    assert "x-internal-token" not in {h.lower() for h in captured["headers"]}


def test_preview_validation_failure_surfaces_typed_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={"error": "INSUFFICIENT_PO_REMAINING",
                  "detail": "Remaining on PO line: 25, requested: 100."},
        )

    with _mock_client(handler) as client, pytest.raises(ZohoIntegrationValidationError) as info:
        preview_receive(_payload(), client=client)
    assert info.value.code == "INSUFFICIENT_PO_REMAINING"
    assert info.value.status_code == 422
    assert "Remaining on PO" in info.value.detail


def test_preview_brand_required_surfaces_typed_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "BRAND_REQUIRED", "detail": "Missing X-Brand."})

    with _mock_client(handler) as client, pytest.raises(ZohoIntegrationValidationError) as info:
        preview_receive(_payload(), client=client)
    assert info.value.code == "BRAND_REQUIRED"


# ---------------------------------------------------------------------------
# Commit happy + sad
# ---------------------------------------------------------------------------


def test_commit_success_returns_parsed_body():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"{_BASE}/zoho/pack_track/receive/commit"
        return httpx.Response(
            200,
            json={"zoho_purchase_receive_id": "Z-RCPT-1", "status": "committed"},
        )

    with _mock_client(handler) as client:
        result = commit_receive(_payload(), client=client)
    assert result["zoho_purchase_receive_id"] == "Z-RCPT-1"


def test_commit_live_write_disabled_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"error": "LIVE_WRITE_DISABLED",
                  "detail": "ENABLE_LIVE_INVENTORY_WRITES is false."},
        )

    with _mock_client(handler) as client, pytest.raises(ZohoIntegrationLiveWriteDisabledError):
        commit_receive(_payload(), client=client)


def test_commit_idempotency_conflict_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={"error": "IDEMPOTENCY_CONFLICT",
                  "detail": "Key seen with a different payload."},
        )

    with _mock_client(handler) as client, pytest.raises(ZohoIntegrationIdempotencyConflictError):
        commit_receive(_payload(), client=client)


def test_commit_rate_limited_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"error": "RATE_LIMIT_EXCEEDED", "detail": "Slow down."},
        )

    with _mock_client(handler) as client, pytest.raises(ZohoIntegrationRateLimitedError):
        commit_receive(_payload(), client=client)


def test_commit_credential_not_found_raises_config_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"error": "CREDENTIAL_NOT_FOUND",
                  "detail": "Brand has no Zoho credential."},
        )

    with _mock_client(handler) as client, pytest.raises(ZohoIntegrationConfigError) as info:
        commit_receive(_payload(), client=client)
    assert info.value.code == "CREDENTIAL_NOT_FOUND"


def test_commit_502_raises_gateway_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="Bad Gateway")

    with _mock_client(handler) as client, pytest.raises(ZohoIntegrationGatewayError):
        commit_receive(_payload(), client=client)


def test_commit_network_error_raises_gateway_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with _mock_client(handler) as client, pytest.raises(ZohoIntegrationGatewayError):
        commit_receive(_payload(), client=client)


# ---------------------------------------------------------------------------
# Idempotency — same key, same payload, twice
# ---------------------------------------------------------------------------


def test_same_idempotency_key_can_be_retried_safely():
    """The client must not crash on a benign retry — same key, same body, same
    response. Mirrors how the service is expected to behave when the operator
    re-submits the same receive."""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({
            "key": request.headers["idempotency-key"],
            "body": json.loads(request.content),
        })
        return httpx.Response(200, json={"zoho_purchase_receive_id": "Z-RCPT-IDEMP"})

    payload = _payload(pack_track_receipt_id="PT-RCPT-IDEMP")
    with _mock_client(handler) as client:
        r1 = commit_receive(payload, client=client)
        r2 = commit_receive(payload, client=client)

    assert r1 == r2
    assert len(calls) == 2
    assert calls[0]["key"] == calls[1]["key"] == "PACK_TRACK_RECEIVE_PT-RCPT-IDEMP"
    assert calls[0]["body"] == calls[1]["body"]


# ---------------------------------------------------------------------------
# Direct-Zoho guard
# ---------------------------------------------------------------------------


_FORBIDDEN_HOSTS = ("zohoapis.com", "accounts.zoho.com")


def test_no_direct_zoho_host_is_contacted_on_commit():
    seen_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        seen_hosts.append(host)
        assert not any(host.endswith(h) for h in _FORBIDDEN_HOSTS), (
            f"Pack Track must not call Zoho directly — saw {host}"
        )
        return httpx.Response(200, json={})

    with _mock_client(handler) as client:
        commit_receive(_payload(), client=client)

    assert seen_hosts == ["zoho-integration.test"]


def test_no_direct_zoho_host_is_contacted_on_preview():
    seen_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_hosts.append(request.url.host)
        return httpx.Response(200, json={})

    with _mock_client(handler) as client:
        preview_receive(_payload(), client=client)

    assert all(not h.endswith(zh) for h in seen_hosts for zh in _FORBIDDEN_HOSTS)
    assert seen_hosts == ["zoho-integration.test"]
