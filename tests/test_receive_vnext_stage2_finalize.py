"""Receiving vNext v2.6.0 Stage 2 — review / finalize / push coverage.

Covers the 18-item test list in the Stage 2 spec:
  1. Feature flag off → review/finalize/retry routes 404.
  2. Review page renders blockers/warnings.
  3. Finalize blocked by missing vendor case #, zero-line case,
     qty <= 0, parcel-missing-tracking.
  4. Over/under count is warning, not blocker.
  5. Missing material_code → warning + Luma NOT_READY behavior.
  6. Finalize materializes exactly one BoxReceipt per ReceiveCaseLine.
  7. BoxReceipt fields snapshot correctly.
  8. ``box_number == f"PT-{packtrack_receipt_id}"``.
  9. ``submission_id == Receive.submission_id``.
 10. ``submission_line_index`` is stable + deterministic.
 11. Second finalize attempt does not duplicate BoxReceipts.
 12. Zoho submit + Luma push called once per eligible leaf (stubs).
 13. Push failure sets ``Receive.status = PUSH_FAILED``.
 14. Retry only re-fires failed/pending/not-ready leaves.
 15. Successful leaves are not re-pushed.
 16. Legacy receive flow still works.
 17. Stage 1 route tests still pass (via running the existing
     ``test_receive_vnext_stage1.py`` file — not duplicated here).
 18. Alembic head remains ``e1f2a3b4c5d7`` (regression).
"""
from __future__ import annotations

import os
import re

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("PACKTRACK_SECRET_KEY", "test-secret")

