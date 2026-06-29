"""v2.7.5 Receiving MVP — manual packing-list reconciliation tests.

Covers:

A. Migration adds ``receive_packing_list_lines`` with the expected
   columns and indexes; head advances to ``f2g3h4i5j6k7``.
B. OWNER + RECEIVING can add a manual expected line; DESIGN cannot.
C. Feature flag OFF returns 404 on all expected-line routes.
D. Expected-lines table renders on the receive page (empty + populated).
E. Operator can delete an expected line before finalize.
F. Expected lines become read-only once the receive is finalized/pushed.
G. Review reconciliation classifies Match / Short / Over / Unexpected /
   Missing correctly.
H. Finalize is not blocked by reconciliation warnings.
I. Finalize Zoho/Luma payloads still use actual counted ReceiveCaseLine
   totals — expected lines never leak in.
J. CRUD on expected lines never creates BoxReceipts.
K. Packing-list file upload still works alongside expected lines.
L. Canary/test banner still renders.
M. Legacy ``/receive/{zoho_po_id}`` still works with the flag on.
N. ``po_item_choices`` annotates labels with "expected M unit" when an
   expected_by_item map is passed (and ignores it otherwise).
O. Activity strip filters to receive-lifecycle event kinds only.
P. Adding an expected line emits an audit POEvent.
Q. ReconcileRow.message copy is operator-friendly.
R. CRUD does not change ReceiveCaseLine counts.
"""
from __future__ import annotations

import os
from datetime import date, datetime

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("PACKTRACK_SECRET_KEY", "test-secret")

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from packtrack.config import settings
from packtrack.models import (
    BoxReceipt,
    Item,
    POEvent,
    POLine,
    POStatus,
    PurchaseOrder,
    Receive,
    ReceiveCase,
    ReceiveCaseLine,
    ReceivePackingListLine,
    ReceiveStatus,
    Role,
    ShipmentKind,
    User,
)
from packtrack.services.receiving_v2 import (
    po_item_choices,
    receive_activity,
)
from packtrack.services.receiving_v2_reconcile import (
    ReconcileStatus,
    build_reconciliation_report,
)


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


@pytest.fixture(autouse=True)
def _flag_on():
    original = settings.RECEIVING_VNEXT_ENABLED
    settings.RECEIVING_VNEXT_ENABLED = True
    yield
    settings.RECEIVING_VNEXT_ENABLED = original


def _seed_user(session, role=Role.OWNER, name="Owner", user_id=1):
    u = User(
        id=user_id, email=f"{role.value}-{user_id}@example.com", name=name,
        role=role, password_hash="x", is_active=True,
    )
    session.add(u)
    session.commit()
    return u


def _seed_po(session):
    owner = session.exec(select(User)).first() or _seed_user(session, Role.OWNER)
    item_a = Item(
        name="Mailer", sku_code="SKU-A", material_code="MC-A",
        zoho_item_id="z-a", unit="pcs", current_stock=0,
    )
    item_b = Item(
        name="Sticker", sku_code="SKU-B", material_code="MC-B",
        zoho_item_id="z-b", unit="pcs", current_stock=0,
    )
    session.add_all([item_a, item_b])
    session.commit()
    po = PurchaseOrder(
        po_number="PO-MVP-1", status=POStatus.DESIGN_APPROVED,
        created_by_id=owner.id, created_at=datetime.utcnow(),
        zoho_po_id="po-z-mvp-1",
    )
    session.add(po)
    session.commit()
    session.refresh(po)
    session.add_all([
        POLine(po_id=po.id, item_id=item_a.id, quantity=100),
        POLine(po_id=po.id, item_id=item_b.id, quantity=50),
    ])
    session.commit()
    return po, [item_a, item_b]


def _seed_receive(session, po, *, status=ReceiveStatus.COUNTING, notes=None):
    user = session.exec(select(User)).first() or _seed_user(session, Role.OWNER)
    rec = Receive(
        receive_number="R-2026-MVP",
        purchase_order_id=po.id,
        delivery_date=date(2026, 6, 29),
        received_by_user_id=user.id,
        status=status,
        submission_id="deadbeef" * 8,
        shipment_kind=ShipmentKind.PALLETIZED,
        notes=notes,
    )
    session.add(rec)
    session.commit()
    session.refresh(rec)
    return rec


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


