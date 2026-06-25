"""Outbound item-update sync (PackTrack -> Zoho) — boundary-safe wrapper.

Background / why this is a wrapper and not a direct Zoho call
------------------------------------------------------------
PackTrack never writes to Zoho directly for the flows that already exist:

* PO creation goes through ``zoho.push_po`` (direct OAuth, being migrated to
  the gateway in a later phase).
* Purchase-receives go through the ``zoho-integration-service``
  (``services/zoho_integration.py``).

There is currently **no item-update endpoint** on either the read gateway
(``ZOHO_GATEWAY_URL`` — list/image only) or the integration service
(receive preview/commit only). So an owner editing a Zoho-owned field
(name / description / vendor / unit) cannot be pushed back to Zoho yet.

Rather than invent a fragile direct Zoho item write from the UI (explicitly
out of scope per the integration boundary), we:

1. Save the edit locally (the route does this).
2. Record an honest outbound state on the item via this wrapper:
   ``pending`` when there's nothing to push to yet, ``synced`` once a real
   push succeeds, ``failed`` with an error message when a wired push fails.
3. Protect those locally-edited fields from being clobbered by the inbound
   Zoho sync while the push is ``pending`` (see ``zoho.sync_items``), so the
   UI never silently reverts an owner's edit.

Wiring a real push later is a single, contained change: implement
``item_write_path_available`` to return True when the endpoint exists and fill
in the ``TODO(zoho-item-write)`` block in ``push_item_update``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlmodel import Session

from packtrack.models import Item

logger = logging.getLogger("packtrack.zoho_item_sync")

# Zoho-owned fields an owner can edit in PackTrack. Editing one of these is
# what makes an item "dirty" for outbound sync. Fields NOT in this set
# (material_code, thresholds, lead days, daily usage) are PackTrack-owned and
# never pushed to Zoho, so editing them does not change push state.
ZOHO_OWNED_EDITABLE_FIELDS: frozenset[str] = frozenset(
    {"name", "description", "vendor", "unit"}
)

PUSH_PENDING = "pending"
PUSH_SYNCED = "synced"
PUSH_FAILED = "failed"


@dataclass
class ItemPushResult:
    """Outcome of an outbound item-update attempt.

    ``pending`` is NOT a failure — it means the edit is safely stored locally
    and is waiting for a Zoho write path to exist. The UI surfaces it as
    "Saved locally · Zoho sync pending".
    """

    status: str
    error: str | None = None

    @property
    def ok_local(self) -> bool:
        return self.status in (PUSH_SYNCED, PUSH_PENDING)


def item_write_path_available() -> bool:
    """Whether a safe Zoho item-update endpoint is wired.

    Returns False today — neither the read gateway nor the integration
    service exposes an item-update write. Flip this (and fill in the push
    block below) when one lands.
    """
    return False


def push_item_update(session: Session, item: Item) -> ItemPushResult:
    """Attempt to push an owner's item edit to Zoho; record honest state.

    Always sets ``zoho_push_attempted_at``. When no write path exists, parks
    the item as ``pending`` (error cleared) and returns — never raises, never
    pretends the remote write happened.
    """
    item.zoho_push_attempted_at = datetime.utcnow()

    if not item_write_path_available():
        item.zoho_push_status = PUSH_PENDING
        item.zoho_push_error = None
        session.add(item)
        session.commit()
        logger.info(
            "item %s edit saved locally; Zoho item-write path not available "
            "(parked pending)",
            item.id,
        )
        return ItemPushResult(PUSH_PENDING)

    # TODO(zoho-item-write): when the gateway/integration service exposes an
    # item-update endpoint, build the payload from the Zoho-owned fields and
    # POST it here through a dedicated client (mirroring zoho_integration.py).
    # On success: set PUSH_SYNCED + clear error. On failure: set PUSH_FAILED +
    # store a truncated error. Until then this branch is unreachable.
    try:  # pragma: no cover - unreachable until a write path is wired
        raise NotImplementedError("Zoho item-update endpoint not implemented")
    except Exception as exc:  # pragma: no cover
        item.zoho_push_status = PUSH_FAILED
        item.zoho_push_error = str(exc)[:1000]
        session.add(item)
        session.commit()
        logger.warning("item %s Zoho push failed: %s", item.id, exc)
        return ItemPushResult(PUSH_FAILED, item.zoho_push_error)


def mark_in_sync(session: Session, item: Item) -> None:
    """Clear outbound push state — used when local values now match Zoho."""
    if item.zoho_push_status is not None or item.zoho_push_error is not None:
        item.zoho_push_status = None
        item.zoho_push_error = None
        session.add(item)
        session.commit()
