"""Inventory adjustments (v2.9.0) — PackTrack-local source of truth.

PackTrack v2.9.0 makes the local ``Item.current_stock`` value
authoritative for packaging quantity. Every change goes through this
service:

  1. Lock the item row with ``SELECT ... FOR UPDATE`` (Postgres) so two
     concurrent operators can't see the same ``quantity_before``.
  2. Read the current stock, convert to ``Decimal`` for the math.
  3. Validate the adjustment (mode, direction, qty, reason, notes,
     non-negative result, non-zero delta unless cycle-count "no
     variance" — v1 just rejects zero delta entirely).
  4. Insert an immutable ``InventoryAdjustment`` row.
  5. Write the new ``current_stock`` back as ``float`` (the column is
     still float on the items table; see model docstring for why).
  6. All in one DB transaction.

Zoho is NEVER contacted from this module. ``enqueue_or_mark_adjustment_sync``
sets ``zoho_sync_status`` to either ``NOT_CONFIGURED`` or ``PENDING``
based on a single config flag. A future worker can pick up PENDING rows
and push them through zoho-integration-service; that worker is not part
of v2.9.0.
"""
from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import func
from sqlmodel import Session, select

from packtrack.config import settings
from packtrack.models import (
    AdjustmentDirection,
    AdjustmentMode,
    AdjustmentReason,
    AdjustmentSource,
    InventoryAdjustment,
    Item,
    User,
    ZohoSyncStatus,
)

# ---------------------------------------------------------------------------
# Reasons + UI labels
# ---------------------------------------------------------------------------


REASON_LABELS: dict[AdjustmentReason, str] = {
    AdjustmentReason.CYCLE_COUNT_CORRECTION: "Cycle-count correction",
    AdjustmentReason.DAMAGED: "Damaged",
    AdjustmentReason.LOST_MISSING: "Lost / missing",
    AdjustmentReason.SAMPLE_OR_RD_USE: "Sample / R&D use",
    AdjustmentReason.PRODUCTION_CONSUMPTION_CORRECTION: "Production-consumption correction",
    AdjustmentReason.FOUND_EXTRA: "Found extra",
    AdjustmentReason.MANUAL_CORRECTION: "Manual correction",
    AdjustmentReason.OTHER: "Other (notes required)",
}


def reason_choices() -> list[tuple[str, str]]:
    """``[(value, human_label), …]`` for the form dropdown."""
    return [(r.value, REASON_LABELS[r]) for r in AdjustmentReason]


# ---------------------------------------------------------------------------
# Adjustment-number generator (ADJ-YYYY-NNNN per year)
# ---------------------------------------------------------------------------


_ADJ_RETRY_LIMIT = 8


def _year_prefix(now: datetime | None = None) -> str:
    n = now or datetime.utcnow()
    return f"ADJ-{n.year:04d}-"


def generate_adjustment_number(
    session: Session, *, now: datetime | None = None,
) -> str:
    """``ADJ-YYYY-NNNN`` — yearly sequence.

    Counts existing adjustments whose number starts with the year prefix
    and picks the next integer. A few retries protect against a
    concurrent race; the DB UNIQUE constraint is the final fence."""
    prefix = _year_prefix(now)
    last_attempt = ""
    for _ in range(_ADJ_RETRY_LIMIT):
        existing = session.scalar(
            select(func.count())
            .select_from(InventoryAdjustment)
            .where(InventoryAdjustment.adjustment_number.like(f"{prefix}%"))
        ) or 0
        candidate = f"{prefix}{existing + 1:04d}"
        collision = session.exec(
            select(InventoryAdjustment.id)
            .where(InventoryAdjustment.adjustment_number == candidate)
        ).first()
        if collision is None:
            return candidate
        last_attempt = candidate
    return f"{last_attempt}-{secrets.token_hex(2)}"


# ---------------------------------------------------------------------------
# Sync seam (NEVER calls Zoho directly)
# ---------------------------------------------------------------------------


