"""v2.7.6 Receiving — packing-list expected-line CSV/text import.

Covers:

A. Service: deterministic matching by material_code / sku / item name.
B. Service: ambiguous and unmatched rows are classified, not imported.
C. Service: invalid quantity rows are flagged.
D. Routes: preview requires OWNER+RECEIVING.
E. Routes: feature flag OFF blocks preview AND commit.
F. Routes: terminal receive blocks preview AND commit.
G. Routes: pasted CSV preview parses + renders.
H. Routes: uploaded CSV preview parses + renders.
I. Routes: preview writes no DB rows.
J. Routes: commit imports only READY rows; skips bad ones.
K. Routes: replace_existing deletes old expected lines before importing.
L. Routes: commit emits the summary POEvent.
M. Routes: import creates no BoxReceipts.
N. Routes: import does not call Zoho/Luma (no network mocks needed —
   we assert the route layer doesn't import either client at runtime).
O. Reconciliation sees imported expected lines (Short warning).
P. Legacy /receive still works.
Q. Packing-list file upload route still reachable.
R. Manual expected-line CRUD still works.
S. XLSX uploads are rejected with a clear message.
T. Empty / no-payload preview returns 400.
"""
from __future__ import annotations

import io
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
    ReceivePackingListLine,
    ReceiveStatus,
    Role,
    ShipmentKind,
    User,
)
from packtrack.services.receiving_v2_import import (
    RowStatus,
    build_preview,
    match_row,
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
        name="Mailer 6x9", sku_code="SKU-MAIL-69", material_code="PT-00172",
        zoho_item_id="z-a", unit="pcs", current_stock=0,
    )
    item_b = Item(
        name="Mailer 9x12", sku_code="SKU-MAIL-912", material_code="PT-00173",
        zoho_item_id="z-b", unit="pcs", current_stock=0,
    )
    item_c = Item(
        name="Sticker pack", sku_code="SKU-STK", material_code="PT-00200",
        zoho_item_id="z-c", unit="pcs", current_stock=0,
    )
    session.add_all([item_a, item_b, item_c])
    session.commit()
    po = PurchaseOrder(
        po_number="PO-IMP-1", status=POStatus.DESIGN_APPROVED,
        created_by_id=owner.id, created_at=datetime.utcnow(),
        zoho_po_id="po-z-imp-1",
    )
    session.add(po)
    session.commit()
    session.refresh(po)
    session.add_all([
        POLine(po_id=po.id, item_id=item_a.id, quantity=200),
        POLine(po_id=po.id, item_id=item_b.id, quantity=100),
        POLine(po_id=po.id, item_id=item_c.id, quantity=500),
    ])
    session.commit()
    return po, [item_a, item_b, item_c]


