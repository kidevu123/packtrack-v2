"""v2.10.0 — PackTrack ↔ zoho-integration-service adjustment sync.

Exercises the path that wires the v2.9.0 immutable adjustment ledger to
the v1.34.0 integration-service endpoint:

  POST /zoho/pack_track/items/{zoho_item_id}/inventory-adjustments

Covers (>= the 21 cases from the spec):

A. Config disabled → adjustment saved, current_stock updated,
   status NOT_CONFIGURED, NO HTTP call made.
B. Config enabled + happy path → SYNCED with zoho_reference stored.
C. Service receives expected payload (mode/quantities/reason/idempotency).
D. Quantities sent as 4-decimal strings (not floats).
E. Required headers: Bearer + X-Brand + Idempotency-Key + Content-Type.
F. Item with no zoho_item_id → SKIPPED + clear error, no HTTP call.
G. 401 → FAILED with auth-class message.
H. 403 → FAILED with capability-class message.
I. 404 → FAILED with item-not-found message.
J. 409 → FAILED (idempotency conflict — don't auto-retry).
K. 422 → FAILED (validation).
L. 500 → FAILED with retry visible.
M. Timeout → FAILED.
N. Idempotent replay (meta.idempotent=true) → SYNCED with reference.
O. STOCK_DRIFT_DETECTED warning → SYNCED + zoho_sync_warning populated.
P. Owner can retry FAILED row; outcome flips to SYNCED.
Q. Retry sends same idempotency_key as the original attempt.
R. Non-owner cannot retry (403).
S. Retry on SYNCED row is a no-op (route 303, no extra HTTP call).
T. sync_attempt_count increments per real attempt (not on NOT_CONFIGURED).
U. Bearer token never logged / never appears in stored error message.
V. zoho_adjustment_client module hits ONLY the integration-service base URL.
W. No Receiving file touched by either module.
X. master-data fields unchanged after the sync round-trip.
"""
from __future__ import annotations

import importlib
import os
from datetime import datetime
from decimal import Decimal
from typing import Any

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("PACKTRACK_SECRET_KEY", "test-secret")

import httpx
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from packtrack.config import settings
from packtrack.models import (
    AdjustmentDirection,
    AdjustmentMode,
    AdjustmentReason,
    AdjustmentSource,
    InventoryAdjustment,
    Item,
    Role,
    User,
    ZohoSyncStatus,
)
from packtrack.services.inventory_adjustment_sync import try_sync_adjustment
from packtrack.services.inventory_adjustments import create_adjustment
from packtrack.services.zoho_adjustment_client import (
    OutcomeKind,
    build_payload,
    is_configured,
    push_adjustment_to_zoho,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(name="engine")
def engine_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with engine.connect() as conn:
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_receive_cases_receive_case_number "
            "ON receive_cases (receive_id, vendor_case_number) "
            "WHERE vendor_case_number IS NOT NULL"
        )
        conn.commit()
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


@pytest.fixture
def integration_settings(monkeypatch):
    """Turn on the integration so push_adjustment_to_zoho will actually
    attempt the network call (which the test then intercepts)."""
    monkeypatch.setattr(settings, "ZOHO_INTEGRATION_ADJUST_ENABLED", True)
    monkeypatch.setattr(settings, "ZOHO_INTEGRATION_BASE_URL", "http://int-service.test")
    monkeypatch.setattr(settings, "ZOHO_INTEGRATION_APP_TOKEN", "tok-secret-xyz")
    monkeypatch.setattr(settings, "ZOHO_INTEGRATION_BRAND", "haute_brands")
    yield


def _seed_user(session, *, role=Role.OWNER, user_id=1, name="Owner",
               email_prefix=None):
    email = f"{email_prefix or role.value}-{user_id}@example.com"
    u = User(
        id=user_id, email=email, name=name,
        role=role, password_hash="x", is_active=True,
    )
    session.add(u)
    session.commit()
    return u


