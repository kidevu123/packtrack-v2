"""Product-line derivation, grouped inventory queries, and Zoho item-sync
boundary behavior (outbound pending state + inbound overwrite protection).
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("PACKTRACK_SECRET_KEY", "test-secret")

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from packtrack import zoho
from packtrack.models import Item
from packtrack.services.inventory import (
    filter_inventory_items,
    group_counts,
)
from packtrack.services.product_line import GENERIC_GROUP, derive_product_line
from packtrack.services.zoho_item_sync import push_item_update

# ---------------------------------------------------------------------------
# Product-line derivation (pure)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("FIX 15mg 12ct Hybrid Focus (Green) - Bottle Label", "FIX"),
        ("FIX Beyond - Citrus Drift - Blister Card + Blister", "FIX Beyond"),
        ("25ct Master Case Box", GENERIC_GROUP),
        ("[Packaging] FIX 30ct - Carton", "FIX"),
        ("Master Case Box", GENERIC_GROUP),
        ("", GENERIC_GROUP),
        (None, GENERIC_GROUP),
        ("ACME", "ACME"),
        ("ACME Pro Deluxe Edition", "ACME Pro"),
    ],
)
def test_derive_product_line(name, expected):
    assert derive_product_line(name) == expected


# ---------------------------------------------------------------------------
# Grouped queries
# ---------------------------------------------------------------------------


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _add(session: Session, name: str, **kw) -> Item:
    it = Item(
        name=name,
        material_code=kw.pop("material_code", f"MC-{name[:6]}"),
        product_line=derive_product_line(name),
        current_stock=kw.pop("current_stock", 10.0),
        **kw,
    )
    session.add(it)
    session.commit()
    session.refresh(it)
    return it


def test_group_counts_buckets_and_orders(session: Session):
    _add(session, "FIX 15mg - Bottle Label")
    _add(session, "FIX 30ct - Carton")
    _add(session, "FIX Beyond - Blister")
    _add(session, "25ct Master Case Box")  # generic

    counts = dict(group_counts(session))
    assert counts["FIX"] == 2
    assert counts["FIX Beyond"] == 1
    assert counts[GENERIC_GROUP] == 1

    # Generic bucket always sorts last.
    ordered = [line for line, _ in group_counts(session)]
    assert ordered[-1] == GENERIC_GROUP


def test_filter_by_group(session: Session):
    _add(session, "FIX 15mg - Bottle Label")
    _add(session, "FIX Beyond - Blister")
    fix_only = filter_inventory_items(session, group="FIX")
    assert [i.name for i in fix_only] == ["FIX 15mg - Bottle Label"]


def test_group_filter_generic_includes_null_product_line(session: Session):
    # Legacy row with no derived product line should fall into the generic bucket.
    session.add(Item(name="Legacy Thing", material_code="MC-L", product_line=None))
    session.commit()
    rows = filter_inventory_items(session, group=GENERIC_GROUP)
    assert any(i.name == "Legacy Thing" for i in rows)


# ---------------------------------------------------------------------------
# Outbound item sync — honest pending state (no write path wired)
# ---------------------------------------------------------------------------


def test_push_item_update_parks_pending(session: Session):
    it = _add(session, "FIX 15mg - Bottle Label")
    result = push_item_update(session, it, payload={"name": it.name})
    assert result.status == "pending"
    assert result.ok_local is True
    assert it.zoho_push_status == "pending"
    assert it.zoho_push_error is None
    assert it.zoho_push_attempted_at is not None


# ---------------------------------------------------------------------------
# Inbound sync — overwrite protection / loop safety
# ---------------------------------------------------------------------------


def test_inbound_sync_preserves_pending_owner_edit(session: Session):
    it = _add(session, "FIX Local Name", vendor="Owner Vendor", current_stock=5.0)
    it.zoho_push_status = "pending"
    session.add(it)
    session.commit()

    raw = {
        "item_id": it.zoho_item_id or "z1",
        "name": "Zoho Overwrote Name",
        "sku": "SKU-1",
        "vendor_name": "Zoho Vendor",
        "description": "zoho desc",
        "unit": "boxes",
        "actual_available_stock": 99,
    }
    zoho._apply_item_sync_fields(it, raw)

    # Pushable owner edits (name/description/unit) are preserved while pending...
    assert it.name == "FIX Local Name"
    # ...but vendor is Zoho-read-only in PackTrack, so it always tracks Zoho...
    assert it.vendor == "Zoho Vendor"
    # ...and stock (not owner-editable) still tracks Zoho.
    assert it.current_stock == 99.0
    # product_line stays in step with the preserved local name.
    assert it.product_line == "FIX Local"


def test_inbound_sync_applies_when_not_pending(session: Session):
    it = _add(session, "Old Name", vendor="Old Vendor")
    it.zoho_push_status = None
    session.add(it)
    session.commit()

    raw = {
        "item_id": it.zoho_item_id or "z2",
        "name": "New Zoho Name",
        "vendor_name": "New Zoho Vendor",
        "actual_available_stock": 3,
    }
    zoho._apply_item_sync_fields(it, raw)
    assert it.name == "New Zoho Name"
    assert it.vendor == "New Zoho Vendor"
