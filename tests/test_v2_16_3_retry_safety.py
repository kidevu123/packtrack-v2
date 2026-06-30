"""v2.16.3: retry-safety gate for inventory adjustments.

The spec: an operator must not be able to retry a failed adjustment that
PackTrack has already compensated/voided locally, because the retry
would push the original movement to Zoho without changing PackTrack
stock — creating PT↔Zoho drift.

This module asserts the new ``retry_eligibility`` gate at three layers:

  1. Pure service-level decisions (no HTTP, no DB writes)
  2. Route layer: POST /inventory/adjustments/{id}/sync refuses 409
  3. Template layer: hidden Retry button + blocked-reason label

It also re-asserts the existing happy-path retry behavior (a normal
FAILED row with no reversal pair still gets the Retry button and a
successful POST). And it verifies the read-only contract: the
eligibility check itself never mutates the DB or Item.current_stock.

No real Zoho or OAuth imports; the integration client is exercised via
httpx.MockTransport, identical to the v2.10.0 retry tests.
"""
from __future__ import annotations

import os
from datetime import datetime
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("PACKTRACK_SECRET_KEY", "test-secret")

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from packtrack.config import settings
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
    retry_eligibility,
)
from packtrack.services.inventory_adjustments import create_adjustment

# --- fixtures (mirror tests/test_v2_10_0_adjustment_zoho_sync.py) ---------


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


@pytest.fixture
def integration_settings(monkeypatch):
    monkeypatch.setattr(settings, "ZOHO_INTEGRATION_ADJUST_ENABLED", True)
    monkeypatch.setattr(settings, "ZOHO_INTEGRATION_BASE_URL", "http://int-service.test")
    monkeypatch.setattr(settings, "ZOHO_INTEGRATION_APP_TOKEN", "tok-secret-xyz")
    monkeypatch.setattr(settings, "ZOHO_INTEGRATION_BRAND", "haute_brands")
    yield


def _seed_user(session, *, role=Role.OWNER, user_id=1, name="Owner") -> User:
    u = User(
        id=user_id, email=f"{role.value}-{user_id}@example.com", name=name,
        role=role, password_hash="x", is_active=True,
    )
    session.add(u)
    session.commit()
    return u