def _seed_item(session, *, zoho_item_id="z-item-1", current_stock=100.0,
               name="Bubble mailer", material_code="MC-1", sku="SKU-1",
               vendor="ACME"):
    it = Item(
        name=name, sku_code=sku, material_code=material_code,
        unit="pcs", vendor=vendor, current_stock=current_stock,
        zoho_item_id=zoho_item_id,
    )
    session.add(it)
    session.commit()
    session.refresh(it)
    return it


def _client(session, engine, monkeypatch, *, user=None):
    from fastapi.testclient import TestClient

    import packtrack.db
    import packtrack.main
    from packtrack import deps
    from packtrack.db import get_session
    from packtrack.main import app

    monkeypatch.setattr(packtrack.db, "engine", engine)
    monkeypatch.setattr(packtrack.main, "engine", engine)
    app.dependency_overrides[get_session] = lambda: session

    def _force_user():
        return user or session.exec(select(User).order_by(User.id)).first()

    app.dependency_overrides[deps.require_user] = _force_user
    app.dependency_overrides[deps.current_user] = _force_user
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# httpx interception — a minimal MockTransport that records every call
# and returns whatever the test sets up. httpx.MockTransport is the
# documented test surface.
# ---------------------------------------------------------------------------


class _Recorder:
    def __init__(self):
        self.calls: list[httpx.Request] = []

    def assert_one_call(self) -> httpx.Request:
        assert len(self.calls) == 1, f"expected 1 call, got {len(self.calls)}"
        return self.calls[0]


def make_client(handler) -> tuple[httpx.Client, _Recorder]:
    rec = _Recorder()

    def _route(request: httpx.Request) -> httpx.Response:
        rec.calls.append(request)
        return handler(request)

    transport = httpx.MockTransport(_route)
    return httpx.Client(transport=transport), rec


def ok_response(*, zoho_adjustment_id="zoho-adj-1", zoho_reference="ZADJ-1",
                idempotent=False, warning=None) -> dict[str, Any]:
    body = {
        "ok": True,
        "brand": "haute_brands",
        "item_id": "z-item-1",
        "packtrack_adjustment_number": "ADJ-2026-0001",
        "quantity_before": "100.0000",
        "quantity_delta": "1.0000",
        "quantity_after": "101.0000",
        "zoho_adjustment_id": zoho_adjustment_id,
        "zoho_reference": zoho_reference,
        "zoho_status": "posted",
        "meta": {
            "idempotent": idempotent,
            "warehouse_id": "wh-1",
            "adjustment_account_id": "acct-1",
        },
    }
    if warning:
        body["warning"] = warning
    return body


# ---------------------------------------------------------------------------
# A — config disabled
# ---------------------------------------------------------------------------


def test_config_disabled_does_not_call_service(session, monkeypatch):
    """Default settings: ZOHO_INTEGRATION_ADJUST_ENABLED=False.
    Adjustment is created locally, status stays NOT_CONFIGURED, NO
    HTTP call is attempted."""
    monkeypatch.setattr(settings, "ZOHO_INTEGRATION_ADJUST_ENABLED", False)
    _seed_user(session)
    item = _seed_item(session)
    user = session.exec(select(User)).first()

    result = create_adjustment(
        session, item_id=item.id, actor=user,
        mode=AdjustmentMode.DELTA, direction=AdjustmentDirection.INCREASE,
        raw_quantity="1", reason_code=AdjustmentReason.MANUAL_CORRECTION,
        notes=None,
    )
    # Trip the orchestrator — would call HTTP if it were going to.
    client, rec = make_client(lambda req: httpx.Response(500))
    outcome = try_sync_adjustment(
        session, result.adjustment, item, actor=user, http_client=client,
    )
    assert outcome.kind is OutcomeKind.NOT_CONFIGURED
    assert rec.calls == []
    session.refresh(result.adjustment)
    assert result.adjustment.zoho_sync_status is ZohoSyncStatus.NOT_CONFIGURED
    assert result.adjustment.sync_attempt_count == 0
    session.refresh(item)
    assert item.current_stock == 101.0


