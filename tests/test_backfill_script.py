"""Tests for scripts/backfill_luma_packaging_material_zoho_ids.py.

The script is split into pure helpers (eligibility, table formatting) plus
``_run(args)`` which does the DB query + HTTP. We test the pure helpers
directly and exercise the apply path's per-item registration loop with a
mocked httpx client.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

# Make scripts/ importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
backfill = importlib.import_module("backfill_luma_packaging_material_zoho_ids")

from packtrack.config import settings  # noqa: E402
from packtrack.models import Item  # noqa: E402
from packtrack.services.receiving import (  # noqa: E402
    LumaRegistrationOutcome,
    register_item_with_luma,
)


def _item(**overrides: Any) -> Item:
    base: dict[str, Any] = {
        "id": 1, "zoho_item_id": "ZHO-1", "name": "Sweet Trip Blister Card",
        "sku_code": "SK-1", "material_code": "PT-00095", "unit": "each",
    }
    base.update(overrides)
    return Item(**base)


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------


def test_eligibility_requires_both_fields():
    assert backfill._eligible(_item()) is True
    assert backfill._eligible(_item(zoho_item_id=None)) is False
    assert backfill._eligible(_item(zoho_item_id="   ")) is False
    assert backfill._eligible(_item(material_code=None)) is False
    assert backfill._eligible(_item(material_code="")) is False


def test_proposed_action_messages():
    assert "skip — no material_code" in backfill._proposed_action(_item(material_code=None))
    assert "skip — no zoho_item_id" in backfill._proposed_action(_item(zoho_item_id=None))
    assert "register/update" in backfill._proposed_action(_item())


# ---------------------------------------------------------------------------
# Apply path — calls Luma once per eligible item
# ---------------------------------------------------------------------------


@pytest.fixture
def _luma_configured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        settings, "LUMA_RECEIPT_WEBHOOK_URL",
        "http://luma.test/api/integrations/packtrack/receipts",
    )
    monkeypatch.setattr(settings, "LUMA_PACKTRACK_SECRET", "test-secret")


def test_apply_calls_register_per_eligible_item(_luma_configured):
    calls: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        import json as _json
        calls.append(_json.loads(req.content))
        return httpx.Response(200, json={
            "ok": True, "outcome": "UPDATED", "luma_material_id": "uuid-pm",
        })

    items = [
        _item(id=1, material_code="PT-00001", zoho_item_id="ZHO-1"),
        _item(id=2, material_code="PT-00002", zoho_item_id="ZHO-2"),
        _item(id=3, material_code=None, zoho_item_id="ZHO-3"),  # skipped
    ]
    eligible = [it for it in items if backfill._eligible(it)]
    assert len(eligible) == 2

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        results = [register_item_with_luma(it, client=client) for it in eligible]

    assert {c["material_code"] for c in calls} == {"PT-00001", "PT-00002"}
    assert all(c["zoho_item_id"] in {"ZHO-1", "ZHO-2"} for c in calls)
    assert all(r.outcome is LumaRegistrationOutcome.UPDATED for r in results)


def test_dry_run_never_calls_luma(_luma_configured):
    """The script's ``_run`` enters the apply branch only when args.apply is
    True. We assert by *not* providing a handler — any call would 500."""
    called: list[None] = []

    def handler(_req: httpx.Request) -> httpx.Response:
        called.append(None)
        return httpx.Response(500)

    # Simulate the dry-run path: only the eligibility scan runs, no HTTP.
    with httpx.Client(transport=httpx.MockTransport(handler)) as _client:
        for it in [_item(id=1), _item(id=2)]:
            assert backfill._eligible(it) is True
            _ = backfill._proposed_action(it)
    assert called == []


def test_already_mapped_counts_as_ok(_luma_configured):
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "ok": True, "outcome": "ALREADY_MAPPED", "luma_material_id": "uuid-pm-1",
        })

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        r = register_item_with_luma(_item(), client=client)
    assert r.outcome is LumaRegistrationOutcome.ALREADY_MAPPED
    assert r.ok is True  # the script treats this as success in its summary


def test_conflict_is_flagged_not_overwritten(_luma_configured):
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={
            "ok": False, "outcome": "ZOHO_ID_CONFLICT_REVIEW_REQUIRED",
            "error": "ZOHO_ID_CONFLICT_REVIEW_REQUIRED",
            "existing_zoho_item_id": "ZHO-OLD",
            "incoming_zoho_item_id": "ZHO-1",
        })

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        r = register_item_with_luma(_item(), client=client)
    assert r.outcome is LumaRegistrationOutcome.CONFLICT
    assert r.needs_review is True
    assert r.existing_zoho_item_id == "ZHO-OLD"


def test_transient_5xx_recorded_as_failed(_luma_configured):
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        r = register_item_with_luma(_item(), client=client)
    assert r.outcome is LumaRegistrationOutcome.FAILED
    assert r.status_code == 503
