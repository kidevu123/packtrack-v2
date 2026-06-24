"""Tests for ``push_luma_receipt`` — locks the wire contract.

Until this file, only the *registration* call was covered. The
receipt-push call is the one that actually decrements Luma's idea of
on-order vs received, so silently breaking its payload would be a real
problem. These tests use ``httpx.MockTransport`` so nothing leaves the
process.

Scope intentionally limited to:
  1. payload field names + presence
  2. secret header name + value
  3. success / 4xx / network-error return contract
  4. dry-run header
  5. missing-webhook short-circuit

NOT in scope here (covered by other test files): registration call,
HTMX retry route, idempotency at the operator layer.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import httpx
import pytest

from packtrack.config import settings
from packtrack.models import BoxReceipt, Confidence, LumaPushStatus
from packtrack.services.receiving import push_luma_receipt

_WEBHOOK = "http://luma.test/api/integrations/packtrack/receipts"
_SECRET = "test-packtrack-secret"


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


def _box(**overrides: Any) -> BoxReceipt:
    base: dict[str, Any] = {
        "id": 1,
        "packtrack_receipt_id": "uuid-receipt-1",
        "purchase_order_id": 10,
        "item_id": 100,
        "material_code": "PT-00095",
        "material_name": "Hyroxi MIT-B 4ct Sweet Trip - 100mg - Blister Card",
        "supplier": "Helen Packaging",
        "supplier_lot_number": "SL-2024-001",
        "box_number": "RCPT-abc12345-1",
        "declared_quantity": 1000.0,
        "counted_quantity": 1000.0,
        "accepted_quantity": 1000.0,
        "unit_of_measure": "each",
        "confidence": Confidence.HIGH,
        "received_by_user_id": 1,
        "received_at": datetime(2026, 6, 24, 15, 30, 0),
    }
    base.update(overrides)
    return BoxReceipt(**base)


def _intercept(handler):
    """Patch httpx.Client used inside push_luma_receipt with a MockTransport-
    backed client. push_luma_receipt instantiates its own httpx.Client so we
    swap the class for the duration of the call."""
    import httpx as _httpx
    original = _httpx.Client

    class _Stub(_httpx.Client):
        def __init__(self, *a, **kw):
            super().__init__(*a, transport=_httpx.MockTransport(handler), **kw)

    return original, _Stub


def _patch_client(handler):
    """Context manager-style helper that yields after swapping httpx.Client."""
    import contextlib

    import httpx as _httpx

    @contextlib.contextmanager
    def _cm():
        original, stub = _intercept(handler)
        _httpx.Client = stub  # type: ignore[misc]
        try:
            yield
        finally:
            _httpx.Client = original  # type: ignore[misc]

    return _cm()


# ---------------------------------------------------------------------------
# Payload shape + secret header
# ---------------------------------------------------------------------------


def test_payload_includes_all_expected_fields_and_secret_header():
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["method"] = req.method
        captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={"ok": True, "received": True})

    with _patch_client(handler):
        ok, err, body = push_luma_receipt(
            _box(), po_number="PO-00263", photo_urls=[], received_by="Alice",
        )

    assert ok is True and err is None and body == {"ok": True, "received": True}
    assert captured["method"] == "POST"
    assert captured["url"] == _WEBHOOK

    # Secret travels under the lowercase 'x-packtrack-secret' header per the
    # Luma /api/integrations/packtrack/{receipts,items} contract. Locked here
    # so a rename surfaces in CI, not in prod.
    assert captured["headers"]["x-packtrack-secret"] == _SECRET
    assert "content-type" in captured["headers"]
    assert captured["headers"]["content-type"].startswith("application/json")

    body = captured["body"]
    # Required fields Luma reads:
    assert body["source_system"] == "PACKTRACK"
    assert body["packtrack_po_id"] == "PO-00263"
    assert body["packtrack_receipt_id"] == "uuid-receipt-1"
    assert body["material_code"] == "PT-00095"
    assert body["material_name"].startswith("Hyroxi MIT-B")
    assert body["supplier"] == "Helen Packaging"
    assert body["supplier_lot_number"] == "SL-2024-001"
    assert body["box_number"] == "RCPT-abc12345-1"
    assert body["declared_quantity"] == 1000      # int, not float
    assert body["counted_quantity"] == 1000
    assert body["unit_of_measure"] == "each"
    assert body["received_at"].endswith("Z")       # ISO + trailing Z
    assert body["received_by"] == "Alice"
    assert "payload" not in body                   # only included when photos exist


def test_payload_includes_photo_urls_when_provided():
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={"ok": True})

    with _patch_client(handler):
        push_luma_receipt(
            _box(), po_number="PO-1", photo_urls=["https://x/a.jpg", "https://x/b.jpg"],
        )

    assert captured["body"]["payload"] == {"photo_urls": ["https://x/a.jpg", "https://x/b.jpg"]}


def test_counted_quantity_is_null_when_box_has_no_count():
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={"ok": True})

    with _patch_client(handler):
        push_luma_receipt(_box(counted_quantity=None), po_number="PO-1", photo_urls=[])

    assert captured["body"]["counted_quantity"] is None


def test_dry_run_adds_header():
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
        return httpx.Response(200, json={"ok": True})

    with _patch_client(handler):
        push_luma_receipt(_box(), po_number="PO-1", photo_urls=[], dry_run=True)

    assert captured["headers"].get("x-packtrack-dry-run") == "true"


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


def test_success_returns_ok_with_parsed_body():
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "luma_receipt_id": "lr-1"})

    with _patch_client(handler):
        ok, err, body = push_luma_receipt(_box(), po_number="PO-1", photo_urls=[])
    assert ok is True
    assert err is None
    assert body == {"ok": True, "luma_receipt_id": "lr-1"}


def test_4xx_returns_failure_with_error_text():
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"error": "MAPPING_MISSING", "detail": "no mapping for PT-00095"})

    with _patch_client(handler):
        ok, err, body = push_luma_receipt(_box(), po_number="PO-1", photo_urls=[])
    assert ok is False
    assert err is not None
    assert "422" in err
    assert "MAPPING_MISSING" in err
    # Body still parsed so the caller can persist the full Luma response.
    assert body == {"error": "MAPPING_MISSING", "detail": "no mapping for PT-00095"}


def test_network_error_returns_failure():
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with _patch_client(handler):
        ok, err, body = push_luma_receipt(_box(), po_number="PO-1", photo_urls=[])
    assert ok is False
    assert err is not None
    assert body is None


def test_missing_webhook_short_circuits_without_http(monkeypatch: pytest.MonkeyPatch):
    """When LUMA_RECEIPT_WEBHOOK_URL is empty the helper must NOT attempt an
    HTTP call. Locked because production has had unset Luma config in the
    past — that case must be a clean no-op, not a crash."""
    monkeypatch.setattr(settings, "LUMA_RECEIPT_WEBHOOK_URL", "")
    called = []

    def handler(_req: httpx.Request) -> httpx.Response:
        called.append(_req.url)
        return httpx.Response(500)

    with _patch_client(handler):
        ok, err, body = push_luma_receipt(_box(), po_number="PO-1", photo_urls=[])
    assert ok is False
    assert err and "not configured" in err
    assert body is None
    assert called == [], "must not attempt HTTP when webhook unset"


# ---------------------------------------------------------------------------
# Push status enum — receipts pre-condition
# ---------------------------------------------------------------------------


def test_compute_luma_readiness_blocks_missing_material_code():
    from packtrack.services.box_receipt import compute_luma_readiness
    assert compute_luma_readiness(None) is LumaPushStatus.NOT_READY
    assert compute_luma_readiness("") is LumaPushStatus.NOT_READY
    assert compute_luma_readiness("   ") is LumaPushStatus.NOT_READY
    assert compute_luma_readiness("PT-00095") is LumaPushStatus.PENDING
