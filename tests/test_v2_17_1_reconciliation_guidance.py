"""v2.17.1: guided operator actions on the reconciliation dashboard.

The v2.17.0 dashboard surfaced exception data; v2.17.1 adds explicit
"what should I do next?" copy per row + an item-detail → reconciliation
cross-link + a top-level "How to resolve exceptions" help block.

This module asserts:
  * recommended_action helper output (pure functions, tested in isolation)
  * each row carries the correct recommended_action when built by the service
  * dashboard renders the action text + the how-to-resolve disclosure
  * variance-bearing items get a "Review reconciliation" cross-link on
    item detail (URL pre-filters by material code → SKU → name)
  * non-owner users still cannot see mutation actions
  * the dashboard remains strictly read-only

Strategy: drive helpers directly, then drive route + template for
visibility/copy. No real Zoho/OAuth imports; integration client never
touched.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("PACKTRACK_SECRET_KEY", "test-secret")

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from packtrack.models import (
    AdjustmentDirection,
    AdjustmentMode,
    AdjustmentReason,
    InventoryAdjustment,
    Item,
    Role,
    User,
    ZohoSyncStatus,
)
from packtrack.services.inventory_adjustment_sync import (
    RetryBlockReason,
    RetryEligibility,
)
from packtrack.services.inventory_adjustments import create_adjustment
from packtrack.services.inventory_reconciliation import (
    StaleSnapshotStatus,
    VarianceStatus,
    build_dashboard,
    compute_stale_snapshot_rows,
    compute_sync_exception_rows,
    compute_variance_rows,
    recommended_exception_action,
    recommended_stale_action,
    recommended_variance_action,
)

NOW = datetime(2026, 6, 30, 12, 0, 0)
STALE = timedelta(hours=24)


# --- fixtures -------------------------------------------------------------


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


def _seed_user(session, *, role=Role.OWNER, user_id=1, name="Owner") -> User:
    u = User(
        id=user_id, email=f"{role.value}-{user_id}@example.com", name=name,
        role=role, password_hash="x", is_active=True,
    )
    session.add(u)
    session.commit()
    return u


def _seed_item(
    session, *,
    name="Bubble mailer", sku="SKU-1", material_code="MC-1",
    product_line="MASTER CASE", current_stock=100.0,
    zoho_item_id: str | None = "z-1",
    snapshot: Decimal | None = None,
    snapshot_at: datetime | None = None,
) -> Item:
    it = Item(
        name=name, sku_code=sku, material_code=material_code,
        product_line=product_line, unit="pcs", vendor="ACME",
        current_stock=current_stock, zoho_item_id=zoho_item_id,
        last_zoho_stock_snapshot=snapshot,
        last_zoho_stock_snapshot_at=snapshot_at,
    )
    session.add(it)
    session.commit()
    session.refresh(it)
    return it


def _make_adj(
    session, item, user, *,
    delta: str = "1",
    status: ZohoSyncStatus = ZohoSyncStatus.FAILED,
    reversal_of: int | None = None,
    voided_at: datetime | None = None,
) -> InventoryAdjustment:
    result = create_adjustment(
        session, item_id=item.id, actor=user,
        mode=AdjustmentMode.DELTA,
        direction=AdjustmentDirection.INCREASE if Decimal(delta) > 0 else AdjustmentDirection.DECREASE,
        raw_quantity=str(abs(Decimal(delta))),
        reason_code=AdjustmentReason.MANUAL_CORRECTION,
        notes=None, reversal_of_adjustment_id=reversal_of,
    )
    adj = result.adjustment
    adj.zoho_sync_status = status
    adj.voided_at = voided_at
    session.add(adj)
    session.commit()
    session.refresh(adj)
    return adj


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


# --- A. recommended-action helpers (pure) ---------------------------------


def test_recommended_variance_action_packtrack_higher():
    assert recommended_variance_action(VarianceStatus.PACKTRACK_HIGHER) == (
        "Review recent adjustments / confirm Zoho sync"
    )


def test_recommended_variance_action_zoho_higher():
    assert recommended_variance_action(VarianceStatus.ZOHO_HIGHER) == (
        "Cycle count or review PackTrack movements"
    )


def test_recommended_variance_action_snapshot_stale():
    assert recommended_variance_action(VarianceStatus.SNAPSHOT_STALE) == (
        "Wait for next sync or review Zoho sync health"
    )


def test_recommended_variance_action_in_sync_returns_empty():
    """IN_SYNC rows never appear in the variance table; the empty action
    string is the signal "no operator step needed"."""
    assert recommended_variance_action(VarianceStatus.IN_SYNC) == ""


def test_recommended_stale_action_missing_with_zoho_id():
    assert recommended_stale_action(StaleSnapshotStatus.MISSING, "z-99") == (
        "Await snapshot sync / check integration"
    )


def test_recommended_stale_action_missing_without_zoho_id():
    assert recommended_stale_action(StaleSnapshotStatus.MISSING, None) == (
        "Link Zoho item or mark as local-only"
    )


def test_recommended_stale_action_stale_status():
    assert recommended_stale_action(StaleSnapshotStatus.STALE, "z-99") == (
        "Wait for next sync or review Zoho sync health"
    )


def test_recommended_exception_action_eligible_failed_says_retry():
    elig = RetryEligibility.ok()
    assert recommended_exception_action(ZohoSyncStatus.FAILED, elig) == "Retry sync"


def test_recommended_exception_action_not_configured_says_check_config():
    elig = RetryEligibility.ok()
    assert recommended_exception_action(
        ZohoSyncStatus.NOT_CONFIGURED, elig,
    ) == "Check integration configuration"


def test_recommended_exception_action_skipped_says_link_item():
    elig = RetryEligibility.ok()
    assert recommended_exception_action(
        ZohoSyncStatus.SKIPPED, elig,
    ) == "Link item to Zoho before syncing"


def test_recommended_exception_action_reversed_says_compensated():
    elig = RetryEligibility.block(RetryBlockReason.REVERSED_LOCALLY)
    assert recommended_exception_action(ZohoSyncStatus.FAILED, elig) == (
        "No action — already compensated locally"
    )


def test_recommended_exception_action_reversal_of_unsynced_says_drift_block():
    elig = RetryEligibility.block(RetryBlockReason.REVERSAL_OF_UNSYNCED)
    assert recommended_exception_action(
        ZohoSyncStatus.NOT_CONFIGURED, elig,
    ) == "No action — retry blocked to prevent drift"


def test_recommended_exception_action_voided_says_voided():
    elig = RetryEligibility.block(RetryBlockReason.VOIDED)
    assert recommended_exception_action(ZohoSyncStatus.FAILED, elig) == (
        "No action — voided locally"
    )


# --- B. service builders attach the action to each row --------------------


def test_variance_row_carries_recommended_action(session):
    _seed_item(
        session, current_stock=110.0,
        snapshot=Decimal("100"),
        snapshot_at=NOW - timedelta(hours=1),
    )
    rows = compute_variance_rows(session, stale_threshold=STALE, now=NOW)
    assert rows[0].recommended_action == (
        "Review recent adjustments / confirm Zoho sync"
    )


def test_stale_row_carries_recommended_action(session):
    # Has zoho_item_id → integration-class guidance.
    _seed_item(session, snapshot=None, snapshot_at=None, zoho_item_id="z-99")
    rows = compute_stale_snapshot_rows(session, stale_threshold=STALE, now=NOW)
    assert rows[0].recommended_action == (
        "Await snapshot sync / check integration"
    )


def test_stale_row_local_only_carries_link_guidance(session):
    _seed_item(
        session, name="Local item", sku="LOCAL-1",
        snapshot=None, snapshot_at=None, zoho_item_id=None,
    )
    rows = compute_stale_snapshot_rows(session, stale_threshold=STALE, now=NOW)
    assert rows[0].recommended_action == (
        "Link Zoho item or mark as local-only"
    )


def test_exception_row_eligible_carries_retry_guidance(session):
    user = _seed_user(session)
    item = _seed_item(session)
    _make_adj(session, item, user, status=ZohoSyncStatus.FAILED)
    rows = compute_sync_exception_rows(session)
    assert rows[0].recommended_action == "Retry sync"


def test_exception_row_reversed_carries_blocked_guidance(session):
    user = _seed_user(session)
    item = _seed_item(session)
    original = _make_adj(session, item, user, delta="1",
                         status=ZohoSyncStatus.FAILED)
    _make_adj(session, item, user, delta="-1",
              status=ZohoSyncStatus.NOT_CONFIGURED,
              reversal_of=original.id)
    rows = compute_sync_exception_rows(session)
    original_row = next(r for r in rows if r.adjustment_id == original.id)
    reversal_row = next(r for r in rows if r.adjustment_id != original.id)
    assert original_row.recommended_action == (
        "No action — already compensated locally"
    )
    assert reversal_row.recommended_action == (
        "No action — retry blocked to prevent drift"
    )


# --- C. template renders guidance -----------------------------------------


def test_dashboard_renders_how_to_resolve_block(session, engine, monkeypatch):
    _seed_user(session)
    client = _client(session, engine, monkeypatch)
    body = client.get("/inventory/reconciliation").text
    assert 'data-testid="reconciliation-how-to-resolve"' in body
    assert "How to resolve exceptions" in body
    # Each policy point appears in the disclosure body.
    assert "Adjust quantity" in body
    assert "Retry sync" in body
    assert "Zoho-snapshot variance is a" in body


def test_dashboard_renders_variance_recommended_action(
    session, engine, monkeypatch,
):
    _seed_user(session)
    item = _seed_item(
        session, current_stock=110.0,
        snapshot=Decimal("100"),
        snapshot_at=NOW - timedelta(hours=1),
    )
    client = _client(session, engine, monkeypatch)
    body = client.get("/inventory/reconciliation").text
    assert f'data-testid="reconciliation-variance-action-{item.id}"' in body
    assert "Review recent adjustments / confirm Zoho sync" in body


def test_dashboard_renders_stale_recommended_action(
    session, engine, monkeypatch,
):
    _seed_user(session)
    item = _seed_item(session, snapshot=None, zoho_item_id="z-9")
    client = _client(session, engine, monkeypatch)
    body = client.get("/inventory/reconciliation").text
    assert f'data-testid="reconciliation-stale-action-{item.id}"' in body
    assert "Await snapshot sync / check integration" in body


def test_dashboard_renders_exception_recommended_action(
    session, engine, monkeypatch,
):
    user = _seed_user(session)
    item = _seed_item(session)
    adj = _make_adj(session, item, user, status=ZohoSyncStatus.FAILED)
    client = _client(session, engine, monkeypatch)
    body = client.get("/inventory/reconciliation").text
    assert f'data-testid="reconciliation-exception-action-{adj.id}"' in body
    # Eligible plain FAILED row → Retry sync copy (also appears as button label).
    assert "Retry sync" in body


def test_dashboard_blocked_row_shows_no_action_copy(
    session, engine, monkeypatch,
):
    user = _seed_user(session)
    item = _seed_item(session)
    original = _make_adj(session, item, user, delta="1",
                         status=ZohoSyncStatus.FAILED)
    _make_adj(session, item, user, delta="-1",
              status=ZohoSyncStatus.NOT_CONFIGURED,
              reversal_of=original.id)
    client = _client(session, engine, monkeypatch)
    body = client.get("/inventory/reconciliation").text
    assert "No action — already compensated locally" in body
    assert "No action — retry blocked to prevent drift" in body


# --- D. non-owner cannot see mutation buttons -----------------------------


def test_non_owner_sees_action_copy_but_no_retry_button(
    session, engine, monkeypatch,
):
    owner = _seed_user(session, role=Role.OWNER)
    designer = _seed_user(session, role=Role.DESIGN, user_id=2, name="Des")
    item = _seed_item(session)
    adj = _make_adj(session, item, owner, status=ZohoSyncStatus.FAILED)
    client = _client(session, engine, monkeypatch, user=designer)
    body = client.get("/inventory/reconciliation").text
    # Action copy still visible (read-only guidance is universal).
    assert f'data-testid="reconciliation-exception-action-{adj.id}"' in body
    assert "Retry sync" in body  # copy text appears in the Next step column
    # But the mutation form is hidden.
    assert f'data-testid="reconciliation-retry-form-{adj.id}"' not in body


# --- E. item detail "Review reconciliation" cross-link --------------------


def test_item_detail_review_reconciliation_link_when_variance(
    session, engine, monkeypatch,
):
    """Item detail should surface a subtle cross-link to the dashboard,
    pre-filtered by the item's material code, when there's a variance."""
    user = _seed_user(session)
    item = _seed_item(
        session, current_stock=110.0,
        material_code="MC-X9",
        snapshot=Decimal("100"),
        snapshot_at=NOW - timedelta(hours=1),
    )
    # Need extended item detail patched off so the page doesn't try to
    # hit zoho-item-detail HTTP. Mirror the pattern from the master-data
    # editor tests.
    import packtrack.routes.inventory as inv
    from packtrack.services.zoho_item_detail import ExtendedItemDetail
    monkeypatch.setattr(
        inv, "build_extended_detail",
        lambda _zid: ExtendedItemDetail(
            available=False, metadata_available=False,
            item={}, custom_fields=[], categories=[], field_policy={},
        ),
    )
    _ = user  # mark used so ruff doesn't flag
    client = _client(session, engine, monkeypatch)
    body = client.get(f"/inventory/{item.id}").text
    assert 'data-testid="item-detail-review-reconciliation"' in body
    # URL-encoded material code lands as the q filter.
    assert "/inventory/reconciliation?q=MC-X9" in body


def test_item_detail_no_reconciliation_link_when_in_sync(
    session, engine, monkeypatch,
):
    """No cross-link should render when PT == Zoho snapshot — there's
    nothing for the operator to investigate."""
    _seed_user(session)
    item = _seed_item(
        session, current_stock=100.0,
        snapshot=Decimal("100"),
        snapshot_at=NOW - timedelta(hours=1),
    )
    import packtrack.routes.inventory as inv
    from packtrack.services.zoho_item_detail import ExtendedItemDetail
    monkeypatch.setattr(
        inv, "build_extended_detail",
        lambda _zid: ExtendedItemDetail(
            available=False, metadata_available=False,
            item={}, custom_fields=[], categories=[], field_policy={},
        ),
    )
    client = _client(session, engine, monkeypatch)
    body = client.get(f"/inventory/{item.id}").text
    assert 'data-testid="item-detail-review-reconciliation"' not in body


# --- F. item-id / q filter for the cross-link round-trip ------------------


def test_reconciliation_q_filter_round_trip_from_item_detail(
    session, engine, monkeypatch,
):
    """Land the user on the dashboard pre-filtered by an item's material
    code (the URL the v2.17.1 cross-link generates) and verify only that
    item's row appears in the variance section."""
    user = _seed_user(session)
    target = _seed_item(
        session, name="Target", sku="SKU-T", material_code="MC-XYZ",
        current_stock=10.0, snapshot=Decimal("5"),
        snapshot_at=NOW - timedelta(hours=1),
        zoho_item_id="z-target",
    )
    _seed_item(
        session, name="Other", sku="SKU-O", material_code="MC-OTHER",
        current_stock=10.0, snapshot=Decimal("5"),
        snapshot_at=NOW - timedelta(hours=1),
        zoho_item_id="z-other",
    )
    _ = user
    client = _client(session, engine, monkeypatch)
    body = client.get("/inventory/reconciliation?q=MC-XYZ").text
    assert f'data-testid="reconciliation-variance-row-{target.id}"' in body
    assert "MC-OTHER" not in body