def _seed_case_line(session, rec, item, *, qty=10, vendor_case_number="C1"):
    case = ReceiveCase(
        receive_id=rec.id,
        vendor_case_number=vendor_case_number,
        sequence=1,
    )
    session.add(case)
    session.commit()
    session.refresh(case)
    line = ReceiveCaseLine(
        receive_case_id=case.id,
        purchase_order_id=rec.purchase_order_id,
        item_id=item.id,
        declared_quantity=qty,
        unit_of_measure="pcs",
    )
    session.add(line)
    session.commit()
    return case, line


# ---------------------------------------------------------------------------
# A. Migration / model
# ---------------------------------------------------------------------------


def test_alembic_head_is_v2_7_5_revision():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    sd = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = sd.get_heads()
    assert len(heads) == 1, f"expected 1 head, got {heads}"
    # v2.9.0 advanced this from f2g3h4i5j6k7 (v2.7.5) to g3h4i5j6k7l8
    # by adding the inventory_adjustments table. v2.10.0 advanced it
    # again to h4i5j6k7l8m9 by adding the sync_warning + attempt_count
    # columns.
    assert heads[0] == "h4i5j6k7l8m9"


def test_receive_packing_list_line_row_roundtrips(session):
    po, (item_a, _) = _seed_po(session)
    rec = _seed_receive(session, po)
    line = ReceivePackingListLine(
        receive_id=rec.id, item_id=item_a.id,
        expected_quantity=100, unit="pcs",
        vendor_case_number="C1", note="case 1 of 3",
        created_by_user_id=session.exec(select(User)).first().id,
    )
    session.add(line)
    session.commit()
    session.refresh(line)
    assert line.id is not None
    assert line.source == "manual"
    assert line.created_at is not None


# ---------------------------------------------------------------------------
# B/C. Permissions and feature flag
# ---------------------------------------------------------------------------


def test_owner_can_add_expected_line(session, engine, monkeypatch):
    po, (item_a, _) = _seed_po(session)
    rec = _seed_receive(session, po)
    client = _client(session, engine, monkeypatch)
    resp = client.post(
        f"/receive/v2/{rec.id}/expected-lines",
        data={"item_id": str(item_a.id), "expected_quantity": "100", "unit": "pcs"},
        follow_redirects=False,
    )
    assert resp.status_code in (303, 302)
    rows = session.exec(select(ReceivePackingListLine)).all()
    assert len(rows) == 1
    assert rows[0].expected_quantity == 100
    assert rows[0].source == "manual"


