"""Inventory adjustment Zoho-sync orchestrator (v2.10.0).

Sits between the route layer and the integration-service HTTP client:

  route → try_sync_adjustment(session, adjustment, item, *, actor, http_client=None)
       → push_adjustment_to_zoho() in zoho_adjustment_client.py
       → persists outcome on the adjustment row, commits

PackTrack remains the source of truth — local ledger + Item.current_stock
are already committed before this orchestrator runs. A FAILED sync
NEVER rolls back the local change.

Status transitions written here:

  any → SYNCED   (service ok=true)
  any → FAILED   (HTTP error, timeout, 4xx/5xx)
  any → SKIPPED  (item has no zoho_item_id)
  any → NOT_CONFIGURED (settings off / incomplete)

Retry semantics (used by both the initial post-create call and the
manual retry route): a row in ``ZohoSyncStatus.SYNCED`` is a no-op —
the integration service is idempotent on ``Idempotency-Key`` so a
re-push would be safe, but we refuse to make the network call to keep
the retry cheap. The route surfaces this as a friendly "already synced"
message.
"""
from __future__ import annotations

import logging
from datetime import datetime

import httpx
from sqlmodel import Session

from packtrack.models import InventoryAdjustment, Item, User, ZohoSyncStatus
from packtrack.services.zoho_adjustment_client import (
    OutcomeKind,
    SyncOutcome,
    is_configured,
    push_adjustment_to_zoho,
)

log = logging.getLogger(__name__)


def _actor_label(actor: User | None) -> str:
    """The string we send as ``created_by`` to the integration service.

    Email is preferred (stable, audit-friendly). Falls back to name,
    then ``user#<id>``. Never returns an empty string."""
    if actor is None:
        return "packtrack-system"
    return (actor.email or actor.name or f"user#{actor.id}").strip() or "packtrack-system"


def try_sync_adjustment(
    session: Session,
    adjustment: InventoryAdjustment,
    item: Item,
    *,
    actor: User | None,
    http_client: httpx.Client | None = None,
) -> SyncOutcome:
    """Push one adjustment to Zoho via the integration service and
    persist the outcome on the row.

    Safe to call after ``create_adjustment`` (initial sync) and from the
    retry route (manual / future cron). Increments ``sync_attempt_count``
    on every call that actually reaches the client. Does NOT increment
    when the row is already SYNCED (no-op).
    """
    # Already-synced rows are a no-op. The integration service IS
    # idempotent, but we avoid an unnecessary HTTP call so the retry
    # route stays cheap and the attempt count doesn't drift.
    if adjustment.zoho_sync_status is ZohoSyncStatus.SYNCED:
        return SyncOutcome(
            kind=OutcomeKind.SYNCED,
            zoho_reference=adjustment.zoho_reference,
        )

    outcome = push_adjustment_to_zoho(
        adjustment, item,
        created_by=_actor_label(actor),
        http_client=http_client,
    )
    _persist_outcome(session, adjustment, outcome)
    return outcome


def _persist_outcome(
    session: Session, adjustment: InventoryAdjustment, outcome: SyncOutcome,
) -> None:
    """Write the outcome to the row and commit. Increments
    ``sync_attempt_count`` whenever a real attempt was made (i.e. NOT
    when config is off — we never actually reached the network)."""
    now = datetime.utcnow()
    adjustment.zoho_sync_status = outcome.to_status()

    # Only count the attempt when we actually went past the config gate.
    # NOT_CONFIGURED means we never tried, so attempt count stays flat.
    if outcome.kind is not OutcomeKind.NOT_CONFIGURED:
        adjustment.sync_attempt_count = (adjustment.sync_attempt_count or 0) + 1

    if outcome.kind in (OutcomeKind.SYNCED, OutcomeKind.SYNCED_IDEMPOTENT):
        if outcome.zoho_reference:
            adjustment.zoho_reference = outcome.zoho_reference
        if outcome.zoho_adjustment_id:
            # We store the upstream id in the same column when the
            # service returns one — it doubles as the reference.
            adjustment.zoho_reference = (
                adjustment.zoho_reference or outcome.zoho_adjustment_id
            )
        adjustment.zoho_synced_at = now
        adjustment.zoho_sync_error = None
        adjustment.zoho_sync_warning = outcome.warning  # may be None
    elif outcome.kind is OutcomeKind.SKIPPED or outcome.kind is OutcomeKind.FAILED:
        adjustment.zoho_sync_error = outcome.error_message
        adjustment.zoho_sync_warning = None
    # NOT_CONFIGURED: leave fields as-is

    session.add(adjustment)
    session.commit()


def initial_status_for_new_adjustment() -> ZohoSyncStatus:
    """Status to stamp BEFORE the network attempt — used by
    ``services.inventory_adjustments.enqueue_or_mark_adjustment_sync``
    when the row is first inserted, before ``try_sync_adjustment`` runs
    in the route layer. PENDING when configured, NOT_CONFIGURED otherwise."""
    return ZohoSyncStatus.PENDING if is_configured() else ZohoSyncStatus.NOT_CONFIGURED