# --- G. read-only invariants + import-surface defense ---------------------


def test_dashboard_build_remains_read_only_with_actions(session):
    """The v2.17.1 recommended_action additions must not introduce any
    write. Snapshot every relevant field and assert byte-equality."""
    user = _seed_user(session)
    item = _seed_item(
        session, current_stock=120.0, snapshot=Decimal("100"),
        snapshot_at=NOW - timedelta(hours=1),
    )
    adj = _make_adj(session, item, user, status=ZohoSyncStatus.FAILED)
    pre = (
        item.current_stock, item.last_zoho_stock_snapshot,
        item.last_zoho_stock_snapshot_at,
        adj.zoho_sync_status, adj.zoho_reference, adj.zoho_sync_error,
        adj.voided_at, adj.quantity_before, adj.quantity_delta,
    )
    build_dashboard(session, stale_threshold_hours=24, now=NOW, reason_labels={})
    session.refresh(item)
    session.refresh(adj)
    post = (
        item.current_stock, item.last_zoho_stock_snapshot,
        item.last_zoho_stock_snapshot_at,
        adj.zoho_sync_status, adj.zoho_reference, adj.zoho_sync_error,
        adj.voided_at, adj.quantity_before, adj.quantity_delta,
    )
    assert pre == post


def test_no_direct_zoho_or_oauth_imports_in_v2_17_1_changes():
    """v2.17.1 only added recommended_action copy + a template link.
    Verify no integration client was inlined."""
    import packtrack.services.inventory_reconciliation as svc_mod
    forbidden = ("zoho.oauth", "zohocrmsdk", "zohoinventory", "requests_oauthlib")
    with open(svc_mod.__file__) as fh:
        src = fh.read()
    for bad in forbidden:
        assert bad not in src, f"{svc_mod.__name__} imports {bad}"
