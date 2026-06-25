"""Receiving vNext v2.5.0 Stage 1 — feature-flag, route, and validation
coverage.

Covers the explicit list in the user prompt:

1. Flag disabled → vNext routes return 404.
2. Flag enabled → vNext routes are reachable.
3. Create receive from PO.
4. Receive number generated as ``R-YYYY-NNNN``.
5. Add a case with free-text vendor case number.
6. Duplicate case number in same receive is rejected cleanly (409, not 500).
7. Add multiple lines under one case.
8. Item search is scoped to PO lines.
9. Totals by item are calculated.
10. Legacy /receive/{zoho_po_id} still works.
12. Alembic chain has a single head (chain test already covers this; an
    extra assertion here confirms the new revision is on the chain).
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("PACKTRACK_SECRET_KEY", "test-secret")

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from packtrack.config import settings
from packtrack.models import (
    Item,
    POLine,
    POStatus,
    PurchaseOrder,
    Receive,
    ReceiveCase,
    ReceiveCaseLine,
    ReceiveStatus,
    Role,
    User,
    ZohoMirror,
)
from packtrack.services.receiving_v2 import (
    generate_receive_number,
    items_for_po,
    totals_by_item,
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
    # SQLite does not enforce CHECK on the StrEnum/string columns we
    # use, and the partial-unique-index syntax (used by the migration
    # for receive_cases) is also a hand-written CREATE — replicate it
    # here so the integrity test asserts what production sees.
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
    """Route tests below patch app.dependency_overrides. Without a
    teardown they leak into later test modules."""
    yield
    from packtrack.main import app
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _restore_flag():
    """Each test that flips the vNext flag must leave it OFF."""
    original = settings.RECEIVING_VNEXT_ENABLED
    yield
    settings.RECEIVING_VNEXT_ENABLED = original


def _seed_user(session: Session, role: Role = Role.RECEIVING) -> User:
    user = User(
        id=1,
        email=f"{role.value}@example.com",
        name=role.value.title(),
        role=role,
        password_hash="x",
        is_active=True,
    )
    session.add(user)
    session.commit()
    return user


def _seed_po_with_items(session: Session, n_items: int = 3) -> tuple[PurchaseOrder, list[Item]]:
    owner = session.exec(select(User)).first()
    if owner is None:
        owner = _seed_user(session, Role.OWNER)
    items: list[Item] = []
    for i in range(n_items):
        it = Item(
            name=f"Item {i:02d}",
            sku_code=f"SKU-{i:02d}",
            material_code=f"MC-{i:02d}",
            unit="EACH",
            current_stock=0,
        )
        session.add(it)
    session.commit()
    items = list(session.exec(select(Item).order_by(Item.id)).all())
    po = PurchaseOrder(
        po_number="PO-VNEXT-001",
        status=POStatus.DESIGN_APPROVED,
        created_by_id=owner.id,
        created_at=datetime.utcnow(),
        zoho_po_id="po-z-vnext-1",
    )
    session.add(po)
    session.commit()
    session.refresh(po)
    for it in items:
        session.add(POLine(po_id=po.id, item_id=it.id, quantity=100))
    session.commit()
    return po, items


def _client(session: Session, engine, monkeypatch: pytest.MonkeyPatch):
    """TestClient wired to our in-memory engine + auto-logged-in user."""
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
        return session.exec(select(User).order_by(User.id)).first()
    app.dependency_overrides[deps.require_user] = _force_user
    app.dependency_overrides[deps.current_user] = _force_user
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# 1 + 2. Feature flag
# ---------------------------------------------------------------------------


def test_vnext_routes_404_when_flag_off(
    session: Session, engine, monkeypatch: pytest.MonkeyPatch,
):
    settings.RECEIVING_VNEXT_ENABLED = False
    _seed_user(session)
    po, _ = _seed_po_with_items(session)
    client = _client(session, engine, monkeypatch)

    for path in [
        f"/receive/v2/new?po_id={po.id}",
        "/receive/v2/1",
        "/receive/v2/1/cases",
        "/receive/v2/1/totals",
        "/receive/v2/1/items-search",
    ]:
        method = client.post if path.endswith("/cases") else client.get
        r = method(path)
        assert r.status_code == 404, f"{path} expected 404 with flag off, got {r.status_code}"


def test_vnext_create_works_when_flag_on(
    session: Session, engine, monkeypatch: pytest.MonkeyPatch,
):
    settings.RECEIVING_VNEXT_ENABLED = True
    _seed_user(session)
    po, _ = _seed_po_with_items(session)
    client = _client(session, engine, monkeypatch)
    r = client.get(f"/receive/v2/new?po_id={po.id}", follow_redirects=False)
    assert r.status_code == 303, r.text
    assert r.headers["Location"].startswith("/receive/v2/")


# ---------------------------------------------------------------------------
# 3 + 4. Create + receive number format
# ---------------------------------------------------------------------------


def test_create_receive_persists_with_r_yyyy_nnnn_number(
    session: Session, engine, monkeypatch: pytest.MonkeyPatch,
):
    settings.RECEIVING_VNEXT_ENABLED = True
    _seed_user(session)
    po, _ = _seed_po_with_items(session)
    client = _client(session, engine, monkeypatch)
    client.get(f"/receive/v2/new?po_id={po.id}", follow_redirects=False)
    rec = session.exec(select(Receive)).first()
    assert rec is not None
    assert re.match(r"^R-\d{4}-\d{4}$", rec.receive_number), rec.receive_number
    assert rec.purchase_order_id == po.id
    assert rec.status == ReceiveStatus.DRAFT
    assert rec.submission_id and len(rec.submission_id) == 32


def test_generate_receive_number_pure_helper_is_deterministic(session: Session):
    fixed = datetime(2026, 6, 25, 12, 0, 0)
    n1 = generate_receive_number(session, now=fixed)
    assert n1 == "R-2026-0001"
    session.add(Receive(
        receive_number=n1, delivery_date=date(2026, 6, 25),
        received_by_user_id=_seed_user(session, Role.OWNER).id,
    ))
    session.commit()
    assert generate_receive_number(session, now=fixed) == "R-2026-0002"


# ---------------------------------------------------------------------------
# 5 + 6. Add case + duplicate vendor case # rejected cleanly
# ---------------------------------------------------------------------------


def test_add_case_with_free_text_vendor_number(
    session: Session, engine, monkeypatch: pytest.MonkeyPatch,
):
    settings.RECEIVING_VNEXT_ENABLED = True
    _seed_user(session)
    po, _ = _seed_po_with_items(session)
    client = _client(session, engine, monkeypatch)
    create = client.get(f"/receive/v2/new?po_id={po.id}", follow_redirects=False)
    rid = int(create.headers["Location"].rsplit("/", 1)[-1])

    r = client.post(
        f"/receive/v2/{rid}/cases",
        data={"vendor_case_number": "BOX-A-7", "case_kind": "master_case"},
    )
    assert r.status_code == 200
    case = session.exec(select(ReceiveCase)).first()
    assert case.vendor_case_number == "BOX-A-7"
    assert case.sequence == 1


def test_duplicate_vendor_case_number_returns_409_not_500(
    session: Session, engine, monkeypatch: pytest.MonkeyPatch,
):
    settings.RECEIVING_VNEXT_ENABLED = True
    _seed_user(session)
    po, _ = _seed_po_with_items(session)
    client = _client(session, engine, monkeypatch)
    rid = int(client.get(f"/receive/v2/new?po_id={po.id}", follow_redirects=False).headers["Location"].rsplit("/", 1)[-1])
    r1 = client.post(f"/receive/v2/{rid}/cases", data={"vendor_case_number": "C-001"})
    assert r1.status_code == 200
    r2 = client.post(f"/receive/v2/{rid}/cases", data={"vendor_case_number": "C-001"})
    assert r2.status_code == 409
    assert "already exists" in r2.text


def test_null_vendor_case_numbers_can_coexist(
    session: Session, engine, monkeypatch: pytest.MonkeyPatch,
):
    """Drafting placeholder cases (no vendor # yet) must not trip the
    partial-unique constraint."""
    settings.RECEIVING_VNEXT_ENABLED = True
    _seed_user(session)
    po, _ = _seed_po_with_items(session)
    client = _client(session, engine, monkeypatch)
    rid = int(client.get(f"/receive/v2/new?po_id={po.id}", follow_redirects=False).headers["Location"].rsplit("/", 1)[-1])
    r1 = client.post(f"/receive/v2/{rid}/cases", data={"vendor_case_number": ""})
    r2 = client.post(f"/receive/v2/{rid}/cases", data={"vendor_case_number": ""})
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert session.scalar(select(__import__("sqlmodel").func.count()).select_from(ReceiveCase)) == 2


# ---------------------------------------------------------------------------
# 7. Multiple lines under one case
# ---------------------------------------------------------------------------


def test_add_multiple_lines_under_one_case(
    session: Session, engine, monkeypatch: pytest.MonkeyPatch,
):
    settings.RECEIVING_VNEXT_ENABLED = True
    _seed_user(session)
    po, items = _seed_po_with_items(session, n_items=3)
    client = _client(session, engine, monkeypatch)
    rid = int(client.get(f"/receive/v2/new?po_id={po.id}", follow_redirects=False).headers["Location"].rsplit("/", 1)[-1])
    client.post(f"/receive/v2/{rid}/cases", data={"vendor_case_number": "C-1"})
    case_id = session.exec(select(ReceiveCase)).first().id

    for it in items:
        r = client.post(
            f"/receive/v2/{rid}/cases/{case_id}/lines",
            data={"item_id": str(it.id), "declared_quantity": "10"},
        )
        assert r.status_code == 200, r.text

    lines = session.exec(select(ReceiveCaseLine).where(ReceiveCaseLine.receive_case_id == case_id)).all()
    assert len(lines) == 3
    assert {line.item_id for line in lines} == {it.id for it in items}


def test_line_requires_item_on_po(
    session: Session, engine, monkeypatch: pytest.MonkeyPatch,
):
    """Stage 1 scopes the line to the PO's items."""
    settings.RECEIVING_VNEXT_ENABLED = True
    _seed_user(session)
    po, _items = _seed_po_with_items(session, n_items=2)
    # extra item that is NOT on the PO
    off_po = Item(name="Off-PO", unit="EACH", current_stock=0)
    session.add(off_po)
    session.commit()
    session.refresh(off_po)

    client = _client(session, engine, monkeypatch)
    rid = int(client.get(f"/receive/v2/new?po_id={po.id}", follow_redirects=False).headers["Location"].rsplit("/", 1)[-1])
    client.post(f"/receive/v2/{rid}/cases", data={"vendor_case_number": "C-1"})
    case_id = session.exec(select(ReceiveCase)).first().id

    r = client.post(
        f"/receive/v2/{rid}/cases/{case_id}/lines",
        data={"item_id": str(off_po.id), "declared_quantity": "5"},
    )
    assert r.status_code == 400
    assert "not on this PO" in r.text


def test_line_requires_positive_qty(
    session: Session, engine, monkeypatch: pytest.MonkeyPatch,
):
    settings.RECEIVING_VNEXT_ENABLED = True
    _seed_user(session)
    po, items = _seed_po_with_items(session)
    client = _client(session, engine, monkeypatch)
    rid = int(client.get(f"/receive/v2/new?po_id={po.id}", follow_redirects=False).headers["Location"].rsplit("/", 1)[-1])
    client.post(f"/receive/v2/{rid}/cases", data={"vendor_case_number": "C-1"})
    case_id = session.exec(select(ReceiveCase)).first().id

    r = client.post(
        f"/receive/v2/{rid}/cases/{case_id}/lines",
        data={"item_id": str(items[0].id), "declared_quantity": "0"},
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# 8. Item search scoped to PO
# ---------------------------------------------------------------------------


def test_items_for_po_returns_only_po_items(session: Session):
    _seed_user(session, Role.OWNER)
    po, items = _seed_po_with_items(session, n_items=3)
    off_po = Item(name="Off-PO", unit="EACH", current_stock=0, material_code="OFF")
    session.add(off_po)
    session.commit()

    on_po = items_for_po(session, po.id)
    assert {it.id for it in on_po} == {it.id for it in items}
    assert off_po.id not in {it.id for it in on_po}


def test_items_for_po_q_filters_by_name_or_material_code(session: Session):
    _seed_user(session, Role.OWNER)
    po, items = _seed_po_with_items(session, n_items=3)
    # Items are named "Item 00", "Item 01", "Item 02" with codes MC-00/01/02.
    only_02_by_code = items_for_po(session, po.id, q="MC-02")
    assert {it.id for it in only_02_by_code} == {items[2].id}
    only_01_by_name = items_for_po(session, po.id, q="Item 01")
    assert {it.id for it in only_01_by_name} == {items[1].id}


def test_items_search_route_scopes_to_po_lines(
    session: Session, engine, monkeypatch: pytest.MonkeyPatch,
):
    settings.RECEIVING_VNEXT_ENABLED = True
    _seed_user(session)
    po, _items = _seed_po_with_items(session, n_items=2)
    off_po = Item(name="Other Item Z", unit="EACH", current_stock=0)
    session.add(off_po)
    session.commit()
    client = _client(session, engine, monkeypatch)
    rid = int(client.get(f"/receive/v2/new?po_id={po.id}", follow_redirects=False).headers["Location"].rsplit("/", 1)[-1])
    r = client.get(f"/receive/v2/{rid}/items-search?q=item")
    assert r.status_code == 200
    assert "Other Item Z" not in r.text
    assert "Item 00" in r.text


# ---------------------------------------------------------------------------
# 9. Totals
# ---------------------------------------------------------------------------


def test_totals_aggregate_per_item_across_cases(session: Session):
    _seed_user(session, Role.OWNER)
    po, items = _seed_po_with_items(session, n_items=2)
    rec = Receive(
        receive_number="R-2026-0001",
        purchase_order_id=po.id,
        delivery_date=date(2026, 6, 25),
        received_by_user_id=1,
    )
    session.add(rec)
    session.commit()
    session.refresh(rec)

    c1 = ReceiveCase(receive_id=rec.id, vendor_case_number="A", sequence=1)
    c2 = ReceiveCase(receive_id=rec.id, vendor_case_number="B", sequence=2)
    session.add_all([c1, c2])
    session.commit()
    session.refresh(c1)
    session.refresh(c2)
    session.add(ReceiveCaseLine(
        receive_case_id=c1.id, purchase_order_id=po.id, item_id=items[0].id,
        declared_quantity=10, counted_quantity=12,
    ))
    session.add(ReceiveCaseLine(
        receive_case_id=c2.id, purchase_order_id=po.id, item_id=items[0].id,
        declared_quantity=5,
    ))
    session.add(ReceiveCaseLine(
        receive_case_id=c1.id, purchase_order_id=po.id, item_id=items[1].id,
        declared_quantity=8,
    ))
    session.commit()

    totals = totals_by_item(session, rec.id)
    by_item = {t.item_id: t for t in totals}
    # item[0]: counted 12 + declared 5 = 17, has_count=True
    assert by_item[items[0].id].total_counted == 17
    assert by_item[items[0].id].has_count is True
    # item[1]: declared 8 (no count), counted-by-default = 8
    assert by_item[items[1].id].total_counted == 8
    assert by_item[items[1].id].has_count is False


# ---------------------------------------------------------------------------
# 10. Legacy /receive/{zoho_po_id} still works
# ---------------------------------------------------------------------------


def test_legacy_receive_form_still_renders(
    session: Session, engine, monkeypatch: pytest.MonkeyPatch,
):
    # Flag is OFF for this test (default); legacy must not depend on vNext.
    _seed_user(session)
    po, items = _seed_po_with_items(session)
    mirror = ZohoMirror(
        zoho_purchaseorder_id=po.zoho_po_id,
        purchaseorder_number=po.po_number,
        line_items=[{
            "item_id": "z-1", "name": items[0].name,
            "quantity": 100, "quantity_received": 0,
        }],
    )
    session.add(mirror)
    session.commit()
    client = _client(session, engine, monkeypatch)
    r = client.get(f"/receive/{po.zoho_po_id}")
    assert r.status_code == 200, r.text
    assert "submission_id" in r.text  # v2.4.1 idempotency token


# ---------------------------------------------------------------------------
# 12. Alembic single head + chain includes the new revision
# ---------------------------------------------------------------------------


def test_alembic_chain_includes_stage1_revision():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    sd = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = sd.get_heads()
    assert len(heads) == 1
    chain = {r.revision for r in sd.walk_revisions()}
    assert "e1f2a3b4c5d7" in chain