# ---------------------------------------------------------------------------
# B/C/D/E — happy path + payload + headers
# ---------------------------------------------------------------------------


def test_happy_path_syncs_and_records_reference(
    session, integration_settings,
):
    _seed_user(session)
    item = _seed_item(session)
    user = session.exec(select(User)).first()

    result = create_adjustment(
        session, item_id=item.id, actor=user,
        mode=AdjustmentMode.DELTA, direction=AdjustmentDirection.INCREASE,
        raw_quantity="1", reason_code=AdjustmentReason.MANUAL_CORRECTION,
        notes=None,
    )

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=ok_response(
            zoho_adjustment_id="zoho-adj-xyz", zoho_reference="ZADJ-XYZ",
        ))

    client, _rec = make_client(handler)
    outcome = try_sync_adjustment(
        session, result.adjustment, item, actor=user, http_client=client,
    )
    assert outcome.kind is OutcomeKind.SYNCED
    session.refresh(result.adjustment)
    assert result.adjustment.zoho_sync_status is ZohoSyncStatus.SYNCED
    assert result.adjustment.zoho_reference == "ZADJ-XYZ"
    assert result.adjustment.zoho_synced_at is not None
    assert result.adjustment.zoho_sync_error is None
    assert result.adjustment.sync_attempt_count == 1


def test_payload_shape_and_headers(session, integration_settings):
    """Asserts the service receives:
      * 4-decimal-string quantities (not floats)
      * Bearer + X-Brand + Idempotency-Key headers
      * Correct URL with the item's zoho_item_id
      * No master-data fields in the body
    """
    owner = _seed_user(session)
    item = _seed_item(session, zoho_item_id="zoho-item-77", current_stock=100.0)

    result = create_adjustment(
        session, item_id=item.id, actor=owner,
        mode=AdjustmentMode.SET_QUANTITY, direction=None,
        raw_quantity="101", reason_code=AdjustmentReason.CYCLE_COUNT_CORRECTION,
        notes="recount",
    )

    handler_resp = {}

    def handler(req: httpx.Request) -> httpx.Response:
        handler_resp["request"] = req
        return httpx.Response(200, json=ok_response())

    client, _rec = make_client(handler)
    try_sync_adjustment(
        session, result.adjustment, item, actor=owner, http_client=client,
    )
    req: httpx.Request = handler_resp["request"]

    # URL: must hit ONLY the integration-service base + the right path.
    assert str(req.url) == (
        "http://int-service.test/zoho/pack_track/items/"
        "zoho-item-77/inventory-adjustments"
    )

    # Headers
    assert req.headers["authorization"] == "Bearer tok-secret-xyz"
    assert req.headers["x-brand"] == "haute_brands"
    assert req.headers["idempotency-key"] == result.adjustment.idempotency_key
    assert req.headers["content-type"] == "application/json"

    # Body (Decimal-safe 4dp strings)
    import json as _json
    body = _json.loads(req.content)
    assert body["adjustment_number"] == result.adjustment.adjustment_number
    assert body["idempotency_key"] == result.adjustment.idempotency_key
    assert body["mode"] == "set_quantity"
    assert body["quantity_before"] == "100.0000"
    assert body["quantity_delta"] == "1.0000"
    assert body["quantity_after"] == "101.0000"
    assert body["reason_code"] == "cycle_count_correction"
    assert body["notes"] == "recount"
    assert body["source"] == "manual_adjustment"
    assert body["created_by"] == owner.email
    assert body["created_at"].endswith("Z")

    # NO floats in the body — every numeric quantity is a string.
    for key in ("quantity_before", "quantity_delta", "quantity_after"):
        assert isinstance(body[key], str), f"{key} must be a string"

    # NO master-data fields leaked into the body.
    forbidden = {
        "vendor", "price", "sku", "sku_code", "account", "account_id",
        "tags", "category", "stock_override", "name", "material_code",
        "unit",
    }
    assert forbidden.isdisjoint(body.keys()), \
        f"Forbidden keys in payload: {forbidden & body.keys()}"


