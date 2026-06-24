"""Edge-case tests for /api/internal/luma-consumption + process_luma_consumption.

Locks several behaviors that are not covered by the existing
``test_consumption.py`` happy-path tests:

  * zero qty is a no-op writer (still records an event for audit)
  * **negative qty INCREMENTS stock** — current behaviour, almost
    certainly unintended. Test exists to surface the gap; do NOT
    change semantics without an explicit decision (see audit P0-2).
  * missing material_code in payload returns the error from the route
    rather than silently swallowing
  * unknown material_code is logged as ``skipped_not_found`` and does
    NOT crash the rest of the batch
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
# Negative qty — DEMONSTRATES THE BUG.
# ---------------------------------------------------------------------------


def test_negative_qty_currently_increments_stock_BUG(session: Session, item: Item):  # noqa: N802
    """⚠ Current behaviour increments stock when Luma sends qty_consumed < 0
    (max(0, prev - (-x)) == prev + x). Almost certainly unintended —
    Luma's contract should never send a negative qty, but PackTrack does
    not reject it.

    Captured here so the next person who touches the consumption service
    sees the gap immediately. Do not 'fix' by clamping; first decide:
    reject (4xx), log + skip, treat as a correction event with its own
    semantics, or silently floor at zero. See audit P0-2.
    """
    process_luma_consumption(session, _payload(-100))
    session.refresh(item)
    assert item.current_stock == 1100.0, (
        "Current code performs max(0, prev - qty) which inverts on negatives; "
        "if this assertion ever fails, someone changed the contract — "
        "update both this test and docs/PACKTRACK_LUMA_CONTRACT.md."
    )


# ---------------------------------------------------------------------------
# Missing material_code in payload
# ---------------------------------------------------------------------------


def test_payload_missing_material_code_crashes_loudly_BUG(session: Session, item: Item):  # noqa: N802
    """The service does ``mat['material_code']`` without a guard. A missing
    key currently raises KeyError → 500 from the route. Captured here as a
    gap to fix; consumption ought to return ``skipped_invalid`` for the
    offending entry, not abort the whole batch."""
    bad = {
        "source": "LUMA",
        "finished_lot_id": "lot-bad",
        "finished_lot_number": "FL-2",
        "released_at": "2026-06-24T15:00:00Z",
        "consumed_materials": [
            {"qty_consumed": 100},                              # missing material_code
            {"material_code": "PT-A", "qty_consumed": 50},      # would be processed
        ],
    }
    with pytest.raises(KeyError):
        process_luma_consumption(session, bad)
    # And the good entry never lands because the exception happened first.
    session.refresh(item)
    assert item.current_stock == 1000.0


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