from datetime import date, datetime

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from packtrack.config import settings
from packtrack.models import (
    BoxReceipt,
    Item,
    LumaPushStatus,
    POEvent,
    POLine,
    POStatus,
    PurchaseOrder,
    Receive,
    ReceiveCase,
    ReceiveCaseLine,
    ReceiveStatus,
    Role,
    ShipmentKind,
    User,
    ZohoMirror,
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
    # Mirror the partial UNIQUE the Stage 1 migration installs on Postgres.
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
def _restore_flag():
    original = settings.RECEIVING_VNEXT_ENABLED
    yield
    settings.RECEIVING_VNEXT_ENABLED = original


def _seed_user(session: Session, role: Role = Role.RECEIVING) -> User:
    user = User(
        id=1, email=f"{role.value}@example.com", name=role.value.title(),
        role=role, password_hash="x", is_active=True,
    )
    session.add(user)
    session.commit()
    return user


def _seed_po(session: Session, *, n_items: int = 2, with_zoho: bool = True) -> tuple[PurchaseOrder, list[Item], ZohoMirror | None]:
    owner = session.exec(select(User)).first() or _seed_user(session, Role.OWNER)
    items: list[Item] = []
    for i in range(n_items):
        items.append(Item(
            name=f"Item {i:02d}",
            sku_code=f"SKU-{i:02d}",
            material_code=f"MC-{i:02d}",
            zoho_item_id=f"z-{i:02d}",
            unit="EACH",
            current_stock=0,
            vendor="ACME",
        ))
    for it in items:
        session.add(it)
    session.commit()
    items = list(session.exec(select(Item).order_by(Item.id)).all())

    po = PurchaseOrder(
        po_number="PO-VNEXT-S2",
        status=POStatus.DESIGN_APPROVED,
        created_by_id=owner.id,
        created_at=datetime.utcnow(),
        zoho_po_id="po-z-vnext-s2" if with_zoho else None,
    )
    session.add(po)
    session.commit()
    session.refresh(po)
    for it in items:
        session.add(POLine(po_id=po.id, item_id=it.id, quantity=100, received_quantity=0))
    session.commit()

    mirror: ZohoMirror | None = None
    if with_zoho:
        mirror = ZohoMirror(
            zoho_purchaseorder_id=po.zoho_po_id,
            purchaseorder_number=po.po_number,
            line_items=[
                {"item_id": it.zoho_item_id, "line_item_id": f"li-{i}",
                 "name": it.name, "quantity": 100, "quantity_received": 0}
                for i, it in enumerate(items)
            ],
        )
        session.add(mirror)
        session.commit()
    return po, items, mirror


def _seed_receive(
    session: Session, user: User, po: PurchaseOrder, *,
    cases: list[tuple[str | None, list[tuple[Item, float, float | None]]]],
    shipment_kind: ShipmentKind = ShipmentKind.PALLETIZED,
    tracking: str | None = None,
) -> Receive:
    """Build a Receive + cases + lines from a compact spec.

    ``cases`` = list of (vendor_case_number, [(item, declared, counted?)…])
    """
    rec = Receive(
        receive_number="R-2026-0001",
        purchase_order_id=po.id,
        delivery_date=date(2026, 6, 25),
        received_by_user_id=user.id,
        status=ReceiveStatus.COUNTING,
        submission_id="abcdef0123456789" * 2,  # 32 chars
        shipment_kind=shipment_kind,
        tracking_number=tracking,
    )
    session.add(rec)
    session.commit()
    session.refresh(rec)
    for case_idx, (vcn, lines) in enumerate(cases, start=1):
        case = ReceiveCase(receive_id=rec.id, vendor_case_number=vcn, sequence=case_idx)
        session.add(case)
        session.commit()
        session.refresh(case)
        for item, declared, counted in lines:
            session.add(ReceiveCaseLine(
                receive_case_id=case.id,
                purchase_order_id=po.id,
                item_id=item.id,
                declared_quantity=declared,
                counted_quantity=counted,
                unit_of_measure=item.unit or "EACH",
            ))
        session.commit()
    return rec


def _finalize(client, receive_id: int, *, confirm: bool = True):
    """POST the finalize route. Default confirms warnings — the test
    fixture's PO has quantity=100 per line so under-count warnings
    would otherwise fire for the small qtys these tests use. Tests
    that specifically assert blocker/warning behavior call the route
    directly with confirm=False."""
    data = {"confirm_warnings": "true"} if confirm else {}
    return client.post(f"/receive/v2/{receive_id}/finalize", data=data)


def _client(session: Session, engine, monkeypatch: pytest.MonkeyPatch):
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


def _stub_externals(monkeypatch: pytest.MonkeyPatch, *, luma_ok: bool = True, zoho_status: str = "committed"):
    """Stub the existing Luma + Zoho push helpers so tests count calls
    without touching the real services. Returns a counters dict."""
    from packtrack.services import receiving as recv_svc
    from packtrack.services import receiving_v2_finalize as finalize_svc

    calls = {"luma_push": 0, "luma_register_item": 0, "luma_register_material": 0,
             "ensure_material_code": 0, "zoho_submit": 0}

    def _luma_push(box, po_number, photo_urls, *, received_by="", dry_run=False):
        calls["luma_push"] += 1
        if luma_ok:
            return True, None, {"ok": True}
        return False, "stub-failed", None

    def _register_material(item):
        calls["luma_register_material"] += 1
        return True, None

    def _register_item(item, *args, **kwargs):
        calls["luma_register_item"] += 1
        from packtrack.services.receiving import (
            LumaRegistrationOutcome,
            LumaRegistrationResult,
        )
        return LumaRegistrationResult(outcome=LumaRegistrationOutcome.ALREADY_MAPPED)

    def _ensure_code(session, item):
        calls["ensure_material_code"] += 1
        return item.material_code

    def _submit_zoho(mirror, submissions, *, operator, session_id, notes=None):
        calls["zoho_submit"] += 1
        from packtrack.services.receiving import ZohoReceiveResult
        return [
            ZohoReceiveResult(submission=s, status=zoho_status, message=None)
            for s in submissions
        ]

    # Patch at both call sites — the finalize service imports them at
    # module load, and the legacy receiving service also exposes them.
    monkeypatch.setattr(finalize_svc, "push_luma_receipt", _luma_push)
    monkeypatch.setattr(finalize_svc, "register_material_with_luma", _register_material)
    monkeypatch.setattr(finalize_svc, "ensure_material_code", _ensure_code)
    monkeypatch.setattr(finalize_svc, "submit_zoho_receives", _submit_zoho)
    monkeypatch.setattr(recv_svc, "push_luma_receipt", _luma_push)
    monkeypatch.setattr(recv_svc, "register_item_with_luma", _register_item)
    monkeypatch.setattr(recv_svc, "register_material_with_luma", _register_material)
    monkeypatch.setattr(recv_svc, "submit_zoho_receives", _submit_zoho)

    # Stub Luma env so the existing settings checks don't short-circuit.
    monkeypatch.setattr(settings, "LUMA_RECEIPT_WEBHOOK_URL", "http://luma.test/r")
    monkeypatch.setattr(settings, "LUMA_PACKTRACK_SECRET", "x")
    return calls


# ---------------------------------------------------------------------------
# 1. Feature flag off blocks all Stage 2 routes
# ---------------------------------------------------------------------------


def test_flag_off_blocks_stage2_routes(session, engine, monkeypatch):
    settings.RECEIVING_VNEXT_ENABLED = False
    _seed_user(session)
    po, _, _ = _seed_po(session)
    rec = _seed_receive(session, session.get(User, 1), po, cases=[
        ("C-1", [(session.get(Item, 1), 10, None)]),
    ])
    client = _client(session, engine, monkeypatch)

    assert client.get(f"/receive/v2/{rec.id}/review").status_code == 404
    assert client.post(f"/receive/v2/{rec.id}/finalize").status_code == 404
    assert client.post(f"/receive/v2/{rec.id}/retry-push").status_code == 404


# ---------------------------------------------------------------------------
# 2. Review page renders blockers + warnings
# ---------------------------------------------------------------------------


def test_review_renders_blockers_and_warnings(session, engine, monkeypatch):
    settings.RECEIVING_VNEXT_ENABLED = True
    _seed_user(session)
    po, items, _ = _seed_po(session)
    # Missing-vendor-# case + an item with no material_code (warning).
    items[1].material_code = None
    session.add(items[1])
    session.commit()
    rec = _seed_receive(session, session.get(User, 1), po, cases=[
        (None, [(items[1], 10, None)]),  # blocker: missing vendor case #
    ])
    client = _client(session, engine, monkeypatch)
    r = client.get(f"/receive/v2/{rec.id}/review")
    assert r.status_code == 200
    assert "CASE_MISSING_VENDOR_NUMBER" in r.text
    assert "ITEM_NO_MATERIAL_CODE" in r.text


# ---------------------------------------------------------------------------
# 3. Finalize blockers
# ---------------------------------------------------------------------------


def test_finalize_blocked_missing_vendor_case_number(session, engine, monkeypatch):
    settings.RECEIVING_VNEXT_ENABLED = True
    _seed_user(session)
    po, items, _ = _seed_po(session)
    rec = _seed_receive(session, session.get(User, 1), po, cases=[
        (None, [(items[0], 10, None)]),
    ])
    client = _client(session, engine, monkeypatch)
    r = client.post(f"/receive/v2/{rec.id}/finalize")
    assert r.status_code == 400
    assert "blocker" in r.text.lower()


def test_finalize_blocked_zero_line_case(session, engine, monkeypatch):
    settings.RECEIVING_VNEXT_ENABLED = True
    _seed_user(session)
    po, _items, _ = _seed_po(session)
    rec = _seed_receive(session, session.get(User, 1), po, cases=[
        ("C-1", []),  # zero-line case
    ])
    client = _client(session, engine, monkeypatch)
    assert client.post(f"/receive/v2/{rec.id}/finalize").status_code == 400


def test_finalize_blocked_parcel_missing_tracking(session, engine, monkeypatch):
    settings.RECEIVING_VNEXT_ENABLED = True
    _seed_user(session)
    po, items, _ = _seed_po(session)
    rec = _seed_receive(session, session.get(User, 1), po, cases=[
        ("C-1", [(items[0], 10, None)]),
    ], shipment_kind=ShipmentKind.PARCEL, tracking=None)
    client = _client(session, engine, monkeypatch)
    assert client.post(f"/receive/v2/{rec.id}/finalize").status_code == 400


def test_finalize_blocked_qty_le_zero(session, engine, monkeypatch):
    settings.RECEIVING_VNEXT_ENABLED = True
    _seed_user(session)
    po, items, _ = _seed_po(session)
    # Build manually to bypass route validation that would reject qty=0.
    rec = Receive(
        receive_number="R-2026-0001", purchase_order_id=po.id,
        delivery_date=date(2026, 6, 25), received_by_user_id=1,
        status=ReceiveStatus.COUNTING, submission_id="x" * 32,
    )
    session.add(rec)
    session.commit()
    session.refresh(rec)
    case = ReceiveCase(receive_id=rec.id, vendor_case_number="C-1", sequence=1)
    session.add(case)
    session.commit()
    session.refresh(case)
    session.add(ReceiveCaseLine(
        receive_case_id=case.id, purchase_order_id=po.id, item_id=items[0].id,
        declared_quantity=0,
    ))
    session.commit()
    client = _client(session, engine, monkeypatch)
    assert client.post(f"/receive/v2/{rec.id}/finalize").status_code == 400


# ---------------------------------------------------------------------------
# 4 + 5. Over/under count + missing material_code = WARNINGS not blockers
# ---------------------------------------------------------------------------


def test_over_count_is_warning_not_blocker(session, engine, monkeypatch):
    settings.RECEIVING_VNEXT_ENABLED = True
    _stub_externals(monkeypatch)
    _seed_user(session)
    po, items, _ = _seed_po(session)
    # PO line is 100; declare 500 → over.
    rec = _seed_receive(session, session.get(User, 1), po, cases=[
        ("C-1", [(items[0], 500, None)]),
    ])
    client = _client(session, engine, monkeypatch)
    review = client.get(f"/receive/v2/{rec.id}/review")
    assert "ITEM_OVER_PO" in review.text
    # Finalize requires confirm_warnings.
    no_confirm = client.post(f"/receive/v2/{rec.id}/finalize")
    assert no_confirm.status_code == 422
    confirmed = client.post(f"/receive/v2/{rec.id}/finalize", data={"confirm_warnings": "true"})
    assert confirmed.status_code == 200


def test_missing_material_code_warning_and_luma_not_ready(session, engine, monkeypatch):
    settings.RECEIVING_VNEXT_ENABLED = True
    calls = _stub_externals(monkeypatch)
    _seed_user(session)
    po, items, _ = _seed_po(session)
    items[0].material_code = None
    session.add(items[0])
    session.commit()
    rec = _seed_receive(session, session.get(User, 1), po, cases=[
        ("C-1", [(items[0], 10, None)]),
    ])
    # Make _ensure_material_code a no-op so material_code stays empty.
    from packtrack.services import receiving_v2_finalize as finalize_svc
    monkeypatch.setattr(finalize_svc, "ensure_material_code", lambda s, it: None)
    client = _client(session, engine, monkeypatch)
    r = client.post(f"/receive/v2/{rec.id}/finalize", data={"confirm_warnings": "true"})
    assert r.status_code == 200
    box = session.exec(select(BoxReceipt).where(BoxReceipt.receive_id == rec.id)).first()
    assert box.luma_push_status == LumaPushStatus.NOT_READY
    # Luma was never called for that leaf (gated by material_code).
    assert calls["luma_push"] == 0


# ---------------------------------------------------------------------------
# 6 + 7 + 8 + 9 + 10. Materialization correctness
# ---------------------------------------------------------------------------


def test_finalize_materializes_one_box_per_line(session, engine, monkeypatch):
    settings.RECEIVING_VNEXT_ENABLED = True
    _stub_externals(monkeypatch)
    _seed_user(session)
    po, items, _ = _seed_po(session)
    rec = _seed_receive(session, session.get(User, 1), po, cases=[
        ("C-1", [(items[0], 5, 5), (items[1], 8, None)]),
        ("C-2", [(items[0], 3, 3)]),
    ])
    client = _client(session, engine, monkeypatch)
    r = _finalize(client, rec.id)
    assert r.status_code == 200
    boxes = session.exec(select(BoxReceipt).where(BoxReceipt.receive_id == rec.id)).all()
    assert len(boxes) == 3
    # All have receive_case_line_id set and the line FK back-points to box.
    for box in boxes:
        assert box.receive_case_line_id is not None
        line = session.get(ReceiveCaseLine, box.receive_case_line_id)
        assert line.box_receipt_id == box.id


def test_box_receipt_field_snapshots(session, engine, monkeypatch):
    settings.RECEIVING_VNEXT_ENABLED = True
    _stub_externals(monkeypatch)
    user = _seed_user(session)
    po, items, _ = _seed_po(session)
    rec = _seed_receive(session, user, po, cases=[
        ("C-1", [(items[0], 12, 11)]),
    ])
    client = _client(session, engine, monkeypatch)
    _finalize(client, rec.id)
    box = session.exec(select(BoxReceipt).where(BoxReceipt.receive_id == rec.id)).first()
    assert box.material_code == items[0].material_code
    assert box.material_name == items[0].name
    assert box.supplier == items[0].vendor
    assert box.declared_quantity == 12
    assert box.counted_quantity == 11
    assert box.accepted_quantity == 11  # counted-takes-precedence
    assert box.purchase_order_id == po.id
    assert box.received_by_user_id == user.id


def test_box_number_uses_pt_compat_format(session, engine, monkeypatch):
    settings.RECEIVING_VNEXT_ENABLED = True
    _stub_externals(monkeypatch)
    _seed_user(session)
    po, items, _ = _seed_po(session)
    rec = _seed_receive(session, session.get(User, 1), po, cases=[
        ("C-1", [(items[0], 10, None)]),
    ])
    _finalize(_client(session, engine, monkeypatch), rec.id)
    box = session.exec(select(BoxReceipt).where(BoxReceipt.receive_id == rec.id)).first()
    # v2.4.1 contract: box_number == "PT-{packtrack_receipt_id}"
    assert box.box_number == f"PT-{box.packtrack_receipt_id}"
    assert re.match(r"^PT-[0-9a-f]{32}$", box.box_number), box.box_number


def test_submission_id_propagates_from_receive(session, engine, monkeypatch):
    settings.RECEIVING_VNEXT_ENABLED = True
    _stub_externals(monkeypatch)
    _seed_user(session)
    po, items, _ = _seed_po(session)
    rec = _seed_receive(session, session.get(User, 1), po, cases=[
        ("C-1", [(items[0], 10, None)]),
    ])
    _finalize(_client(session, engine, monkeypatch), rec.id)
    box = session.exec(select(BoxReceipt).where(BoxReceipt.receive_id == rec.id)).first()
    assert box.submission_id == rec.submission_id


def test_submission_line_index_is_stable_and_deterministic(session, engine, monkeypatch):
    """Order: cases by (sequence, id), lines within case by id.
    Indices start at 1 and never collide."""
    settings.RECEIVING_VNEXT_ENABLED = True
    _stub_externals(monkeypatch)
    _seed_user(session)
    po, items, _ = _seed_po(session)
    rec = _seed_receive(session, session.get(User, 1), po, cases=[
        ("C-1", [(items[0], 1, None), (items[1], 2, None)]),
        ("C-2", [(items[0], 3, None)]),
    ])
    _finalize(_client(session, engine, monkeypatch), rec.id)
    boxes = session.exec(
        select(BoxReceipt).where(BoxReceipt.receive_id == rec.id).order_by(BoxReceipt.submission_line_index)
    ).all()
    assert [b.submission_line_index for b in boxes] == [1, 2, 3]
    assert [b.declared_quantity for b in boxes] == [1.0, 2.0, 3.0]


# ---------------------------------------------------------------------------
# 11. Finalize idempotency — second attempt does not double
# ---------------------------------------------------------------------------


def test_second_finalize_does_not_duplicate(session, engine, monkeypatch):
    settings.RECEIVING_VNEXT_ENABLED = True
    _stub_externals(monkeypatch)
    _seed_user(session)
    po, items, _ = _seed_po(session)
    rec = _seed_receive(session, session.get(User, 1), po, cases=[
        ("C-1", [(items[0], 10, None)]),
    ])
    client = _client(session, engine, monkeypatch)
    r1 = _finalize(client, rec.id)
    assert r1.status_code == 200
    n_before = session.scalar(
        select(__import__("sqlmodel").func.count()).select_from(BoxReceipt)
    )
    # Second finalize is blocked by ALREADY_FINALIZED — but even if a
    # retry weasels around the route guard at the service layer, the
    # materialization helper is idempotent on existing line.box_receipt_id.
    r2 = client.post(f"/receive/v2/{rec.id}/finalize")
    assert r2.status_code == 400  # ALREADY_FINALIZED blocker
    n_after = session.scalar(
        select(__import__("sqlmodel").func.count()).select_from(BoxReceipt)
    )
    assert n_after == n_before


# ---------------------------------------------------------------------------
# 12. Push fan-out is once-per-eligible-leaf
# ---------------------------------------------------------------------------


def test_push_called_once_per_eligible_leaf(session, engine, monkeypatch):
    settings.RECEIVING_VNEXT_ENABLED = True
    calls = _stub_externals(monkeypatch)
    _seed_user(session)
    po, items, _ = _seed_po(session)
    rec = _seed_receive(session, session.get(User, 1), po, cases=[
        ("C-1", [(items[0], 5, None), (items[1], 7, None)]),
        ("C-2", [(items[0], 3, None)]),
    ])
    _finalize(_client(session, engine, monkeypatch), rec.id)
    assert calls["luma_push"] == 3
    assert calls["zoho_submit"] == 1  # one batch call for the whole receive
    session.refresh(rec)
    assert rec.status == ReceiveStatus.PUSHED_OK


# ---------------------------------------------------------------------------
# 13 + 14 + 15. Failure + retry semantics
# ---------------------------------------------------------------------------


def test_luma_failure_sets_push_failed(session, engine, monkeypatch):
    settings.RECEIVING_VNEXT_ENABLED = True
    _stub_externals(monkeypatch, luma_ok=False)
    _seed_user(session)
    po, items, _ = _seed_po(session)
    rec = _seed_receive(session, session.get(User, 1), po, cases=[
        ("C-1", [(items[0], 5, None)]),
    ])
    _finalize(_client(session, engine, monkeypatch), rec.id)
    session.refresh(rec)
    assert rec.status == ReceiveStatus.PUSH_FAILED
    box = session.exec(select(BoxReceipt).where(BoxReceipt.receive_id == rec.id)).first()
    assert box.luma_push_status == LumaPushStatus.FAILED


def test_retry_only_re_fires_failed_leaves(session, engine, monkeypatch):
    settings.RECEIVING_VNEXT_ENABLED = True
    calls = _stub_externals(monkeypatch, luma_ok=False)
    _seed_user(session)
    po, items, _ = _seed_po(session)
    rec = _seed_receive(session, session.get(User, 1), po, cases=[
        ("C-1", [(items[0], 5, None), (items[1], 7, None)]),
    ])
    client = _client(session, engine, monkeypatch)
    _finalize(client, rec.id)
    # 2 failed leaves → 2 luma calls
    assert calls["luma_push"] == 2

    # Mark one leaf as PUSHED to simulate partial fix.
    boxes = session.exec(
        select(BoxReceipt).where(BoxReceipt.receive_id == rec.id).order_by(BoxReceipt.id)
    ).all()
    boxes[0].luma_push_status = LumaPushStatus.PUSHED
    session.add(boxes[0])
    session.commit()

    # Now flip stub to succeed; retry should only push the still-failed one.
    _stub_externals(monkeypatch, luma_ok=True)
    # _stub_externals resets the counter; re-capture after the second stub.
    from packtrack.services import receiving_v2_finalize as finalize_svc
    luma_calls = {"n": 0}
    def _ok(box, po_number, photo_urls, **_):
        luma_calls["n"] += 1
        return True, None, {"ok": True}
    monkeypatch.setattr(finalize_svc, "push_luma_receipt", _ok)

    r = client.post(f"/receive/v2/{rec.id}/retry-push")
    assert r.status_code == 200
    # Only 1 leaf was failed-and-eligible; only 1 luma call.
    assert luma_calls["n"] == 1


# ---------------------------------------------------------------------------
# 16. Legacy receive form still works (no regression)
# ---------------------------------------------------------------------------


def test_legacy_receive_form_still_works(session, engine, monkeypatch):
    """Stage 2 must not touch the legacy /receive/{zoho_po_id} flow."""
    _seed_user(session)
    _po, _items, mirror = _seed_po(session)
    client = _client(session, engine, monkeypatch)
    r = client.get(f"/receive/{mirror.zoho_purchaseorder_id}")
    assert r.status_code == 200
    assert "submission_id" in r.text  # v2.4.1 token still embedded


# ---------------------------------------------------------------------------
# 17 — separate test file ``test_receive_vnext_stage1.py`` covers Stage 1
# routes; running the full suite (pytest -q) exercises both files.
# 18. Alembic head regression
# ---------------------------------------------------------------------------


def test_alembic_head_unchanged():
    """Stage 2 adds no migration; the head must still be the Stage 1 rev."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    sd = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = sd.get_heads()
    assert len(heads) == 1
    assert heads[0] == "e1f2a3b4c5d7"


# ---------------------------------------------------------------------------
# Bonus: POEvents are emitted for the audit trail
# ---------------------------------------------------------------------------


def test_finalize_emits_po_events(session, engine, monkeypatch):
    settings.RECEIVING_VNEXT_ENABLED = True
    _stub_externals(monkeypatch)
    _seed_user(session)
    po, items, _ = _seed_po(session)
    rec = _seed_receive(session, session.get(User, 1), po, cases=[
        ("C-1", [(items[0], 5, None)]),
    ])
    _finalize(_client(session, engine, monkeypatch), rec.id)
    kinds = {e.kind for e in session.exec(select(POEvent).where(POEvent.po_id == po.id)).all()}
    assert "receive_finalized" in kinds
    assert "receive_pushed_ok" in kinds