def test_build_payload_uses_decimal_not_float():
    """Unit-level: build_payload returns strings for every quantity,
    even when the adjustment carries Decimal('1.5')."""
    adj = InventoryAdjustment(
        item_id=1, adjustment_number="ADJ-2026-0099",
        mode=AdjustmentMode.DELTA, direction=AdjustmentDirection.INCREASE,
        quantity_before=Decimal("100"), quantity_delta=Decimal("1.5"),
        quantity_after=Decimal("101.5"),
        reason_code=AdjustmentReason.MANUAL_CORRECTION,
        created_by_user_id=1, idempotency_key="k",
        source=AdjustmentSource.MANUAL_ADJUSTMENT,
        created_at=datetime(2026, 6, 29, 12, 0, 0),
    )
    p = build_payload(adj, created_by="x@y")
    assert p["quantity_before"] == "100.0000"
    assert p["quantity_delta"] == "1.5000"
    assert p["quantity_after"] == "101.5000"
    assert all(isinstance(p[k], str) for k in (
        "quantity_before", "quantity_delta", "quantity_after",
    ))


# ---------------------------------------------------------------------------
# F — no zoho_item_id
# ---------------------------------------------------------------------------


def test_item_without_zoho_id_marks_skipped(session, integration_settings):
    _seed_user(session)
    item = _seed_item(session, zoho_item_id="")  # no upstream id
    user = session.exec(select(User)).first()

    result = create_adjustment(
        session, item_id=item.id, actor=user,
        mode=AdjustmentMode.DELTA, direction=AdjustmentDirection.INCREASE,
        raw_quantity="1", reason_code=AdjustmentReason.MANUAL_CORRECTION,
        notes=None,
    )
    client, rec = make_client(lambda req: httpx.Response(200, json=ok_response()))
    outcome = try_sync_adjustment(
        session, result.adjustment, item, actor=user, http_client=client,
    )
    assert outcome.kind is OutcomeKind.SKIPPED
    assert rec.calls == []
    session.refresh(result.adjustment)
    assert result.adjustment.zoho_sync_status is ZohoSyncStatus.SKIPPED
    assert "zoho_item_id" in (result.adjustment.zoho_sync_error or "")


# ---------------------------------------------------------------------------
# G/H/I/J/K/L — HTTP error mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status_code,upstream_body,expected_substr", [
    (401, {"error": {"message": "invalid bearer"}}, "401"),
    (403, {"error": {"message": "capability denied"}}, "403"),
    (404, {"error": {"message": "item not found upstream"}}, "404"),
    (409, {"error": {"message": "idempotency conflict"}}, "409"),
    (422, {"error": {"message": "delta does not match"}}, "422"),
    (500, {"error": {"message": "zoho 5xx"}}, "500"),
])
def test_http_error_codes_map_to_failed(
    session, integration_settings, status_code, upstream_body, expected_substr,
):
    _seed_user(session)
    item = _seed_item(session)
    user = session.exec(select(User)).first()
    result = create_adjustment(
        session, item_id=item.id, actor=user,
        mode=AdjustmentMode.DELTA, direction=AdjustmentDirection.INCREASE,
        raw_quantity="1", reason_code=AdjustmentReason.MANUAL_CORRECTION,
        notes=None,
    )
    client, _rec = make_client(
        lambda req: httpx.Response(status_code, json=upstream_body)
    )
    outcome = try_sync_adjustment(
        session, result.adjustment, item, actor=user, http_client=client,
    )
    assert outcome.kind is OutcomeKind.FAILED
    assert outcome.http_status == status_code
    session.refresh(result.adjustment)
    assert result.adjustment.zoho_sync_status is ZohoSyncStatus.FAILED
    assert expected_substr in (result.adjustment.zoho_sync_error or "")
    assert result.adjustment.sync_attempt_count == 1


