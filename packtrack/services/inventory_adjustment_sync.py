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

v2.16.3 — retry eligibility (``retry_eligibility``) is the single
authoritative gate for "can this adjustment be safely re-pushed to
Zoho?". Route + UI both call it; the rules sit here so the same answer
is given regardless of the caller. See the function docstring for the
four rules and the bug they prevent.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

import httpx
from sqlmodel import Session, select

from packtrack.models import InventoryAdjustment, Item, User, ZohoSyncStatus
from packtrack.services.zoho_adjustment_client import (
    OutcomeKind,
    SyncOutcome,
    is_configured,
    push_adjustment_to_zoho,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# v2.16.3 — retry-safety gate
# ---------------------------------------------------------------------------


class RetryBlockReason(StrEnum):
    """Why a retry was refused. Strings double as machine-readable codes
    (route response, telemetry, tests). Human copy lives in
    ``_RETRY_BLOCK_MESSAGES`` below."""

    ALREADY_SYNCED = "already_synced"
    VOIDED = "voided_locally"
    REVERSED_LOCALLY = "reversed_locally"
    REVERSAL_OF_UNSYNCED = "reversal_of_unsynced"


# Operator-facing copy for each block reason. Short — these render
# inline in the history table next to the row.
_RETRY_BLOCK_MESSAGES: dict[RetryBlockReason, str] = {
    RetryBlockReason.ALREADY_SYNCED:
        "Already synced — nothing to retry.",
    RetryBlockReason.VOIDED:
        "Voided locally — do not retry.",
    RetryBlockReason.REVERSED_LOCALLY:
        "Reversed locally — do not retry.",
    RetryBlockReason.REVERSAL_OF_UNSYNCED:
        "Original adjustment is not synced; pushing the reversal alone "
        "would drift Zoho. Retry blocked.",
}


@dataclass(frozen=True)
class RetryEligibility:
    """Outcome of the retry-safety check.

    ``allowed`` is the single boolean the route + UI act on. ``reason``
    and ``detail`` are populated only when blocked, and identify which
    rule fired (for tests + telemetry) and the human-readable text.
    """

    allowed: bool
    reason: RetryBlockReason | None = None
    detail: str = ""

    @classmethod
    def ok(cls) -> RetryEligibility:
        return cls(allowed=True)

    @classmethod
    def block(cls, reason: RetryBlockReason, detail: str = "") -> RetryEligibility:
        return cls(
            allowed=False,
            reason=reason,
            detail=detail or _RETRY_BLOCK_MESSAGES[reason],
        )


def retry_eligibility(
    session: Session, adjustment: InventoryAdjustment,
) -> RetryEligibility:
    """Decide whether ``adjustment`` can safely be (re-)pushed to Zoho.

    READ-ONLY — never writes to the DB, never calls Zoho or the
    integration service, never touches ``Item.current_stock``. Runs at
    the start of the retry route AND in the history template so the
    answer is identical at both gates.

    Rules, in order — first match wins:

    1. **SYNCED** — already pushed. The orchestrator no-ops anyway, but
       blocking here keeps the UI honest ("Already synced", no button).

    2. **VOIDED** — row was administratively voided (``voided_at`` set).
       A void is a deliberate "this movement never should have happened
       upstream"; retrying would contradict the void.

    3. **REVERSED LOCALLY** — some other adjustment exists with
       ``reversal_of_adjustment_id == self.id``. PackTrack has already
       compensated this row's stock delta locally; pushing this row to
       Zoho alone would create PT↔Zoho drift (the spec's core concern).

    4. **REVERSAL OF UNSYNCED** — this row IS itself a reversal
       (``reversal_of_adjustment_id`` set) AND the original it cancels
       is not ``SYNCED``. Pushing only the reversal-half of a never-
       synced pair would drift Zoho in the opposite direction. The pair
       is incoherent until the original is reconciled.

    All other rows — FAILED/PENDING/NOT_CONFIGURED/SKIPPED with no
    void, no child reversal, and no unsynced-parent — remain retryable.
    """
    if adjustment.zoho_sync_status is ZohoSyncStatus.SYNCED:
        return RetryEligibility.block(RetryBlockReason.ALREADY_SYNCED)

    if adjustment.voided_at is not None:
        return RetryEligibility.block(RetryBlockReason.VOIDED)

    # Rule 3 — child rows that cancel this one. Match the FK and take
    # the first hit; we don't need to enumerate all reversers.
    child_reverser = session.exec(
        select(InventoryAdjustment.id)
        .where(InventoryAdjustment.reversal_of_adjustment_id == adjustment.id)
        .limit(1)
    ).first()
    if child_reverser is not None:
        return RetryEligibility.block(
            RetryBlockReason.REVERSED_LOCALLY,
            detail=(
                f"Reversed locally by adjustment #{child_reverser} — "
                "do not retry."
            ),
        )

    # Rule 4 — this row is a reversal pointing at an unsynced original.
    if adjustment.reversal_of_adjustment_id is not None:
        original = session.get(
            InventoryAdjustment, adjustment.reversal_of_adjustment_id,
        )
        if original is None or original.zoho_sync_status is not ZohoSyncStatus.SYNCED:
            original_label = (
                f"#{adjustment.reversal_of_adjustment_id}"
                if original is None
                else f"#{original.id} ({original.zoho_sync_status.value})"
            )
            return RetryEligibility.block(
                RetryBlockReason.REVERSAL_OF_UNSYNCED,
                detail=(
                    f"Original adjustment {original_label} is not synced; "
                    "pushing the reversal alone would drift Zoho. "
                    "Retry blocked."
                ),
            )

    return RetryEligibility.ok()


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
