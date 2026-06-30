"""Inventory stock-ownership policy (v2.11.0).

Single source of truth for "who is allowed to write
``Item.current_stock``." Lives here as a module so the rule + the
allowlist are documented in code (not just in a PR description).

Allowed writers — every entry below corresponds to a real, audited
movement workflow:

  * ``services/inventory_adjustments.create_adjustment``
    — v2.9.0 immutable adjustment ledger. The canonical adjust path.
  * ``services/consumption.apply_consumption_event``
    — Luma finished-lot consumption (production usage).
  * ``routes/purchase_orders.po_receive_*`` (legacy)
    — pre-vNext receive flow. Still active for non-vNext POs.
  * ``zoho.sync_items`` ONLY for brand-new items on first insert
    — initial seed of ``current_stock`` from the upstream Zoho value
      so a new SKU shows the right opening number. Existing items
      NEVER have their ``current_stock`` written from inbound sync.

Forbidden writers — must NEVER write ``current_stock``:

  * ``zoho.sync_items`` for existing items
  * any master-data edit route (``routes/inventory.update_item``,
    ``routes/inventory.edit_item_thresholds``) — current_stock has
    never been editable from these and v2.11.0 keeps that invariant.

The module provides three helpers used elsewhere in the codebase:

  * ``record_zoho_stock_snapshot(item, raw_stock, *, now=None)`` —
    write the snapshot + snapshot_at columns. Pure, no commit.
  * ``zoho_stock_variance(item)`` — Decimal difference between
    PackTrack and Zoho, or None when no snapshot. Used by the
    item-detail UI to show a variance pill.
  * ``parse_zoho_stock(raw)`` — pull the upstream stock value out of
    a Zoho item payload as a ``Decimal``. None when missing.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from packtrack.models import Item


def parse_zoho_stock(raw: dict[str, Any]) -> Decimal | None:
    """Pull the upstream stock-on-hand value from a Zoho item payload.

    Falls through several common field names that Zoho Inventory has
    used across API versions. Returns ``None`` if the field is missing
    or non-numeric — the caller treats that as "no snapshot to record."
    """
    if not isinstance(raw, dict):
        return None
    for key in (
        "actual_available_stock",
        "available_stock",
        "stock_on_hand",
        "quantity",
        "stock",
    ):
        value = raw.get(key)
        if value is None or value == "":
            continue
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return None
    return None


def record_zoho_stock_snapshot(
    item: Item, raw_stock: Decimal | None, *, now: datetime | None = None,
) -> None:
    """Update the two snapshot columns on the item. Does NOT commit and
    does NOT touch ``current_stock``. Caller decides when to commit."""
    if raw_stock is None:
        # Treat missing upstream value as "nothing to snapshot" — leave
        # the prior snapshot in place rather than overwriting with None.
        return
    item.last_zoho_stock_snapshot = raw_stock
    item.last_zoho_stock_snapshot_at = now or datetime.utcnow()


def zoho_stock_variance(item: Item) -> Decimal | None:
    """``Decimal(packtrack - zoho)``, or None when no snapshot exists.

    Sign convention: positive when PackTrack has MORE than Zoho thinks
    (likely Zoho is stale or missed an adjustment); negative when
    PackTrack has LESS (likely upstream sale/movement we didn't see)."""
    if item.last_zoho_stock_snapshot is None:
        return None
    pt = Decimal(str(item.current_stock or 0))
    return pt - item.last_zoho_stock_snapshot