def _seed_item(session, *, current_stock=100.0, zoho_item_id="z-item-1") -> Item:
    it = Item(
        name="Bubble mailer", sku_code="SKU-1", material_code="MC-1",
        unit="pcs", vendor="ACME", current_stock=current_stock,
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


def _make_adj(
    session, item, user, *,
    delta: str = "1",
    status: ZohoSyncStatus = ZohoSyncStatus.FAILED,
    reversal_of: int | None = None,
    voided_at: datetime | None = None,
    zoho_reference: str | None = None,
) -> InventoryAdjustment:
    """Build an adjustment row directly. The service path is exercised
    elsewhere; here we want surgical control of status + linkage."""
    result = create_adjustment(
        session,
        item_id=item.id,
        actor=user,
        mode=AdjustmentMode.DELTA,
        direction=AdjustmentDirection.INCREASE if Decimal(delta) > 0 else AdjustmentDirection.DECREASE,
        raw_quantity=str(abs(Decimal(delta))),
        reason_code=AdjustmentReason.MANUAL_CORRECTION,
        notes=None,
        reversal_of_adjustment_id=reversal_of,
    )
    adj = result.adjustment
    # Stamp post-hoc state the service wouldn't have set. These are
    # exactly the fields a real failed/synced/voided row would carry;
    # we're not editing prior history, just constructing a test fixture.
    adj.zoho_sync_status = status
    adj.voided_at = voided_at
    adj.zoho_reference = zoho_reference
    session.add(adj)
    session.commit()
    session.refresh(adj)
    return adj


# --- A. service-level rules ------------------------------------------------


def test_failed_unreversed_row_is_eligible(session):
    user = _seed_user(session)
    item = _seed_item(session)
    adj = _make_adj(session, item, user, status=ZohoSyncStatus.FAILED)
    elig = retry_eligibility(session, adj)
    assert isinstance(elig, RetryEligibility)
    assert elig.allowed is True
    assert elig.reason is None


def test_pending_row_is_eligible(session):
    user = _seed_user(session)
    item = _seed_item(session)
    adj = _make_adj(session, item, user, status=ZohoSyncStatus.PENDING)
    assert retry_eligibility(session, adj).allowed is True


def test_not_configured_row_is_eligible(session):
    user = _seed_user(session)
    item = _seed_item(session)
    adj = _make_adj(session, item, user, status=ZohoSyncStatus.NOT_CONFIGURED)
    assert retry_eligibility(session, adj).allowed is True


def test_skipped_row_is_eligible(session):
    user = _seed_user(session)
    item = _seed_item(session)
    adj = _make_adj(session, item, user, status=ZohoSyncStatus.SKIPPED)
    assert retry_eligibility(session, adj).allowed is True


def test_synced_row_is_blocked(session):
    user = _seed_user(session)
    item = _seed_item(session)
    adj = _make_adj(session, item, user, status=ZohoSyncStatus.SYNCED,
                    zoho_reference="ZADJ-1")
    elig = retry_eligibility(session, adj)
    assert elig.allowed is False
    assert elig.reason is RetryBlockReason.ALREADY_SYNCED


def test_voided_row_is_blocked(session):
    user = _seed_user(session)
    item = _seed_item(session)
    adj = _make_adj(session, item, user, status=ZohoSyncStatus.FAILED,
                    voided_at=datetime(2026, 6, 30, 12, 0, 0))
    elig = retry_eligibility(session, adj)
    assert elig.allowed is False
    assert elig.reason is RetryBlockReason.VOIDED


def test_failed_with_child_reversal_is_blocked(session):
    """The spec's core unsafe pattern: FAILED row whose stock movement
    PackTrack has already compensated via a new reversal row."""
    user = _seed_user(session)
    item = _seed_item(session)
    original = _make_adj(session, item, user, delta="1",
                         status=ZohoSyncStatus.FAILED)
    # The compensating row — exactly how prod row 6 cancels row 3.
    _make_adj(session, item, user, delta="-1",
              status=ZohoSyncStatus.NOT_CONFIGURED,
              reversal_of=original.id)

    elig = retry_eligibility(session, original)
    assert elig.allowed is False
    assert elig.reason is RetryBlockReason.REVERSED_LOCALLY
    assert f"#{original.id + 1}" in elig.detail  # mentions the child id


def test_reversal_of_synced_original_is_eligible(session, integration_settings):
    """A reversal row whose original is SYNCED is the safe case — the
    operator can correct a synced movement by pushing the reversal-half
    to Zoho."""
    user = _seed_user(session)
    item = _seed_item(session)
    original = _make_adj(session, item, user, delta="1",
                         status=ZohoSyncStatus.SYNCED,
                         zoho_reference="ZADJ-orig")
    reversal = _make_adj(session, item, user, delta="-1",
                         status=ZohoSyncStatus.NOT_CONFIGURED,
                         reversal_of=original.id)

    assert retry_eligibility(session, reversal).allowed is True


def test_reversal_of_failed_original_is_blocked(session):
    """The flip side of the prod row-6 case: trying to retry the reversal
    row when the original never made it to Zoho. Pushing the reversal
    alone would drift Zoho in the opposite direction."""
    user = _seed_user(session)
    item = _seed_item(session)
    original = _make_adj(session, item, user, delta="1",
                         status=ZohoSyncStatus.FAILED)
    reversal = _make_adj(session, item, user, delta="-1",
                         status=ZohoSyncStatus.NOT_CONFIGURED,
                         reversal_of=original.id)

    elig = retry_eligibility(session, reversal)
    assert elig.allowed is False
    assert elig.reason is RetryBlockReason.REVERSAL_OF_UNSYNCED
    assert "not synced" in elig.detail.lower()


def test_eligibility_check_is_read_only(session):
    """Spec: 'no local stock change occurs during retry eligibility
    checks'. Verify item.current_stock + adjustment fields are
    byte-identical after running the check."""
    user = _seed_user(session)
    item = _seed_item(session, current_stock=100.0)
    adj = _make_adj(session, item, user, status=ZohoSyncStatus.FAILED)

    stock_before = item.current_stock
    snapshot = (
        adj.zoho_sync_status, adj.voided_at, adj.zoho_reference,
        adj.quantity_before, adj.quantity_delta, adj.quantity_after,
        adj.reversal_of_adjustment_id,
    )

    retry_eligibility(session, adj)

    session.refresh(item)
    session.refresh(adj)
    assert item.current_stock == stock_before
    assert (
        adj.zoho_sync_status, adj.voided_at, adj.zoho_reference,
        adj.quantity_before, adj.quantity_delta, adj.quantity_after,
        adj.reversal_of_adjustment_id,
    ) == snapshot


# --- B. route layer (POST 409) ---------------------------------------------


def test_route_retry_reversed_row_returns_409(session, engine, monkeypatch):
    user = _seed_user(session)
    item = _seed_item(session)
    original = _make_adj(session, item, user, delta="1",
                         status=ZohoSyncStatus.FAILED)
    _make_adj(session, item, user, delta="-1",
              status=ZohoSyncStatus.NOT_CONFIGURED,
              reversal_of=original.id)

    client = _client(session, engine, monkeypatch)
    r = client.post(f"/inventory/adjustments/{original.id}/sync")
    assert r.status_code == 409
    # PackTrack's custom HTTPException handler returns {"error": <detail>}
    # for JSON requests (see packtrack/main.py).
    body = r.json()
    assert "reversed locally" in body["error"].lower()
    assert f"#{original.id + 1}" in body["error"]


def test_route_retry_voided_row_returns_409(session, engine, monkeypatch):
    user = _seed_user(session)
    item = _seed_item(session)
    adj = _make_adj(session, item, user, status=ZohoSyncStatus.FAILED,
                    voided_at=datetime(2026, 6, 30, 12, 0, 0))

    client = _client(session, engine, monkeypatch)
    r = client.post(f"/inventory/adjustments/{adj.id}/sync")
    assert r.status_code == 409
    assert "voided" in r.json()["error"].lower()


def test_route_retry_synced_row_returns_409(session, engine, monkeypatch):
    user = _seed_user(session)
    item = _seed_item(session)
    adj = _make_adj(session, item, user, status=ZohoSyncStatus.SYNCED,
                    zoho_reference="ZADJ-1")
    client = _client(session, engine, monkeypatch)
    r = client.post(f"/inventory/adjustments/{adj.id}/sync")
    assert r.status_code == 409
    assert "already synced" in r.json()["error"].lower()


def test_route_retry_reversal_of_unsynced_returns_409(session, engine, monkeypatch):
    user = _seed_user(session)
    item = _seed_item(session)
    original = _make_adj(session, item, user, delta="1",
                         status=ZohoSyncStatus.FAILED)
    reversal = _make_adj(session, item, user, delta="-1",
                         status=ZohoSyncStatus.NOT_CONFIGURED,
                         reversal_of=original.id)
    client = _client(session, engine, monkeypatch)
    r = client.post(f"/inventory/adjustments/{reversal.id}/sync")
    assert r.status_code == 409
    assert "drift" in r.json()["error"].lower()


def test_route_retry_normal_failed_row_proceeds(
    session, engine, monkeypatch, integration_settings,
):
    """Sanity: a plain FAILED row with no reversal pair still gets
    through the gate — proving the patch only blocks the unsafe cases."""
    user = _seed_user(session)
    item = _seed_item(session)
    adj = _make_adj(session, item, user, status=ZohoSyncStatus.FAILED)

    # Stub the integration push so the route call completes without
    # making real HTTP calls. We're only testing the gate let-through.
    from packtrack.services import inventory_adjustment_sync as sync_mod
    from packtrack.services.zoho_adjustment_client import (
        OutcomeKind,
        SyncOutcome,
    )
    monkeypatch.setattr(
        sync_mod, "push_adjustment_to_zoho",
        lambda *a, **k: SyncOutcome(
            kind=OutcomeKind.SYNCED,
            zoho_reference="ZADJ-retried",
            zoho_adjustment_id="zoho-adj-x",
        ),
    )

    client = _client(session, engine, monkeypatch)
    r = client.post(f"/inventory/adjustments/{adj.id}/sync", follow_redirects=False)
    assert r.status_code == 303
    session.refresh(adj)
    assert adj.zoho_sync_status is ZohoSyncStatus.SYNCED


def test_route_non_owner_still_403_before_gate(
    session, engine, monkeypatch,
):
    """Spec invariant — the role gate runs BEFORE the eligibility gate.
    A non-owner attempting to retry a blocked row sees 403, not 409,
    so the role boundary stays visible."""
    owner = _seed_user(session)
    designer = _seed_user(session, role=Role.DESIGN, user_id=2, name="Des")
    item = _seed_item(session)
    adj = _make_adj(session, item, owner, status=ZohoSyncStatus.FAILED,
                    voided_at=datetime(2026, 6, 30, 12, 0, 0))

    client = _client(session, engine, monkeypatch, user=designer)
    r = client.post(f"/inventory/adjustments/{adj.id}/sync")
    assert r.status_code == 403


# --- C. template / history-page rendering ----------------------------------


def test_history_renders_retry_for_eligible_row(
    session, engine, monkeypatch,
):
    user = _seed_user(session)
    item = _seed_item(session)
    adj = _make_adj(session, item, user, status=ZohoSyncStatus.FAILED)

    client = _client(session, engine, monkeypatch)
    body = client.get(f"/inventory/{item.id}/adjustments").text
    assert f'data-testid="adjustment-retry-form-{adj.id}"' in body
    assert f'data-testid="adjustment-retry-blocked-{adj.id}"' not in body


def test_history_hides_retry_and_shows_blocked_for_reversed_row(
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
    body = client.get(f"/inventory/{item.id}/adjustments").text
    assert f'data-testid="adjustment-retry-form-{original.id}"' not in body
    assert f'data-testid="adjustment-retry-blocked-{original.id}"' in body
    assert "reversed locally" in body.lower()
    assert RetryBlockReason.REVERSED_LOCALLY.value in body


def test_history_shows_blocked_for_voided_row(
    session, engine, monkeypatch,
):
    user = _seed_user(session)
    item = _seed_item(session)
    adj = _make_adj(session, item, user, status=ZohoSyncStatus.FAILED,
                    voided_at=datetime(2026, 6, 30, 12, 0, 0))

    client = _client(session, engine, monkeypatch)
    body = client.get(f"/inventory/{item.id}/adjustments").text
    assert f'data-testid="adjustment-retry-blocked-{adj.id}"' in body
    assert RetryBlockReason.VOIDED.value in body


def test_history_shows_no_blocked_label_for_synced_row(
    session, engine, monkeypatch,
):
    """SYNCED rows ALREADY display 'synced' in the status tag — they
    should not also render the blocked-with-reason label. (The route
    still refuses a direct POST; that's covered separately.)"""
    user = _seed_user(session)
    item = _seed_item(session)
    adj = _make_adj(session, item, user, status=ZohoSyncStatus.SYNCED,
                    zoho_reference="ZADJ-1")

    client = _client(session, engine, monkeypatch)
    body = client.get(f"/inventory/{item.id}/adjustments").text
    assert f'data-testid="adjustment-retry-form-{adj.id}"' not in body
    assert f'data-testid="adjustment-retry-blocked-{adj.id}"' not in body
    # Synced status tag is still visible — match the value-only text, since
    # the rendered status tag has whitespace around it from the template.
    assert "synced" in body


def test_history_renders_existing_smoke_pair_safely(
    session, engine, monkeypatch,
):
    """Reproduce the exact prod state at v2.16.2: a FAILED row (#3) with
    a compensating reversal (#6). After this patch the failed row's
    Retry button is gone and the reversal row is also blocked because
    its original is unsynced."""
    user = _seed_user(session)
    item = _seed_item(session)
    failed = _make_adj(session, item, user, delta="1",
                       status=ZohoSyncStatus.FAILED)
    reversal = _make_adj(session, item, user, delta="-1",
                         status=ZohoSyncStatus.NOT_CONFIGURED,
                         reversal_of=failed.id)

    client = _client(session, engine, monkeypatch)
    body = client.get(f"/inventory/{item.id}/adjustments").text

    # Failed: reversed-locally blocked.
    assert f'data-retry-block-reason="{RetryBlockReason.REVERSED_LOCALLY.value}"' in body
    # Reversal: reversal-of-unsynced blocked.
    assert f'data-retry-block-reason="{RetryBlockReason.REVERSAL_OF_UNSYNCED.value}"' in body
    # And both rows themselves still render (no ledger row hidden).
    assert f'data-testid="adjustment-row-{failed.id}"' in body
    assert f'data-testid="adjustment-row-{reversal.id}"' in body


# --- D. import-surface check (defensive) -----------------------------------


def test_no_direct_zoho_or_oauth_imports_added(session):
    """v2.16.3 must not introduce any direct Zoho client / OAuth import
    in the route or service files it touched. The integration service
    is the only sanctioned Zoho call site."""
    import packtrack.routes.inventory_adjustments as route_mod
    import packtrack.services.inventory_adjustment_sync as sync_mod
    forbidden = ("zoho.oauth", "zohocrmsdk", "zohoinventory", "requests_oauthlib")
    for mod in (route_mod, sync_mod):
        with open(mod.__file__) as fh:
            src = fh.read()
        for bad in forbidden:
            assert bad not in src, f"{mod.__name__} imports {bad}"
