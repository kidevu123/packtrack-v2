"""Edge-case tests for /api/internal/luma-consumption + process_luma_consumption.

Locks the post-v2.4.1 behaviour for several edges:

  * zero qty is a no-op writer (still records an event for audit)
  * **negative qty is rejected** as ``skipped_invalid`` (P0-2 fix)
  * **missing per-entry material_code** is rejected as ``skipped_invalid``
    and the batch continues processing the valid entries (P0-3 fix)
  * unknown material_code is logged as ``skipped_not_found`` and does
    NOT crash the rest of the batch
  * non-numeric / missing qty_consumed is rejected as ``skipped_invalid``
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("PACKTRACK_SECRET_KEY", "test-secret")

import pytest
from sqlmodel import Session, create_engine, select

from packtrack.models import Item, MaterialConsumptionEvent, User
from packtrack.services.consumption import process_luma_consumption


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Item.metadata.tables["items"].create(bind=engine)
    MaterialConsumptionEvent.metadata.tables["material_consumption_events"].create(bind=engine)
    User.metadata.tables["users"].create(bind=engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def item(session: Session) -> Item:
    it = Item(
        name="Sweet Trip Blister", unit="each",
        current_stock=1000.0, reorder_point=200.0, critical_point=50.0,
        daily_usage_rate=0.0, material_code="PT-A",
    )
    session.add(it)
    session.commit()
    session.refresh(it)
    return it


def _payload(qty, *, finished_lot_id="lot-1"):
    return {
        "source": "LUMA",
        "finished_lot_id": finished_lot_id,
        "finished_lot_number": "FL-1",
        "released_at": "2026-06-24T15:00:00Z",
        "consumed_materials": [{"material_code": "PT-A", "qty_consumed": qty}],
    }


# ---------------------------------------------------------------------------
# Zero qty
# ---------------------------------------------------------------------------


def test_zero_qty_records_event_and_does_not_change_stock(session: Session, item: Item):
    process_luma_consumption(session, _payload(0))
    session.refresh(item)
    assert item.current_stock == 1000.0
    events = session.exec(
        select(MaterialConsumptionEvent).where(MaterialConsumptionEvent.item_id == item.id)
    ).all()
    assert len(events) == 1
    assert events[0].qty_consumed == 0.0


# ---------------------------------------------------------------------------
# P0-2: negative qty is rejected, stock untouched, no event recorded.
# ---------------------------------------------------------------------------


def test_negative_qty_is_rejected_and_stock_untouched(session: Session, item: Item):
    result = process_luma_consumption(session, _payload(-100))
    session.refresh(item)

    # Stock unchanged.
    assert item.current_stock == 1000.0

    # The entry is reported as skipped_invalid with a reason.
    entry = result["processed"][0]
    assert entry["status"] == "skipped_invalid"
    assert "negative" in entry["reason"].lower()
    assert entry["material_code"] == "PT-A"

    # No MaterialConsumptionEvent was written (no misleading audit row).
    events = session.exec(
        select(MaterialConsumptionEvent).where(MaterialConsumptionEvent.item_id == item.id)
    ).all()
    assert events == []


def test_non_numeric_qty_is_rejected(session: Session, item: Item):
    result = process_luma_consumption(session, _payload("not-a-number"))
    session.refresh(item)
    assert item.current_stock == 1000.0
    assert result["processed"][0]["status"] == "skipped_invalid"
    assert "non-numeric" in result["processed"][0]["reason"]


def test_missing_qty_is_rejected(session: Session, item: Item):
    bad = {
        "source": "LUMA",
        "finished_lot_id": "lot-missing-qty",
        "finished_lot_number": "FL-Q",
        "released_at": "2026-06-24T15:00:00Z",
        "consumed_materials": [{"material_code": "PT-A"}],  # qty_consumed missing
    }
    result = process_luma_consumption(session, bad)
    session.refresh(item)
    assert item.current_stock == 1000.0
    assert result["processed"][0]["status"] == "skipped_invalid"
    assert "qty_consumed" in result["processed"][0]["reason"]


# ---------------------------------------------------------------------------
# P0-3: missing per-entry material_code is rejected but does not crash the batch.
# ---------------------------------------------------------------------------


def test_missing_material_code_skips_invalid_entry_and_continues_batch(
    session: Session, item: Item,
):
    bad_then_good = {
        "source": "LUMA",
        "finished_lot_id": "lot-mixed-2",
        "finished_lot_number": "FL-2",
        "released_at": "2026-06-24T15:00:00Z",
        "consumed_materials": [
            {"qty_consumed": 100},                              # missing material_code → skipped_invalid
            {"material_code": "PT-A", "qty_consumed": 50},      # valid → updated
        ],
    }
    result = process_luma_consumption(session, bad_then_good)
    session.refresh(item)

    # Valid entry was applied.
    assert item.current_stock == 950.0

    # First entry surfaces as skipped_invalid; second as updated.
    statuses = [r["status"] for r in result["processed"]]
    assert statuses == ["skipped_invalid", "updated"]
    assert result["processed"][0]["material_code"] is None
    assert result["processed"][0]["reason"] == "missing material_code"


def test_unknown_material_code_skips_without_crashing(session: Session, item: Item):
    result = process_luma_consumption(
        session,
        {
            "source": "LUMA",
            "finished_lot_id": "lot-mixed",
            "finished_lot_number": "FL-3",
            "released_at": "2026-06-24T15:00:00Z",
            "consumed_materials": [
                {"material_code": "PT-UNKNOWN", "qty_consumed": 100},
                {"material_code": "PT-A", "qty_consumed": 50},
            ],
        },
    )
    by_code = {r["material_code"]: r["status"] for r in result["processed"]}
    assert by_code["PT-UNKNOWN"] == "skipped_not_found"
    assert by_code["PT-A"] == "updated"
    session.refresh(item)
    assert item.current_stock == 950.0
