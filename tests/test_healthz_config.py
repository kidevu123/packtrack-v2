"""Tests for the /healthz endpoint's config-status fields.

The endpoint exposes three independent Zoho-config axes — gateway (read sync),
zoho-integration-service (receive writes), and legacy direct-OAuth. These tests
lock the field names + truthiness rules so a future settings refactor cannot
silently break the operator-visible health output.

We mock the DB connection so the test stays pure and doesn't need Postgres.
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from packtrack.config import settings


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Provide a TestClient with the DB engine stubbed.

    Imported lazily so that ``packtrack.main``'s side-effects (template
    registration, scheduler init) run with the patched engine in place.
    """
    class _FakeConn:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def exec_driver_sql(self, *_, **__): return None

    class _FakeEngine:
        def connect(self): return _FakeConn()

    monkeypatch.setattr("packtrack.db.engine", _FakeEngine())
    monkeypatch.setattr("packtrack.main.engine", _FakeEngine())

    from packtrack.main import app
    return TestClient(app)


def _set(monkeypatch: pytest.MonkeyPatch, **values: Any) -> None:
    for k, v in values.items():
        monkeypatch.setattr(settings, k, v)


def test_healthz_reports_all_three_zoho_axes_when_only_integration_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """The reproducer for the bug this fix addresses: integration service is
    wired (ZOHO_INTEGRATION_*) but no legacy direct-OAuth creds are held."""
    _set(monkeypatch,
         ZOHO_GATEWAY_URL="http://gw.test", ZOHO_GATEWAY_TOKEN="tok",
         ZOHO_GATEWAY_BRAND="haute_brands",
         ZOHO_INTEGRATION_BASE_URL="http://int.test",
         ZOHO_INTEGRATION_APP_TOKEN="sek", ZOHO_INTEGRATION_BRAND="haute_brands",
         ZOHO_CLIENT_ID="", ZOHO_CLIENT_SECRET="",
         ZOHO_REFRESH_TOKEN="", ZOHO_ORG_ID="")
    body = client.get("/healthz").json()

    assert body["ok"] is True
    assert body["gateway_configured"] is True
    assert body["zoho_integration_configured"] is True
    assert body["legacy_zoho_configured"] is False
    # Backcompat alias kept so existing monitors don't break.
    assert body["zoho_configured"] is False


def test_healthz_distinguishes_integration_from_legacy(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """All four legacy direct-OAuth fields present → legacy_zoho_configured=True;
    integration env empty → zoho_integration_configured=False. They must not
    flip together."""
    _set(monkeypatch,
         ZOHO_INTEGRATION_BASE_URL="", ZOHO_INTEGRATION_APP_TOKEN="",
         ZOHO_INTEGRATION_BRAND="",
         ZOHO_CLIENT_ID="cid", ZOHO_CLIENT_SECRET="csec",
         ZOHO_REFRESH_TOKEN="rt", ZOHO_ORG_ID="org")
    body = client.get("/healthz").json()
    assert body["legacy_zoho_configured"] is True
    assert body["zoho_configured"] is True
    assert body["zoho_integration_configured"] is False


def test_healthz_all_off_when_no_zoho_env_set(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    _set(monkeypatch,
         ZOHO_GATEWAY_URL="", ZOHO_GATEWAY_TOKEN="", ZOHO_GATEWAY_BRAND="",
         ZOHO_INTEGRATION_BASE_URL="", ZOHO_INTEGRATION_APP_TOKEN="",
         ZOHO_INTEGRATION_BRAND="",
         ZOHO_CLIENT_ID="", ZOHO_CLIENT_SECRET="",
         ZOHO_REFRESH_TOKEN="", ZOHO_ORG_ID="")
    body = client.get("/healthz").json()
    assert body["gateway_configured"] is False
    assert body["zoho_integration_configured"] is False
    assert body["legacy_zoho_configured"] is False
    assert body["zoho_configured"] is False


def test_settings_integration_property_requires_all_three_keys(
    monkeypatch: pytest.MonkeyPatch,
):
    """``zoho_integration_configured`` is True only when base URL + token +
    brand are ALL truthy — partial config must read as not configured."""
    cases = [
        ("",   "tok", "brand", False),
        ("u",  "",    "brand", False),
        ("u",  "tok", "",      False),
        ("u",  "tok", "brand", True),
    ]
    for base, token, brand, expected in cases:
        _set(monkeypatch,
             ZOHO_INTEGRATION_BASE_URL=base,
             ZOHO_INTEGRATION_APP_TOKEN=token,
             ZOHO_INTEGRATION_BRAND=brand)
        assert settings.zoho_integration_configured is expected, (
            f"base={base!r} token={token!r} brand={brand!r}"
        )
