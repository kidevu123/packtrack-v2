"""Cycle-count batch adjustment workflow (v2.14.0).

A thin orchestrator around the existing v2.9.0 immutable adjustment
ledger. The owner enters counted quantities for a list of items, the
service:

  1. Validates **every** row up front (all-or-nothing). If any row is
     bad, NO local stock and NO ledger row is written.
  2. For each row with a non-zero variance (default), creates an
     ``InventoryAdjustment`` via ``services.inventory_adjustments
     .create_adjustment`` — same transactional + locking path the
     single-item adjuster uses.
  3. Each adjustment is stamped:
        * mode        = SET_QUANTITY
        * source      = CYCLE_COUNT
        * reason_code = CYCLE_COUNT_CORRECTION
     (operator-supplied per-row notes are preserved verbatim; if
     blank, defaults to "Cycle count adjustment".)
  4. After local persistence, ``try_sync_adjustment`` is invoked per
     row through the existing v2.10.0 path. A failed Zoho sync does
     NOT roll back local stock (per v2.11.0 ownership policy).

The service NEVER edits ``Item.current_stock`` directly, NEVER inserts
into ``inventory_adjustments`` directly, and NEVER imports a Zoho
client. All three paths go through the existing service modules.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

import httpx
from sqlmodel import Session

from packtrack.models import (
    AdjustmentMode,
    AdjustmentReason,
    AdjustmentSource,
    InventoryAdjustment,
    Item,
    User,
    ZohoSyncStatus,
)
from packtrack.services.inventory_adjustment_sync import try_sync_adjustment
from packtrack.services.inventory_adjustments import (
    AdjustmentError,
    create_adjustment,
)

DEFAULT_NOTE = "Cycle count adjustment"


# ---------------------------------------------------------------------------
# Input / output shapes
# ---------------------------------------------------------------------------


@dataclass
class CycleCountInputRow:
    """One row submitted by the operator.

    ``raw_counted`` is the literal string from the form; the service
    parses it as Decimal. ``note`` overrides ``shared_note`` per-row;
    blank → DEFAULT_NOTE.
    """

    item_id: int
    raw_counted: str
    note: str | None = None


@dataclass(frozen=True)
class RowOutcome:
    """One row's result after the batch ran.

    Three terminal kinds:
      * ``skipped_zero_variance`` — counted == current_stock, no
        adjustment created
      * ``created`` — adjustment created locally; ``adjustment_id``
        + ``sync_status`` populated
    Validation failures don't surface as RowOutcome — the whole batch
    aborts before any adjustment is created and the route re-renders
    the form with ``errors``.
    """

    item_id: int
    item_name: str
    quantity_before: Decimal
    quantity_after: Decimal
    quantity_delta: Decimal
    kind: str  # "created" | "skipped_zero_variance"
    adjustment_id: int | None = None
    adjustment_number: str | None = None
    sync_status: ZohoSyncStatus | None = None
    sync_error: str | None = None
    note_used: str = ""


@dataclass
class CycleCountValidationError:
    """One row's failed validation. Surfaced to the form so the
    operator can fix the offending input. Never partially applies."""

    item_id: int
    item_name: str
    message: str


@dataclass
class BatchOutcome:
    """The post-submit summary the route hands to the result template.

    Always populated on success (errors empty). On validation failure,
    ``errors`` is non-empty and ``rows`` is empty (nothing was applied).
    """

    rows: list[RowOutcome] = field(default_factory=list)
    errors: list[CycleCountValidationError] = field(default_factory=list)

    @property
    def created_count(self) -> int:
        return sum(1 for r in self.rows if r.kind == "created")

    @property
    def skipped_count(self) -> int:
        return sum(1 for r in self.rows if r.kind == "skipped_zero_variance")

    @property
    def sync_counts(self) -> dict[str, int]:
        out = {s.value: 0 for s in ZohoSyncStatus}
        for r in self.rows:
            if r.sync_status is not None:
                out[r.sync_status.value] += 1
        return out


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _parse_counted(raw: str, *, field_name: str) -> Decimal:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise AdjustmentError(f"{field_name} is required.")
    try:
        d = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        raise AdjustmentError(
            f"{field_name} must be a number (got {raw!r})."
        ) from None
    if d < 0:
        raise AdjustmentError(f"{field_name} cannot be negative.")
    return d


def _validate_all(
    session: Session, inputs: list[CycleCountInputRow],
) -> tuple[
    list[tuple[CycleCountInputRow, Item, Decimal, Decimal]],
    list[CycleCountValidationError],
]:
    """All-or-nothing validation pass.

    Returns ``(prepared, errors)``. ``prepared`` carries
    ``(input_row, item, current_stock_decimal, counted_decimal)`` for
    every input that survived validation. When ``errors`` is non-empty
    the caller MUST NOT proceed with any persistence.
    """
    prepared: list[tuple[CycleCountInputRow, Item, Decimal, Decimal]] = []
    errors: list[CycleCountValidationError] = []
    seen_ids: set[int] = set()

    for row in inputs:
        item = session.get(Item, row.item_id)
        if item is None:
            errors.append(CycleCountValidationError(
                item_id=row.item_id,
                item_name=f"item {row.item_id}",
                message="Item not found.",
            ))
            continue
        if item.id in seen_ids:
            errors.append(CycleCountValidationError(
                item_id=row.item_id, item_name=item.name or "",
                message="Duplicate row for this item in the batch.",
            ))
            continue
        seen_ids.add(item.id)

        try:
            counted = _parse_counted(row.raw_counted, field_name="Counted quantity")
        except AdjustmentError as exc:
            errors.append(CycleCountValidationError(
                item_id=row.item_id, item_name=item.name or "",
                message=str(exc),
            ))
            continue

        current = Decimal(str(item.current_stock or 0))
        # quantity_after = counted (set_quantity semantics). The same
        # negative-stock guard ``create_adjustment`` enforces. We
        # check it here too so the error surfaces inline on the form
        # rather than as a generic AdjustmentError from the inner call.
        if counted < 0:
            errors.append(CycleCountValidationError(
                item_id=row.item_id, item_name=item.name or "",
                message="Counted quantity cannot be negative.",
            ))
            continue

        prepared.append((row, item, current, counted))

    return prepared, errors


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def submit_cycle_count(
    session: Session,
    *,
    actor: User,
    inputs: list[CycleCountInputRow],
    shared_note: str | None = None,
    http_client: httpx.Client | None = None,
) -> BatchOutcome:
    """Run the full batch.

    Caller is responsible for the OWNER-only authorization check; this
    service trusts ``actor``. The route layer enforces it with the
    standard ``user.role != Role.OWNER → 403`` pattern.

    Returns a ``BatchOutcome``. On validation failure (any row), the
    outcome has ``errors`` set and zero ``rows`` (nothing applied).
    """
    if not inputs:
        return BatchOutcome()

    prepared, errors = _validate_all(session, inputs)
    if errors:
        # All-or-nothing: don't persist anything if any row is bad.
        return BatchOutcome(rows=[], errors=errors)

    shared = (shared_note or "").strip()
    rows: list[RowOutcome] = []
    for row, item, current, counted in prepared:
        delta = counted - current
        per_row_note = (row.note or "").strip() or shared or DEFAULT_NOTE

        if delta == 0:
            rows.append(RowOutcome(
                item_id=item.id, item_name=item.name or "",
                quantity_before=current,
                quantity_after=counted, quantity_delta=Decimal("0"),
                kind="skipped_zero_variance",
                note_used=per_row_note,
            ))
            continue

        # Delegate to the standard adjustment service — same row-lock,
        # transactional update, immutable insert.
        result = create_adjustment(
            session,
            item_id=item.id,
            actor=actor,
            mode=AdjustmentMode.SET_QUANTITY,
            direction=None,  # derived from sign
            raw_quantity=str(counted),
            reason_code=AdjustmentReason.CYCLE_COUNT_CORRECTION,
            notes=per_row_note,
            source=AdjustmentSource.CYCLE_COUNT,
        )
        adj: InventoryAdjustment = result.adjustment

        # v2.10.0 sync path — same orchestrator the single-item flow
        # uses. Failures are recorded on the row but never roll back
        # local stock (v2.11.0 ownership policy).
        sync_outcome = try_sync_adjustment(
            session, adj, item, actor=actor, http_client=http_client,
        )

        rows.append(RowOutcome(
            item_id=item.id, item_name=item.name or "",
            quantity_before=adj.quantity_before,
            quantity_after=adj.quantity_after,
            quantity_delta=adj.quantity_delta,
            kind="created",
            adjustment_id=adj.id,
            adjustment_number=adj.adjustment_number,
            sync_status=sync_outcome.to_status(),
            sync_error=sync_outcome.error_message,
            note_used=per_row_note,
        ))

    return BatchOutcome(rows=rows, errors=[])