def _adjustment_sync_configured() -> bool:
    """True when a future zoho-integration-service adjustment endpoint
    has been configured. Stays False until the service surface exists.

    Reads from a settings flag so prod operators can flip it on without
    a code change once the endpoint ships. PackTrack itself never makes
    the HTTP call — a separate worker picks up PENDING rows."""
    return bool(
        getattr(settings, "ZOHO_INTEGRATION_ADJUST_ENABLED", False)
        and getattr(settings, "ZOHO_INTEGRATION_BASE_URL", "")
        and getattr(settings, "ZOHO_INTEGRATION_APP_TOKEN", "")
    )


def enqueue_or_mark_adjustment_sync(
    adjustment: InventoryAdjustment,
) -> ZohoSyncStatus:
    """Set ``zoho_sync_status`` on a fresh adjustment WITHOUT making any
    outbound call.

    * If the future adjustment endpoint is configured → ``PENDING``
      (a worker will push it later via zoho-integration-service).
    * Otherwise → ``NOT_CONFIGURED`` (PackTrack remains the local source
      of truth; the operator sees the status in the UI but nothing
      attempts to sync).

    Returns the chosen status so the route layer can surface it in the
    success message without re-reading the row.
    """
    new_status = (
        ZohoSyncStatus.PENDING if _adjustment_sync_configured()
        else ZohoSyncStatus.NOT_CONFIGURED
    )
    adjustment.zoho_sync_status = new_status
    return new_status


# ---------------------------------------------------------------------------
# Validation + creation
# ---------------------------------------------------------------------------


class AdjustmentError(ValueError):
    """Operator-facing validation failure. The route layer maps this to
    a 400/422 — the message is safe to show in the form."""


@dataclass
class AdjustmentPreview:
    """The math result without persisting anything. Useful for tests +
    a future "live preview" HTMX endpoint."""

    mode: AdjustmentMode
    direction: AdjustmentDirection
    quantity_before: Decimal
    quantity_delta: Decimal  # signed
    quantity_after: Decimal


def _parse_decimal(value: str | float | int | Decimal, *, field: str) -> Decimal:
    if value is None or value == "":
        raise AdjustmentError(f"{field} is required.")
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise AdjustmentError(
            f"{field} must be a number (got {value!r})."
        ) from None
    return d


def compute_preview(
    *,
    current_stock: Decimal,
    mode: AdjustmentMode,
    direction: AdjustmentDirection | None,
    raw_quantity: str | float | int | Decimal,
    allow_negative_stock: bool = False,
) -> AdjustmentPreview:
    """Pure-function math: no DB, no Item.

    The form sends a positive ``raw_quantity``; ``direction`` determines
    the sign for ``DELTA`` mode. For ``SET_QUANTITY`` mode the operator
    enters the new counted total and the service computes ``delta``;
    direction is derived from the sign of (new - before).
    """
    qty = _parse_decimal(raw_quantity, field="Quantity")
    if qty < 0:
        raise AdjustmentError("Quantity must be a non-negative number.")

    if mode is AdjustmentMode.SET_QUANTITY:
        new_total = qty
        delta = new_total - current_stock
        if delta == 0:
            raise AdjustmentError(
                "Counted quantity is identical to current stock — "
                "no adjustment to record."
            )
        derived_direction = (
            AdjustmentDirection.INCREASE if delta > 0
            else AdjustmentDirection.DECREASE
        )
        quantity_after = new_total
    else:  # DELTA
        if direction is None:
            raise AdjustmentError(
                "Pick Increase or Decrease for a delta adjustment."
            )
        if qty == 0:
            raise AdjustmentError(
                "Quantity must be greater than zero for a delta adjustment."
            )
        signed = qty if direction is AdjustmentDirection.INCREASE else -qty
        delta = signed
        derived_direction = direction
        quantity_after = current_stock + delta

    if quantity_after < 0 and not allow_negative_stock:
        raise AdjustmentError(
            f"This adjustment would take stock to {quantity_after:f}; "
            "negative stock is not permitted."
        )

    return AdjustmentPreview(
        mode=mode,
        direction=derived_direction,
        quantity_before=current_stock,
        quantity_delta=delta,
        quantity_after=quantity_after,
    )