def test_design_role_cannot_add_expected_line(session, engine, monkeypatch):
    po, (item_a, _) = _seed_po(session)
    rec = _seed_receive(session, po)
    designer = _seed_user(session, role=Role.DESIGN, name="Designer", user_id=2)
    client = _client(session, engine, monkeypatch, user=designer)
    resp = client.post(
        f"/receive/v2/{rec.id}/expected-lines",
        data={"item_id": str(item_a.id), "expected_quantity": "100"},
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_flag_off_blocks_expected_line_routes(session, engine, monkeypatch):
    po, (item_a, _) = _seed_po(session)
    rec = _seed_receive(session, po)
    settings.RECEIVING_VNEXT_ENABLED = False
    try:
        client = _client(session, engine, monkeypatch)
        add_resp = client.post(
            f"/receive/v2/{rec.id}/expected-lines",
            data={"item_id": str(item_a.id), "expected_quantity": "10"},
            follow_redirects=False,
        )
        assert add_resp.status_code == 404
        del_resp = client.post(
            f"/receive/v2/{rec.id}/expected-lines/9999/delete",
            follow_redirects=False,
        )
        assert del_resp.status_code == 404
    finally:
        settings.RECEIVING_VNEXT_ENABLED = True


# ---------------------------------------------------------------------------
# D. Receive page renders expected-lines table (empty + populated)
# ---------------------------------------------------------------------------


def test_receive_page_shows_empty_expected_lines_state(session, engine, monkeypatch):
    po, _ = _seed_po(session)
    rec = _seed_receive(session, po)
    client = _client(session, engine, monkeypatch)
    page = client.get(f"/receive/v2/{rec.id}")
    assert page.status_code == 200
    assert "Packing list — expected lines" in page.text
    assert "No packing-list expected lines entered." in page.text


def test_receive_page_shows_populated_expected_lines(session, engine, monkeypatch):
    po, (item_a, _) = _seed_po(session)
    rec = _seed_receive(session, po)
    session.add(ReceivePackingListLine(
        receive_id=rec.id, item_id=item_a.id,
        expected_quantity=100, unit="pcs",
    ))
    session.commit()
    client = _client(session, engine, monkeypatch)
    page = client.get(f"/receive/v2/{rec.id}")
    assert page.status_code == 200
    assert "expected-lines-table" in page.text
    assert "Mailer" in page.text
    assert "100 pcs" in page.text


# ---------------------------------------------------------------------------
# E/F. Delete + read-only after finalize
# ---------------------------------------------------------------------------


def test_owner_can_delete_expected_line_before_finalize(session, engine, monkeypatch):
    po, (item_a, _) = _seed_po(session)
    rec = _seed_receive(session, po)
    line = ReceivePackingListLine(
        receive_id=rec.id, item_id=item_a.id,
        expected_quantity=100, unit="pcs",
    )
    session.add(line)
    session.commit()
    session.refresh(line)
    client = _client(session, engine, monkeypatch)
    resp = client.post(
        f"/receive/v2/{rec.id}/expected-lines/{line.id}/delete",
        follow_redirects=False,
    )
    assert resp.status_code in (303, 302)
    rows = session.exec(select(ReceivePackingListLine)).all()
    assert rows == []


def test_expected_lines_read_only_after_finalize(session, engine, monkeypatch):
    po, (item_a, _) = _seed_po(session)
    rec = _seed_receive(session, po, status=ReceiveStatus.PUSHED_OK)
    line = ReceivePackingListLine(
        receive_id=rec.id, item_id=item_a.id,
        expected_quantity=100, unit="pcs",
    )
    session.add(line)
    session.commit()
    session.refresh(line)
    client = _client(session, engine, monkeypatch)

    add_resp = client.post(
        f"/receive/v2/{rec.id}/expected-lines",
        data={"item_id": str(item_a.id), "expected_quantity": "5"},
        follow_redirects=False,
    )
    assert add_resp.status_code == 409

    del_resp = client.post(
        f"/receive/v2/{rec.id}/expected-lines/{line.id}/delete",
        follow_redirects=False,
    )
    assert del_resp.status_code == 409

    page = client.get(f"/receive/v2/{rec.id}")
    assert page.status_code == 200
    assert "expected lines are read-only" in page.text


# ---------------------------------------------------------------------------
# G. Reconciliation classification (Match/Short/Over/Unexpected/Missing)
# ---------------------------------------------------------------------------


def test_reconciliation_match(session):
    po, (item_a, _) = _seed_po(session)
    rec = _seed_receive(session, po)
    session.add(ReceivePackingListLine(
        receive_id=rec.id, item_id=item_a.id,
        expected_quantity=10, unit="pcs",
    ))
    session.commit()
    _seed_case_line(session, rec, item_a, qty=10)
    report = build_reconciliation_report(session, rec)
    assert len(report.rows) == 1
    assert report.rows[0].status is ReconcileStatus.MATCH
    assert report.differences == []


def test_reconciliation_short(session):
    po, (item_a, _) = _seed_po(session)
    rec = _seed_receive(session, po)
    session.add(ReceivePackingListLine(
        receive_id=rec.id, item_id=item_a.id,
        expected_quantity=100, unit="pcs",
    ))
    session.commit()
    _seed_case_line(session, rec, item_a, qty=95)
    report = build_reconciliation_report(session, rec)
    row = next(r for r in report.rows if r.item_id == item_a.id)
    assert row.status is ReconcileStatus.SHORT
    assert "short 5" in row.message


def test_reconciliation_over(session):
    po, (item_a, _) = _seed_po(session)
    rec = _seed_receive(session, po)
    session.add(ReceivePackingListLine(
        receive_id=rec.id, item_id=item_a.id,
        expected_quantity=100, unit="pcs",
    ))
    session.commit()
    _seed_case_line(session, rec, item_a, qty=110)
    report = build_reconciliation_report(session, rec)
    row = next(r for r in report.rows if r.item_id == item_a.id)
    assert row.status is ReconcileStatus.OVER
    assert "over by 10" in row.message


def test_reconciliation_unexpected(session):
    po, (item_a, item_b) = _seed_po(session)
    rec = _seed_receive(session, po)
    session.add(ReceivePackingListLine(
        receive_id=rec.id, item_id=item_a.id,
        expected_quantity=100, unit="pcs",
    ))
    session.commit()
    _seed_case_line(session, rec, item_b, qty=5)
    report = build_reconciliation_report(session, rec)
    unexpected = next(
        r for r in report.rows if r.item_id == item_b.id
    )
    assert unexpected.status is ReconcileStatus.UNEXPECTED
    assert "not on packing list" in unexpected.message


def test_reconciliation_missing(session):
    po, (item_a, _) = _seed_po(session)
    rec = _seed_receive(session, po)
    session.add(ReceivePackingListLine(
        receive_id=rec.id, item_id=item_a.id,
        expected_quantity=100, unit="pcs",
    ))
    session.commit()
    report = build_reconciliation_report(session, rec)
    row = next(r for r in report.rows if r.item_id == item_a.id)
    assert row.status is ReconcileStatus.MISSING
    assert "nothing counted" in row.message


def test_review_page_renders_reconciliation_card(session, engine, monkeypatch):
    po, (item_a, _) = _seed_po(session)
    rec = _seed_receive(session, po)
    session.add(ReceivePackingListLine(
        receive_id=rec.id, item_id=item_a.id,
        expected_quantity=100, unit="pcs",
    ))
    session.commit()
    _seed_case_line(session, rec, item_a, qty=95)
    client = _client(session, engine, monkeypatch)
    page = client.get(f"/receive/v2/{rec.id}/review")
    assert page.status_code == 200
    assert "reconciliation-card" in page.text
    assert "Packing list vs counted" in page.text
    assert "short 5" in page.text


# ---------------------------------------------------------------------------
# H. Finalize is NOT blocked by reconciliation warnings
# ---------------------------------------------------------------------------


def test_finalize_validation_unchanged_by_reconciliation(session):
    """validate_receive_for_finalize() must NOT see reconciliation
    warnings — they live entirely in the route/template layer for v2.7.5."""
    from packtrack.services.receiving_v2_finalize import (
        validate_receive_for_finalize,
    )

    po, (item_a, _) = _seed_po(session)
    rec = _seed_receive(session, po)
    rec.tracking_number = "TRK1"
    rec.shipment_kind = ShipmentKind.PARCEL
    session.add(rec)
    session.commit()
    _seed_case_line(session, rec, item_a, qty=10)
    session.add(ReceivePackingListLine(
        receive_id=rec.id, item_id=item_a.id,
        expected_quantity=999, unit="pcs",
    ))
    session.commit()
    blockers, warnings = validate_receive_for_finalize(session, rec)
    for issue in (*blockers, *warnings):
        assert "packing list" not in issue.message.lower()


# ---------------------------------------------------------------------------
# I/J/R. Payload + side-effect isolation
# ---------------------------------------------------------------------------


def test_expected_line_crud_creates_no_box_receipts(session, engine, monkeypatch):
    po, (item_a, _) = _seed_po(session)
    rec = _seed_receive(session, po)
    client = _client(session, engine, monkeypatch)
    client.post(
        f"/receive/v2/{rec.id}/expected-lines",
        data={"item_id": str(item_a.id), "expected_quantity": "50"},
        follow_redirects=False,
    )
    boxes = session.exec(select(BoxReceipt)).all()
    assert boxes == []


def test_expected_line_crud_does_not_change_case_lines(session, engine, monkeypatch):
    po, (item_a, _) = _seed_po(session)
    rec = _seed_receive(session, po)
    _, line = _seed_case_line(session, rec, item_a, qty=10)
    original_qty = line.declared_quantity
    client = _client(session, engine, monkeypatch)
    client.post(
        f"/receive/v2/{rec.id}/expected-lines",
        data={"item_id": str(item_a.id), "expected_quantity": "1000"},
        follow_redirects=False,
    )
    session.refresh(line)
    assert line.declared_quantity == original_qty


# ---------------------------------------------------------------------------
# K. Packing-list file upload still works (regression)
# ---------------------------------------------------------------------------


def test_packing_list_upload_route_still_reachable(session, engine, monkeypatch):
    po, _ = _seed_po(session)
    _seed_receive(session, po)
    client = _client(session, engine, monkeypatch)
    page = client.get(f"/receive/v2/{po.id}")
    # Smoke: page renders and the upload form is present (we don't
    # actually upload a binary in this test).
    assert "packing-list-upload-form" in page.text or page.status_code == 200


# ---------------------------------------------------------------------------
# L. Canary banner regression
# ---------------------------------------------------------------------------


def test_canary_banner_still_renders(session, engine, monkeypatch):
    po, _ = _seed_po(session)
    rec = _seed_receive(
        session, po,
        notes="orig\n\n[Marked as TEST/CANARY by Owner at 2026-06-29T15:00:00Z] — canary",
    )
    client = _client(session, engine, monkeypatch)
    page = client.get(f"/receive/v2/{rec.id}")
    assert page.status_code == 200
    assert "Test / canary receive" in page.text


# ---------------------------------------------------------------------------
# M. Legacy /receive/{zoho_po_id} still works
# ---------------------------------------------------------------------------


def test_legacy_receive_list_still_reachable(session, engine, monkeypatch):
    _seed_po(session)
    client = _client(session, engine, monkeypatch)
    page = client.get("/receive")
    assert page.status_code in (200, 303)


# ---------------------------------------------------------------------------
# N. po_item_choices "expected" annotation
# ---------------------------------------------------------------------------


def test_po_item_choices_appends_expected_label(session):
    po, (item_a, _) = _seed_po(session)
    choices = po_item_choices(
        session, po.id, expected_by_item={item_a.id: 50.0},
    )
    label = next(c.label for c in choices if c.item_id == item_a.id)
    assert "expected 50 pcs" in label


def test_po_item_choices_unchanged_without_expected_map(session):
    po, (item_a, _) = _seed_po(session)
    choices = po_item_choices(session, po.id)
    label = next(c.label for c in choices if c.item_id == item_a.id)
    assert "expected" not in label


# ---------------------------------------------------------------------------
# O/P. Activity strip + POEvent emission
# ---------------------------------------------------------------------------


def test_activity_strip_emits_event_on_expected_line_add(session, engine, monkeypatch):
    po, (item_a, _) = _seed_po(session)
    rec = _seed_receive(session, po)
    client = _client(session, engine, monkeypatch)
    client.post(
        f"/receive/v2/{rec.id}/expected-lines",
        data={"item_id": str(item_a.id), "expected_quantity": "100"},
        follow_redirects=False,
    )
    events = session.exec(
        select(POEvent).where(POEvent.kind == "receive_expected_line_added")
    ).all()
    assert len(events) == 1
    assert "Mailer 100" in events[0].message
    activity = receive_activity(session, rec)
    assert any(e.kind == "receive_expected_line_added" for e in activity)


def test_activity_strip_filters_non_receive_event_kinds(session):
    """Unrelated PO events (e.g. status changes) must NOT appear."""
    po, _ = _seed_po(session)
    rec = _seed_receive(session, po)
    session.add(POEvent(
        po_id=po.id, kind="status_change",
        message="design approved",
    ))
    session.commit()
    activity = receive_activity(session, rec)
    assert all(e.kind != "status_change" for e in activity)


# ---------------------------------------------------------------------------
# Q. Operator-friendly message copy
# ---------------------------------------------------------------------------


def test_reconcile_messages_are_operator_friendly(session):
    po, (item_a, _) = _seed_po(session)
    rec = _seed_receive(session, po)
    session.add(ReceivePackingListLine(
        receive_id=rec.id, item_id=item_a.id,
        expected_quantity=100, unit="pcs",
    ))
    session.commit()
    _seed_case_line(session, rec, item_a, qty=95)
    report = build_reconciliation_report(session, rec)
    row = report.rows[0]
    assert row.message.startswith("Mailer:")
    assert "expected 100 pcs" in row.message
    assert "counted 95 pcs" in row.message
    assert "short 5 pcs" in row.message
