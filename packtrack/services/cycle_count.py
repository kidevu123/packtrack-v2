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

import csv
import io
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

import httpx
from sqlmodel import Session, select

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


# ---------------------------------------------------------------------------
# v2.18.0 — Count-sheet export (READ-ONLY)
# ---------------------------------------------------------------------------
#
# Helpers that produce a printable/CSV count sheet for operators doing a
# physical count. **STRICTLY READ-ONLY**: no DB writes, no Zoho calls,
# no mutation of Item.current_stock or any adjustment row.
#
# Whitelist policy (security): only fields explicitly listed in
# COUNT_SHEET_COLUMNS reach the CSV. Pricing, accounts, vendor IDs,
# integration-service tokens, and Zoho sync error messages are
# deliberately excluded — see the test
# tests/test_v2_18_0_cycle_count_sheet::test_csv_excludes_sensitive_fields.

# CSV column order. The exact tuple the test asserts on. Adding a column
# requires an explicit security review (see the docstring above).
COUNT_SHEET_COLUMNS: tuple[str, ...] = (
    "item_id",
    "item_name",
    "material_code",
    "sku_code",
    "vendor",
    "product_line",
    "current_packtrack_qty",
    "zoho_snapshot_qty",
    "zoho_variance",
    "counted_qty",
    "notes",
)


@dataclass(frozen=True)
class CountSheetRow:
    """One row in the printable / CSV count sheet.

    ``counted_qty`` and ``notes`` are intentionally blank — they are the
    columns the operator fills in by hand or in a spreadsheet. The other
    columns are reference values pulled at export time."""

    item_id: int
    item_name: str
    material_code: str
    sku_code: str
    vendor: str
    product_line: str
    current_packtrack_qty: Decimal
    zoho_snapshot_qty: Decimal | None
    zoho_variance: Decimal | None
    counted_qty: str = ""
    notes: str = ""


def _decimal_or_none(value) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _to_decimal(value) -> Decimal:
    return _decimal_or_none(value) or Decimal("0")


def _matches(needle: str, *fields: str | None) -> bool:
    """Case-insensitive substring match across name/material_code/sku.
    Empty needle matches everything."""
    if not needle:
        return True
    n = needle.lower().strip()
    if not n:
        return True
    return any(f and n in f.lower() for f in fields)


def build_count_sheet_rows(
    session: Session, *,
    q: str = "",
    product_line: str = "",
) -> list[CountSheetRow]:
    """Build the count sheet for the operator-supplied filter set.

    Mirrors the cycle-count form's filter contract so the exported CSV
    matches what the operator sees on screen: a case-insensitive
    substring across name/material_code/sku for ``q``, and an exact
    match on ``product_line`` for the dropdown.

    Read-only — no DB writes, no Zoho calls, no integration-service
    calls. Returns rows sorted by item name (matching the form)."""
    items = session.exec(select(Item).order_by(Item.name)).all()
    rows: list[CountSheetRow] = []
    for it in items:
        if not _matches(q, it.name, it.material_code, it.sku_code):
            continue
        if product_line and (it.product_line or "") != product_line:
            continue
        pt = _to_decimal(it.current_stock)
        zoho = _decimal_or_none(it.last_zoho_stock_snapshot)
        variance = (pt - zoho) if zoho is not None else None
        rows.append(CountSheetRow(
            item_id=it.id,
            item_name=it.name or f"item #{it.id}",
            material_code=it.material_code or "",
            sku_code=it.sku_code or "",
            vendor=it.vendor or "",
            product_line=it.product_line or "",
            current_packtrack_qty=pt,
            zoho_snapshot_qty=zoho,
            zoho_variance=variance,
        ))
    return rows


def _format_decimal(value: Decimal | None) -> str:
    """Decimal-safe CSV cell formatter — strips trailing zeros while
    preserving precision for integer-shaped values. None → empty cell."""
    if value is None:
        return ""
    # Quantize down to drop spurious trailing zeros (Decimal('5.0000')
    # → '5'), but keep meaningful precision intact (Decimal('5.5000')
    # → '5.5'). Falls back to str() if normalize ever raises.
    try:
        normalized = value.normalize()
        # normalize() can produce scientific notation for tiny values;
        # fix up by formatting at the original scale.
        s = format(normalized, "f")
    except (InvalidOperation, ValueError):
        s = str(value)
    return s


def format_count_sheet_csv(rows: Iterable[CountSheetRow]) -> str:
    """Render rows to a CSV string using the locked COUNT_SHEET_COLUMNS.

    Format choices: csv.QUOTE_MINIMAL so notes with commas don't break;
    \\r\\n line terminator (RFC 4180); Decimal cells formatted via
    _format_decimal so the upstream value's precision is preserved.
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(COUNT_SHEET_COLUMNS)
    for r in rows:
        writer.writerow([
            r.item_id,
            r.item_name,
            r.material_code,
            r.sku_code,
            r.vendor,
            r.product_line,
            _format_decimal(r.current_packtrack_qty),
            _format_decimal(r.zoho_snapshot_qty),
            _format_decimal(r.zoho_variance),
            r.counted_qty,
            r.notes,
        ])
    return buf.getvalue()


def list_product_lines(session: Session) -> list[str]:
    """Distinct, non-null Item.product_line values — for the
    count-sheet form's product-line dropdown filter."""
    values = session.exec(
        select(Item.product_line).where(Item.product_line.is_not(None))
    ).all()
    return sorted({pl for pl in values if pl})