def _lock_item(session: Session, item_id: int) -> Item | None:
    """``SELECT … FOR UPDATE`` on the item row. SQLite ignores the
    ``with_for_update()`` hint silently, which is fine for tests; real
    locking applies on Postgres."""
    return session.exec(
        select(Item).where(Item.id == item_id).with_for_update()
    ).first()


@dataclass
class AdjustmentResult:
    adjustment: InventoryAdjustment
    sync_status: ZohoSyncStatus


def create_adjustment(
    session: Session,
    *,
    item_id: int,
    actor: User,
    mode: AdjustmentMode,
    direction: AdjustmentDirection | None,
    raw_quantity: str | float | int | Decimal,
    reason_code: AdjustmentReason,
    notes: str | None,
    source: AdjustmentSource = AdjustmentSource.MANUAL_ADJUSTMENT,
    reversal_of_adjustment_id: int | None = None,
    allow_negative_stock: bool = False,
    idempotency_key: str | None = None,
    now: datetime | None = None,
) -> AdjustmentResult:
    """Transactional ledger insert + ``Item.current_stock`` update.

    Caller is responsible for the authorization check — this service
    trusts ``actor`` to already be an owner. The route layer enforces
    that with the existing ``user.role != Role.OWNER → 403`` pattern.
    """
    if reason_code is AdjustmentReason.OTHER and not (notes or "").strip():
        raise AdjustmentError("Reason 'Other' requires a note explaining the change.")

    item = _lock_item(session, item_id)
    if item is None:
        raise AdjustmentError(f"Item {item_id} not found.")

    current_stock = Decimal(str(item.current_stock or 0))
    preview = compute_preview(
        current_stock=current_stock,
        mode=mode,
        direction=direction,
        raw_quantity=raw_quantity,
        allow_negative_stock=allow_negative_stock,
    )

    now = now or datetime.utcnow()
    adjustment = InventoryAdjustment(
        item_id=item.id,
        adjustment_number=generate_adjustment_number(session, now=now),
        mode=preview.mode,
        direction=preview.direction,
        quantity_before=preview.quantity_before,
        quantity_delta=preview.quantity_delta,
        quantity_after=preview.quantity_after,
        reason_code=reason_code,
        notes=(notes or "").strip() or None,
        created_by_user_id=actor.id,
        created_at=now,
        source=source,
        reversal_of_adjustment_id=reversal_of_adjustment_id,
        idempotency_key=idempotency_key or uuid.uuid4().hex,
    )
    sync_status = enqueue_or_mark_adjustment_sync(adjustment)
    session.add(adjustment)

    # Write the new stock back. Stored as float on the Item table for
    # now (see model docstring) — convert the Decimal once, at the
    # single mutation point.
    item.current_stock = float(preview.quantity_after)
    session.add(item)

    session.commit()
    session.refresh(adjustment)
    session.refresh(item)
    return AdjustmentResult(adjustment=adjustment, sync_status=sync_status)


# ---------------------------------------------------------------------------
# History helpers
# ---------------------------------------------------------------------------


def history_for_item(
    session: Session, item_id: int, *, limit: int = 50,
) -> list[InventoryAdjustment]:
    return session.exec(
        select(InventoryAdjustment)
        .where(InventoryAdjustment.item_id == item_id)
        .order_by(InventoryAdjustment.created_at.desc(), InventoryAdjustment.id.desc())
        .limit(limit)
    ).all()


def global_history(
    session: Session,
    *,
    item_id: int | None = None,
    reason_code: AdjustmentReason | None = None,
    sync_status: ZohoSyncStatus | None = None,
    limit: int = 200,
) -> list[InventoryAdjustment]:
    stmt = select(InventoryAdjustment)
    if item_id is not None:
        stmt = stmt.where(InventoryAdjustment.item_id == item_id)
    if reason_code is not None:
        stmt = stmt.where(InventoryAdjustment.reason_code == reason_code)
    if sync_status is not None:
        stmt = stmt.where(InventoryAdjustment.zoho_sync_status == sync_status)
    stmt = stmt.order_by(
        InventoryAdjustment.created_at.desc(),
        InventoryAdjustment.id.desc(),
    ).limit(limit)
    return session.exec(stmt).all()
