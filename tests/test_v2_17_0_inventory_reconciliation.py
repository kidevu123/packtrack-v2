"""v2.17.0: Inventory reconciliation & sync-exceptions dashboard.

The dashboard is the operator's single place to see items + adjustment
rows that need attention. This module tests the three sections
(variance / stale snapshot / sync exceptions), the summary cards, every
filter combination, the v2.16.3 retry-eligibility surfacing, and the
hard read-only invariant (the dashboard never mutates stock or any
ledger row).

Strategy: drive the service layer directly for the section + summary
math, then drive the route + template for visibility / filter / owner
behavior. All HTTP-free; the integration client is never touched.
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
from packtrack.services.inventory_adjustments import create_adjustment
from packtrack.services.inventory_reconciliation import (
    Filters,
    StaleSnapshotStatus,
    VarianceStatus,
    apply_filters,
    build_dashboard,
    compute_stale_snapshot_rows,
    compute_summary,
    compute_sync_exception_rows,
    compute_variance_rows,
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
    name="Bubble mailer",
    sku="SKU-1",
    material_code="MC-1",
    product_line="MASTER CASE",
    current_stock=100.0,
    zoho_item_id: str | None = None,
    snapshot: Decimal | None = None,
    snapshot_at: datetime | None = None,
) -> Item:
    # zoho_item_id has unique=True in the model. Derive a per-row id from
    # the sku when the caller didn't pin one, so multi-item tests don't
    # collide on the shared default.
    zid = zoho_item_id if zoho_item_id is not None else f"z-{sku}"
    it = Item(
        name=name, sku_code=sku, material_code=material_code,
        product_line=product_line, unit="pcs", vendor="ACME",
        current_stock=current_stock, zoho_item_id=zid,
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
    zoho_reference: str | None = None,
) -> InventoryAdjustment:
    result = create_adjustment(
        session,
        item_id=item.id, actor=user,
        mode=AdjustmentMode.DELTA,
        direction=AdjustmentDirection.INCREASE if Decimal(delta) > 0 else AdjustmentDirection.DECREASE,
        raw_quantity=str(abs(Decimal(delta))),
        reason_code=AdjustmentReason.MANUAL_CORRECTION,
        notes=None, reversal_of_adjustment_id=reversal_of,
    )
    adj = result.adjustment
    adj.zoho_sync_status = status
    adj.voided_at = voided_at
    adj.zoho_reference = zoho_reference
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


# --- A. service-level: variance section -----------------------------------


def test_variance_row_appears_when_pt_differs_from_snapshot(session):
    item = _seed_item(
        session, current_stock=110.0,
        snapshot=Decimal("100"),
        snapshot_at=NOW - timedelta(hours=1),
    )
    rows = compute_variance_rows(session, stale_threshold=STALE, now=NOW)
    assert len(rows) == 1
    r = rows[0]
    assert r.item_id == item.id
    assert r.variance == Decimal("10")
    assert r.status is VarianceStatus.PACKTRACK_HIGHER
    assert r.snapshot_stale is False


def test_variance_row_zoho_higher_status(session):
    _seed_item(
        session, current_stock=90.0,
        snapshot=Decimal("100"),
        snapshot_at=NOW - timedelta(hours=1),
    )
    rows = compute_variance_rows(session, stale_threshold=STALE, now=NOW)
    assert rows[0].status is VarianceStatus.ZOHO_HIGHER
    assert rows[0].variance == Decimal("-10")


def test_in_sync_item_is_excluded_from_variance_rows(session):
    _seed_item(
        session, current_stock=100.0,
        snapshot=Decimal("100"),
        snapshot_at=NOW - timedelta(hours=1),
    )
    rows = compute_variance_rows(session, stale_threshold=STALE, now=NOW)
    assert rows == []


def test_stale_but_equal_snapshot_still_listed(session):
    """Stale snapshot is its own concern even when values match — the
    operator should re-confirm before trusting it."""
    _seed_item(
        session, current_stock=100.0,
        snapshot=Decimal("100"),
        snapshot_at=NOW - timedelta(hours=48),  # past 24h threshold
    )
    rows = compute_variance_rows(session, stale_threshold=STALE, now=NOW)
    assert len(rows) == 1
    assert rows[0].status is VarianceStatus.SNAPSHOT_STALE


# --- B. service-level: stale / missing snapshot section -------------------


def test_missing_snapshot_row(session):
    item = _seed_item(session, snapshot=None, snapshot_at=None)
    rows = compute_stale_snapshot_rows(session, stale_threshold=STALE, now=NOW)
    assert len(rows) == 1
    assert rows[0].item_id == item.id
    assert rows[0].status is StaleSnapshotStatus.MISSING


def test_stale_snapshot_row(session):
    item = _seed_item(
        session, snapshot=Decimal("100"),
        snapshot_at=NOW - timedelta(hours=30),
    )
    rows = compute_stale_snapshot_rows(session, stale_threshold=STALE, now=NOW)
    assert len(rows) == 1
    assert rows[0].item_id == item.id
    assert rows[0].status is StaleSnapshotStatus.STALE


def test_fresh_snapshot_does_not_appear_in_stale_section(session):
    _seed_item(
        session, snapshot=Decimal("100"),
        snapshot_at=NOW - timedelta(hours=2),
    )
    rows = compute_stale_snapshot_rows(session, stale_threshold=STALE, now=NOW)
    assert rows == []


def test_missing_rows_sort_before_stale_rows(session):
    _seed_item(
        session, name="Stale", sku="SKU-stale",
        snapshot=Decimal("1"), snapshot_at=NOW - timedelta(hours=48),
    )
    _seed_item(session, name="Missing", sku="SKU-missing")  # snapshot=None
    rows = compute_stale_snapshot_rows(session, stale_threshold=STALE, now=NOW)
    statuses = [r.status for r in rows]
    assert statuses == [StaleSnapshotStatus.MISSING, StaleSnapshotStatus.STALE]


# --- C. service-level: sync exceptions section ----------------------------


def test_failed_adjustment_appears_in_exceptions(session):
    user = _seed_user(session)
    item = _seed_item(session)
    adj = _make_adj(session, item, user, status=ZohoSyncStatus.FAILED)
    rows = compute_sync_exception_rows(session)
    assert len(rows) == 1
    assert rows[0].adjustment_id == adj.id
    assert rows[0].eligibility.allowed is True  # plain FAILED, no reversal


def test_synced_adjustment_does_not_appear_in_exceptions(session):
    user = _seed_user(session)
    item = _seed_item(session)
    _make_adj(session, item, user, status=ZohoSyncStatus.SYNCED,
              zoho_reference="ZADJ-1")
    rows = compute_sync_exception_rows(session)
    assert rows == []


def test_reversed_adjustment_shows_blocked_reason(session):
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
    assert original_row.eligibility.allowed is False
    assert original_row.eligibility.reason.value == "reversed_locally"
    assert reversal_row.eligibility.allowed is False
    assert reversal_row.eligibility.reason.value == "reversal_of_unsynced"


# --- D. summary cards -----------------------------------------------------


def test_summary_counts_are_correct(session):
    user = _seed_user(session)
    # 1 in sync, 1 variance, 1 stale, 1 missing — 4 items total
    _seed_item(session, name="aligned", sku="A",
               current_stock=10.0, snapshot=Decimal("10"),
               snapshot_at=NOW - timedelta(hours=1))
    _seed_item(session, name="varies", sku="B",
               current_stock=15.0, snapshot=Decimal("10"),
               snapshot_at=NOW - timedelta(hours=1))
    _seed_item(session, name="stale", sku="C",
               current_stock=20.0, snapshot=Decimal("20"),
               snapshot_at=NOW - timedelta(hours=48))
    item_missing = _seed_item(session, name="missing", sku="D",
                              current_stock=30.0,
                              snapshot=None, snapshot_at=None)

    # 2 exceptions: 1 retryable failed, 1 blocked synced (not in exceptions)
    _make_adj(session, item_missing, user, status=ZohoSyncStatus.FAILED)

    summary = compute_summary(session, stale_threshold=STALE, now=NOW)
    assert summary.total_items == 4
    assert summary.items_in_sync == 1
    assert summary.items_with_variance == 1
    assert summary.items_stale_or_missing == 2  # stale + missing
    assert summary.failed_sync_rows == 1
    assert summary.retryable_sync_rows == 1
    assert summary.blocked_sync_rows == 0


def test_summary_counts_blocked_exceptions(session):
    user = _seed_user(session)
    item = _seed_item(session)
    original = _make_adj(session, item, user, delta="1",
                         status=ZohoSyncStatus.FAILED)
    _make_adj(session, item, user, delta="-1",
              status=ZohoSyncStatus.NOT_CONFIGURED,
              reversal_of=original.id)
    summary = compute_summary(session, stale_threshold=STALE, now=NOW)
    assert summary.retryable_sync_rows == 0
    # Both rows are blocked: original by reversed_locally, reversal by
    # reversal_of_unsynced.
    assert summary.blocked_sync_rows == 2


# --- E. filters -----------------------------------------------------------


def test_search_filter_matches_name(session):
    _seed_user(session)
    a = _seed_item(session, name="Apple", sku="A",
                   current_stock=10.0, snapshot=Decimal("5"),
                   snapshot_at=NOW - timedelta(hours=1))
    _seed_item(session, name="Banana", sku="B",
               current_stock=10.0, snapshot=Decimal("5"),
               snapshot_at=NOW - timedelta(hours=1))
    variance = compute_variance_rows(session, stale_threshold=STALE, now=NOW)
    v_out, _, _ = apply_filters(
        variance_rows=variance, stale_rows=[], exception_rows=[],
        filters=Filters(q="app"),
    )
    assert len(v_out) == 1
    assert v_out[0].item_id == a.id


def test_variance_only_filter_hides_other_sections(session):
    user = _seed_user(session)
    item = _seed_item(session, current_stock=10.0,
                      snapshot=Decimal("5"),
                      snapshot_at=NOW - timedelta(hours=1))
    _make_adj(session, item, user, status=ZohoSyncStatus.FAILED)
    stale = compute_stale_snapshot_rows(session, stale_threshold=STALE, now=NOW)
    exceptions = compute_sync_exception_rows(session)
    variance = compute_variance_rows(session, stale_threshold=STALE, now=NOW)

    v_out, s_out, e_out = apply_filters(
        variance_rows=variance, stale_rows=stale,
        exception_rows=exceptions, filters=Filters(variance_only=True),
    )
    assert v_out  # kept
    assert s_out == []  # hidden
    assert e_out == []  # hidden


def test_retryable_only_filter_drops_blocked_rows(session):
    user = _seed_user(session)
    item = _seed_item(session)
    original = _make_adj(session, item, user, delta="1",
                         status=ZohoSyncStatus.FAILED)
    _make_adj(session, item, user, delta="-1",
              status=ZohoSyncStatus.NOT_CONFIGURED,
              reversal_of=original.id)
    # Add one retryable plain FAILED row separately
    other = _seed_item(session, name="other", sku="OTHER")
    plain = _make_adj(session, other, user, status=ZohoSyncStatus.FAILED)

    exceptions = compute_sync_exception_rows(session)
    _, _, e_out = apply_filters(
        variance_rows=[], stale_rows=[],
        exception_rows=exceptions, filters=Filters(retryable_only=True),
    )
    ids = {r.adjustment_id for r in e_out}
    assert ids == {plain.id}


def test_failed_only_filter_keeps_only_failed_status(session):
    user = _seed_user(session)
    item = _seed_item(session)
    fail = _make_adj(session, item, user, status=ZohoSyncStatus.FAILED)
    _make_adj(session, item, user, status=ZohoSyncStatus.NOT_CONFIGURED)
    exceptions = compute_sync_exception_rows(session)
    _, _, e_out = apply_filters(
        variance_rows=[], stale_rows=[],
        exception_rows=exceptions, filters=Filters(failed_only=True),
    )
    assert {r.adjustment_id for r in e_out} == {fail.id}


def test_product_line_filter_scopes_item_sections(session):
    _seed_item(session, name="A1", sku="A1", product_line="LINE A",
               current_stock=10.0, snapshot=Decimal("5"),
               snapshot_at=NOW - timedelta(hours=1))
    _seed_item(session, name="B1", sku="B1", product_line="LINE B",
               current_stock=10.0, snapshot=Decimal("5"),
               snapshot_at=NOW - timedelta(hours=1))
    variance = compute_variance_rows(session, stale_threshold=STALE, now=NOW)
    v_out, _, _ = apply_filters(
        variance_rows=variance, stale_rows=[], exception_rows=[],
        filters=Filters(product_line="LINE A"),
    )
    assert len(v_out) == 1
    assert v_out[0].product_line == "LINE A"


# --- F. read-only invariants ----------------------------------------------


def test_dashboard_build_does_not_mutate_item_stock(session):
    user = _seed_user(session)
    item = _seed_item(session, current_stock=100.0,
                      snapshot=Decimal("50"),
                      snapshot_at=NOW - timedelta(hours=1))
    _make_adj(session, item, user, status=ZohoSyncStatus.FAILED)
    stock_before = item.current_stock
    snapshot_before = item.last_zoho_stock_snapshot
    snapshot_at_before = item.last_zoho_stock_snapshot_at

    build_dashboard(session, stale_threshold_hours=24, now=NOW,
                    reason_labels={})

    session.refresh(item)
    assert item.current_stock == stock_before
    assert item.last_zoho_stock_snapshot == snapshot_before
    assert item.last_zoho_stock_snapshot_at == snapshot_at_before


def test_dashboard_build_does_not_mutate_adjustments(session):
    user = _seed_user(session)
    item = _seed_item(session)
    adj = _make_adj(session, item, user, status=ZohoSyncStatus.FAILED)
    snapshot = (
        adj.zoho_sync_status, adj.zoho_reference, adj.zoho_sync_error,
        adj.zoho_sync_warning, adj.sync_attempt_count,
        adj.voided_at, adj.reversal_of_adjustment_id,
        adj.quantity_before, adj.quantity_delta, adj.quantity_after,
    )
    build_dashboard(session, stale_threshold_hours=24, now=NOW,
                    reason_labels={})
    session.refresh(adj)
    assert (
        adj.zoho_sync_status, adj.zoho_reference, adj.zoho_sync_error,
        adj.zoho_sync_warning, adj.sync_attempt_count,
        adj.voided_at, adj.reversal_of_adjustment_id,
        adj.quantity_before, adj.quantity_delta, adj.quantity_after,
    ) == snapshot


# --- G. route + template --------------------------------------------------


def test_route_renders_for_authorized_user(session, engine, monkeypatch):
    _seed_user(session)
    client = _client(session, engine, monkeypatch)
    r = client.get("/inventory/reconciliation")
    assert r.status_code == 200
    assert 'data-testid="reconciliation-summary"' in r.text


def test_route_unauthenticated_redirects(session, engine, monkeypatch):
    """Sanity check: dashboard is not public — require_user kicks in."""
    from fastapi.testclient import TestClient

    import packtrack.db
    import packtrack.main
    from packtrack.db import get_session
    from packtrack.main import app

    monkeypatch.setattr(packtrack.db, "engine", engine)
    monkeypatch.setattr(packtrack.main, "engine", engine)
    app.dependency_overrides[get_session] = lambda: session

    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/inventory/reconciliation", follow_redirects=False)
    assert r.status_code in (302, 303, 401, 403)


def test_route_owner_sees_retry_button_for_eligible_row(
    session, engine, monkeypatch,
):
    user = _seed_user(session, role=Role.OWNER)
    item = _seed_item(session)
    adj = _make_adj(session, item, user, status=ZohoSyncStatus.FAILED)
    client = _client(session, engine, monkeypatch)
    body = client.get("/inventory/reconciliation").text
    assert f'data-testid="reconciliation-retry-form-{adj.id}"' in body


def test_route_non_owner_does_not_see_retry_button(
    session, engine, monkeypatch,
):
    owner = _seed_user(session, role=Role.OWNER)
    designer = _seed_user(session, role=Role.DESIGN, user_id=2, name="Des")
    item = _seed_item(session)
    adj = _make_adj(session, item, owner, status=ZohoSyncStatus.FAILED)
    client = _client(session, engine, monkeypatch, user=designer)
    body = client.get("/inventory/reconciliation").text
    assert f'data-testid="reconciliation-retry-form-{adj.id}"' not in body
    # But the row itself still renders — visibility != mutation.
    assert f'data-testid="reconciliation-exception-row-{adj.id}"' in body


def test_route_blocked_row_shows_block_reason_no_retry(
    session, engine, monkeypatch,
):
    user = _seed_user(session, role=Role.OWNER)
    item = _seed_item(session)
    original = _make_adj(session, item, user, delta="1",
                         status=ZohoSyncStatus.FAILED)
    _make_adj(session, item, user, delta="-1",
              status=ZohoSyncStatus.NOT_CONFIGURED,
              reversal_of=original.id)
    client = _client(session, engine, monkeypatch)
    body = client.get("/inventory/reconciliation").text
    assert f'data-testid="reconciliation-retry-form-{original.id}"' not in body
    assert 'data-eligibility="reversed_locally"' in body


def test_route_variance_only_filter_via_query(
    session, engine, monkeypatch,
):
    user = _seed_user(session, role=Role.OWNER)
    item = _seed_item(session, current_stock=10.0,
                      snapshot=Decimal("5"),
                      snapshot_at=NOW - timedelta(hours=1))
    _make_adj(session, item, user, status=ZohoSyncStatus.FAILED)
    client = _client(session, engine, monkeypatch)

    body = client.get("/inventory/reconciliation?variance_only=true").text
    assert 'data-testid="reconciliation-variance-section"' in body
    assert 'data-testid="reconciliation-stale-section"' not in body
    assert 'data-testid="reconciliation-exceptions-section"' not in body


def test_route_search_filter_via_query(session, engine, monkeypatch):
    _seed_user(session, role=Role.OWNER)
    a = _seed_item(session, name="Apple bag", sku="APPLE-1",
                   current_stock=10.0, snapshot=Decimal("5"),
                   snapshot_at=NOW - timedelta(hours=1))
    _seed_item(session, name="Banana box", sku="BANANA-1",
               current_stock=10.0, snapshot=Decimal("5"),
               snapshot_at=NOW - timedelta(hours=1))
    client = _client(session, engine, monkeypatch)

    body = client.get("/inventory/reconciliation?q=apple").text
    assert f'data-testid="reconciliation-variance-row-{a.id}"' in body
    assert 'BANANA-1' not in body


def test_route_summary_renders_card_values(session, engine, monkeypatch):
    user = _seed_user(session, role=Role.OWNER)
    item = _seed_item(session, current_stock=10.0,
                      snapshot=Decimal("5"),
                      snapshot_at=NOW - timedelta(hours=1))
    _make_adj(session, item, user, status=ZohoSyncStatus.FAILED)
    client = _client(session, engine, monkeypatch)
    body = client.get("/inventory/reconciliation").text
    # Summary card region rendered; specific numbers are template-format
    # so just assert the card container + that the labels appear.
    assert 'data-testid="reconciliation-summary"' in body
    for label in ("Total items", "In sync", "Variance", "Stale / missing",
                  "Failed sync", "Retryable", "Blocked"):
        assert label in body


# --- H. import-surface defense + invariants -------------------------------


def test_no_direct_zoho_or_oauth_imports(session):
    """v2.17.0 must not introduce any direct Zoho / OAuth import in the
    new dashboard service or route. Integration calls only happen via
    the existing v2.10.0 zoho-integration-service client."""
    import packtrack.routes.inventory_reconciliation as route_mod
    import packtrack.services.inventory_reconciliation as svc_mod
    forbidden = ("zoho.oauth", "zohocrmsdk", "zohoinventory", "requests_oauthlib")
    for mod in (route_mod, svc_mod):
        with open(mod.__file__) as fh:
            src = fh.read()
        for bad in forbidden:
            assert bad not in src, f"{mod.__name__} imports {bad}"


def test_existing_retry_route_still_works_for_eligible_row(
    session, engine, monkeypatch,
):
    """v2.16.3 contract: a plain FAILED row (the kind the dashboard
    surfaces as Retryable) still hits the orchestrator unchanged."""
    user = _seed_user(session, role=Role.OWNER)
    item = _seed_item(session)
    adj = _make_adj(session, item, user, status=ZohoSyncStatus.FAILED)

    from packtrack.services import inventory_adjustment_sync as sync_mod
    from packtrack.services.zoho_adjustment_client import (
        OutcomeKind,
        SyncOutcome,
    )
    monkeypatch.setattr(
        sync_mod, "push_adjustment_to_zoho",
        lambda *a, **k: SyncOutcome(
            kind=OutcomeKind.SYNCED,
            zoho_reference="ZADJ-x",
            zoho_adjustment_id="z-1",
        ),
    )
    client = _client(session, engine, monkeypatch)
    r = client.post(
        f"/inventory/adjustments/{adj.id}/sync", follow_redirects=False,
    )
    assert r.status_code == 303
    session.refresh(adj)
    assert adj.zoho_sync_status is ZohoSyncStatus.SYNCED