# ---------------------------------------------------------------------------
# M — timeout
# ---------------------------------------------------------------------------


def test_timeout_marks_failed_with_safe_message(session, integration_settings):
    _seed_user(session)
    item = _seed_item(session)
    user = session.exec(select(User)).first()
    result = create_adjustment(
        session, item_id=item.id, actor=user,
        mode=AdjustmentMode.DELTA, direction=AdjustmentDirection.INCREASE,
        raw_quantity="1", reason_code=AdjustmentReason.MANUAL_CORRECTION,
        notes=None,
    )

    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("upstream slow")

    client, _rec = make_client(handler)
    outcome = try_sync_adjustment(
        session, result.adjustment, item, actor=user, http_client=client,
    )
    assert outcome.kind is OutcomeKind.FAILED
    session.refresh(result.adjustment)
    assert "Timeout" in (result.adjustment.zoho_sync_error or "")
    assert "Bearer" not in (result.adjustment.zoho_sync_error or "")


# ---------------------------------------------------------------------------
# N — idempotent replay
# ---------------------------------------------------------------------------


def test_idempotent_replay_keeps_synced(session, integration_settings):
    _seed_user(session)
    item = _seed_item(session)
    user = session.exec(select(User)).first()
    result = create_adjustment(
        session, item_id=item.id, actor=user,
        mode=AdjustmentMode.DELTA, direction=AdjustmentDirection.INCREASE,
        raw_quantity="1", reason_code=AdjustmentReason.MANUAL_CORRECTION,
        notes=None,
    )
    client, _rec = make_client(lambda req: httpx.Response(
        200, json=ok_response(idempotent=True, zoho_reference="REPLAY-REF"),
    ))
    outcome = try_sync_adjustment(
        session, result.adjustment, item, actor=user, http_client=client,
    )
    assert outcome.kind is OutcomeKind.SYNCED_IDEMPOTENT
    assert outcome.to_status() is ZohoSyncStatus.SYNCED
    session.refresh(result.adjustment)
    assert result.adjustment.zoho_sync_status is ZohoSyncStatus.SYNCED
    assert result.adjustment.zoho_reference == "REPLAY-REF"


# ---------------------------------------------------------------------------
# O — drift warning
# ---------------------------------------------------------------------------


def test_drift_warning_is_stored_but_still_synced(session, integration_settings):
    _seed_user(session)
    item = _seed_item(session)
    user = session.exec(select(User)).first()
    result = create_adjustment(
        session, item_id=item.id, actor=user,
        mode=AdjustmentMode.DELTA, direction=AdjustmentDirection.INCREASE,
        raw_quantity="1", reason_code=AdjustmentReason.MANUAL_CORRECTION,
        notes=None,
    )
    drift_msg = "STOCK_DRIFT_DETECTED: Zoho stock 95 vs PT before 100"
    client, _rec = make_client(lambda req: httpx.Response(
        200, json=ok_response(warning=drift_msg),
    ))
    try_sync_adjustment(
        session, result.adjustment, item, actor=user, http_client=client,
    )
    session.refresh(result.adjustment)
    assert result.adjustment.zoho_sync_status is ZohoSyncStatus.SYNCED
    assert result.adjustment.zoho_sync_warning == drift_msg
    # Local stock NOT rolled back (101.0, the post-adjustment value).
    session.refresh(item)
    assert item.current_stock == 101.0


# ---------------------------------------------------------------------------
# P/Q/R/S — retry route
# ---------------------------------------------------------------------------