def _seed_receive(session, po, *, status=ReceiveStatus.COUNTING):
    user = session.exec(select(User)).first() or _seed_user(session, Role.OWNER)
    rec = Receive(
        receive_number="R-2026-IMP",
        purchase_order_id=po.id,
        delivery_date=date(2026, 6, 29),
        received_by_user_id=user.id,
        status=status,
        submission_id="cafebabe" * 8,
        shipment_kind=ShipmentKind.PALLETIZED,
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


# ---------------------------------------------------------------------------
# A/B/C. Service-level matching and classification
# ---------------------------------------------------------------------------


def test_match_row_by_material_code_exact(session):
    _po, items = _seed_po(session)
    outcome = match_row(
        items=items, raw_material="PT-00172", raw_sku="", raw_name="",
    )
    assert outcome.status is RowStatus.READY
    assert outcome.item.name == "Mailer 6x9"


def test_match_row_by_item_name_case_insensitive(session):
    _po, items = _seed_po(session)
    outcome = match_row(
        items=items, raw_material="", raw_sku="", raw_name="sticker pack",
    )
    assert outcome.status is RowStatus.READY
    assert outcome.item.material_code == "PT-00200"


def test_match_row_by_unambiguous_substring(session):
    _po, items = _seed_po(session)
    outcome = match_row(
        items=items, raw_material="", raw_sku="", raw_name="sticker",
    )
    assert outcome.status is RowStatus.READY


def test_match_row_ambiguous_when_substring_matches_multiple(session):
    _po, items = _seed_po(session)
    outcome = match_row(
        items=items, raw_material="", raw_sku="", raw_name="mailer",
    )
    assert outcome.status is RowStatus.AMBIGUOUS
    assert "2 items" in outcome.detail


def test_match_row_unmatched_for_unknown_item(session):
    _po, items = _seed_po(session)
    outcome = match_row(
        items=items, raw_material="", raw_sku="", raw_name="Phantom Item",
    )
    assert outcome.status is RowStatus.UNMATCHED


def test_build_preview_invalid_quantity_is_invalid_qty(session):
    po, _ = _seed_po(session)
    rec = _seed_receive(session, po)
    text = "material_code,quantity\nPT-00172,abc\n"
    report = build_preview(session, rec, text=text)
    assert len(report.rows) == 1
    assert report.rows[0].status is RowStatus.INVALID_QTY


def test_build_preview_zero_quantity_is_invalid_qty(session):
    po, _ = _seed_po(session)
    rec = _seed_receive(session, po)
    text = "material_code,quantity\nPT-00172,0\n"
    report = build_preview(session, rec, text=text)
    assert report.rows[0].status is RowStatus.INVALID_QTY


def test_build_preview_picks_tab_delimiter(session):
    po, _ = _seed_po(session)
    rec = _seed_receive(session, po)
    text = "material_code\tquantity\nPT-00172\t100\n"
    report = build_preview(session, rec, text=text)
    assert report.rows[0].status is RowStatus.READY
    assert report.rows[0].expected_quantity == 100


# ---------------------------------------------------------------------------
# D. Routes: auth
# ---------------------------------------------------------------------------


def test_preview_forbids_design_role(session, engine, monkeypatch):
    po, _ = _seed_po(session)
    rec = _seed_receive(session, po)
    designer = _seed_user(session, role=Role.DESIGN, name="Designer", user_id=2)
    client = _client(session, engine, monkeypatch, user=designer)
    resp = client.post(
        f"/receive/v2/{rec.id}/expected-lines/import/preview",
        data={"paste_text": "material_code,quantity\nPT-00172,100\n"},
    )
    assert resp.status_code == 403


def test_commit_forbids_design_role(session, engine, monkeypatch):
    po, _ = _seed_po(session)
    rec = _seed_receive(session, po)
    designer = _seed_user(session, role=Role.DESIGN, name="Designer", user_id=2)
    client = _client(session, engine, monkeypatch, user=designer)
    resp = client.post(
        f"/receive/v2/{rec.id}/expected-lines/import/commit",
        data={"import_text": "material_code,quantity\nPT-00172,100\n"},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# E. Feature flag OFF
# ---------------------------------------------------------------------------


def test_flag_off_blocks_preview_and_commit(session, engine, monkeypatch):
    po, _ = _seed_po(session)
    rec = _seed_receive(session, po)
    settings.RECEIVING_VNEXT_ENABLED = False
    try:
        client = _client(session, engine, monkeypatch)
        for path in ("preview", "commit"):
            resp = client.post(
                f"/receive/v2/{rec.id}/expected-lines/import/{path}",
                data={"paste_text": "material_code,quantity\nPT-00172,100\n",
                      "import_text": "material_code,quantity\nPT-00172,100\n"},
            )
            assert resp.status_code == 404, path
    finally:
        settings.RECEIVING_VNEXT_ENABLED = True


# ---------------------------------------------------------------------------
# F. Terminal receive blocks both
# ---------------------------------------------------------------------------


def test_terminal_receive_blocks_preview_and_commit(session, engine, monkeypatch):
    po, _ = _seed_po(session)
    rec = _seed_receive(session, po, status=ReceiveStatus.PUSHED_OK)
    client = _client(session, engine, monkeypatch)
    for path in ("preview", "commit"):
        resp = client.post(
            f"/receive/v2/{rec.id}/expected-lines/import/{path}",
            data={"paste_text": "material_code,quantity\nPT-00172,100\n",
                  "import_text": "material_code,quantity\nPT-00172,100\n"},
        )
        assert resp.status_code == 409, path


# ---------------------------------------------------------------------------
# G/H/I. Preview parses + writes nothing
# ---------------------------------------------------------------------------


def test_preview_renders_pasted_csv(session, engine, monkeypatch):
    po, _ = _seed_po(session)
    rec = _seed_receive(session, po)
    client = _client(session, engine, monkeypatch)
    csv_text = (
        "material_code,item,quantity,unit,vendor_case_number,note\n"
        "PT-00172,,100,pcs,CASE-001,\n"
        ",Sticker pack,500,pcs,CASE-002,green run\n"
    )
    resp = client.post(
        f"/receive/v2/{rec.id}/expected-lines/import/preview",
        data={"paste_text": csv_text},
    )
    assert resp.status_code == 200
    assert "import-preview-table" in resp.text
    assert "Mailer 6x9" in resp.text
    assert "Sticker pack" in resp.text
    # Ready count summary chip (v2.13.0 renamed "Ready to import" to
    # just "Ready" as part of the per-status breakdown panel).
    assert "import-preview-summary" in resp.text
    assert "Ready" in resp.text


def test_preview_renders_uploaded_csv(session, engine, monkeypatch):
    po, _ = _seed_po(session)
    rec = _seed_receive(session, po)
    client = _client(session, engine, monkeypatch)
    body = b"material_code,quantity\nPT-00172,100\n"
    resp = client.post(
        f"/receive/v2/{rec.id}/expected-lines/import/preview",
        files={"file": ("packing.csv", io.BytesIO(body), "text/csv")},
    )
    assert resp.status_code == 200
    assert "Mailer 6x9" in resp.text


def test_preview_writes_no_db_rows(session, engine, monkeypatch):
    po, _ = _seed_po(session)
    rec = _seed_receive(session, po)
    client = _client(session, engine, monkeypatch)
    client.post(
        f"/receive/v2/{rec.id}/expected-lines/import/preview",
        data={"paste_text": "material_code,quantity\nPT-00172,100\n"},
    )
    rows = session.exec(select(ReceivePackingListLine)).all()
    assert rows == []
    events = session.exec(select(POEvent)).all()
    assert events == []


def test_preview_classifies_mixed_rows(session, engine, monkeypatch):
    po, _ = _seed_po(session)
    rec = _seed_receive(session, po)
    client = _client(session, engine, monkeypatch)
    csv_text = (
        "material_code,item,quantity\n"
        "PT-00172,,100\n"           # ready
        ",mailer,50\n"                # ambiguous (matches both mailers)
        ",ghost,10\n"                 # unmatched
        "PT-00200,,abc\n"             # invalid_qty
    )
    resp = client.post(
        f"/receive/v2/{rec.id}/expected-lines/import/preview",
        data={"paste_text": csv_text},
    )
    assert resp.status_code == 200
    assert "ready" in resp.text
    assert "ambiguous" in resp.text
    assert "unmatched" in resp.text
    assert "invalid_qty" in resp.text


# ---------------------------------------------------------------------------
# J/K/L/M. Commit
# ---------------------------------------------------------------------------


def test_commit_imports_ready_rows_and_skips_bad(session, engine, monkeypatch):
    po, _ = _seed_po(session)
    rec = _seed_receive(session, po)
    client = _client(session, engine, monkeypatch)
    csv_text = (
        "material_code,item,quantity,unit\n"
        "PT-00172,,100,pcs\n"   # ready
        "PT-00200,,abc,pcs\n"   # invalid_qty (skipped)
        ",ghost,10,pcs\n"        # unmatched (skipped)
    )
    resp = client.post(
        f"/receive/v2/{rec.id}/expected-lines/import/commit",
        data={"import_text": csv_text},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    rows = session.exec(
        select(ReceivePackingListLine).where(ReceivePackingListLine.receive_id == rec.id)
    ).all()
    assert len(rows) == 1
    assert rows[0].expected_quantity == 100
    assert rows[0].source == "csv_import"


def test_commit_replace_existing_drops_old_lines_first(session, engine, monkeypatch):
    po, (item_a, _, _) = _seed_po(session)
    rec = _seed_receive(session, po)
    session.add(ReceivePackingListLine(
        receive_id=rec.id, item_id=item_a.id,
        expected_quantity=999, unit="pcs", source="manual",
    ))
    session.commit()
    client = _client(session, engine, monkeypatch)
    resp = client.post(
        f"/receive/v2/{rec.id}/expected-lines/import/commit",
        data={
            "import_text": "material_code,quantity\nPT-00172,100\n",
            "replace_existing": "true",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    rows = session.exec(select(ReceivePackingListLine)).all()
    assert len(rows) == 1
    assert rows[0].expected_quantity == 100
    assert rows[0].source == "csv_import"


def test_commit_emits_summary_po_event(session, engine, monkeypatch):
    po, _ = _seed_po(session)
    rec = _seed_receive(session, po)
    client = _client(session, engine, monkeypatch)
    client.post(
        f"/receive/v2/{rec.id}/expected-lines/import/commit",
        data={"import_text": "material_code,quantity\nPT-00172,100\nPT-00200,abc\n"},
        follow_redirects=False,
    )
    events = session.exec(
        select(POEvent).where(POEvent.kind == "receive_expected_lines_imported")
    ).all()
    assert len(events) == 1
    assert "Imported 1 packing-list expected line" in events[0].message
    assert "1 skipped" in events[0].message


def test_commit_creates_no_box_receipts(session, engine, monkeypatch):
    po, _ = _seed_po(session)
    rec = _seed_receive(session, po)
    client = _client(session, engine, monkeypatch)
    client.post(
        f"/receive/v2/{rec.id}/expected-lines/import/commit",
        data={"import_text": "material_code,quantity\nPT-00172,100\n"},
        follow_redirects=False,
    )
    assert session.exec(select(BoxReceipt)).all() == []


def test_import_module_does_not_touch_zoho_or_luma_clients():
    """v2.7.6 has no Zoho/Luma side effects. Validate at the source level
    that the import service file imports neither client. Avoids a future
    drift where someone wires push into the import path."""
    import packtrack.services.receiving_v2_import as mod
    with open(mod.__file__) as f:
        src = f.read()
    assert "luma" not in src.lower()
    assert "zoho" not in src.lower()


# ---------------------------------------------------------------------------
# O. Reconciliation sees imported lines
# ---------------------------------------------------------------------------


def test_imported_lines_show_in_review_reconciliation(session, engine, monkeypatch):
    po, (item_a, _, _) = _seed_po(session)
    rec = _seed_receive(session, po)
    client = _client(session, engine, monkeypatch)
    client.post(
        f"/receive/v2/{rec.id}/expected-lines/import/commit",
        data={"import_text": "material_code,quantity\nPT-00172,100\n"},
        follow_redirects=False,
    )
    # No case lines counted yet → MISSING
    report = build_reconciliation_report(session, rec)
    assert any(
        r.item_id == item_a.id and r.status is ReconcileStatus.MISSING
        for r in report.rows
    )
    page = client.get(f"/receive/v2/{rec.id}/review")
    assert page.status_code == 200
    assert "Mailer 6x9" in page.text


# ---------------------------------------------------------------------------
# P. Legacy /receive regression
# ---------------------------------------------------------------------------


def test_legacy_receive_list_still_reachable(session, engine, monkeypatch):
    _seed_po(session)
    client = _client(session, engine, monkeypatch)
    resp = client.get("/receive")
    assert resp.status_code in (200, 303)


# ---------------------------------------------------------------------------
# Q. Packing-list file upload form still rendered (regression)
# ---------------------------------------------------------------------------


def test_packing_list_upload_form_still_rendered(session, engine, monkeypatch):
    po, _ = _seed_po(session)
    rec = _seed_receive(session, po)
    client = _client(session, engine, monkeypatch)
    page = client.get(f"/receive/v2/{rec.id}")
    assert page.status_code == 200
    assert "packing-list-upload-form" in page.text


# ---------------------------------------------------------------------------
# R. Manual expected-line CRUD still works
# ---------------------------------------------------------------------------


def test_manual_expected_line_add_still_works(session, engine, monkeypatch):
    po, (item_a, _, _) = _seed_po(session)
    rec = _seed_receive(session, po)
    client = _client(session, engine, monkeypatch)
    resp = client.post(
        f"/receive/v2/{rec.id}/expected-lines",
        data={"item_id": str(item_a.id), "expected_quantity": "10", "unit": "pcs"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    rows = session.exec(select(ReceivePackingListLine)).all()
    assert len(rows) == 1
    assert rows[0].source == "manual"


# ---------------------------------------------------------------------------
# S. XLSX uploads rejected with a clear message
# ---------------------------------------------------------------------------


def test_xlsx_upload_rejected(session, engine, monkeypatch):
    po, _ = _seed_po(session)
    rec = _seed_receive(session, po)
    client = _client(session, engine, monkeypatch)
    body = b"PK\x03\x04 fake xlsx content"
    resp = client.post(
        f"/receive/v2/{rec.id}/expected-lines/import/preview",
        files={"file": ("packing.xlsx", io.BytesIO(body),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert resp.status_code == 400
    assert "XLSX" in resp.text


# ---------------------------------------------------------------------------
# T. Empty payload rejected
# ---------------------------------------------------------------------------


def test_empty_payload_rejected(session, engine, monkeypatch):
    po, _ = _seed_po(session)
    rec = _seed_receive(session, po)
    client = _client(session, engine, monkeypatch)
    resp = client.post(
        f"/receive/v2/{rec.id}/expected-lines/import/preview",
        data={"paste_text": ""},
    )
    assert resp.status_code == 400