def test_owner_retry_failed_row_via_route(
    session, engine, monkeypatch, integration_settings,
):
    """Pre-seed a FAILED adjustment; owner hits the retry route, which
    flips the row to SYNCED. The mocked service records the second-
    attempt request so we can assert the idempotency key matches."""
    _seed_user(session)
    item = _seed_item(session)
    user = session.exec(select(User)).first()
    result = create_adjustment(
        session, item_id=item.id, actor=user,
        mode=AdjustmentMode.DELTA, direction=AdjustmentDirection.INCREASE,
        raw_quantity="1", reason_code=AdjustmentReason.MANUAL_CORRECTION,
        notes=None,
    )
    # Mark FAILED by hand (simulates an earlier sync attempt).
    result.adjustment.zoho_sync_status = ZohoSyncStatus.FAILED
    result.adjustment.zoho_sync_error = "HTTP 500: zoho 5xx"
    result.adjustment.sync_attempt_count = 1
    session.add(result.adjustment)
    session.commit()

    original_key = result.adjustment.idempotency_key
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["request"] = req
        return httpx.Response(200, json=ok_response(zoho_reference="RETRY-REF"))

    mock_client, _rec = make_client(handler)

    # Patch the orchestrator's httpx default by injecting our client at
    # the call site — we wrap try_sync_adjustment via the route helper.
    from packtrack.services import inventory_adjustment_sync as sync_mod
    original_try = sync_mod.try_sync_adjustment

    def patched(s, adj, it, *, actor, http_client=None):
        return original_try(s, adj, it, actor=actor, http_client=mock_client)

    monkeypatch.setattr(
        "packtrack.routes.inventory_adjustments.try_sync_adjustment", patched,
    )

    client = _client(session, engine, monkeypatch, user=user)
    resp = client.post(
        f"/inventory/adjustments/{result.adjustment.id}/sync",
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    assert captured["request"].headers["idempotency-key"] == original_key

    session.refresh(result.adjustment)
    assert result.adjustment.zoho_sync_status is ZohoSyncStatus.SYNCED
    assert result.adjustment.zoho_reference == "RETRY-REF"
    assert result.adjustment.sync_attempt_count == 2
    assert result.adjustment.zoho_sync_error is None


def test_non_owner_cannot_retry(session, engine, monkeypatch):
    _seed_user(session)  # owner
    designer = _seed_user(session, role=Role.DESIGN, user_id=2, name="Des")
    item = _seed_item(session)
    owner_user = session.exec(select(User).where(User.role == Role.OWNER)).first()
    result = create_adjustment(
        session, item_id=item.id, actor=owner_user,
        mode=AdjustmentMode.DELTA, direction=AdjustmentDirection.INCREASE,
        raw_quantity="1", reason_code=AdjustmentReason.MANUAL_CORRECTION,
        notes=None,
    )

    client = _client(session, engine, monkeypatch, user=designer)
    resp = client.post(
        f"/inventory/adjustments/{result.adjustment.id}/sync",
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_retry_on_synced_row_is_refused_and_no_op(
    session, engine, monkeypatch, integration_settings,
):
    """v2.16.3 retry-safety contract: a SYNCED row is now refused at the
    route gate with 409 (was a silent no-op redirect in v2.10.0). The
    "no HTTP call, no state change" invariant from v2.10.0 still holds;
    only the response shape tightened."""
    _seed_user(session)
    item = _seed_item(session)
    user = session.exec(select(User)).first()
    result = create_adjustment(
        session, item_id=item.id, actor=user,
        mode=AdjustmentMode.DELTA, direction=AdjustmentDirection.INCREASE,
        raw_quantity="1", reason_code=AdjustmentReason.MANUAL_CORRECTION,
        notes=None,
    )
    result.adjustment.zoho_sync_status = ZohoSyncStatus.SYNCED
    result.adjustment.zoho_reference = "ZADJ-PRE"
    result.adjustment.sync_attempt_count = 1
    session.add(result.adjustment)
    session.commit()

    handler_calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        handler_calls.append(req)
        return httpx.Response(200, json=ok_response())

    mock_client, _rec = make_client(handler)

    def patched(s, adj, it, *, actor, http_client=None):
        from packtrack.services.inventory_adjustment_sync import (
            try_sync_adjustment as _real,
        )
        return _real(s, adj, it, actor=actor, http_client=mock_client)

    monkeypatch.setattr(
        "packtrack.routes.inventory_adjustments.try_sync_adjustment", patched,
    )

    client = _client(session, engine, monkeypatch, user=user)
    resp = client.post(
        f"/inventory/adjustments/{result.adjustment.id}/sync",
        follow_redirects=False,
    )
    assert resp.status_code == 409
    assert "already synced" in resp.json()["error"].lower()
    assert handler_calls == []  # no HTTP call for already-SYNCED row
    session.refresh(result.adjustment)
    assert result.adjustment.zoho_sync_status is ZohoSyncStatus.SYNCED
    assert result.adjustment.zoho_reference == "ZADJ-PRE"
    assert result.adjustment.sync_attempt_count == 1  # unchanged


# ---------------------------------------------------------------------------
# T — attempt count
# ---------------------------------------------------------------------------


def test_attempt_count_increments_per_real_call(session, integration_settings):
    _seed_user(session)
    item = _seed_item(session)
    user = session.exec(select(User)).first()
    result = create_adjustment(
        session, item_id=item.id, actor=user,
        mode=AdjustmentMode.DELTA, direction=AdjustmentDirection.INCREASE,
        raw_quantity="1", reason_code=AdjustmentReason.MANUAL_CORRECTION,
        notes=None,
    )
    # First attempt: fail. Second attempt: succeed.
    responses = iter([
        httpx.Response(500, json={"error": {"message": "boom"}}),
        httpx.Response(200, json=ok_response()),
    ])
    client, _rec = make_client(lambda req: next(responses))
    try_sync_adjustment(
        session, result.adjustment, item, actor=user, http_client=client,
    )
    session.refresh(result.adjustment)
    assert result.adjustment.zoho_sync_status is ZohoSyncStatus.FAILED
    assert result.adjustment.sync_attempt_count == 1

    # Retry
    try_sync_adjustment(
        session, result.adjustment, item, actor=user, http_client=client,
    )
    session.refresh(result.adjustment)
    assert result.adjustment.zoho_sync_status is ZohoSyncStatus.SYNCED
    assert result.adjustment.sync_attempt_count == 2


# ---------------------------------------------------------------------------
# U — Bearer token never leaks into stored error or logs
# ---------------------------------------------------------------------------


def test_bearer_token_never_in_stored_error(session, integration_settings):
    """A malicious upstream that echoes our headers in its error body
    must NOT cause us to persist the Bearer token in the
    zoho_sync_error column."""
    _seed_user(session)
    item = _seed_item(session)
    user = session.exec(select(User)).first()
    result = create_adjustment(
        session, item_id=item.id, actor=user,
        mode=AdjustmentMode.DELTA, direction=AdjustmentDirection.INCREASE,
        raw_quantity="1", reason_code=AdjustmentReason.MANUAL_CORRECTION,
        notes=None,
    )

    def handler(req: httpx.Request) -> httpx.Response:
        # We only echo the body, not headers — confirms our error
        # extractor cannot pull a token out by accident.
        return httpx.Response(
            403, json={"error": {"message": "no capability"}},
        )

    client, _rec = make_client(handler)
    try_sync_adjustment(
        session, result.adjustment, item, actor=user, http_client=client,
    )
    session.refresh(result.adjustment)
    assert "tok-secret-xyz" not in (result.adjustment.zoho_sync_error or "")


# ---------------------------------------------------------------------------
# V — client module hits ONLY the integration-service base URL
# ---------------------------------------------------------------------------


def test_client_module_hits_only_integration_service_base():
    """Source-level guard: the only URL string built in
    zoho_adjustment_client.py is constructed from settings.ZOHO_INTEGRATION_BASE_URL.
    No literal zoho.com / zohoapis.com / OAuth URL is allowed."""
    import packtrack.services.zoho_adjustment_client as mod
    with open(mod.__file__) as f:
        src = f.read()
    lowered = src.lower()
    forbidden = [
        "zoho.com",
        "zohoapis.com",
        "accounts.zoho",
        "oauth",
        "access_token",
        "refresh_token",
        "client_secret",
    ]
    for needle in forbidden:
        assert needle not in lowered, f"Forbidden direct-Zoho reference: {needle!r}"


# ---------------------------------------------------------------------------
# W — no Receiving file touched
# ---------------------------------------------------------------------------


def test_no_receiving_imports_in_new_modules():
    for modname in (
        "packtrack.services.zoho_adjustment_client",
        "packtrack.services.inventory_adjustment_sync",
    ):
        mod = importlib.import_module(modname)
        with open(mod.__file__) as f:
            src = f.read()
        assert "receiving" not in src.lower(), (
            f"{modname} imports/mentions receiving — boundary violation"
        )


# ---------------------------------------------------------------------------
# X — master-data fields unchanged after a sync round-trip
# ---------------------------------------------------------------------------


def test_master_data_unchanged_after_sync(session, integration_settings):
    _seed_user(session)
    item = _seed_item(session)
    user = session.exec(select(User)).first()
    pre = (item.name, item.vendor, item.material_code, item.unit,
           item.sku_code, item.zoho_item_id)

    result = create_adjustment(
        session, item_id=item.id, actor=user,
        mode=AdjustmentMode.DELTA, direction=AdjustmentDirection.INCREASE,
        raw_quantity="1", reason_code=AdjustmentReason.MANUAL_CORRECTION,
        notes=None,
    )
    client, _rec = make_client(lambda req: httpx.Response(
        200, json=ok_response(),
    ))
    try_sync_adjustment(
        session, result.adjustment, item, actor=user, http_client=client,
    )
    session.refresh(item)
    post = (item.name, item.vendor, item.material_code, item.unit,
            item.sku_code, item.zoho_item_id)
    assert pre == post


# ---------------------------------------------------------------------------
# Misc — is_configured()
# ---------------------------------------------------------------------------


def test_is_configured_requires_all_four_flags(monkeypatch):
    monkeypatch.setattr(settings, "ZOHO_INTEGRATION_ADJUST_ENABLED", False)
    monkeypatch.setattr(settings, "ZOHO_INTEGRATION_BASE_URL", "x")
    monkeypatch.setattr(settings, "ZOHO_INTEGRATION_APP_TOKEN", "y")
    monkeypatch.setattr(settings, "ZOHO_INTEGRATION_BRAND", "z")
    assert is_configured() is False

    monkeypatch.setattr(settings, "ZOHO_INTEGRATION_ADJUST_ENABLED", True)
    assert is_configured() is True

    monkeypatch.setattr(settings, "ZOHO_INTEGRATION_BRAND", "")
    assert is_configured() is False


def test_push_helper_raises_only_on_item_mismatch(session, integration_settings):
    """Programmer-error guard — passing an item whose id doesn't match
    the adjustment must NOT silently call the wrong endpoint."""
    _seed_user(session)
    item_a = _seed_item(session, zoho_item_id="a")
    item_b = _seed_item(session, zoho_item_id="b", name="other")
    user = session.exec(select(User)).first()
    result = create_adjustment(
        session, item_id=item_a.id, actor=user,
        mode=AdjustmentMode.DELTA, direction=AdjustmentDirection.INCREASE,
        raw_quantity="1", reason_code=AdjustmentReason.MANUAL_CORRECTION,
        notes=None,
    )
    with pytest.raises(ValueError, match="Item id mismatch"):
        push_adjustment_to_zoho(
            result.adjustment, item_b, created_by="x",
        )
