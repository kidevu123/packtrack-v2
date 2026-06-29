"""Receiving vNext v2.7.6 — packing-list CSV/text import (no PDF/OCR).

Parses pasted text or an uploaded CSV into preview rows, each mapped to
a PO item via deterministic matching (no fuzzy/AI). The preview is pure
read — no DB writes. The route layer commits only ``READY`` rows into
``ReceivePackingListLine``.

Format (header-driven, flexible synonyms):

  Required: one of {item, item_name, name, sku, material_code} +
            one of {quantity, expected_quantity, qty}
  Optional: unit, vendor_case_number (or case / case_number), note

Matching rules, evaluated in order, scoped to the receive's PO lines:

  1. ``material_code`` exact (case-insensitive)
  2. ``sku`` exact (case-insensitive) — same column as ``sku_code`` on Item
  3. ``item_name`` exact (case-insensitive)
  4. ``item_name`` unambiguous substring containment (case-insensitive)

If multiple items match → AMBIGUOUS. No match → UNMATCHED. Invalid /
non-positive quantity → INVALID_QTY. Otherwise → READY.

XLSX is intentionally not supported in v2.7.6 because the project has
no XLSX dependency on the runtime; see
``docs/CURRENT_PHASE_STATUS.md`` for the rationale and follow-up.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from enum import StrEnum

from sqlmodel import Session, select

from packtrack.models import Item, POLine, Receive


class RowStatus(StrEnum):
    READY = "ready"
    UNMATCHED = "unmatched"
    AMBIGUOUS = "ambiguous"
    INVALID_QTY = "invalid_qty"
    INVALID = "invalid"  # missing required fields, etc.


# ---------------------------------------------------------------------------
# Column synonyms (lowercased)
# ---------------------------------------------------------------------------


_ITEM_NAME_KEYS = {"item", "item_name", "name", "product", "product_name"}
_SKU_KEYS = {"sku", "sku_code", "code"}
_MATERIAL_KEYS = {"material_code", "material", "mat_code"}
_QUANTITY_KEYS = {"quantity", "expected_quantity", "qty", "amount", "count"}
_UNIT_KEYS = {"unit", "uom", "units"}
_CASE_KEYS = {"vendor_case_number", "case", "case_number", "case_no", "carton"}
_NOTE_KEYS = {"note", "notes", "remark", "remarks", "comment"}


@dataclass
class PreviewRow:
    """One parsed CSV row + match result. The route exposes a list of
    these via the preview template; ``commit`` re-parses and only acts
    on rows with ``status == READY``."""

    line_no: int  # 1-based, header row counts as 0
    raw_item: str
    raw_quantity: str
    item_id: int | None
    item_name: str
    material_code: str
    expected_quantity: float
    unit: str
    vendor_case_number: str
    note: str
    status: RowStatus
    detail: str = ""  # human-readable status detail


@dataclass
class PreviewReport:
    rows: list[PreviewRow]
    error: str | None = None  # whole-payload error (e.g. no header)

    @property
    def ready_rows(self) -> list[PreviewRow]:
        return [r for r in self.rows if r.status is RowStatus.READY]

    @property
    def skipped_rows(self) -> list[PreviewRow]:
        return [r for r in self.rows if r.status is not RowStatus.READY]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


_DELIMITERS = [",", "\t", ";", "|"]


def _detect_dialect(sample: str) -> str:
    """Pick the delimiter with the highest header-row consistency.

    Tries comma → tab → semicolon → pipe. Fallback is comma. Uses the
    first non-empty line to count fields and avoid Sniffer's silent
    "looks like one cell" failure on tab-separated input."""
    head = next(
        (ln for ln in (sample or "").splitlines() if ln.strip()),
        "",
    )
    if not head:
        return ","
    best, best_n = ",", 1
    for d in _DELIMITERS:
        n = len(head.split(d))
        if n > best_n:
            best, best_n = d, n
    return best


def parse_csv_text(text: str) -> tuple[list[dict[str, str]], str | None]:
    """Return (rows-as-lowercased-dicts, error_or_None).

    Header row is required. Whitespace is stripped from header keys and
    cell values. Rows that are entirely empty are dropped. Returns an
    error string when the input has no header or no data rows.
    """
    if not (text or "").strip():
        return [], "Paste/upload was empty."
    delimiter = _detect_dialect(text)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = list(reader)
    if not rows:
        return [], "No rows found."
    header = [(c or "").strip().lower() for c in rows[0]]
    if not any(header):
        return [], "First row must be a header row."
    out: list[dict[str, str]] = []
    for raw in rows[1:]:
        cells = [(c or "").strip() for c in raw]
        if not any(cells):
            continue
        out.append({
            header[i]: (cells[i] if i < len(cells) else "")
            for i in range(len(header))
        })
    if not out:
        return [], "No data rows after the header."
    return out, None


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


@dataclass
class _MatchOutcome:
    item: Item | None
    status: RowStatus
    detail: str = ""


def _po_items(session: Session, po_id: int) -> list[Item]:
    rows = session.exec(
        select(Item).join(POLine, POLine.item_id == Item.id).where(POLine.po_id == po_id)
    ).all()
    seen: set[int] = set()
    out: list[Item] = []
    for it in rows:
        if it.id in seen:
            continue
        seen.add(it.id)
        out.append(it)
    return out


def match_row(
    *,
    items: list[Item],
    raw_material: str,
    raw_sku: str,
    raw_name: str,
) -> _MatchOutcome:
    """Deterministic match against the PO-scoped item list. Order:
    material_code exact → sku_code exact → item.name exact → unambiguous
    substring containment on name. Case-insensitive throughout."""
    mat = (raw_material or "").strip().lower()
    sku = (raw_sku or "").strip().lower()
    name = (raw_name or "").strip().lower()

    if not (mat or sku or name):
        return _MatchOutcome(
            None, RowStatus.UNMATCHED,
            "Row has no item / sku / material_code value.",
        )

    if mat:
        hits = [it for it in items if (it.material_code or "").lower() == mat]
        if len(hits) == 1:
            return _MatchOutcome(hits[0], RowStatus.READY)
        if len(hits) > 1:
            return _MatchOutcome(
                None, RowStatus.AMBIGUOUS,
                f"material_code {mat!r} matches {len(hits)} items.",
            )

    if sku:
        hits = [it for it in items if (it.sku_code or "").lower() == sku]
        if len(hits) == 1:
            return _MatchOutcome(hits[0], RowStatus.READY)
        if len(hits) > 1:
            return _MatchOutcome(
                None, RowStatus.AMBIGUOUS,
                f"sku {sku!r} matches {len(hits)} items.",
            )

    if name:
        exact = [it for it in items if (it.name or "").lower() == name]
        if len(exact) == 1:
            return _MatchOutcome(exact[0], RowStatus.READY)
        if len(exact) > 1:
            return _MatchOutcome(
                None, RowStatus.AMBIGUOUS,
                f"item name {name!r} matches {len(exact)} items exactly.",
            )
        contains = [it for it in items if name in (it.name or "").lower()]
        if len(contains) == 1:
            return _MatchOutcome(contains[0], RowStatus.READY)
        if len(contains) > 1:
            return _MatchOutcome(
                None, RowStatus.AMBIGUOUS,
                f"item name {name!r} matches {len(contains)} items by substring.",
            )

    return _MatchOutcome(
        None, RowStatus.UNMATCHED,
        "No PO item matches by material_code, sku, or name.",
    )


def _first_value(row: dict[str, str], keys: set[str]) -> str:
    for k in keys:
        v = row.get(k)
        if v:
            return v
    return ""


def build_preview(
    session: Session, receive: Receive, *, text: str,
) -> PreviewReport:
    """Pure read. Parses ``text`` and classifies each row."""
    rows, err = parse_csv_text(text)
    if err is not None:
        return PreviewReport(rows=[], error=err)

    items = (
        _po_items(session, receive.purchase_order_id)
        if receive.purchase_order_id is not None
        else []
    )

    out: list[PreviewRow] = []
    for i, row in enumerate(rows, start=1):
        raw_name = _first_value(row, _ITEM_NAME_KEYS)
        raw_sku = _first_value(row, _SKU_KEYS)
        raw_material = _first_value(row, _MATERIAL_KEYS)
        raw_quantity = _first_value(row, _QUANTITY_KEYS)
        unit = _first_value(row, _UNIT_KEYS)
        case_no = _first_value(row, _CASE_KEYS)
        note = _first_value(row, _NOTE_KEYS)

        outcome = match_row(
            items=items, raw_material=raw_material,
            raw_sku=raw_sku, raw_name=raw_name,
        )

        try:
            qty = float(raw_quantity)
        except (TypeError, ValueError):
            qty = 0.0
            qty_ok = False
        else:
            qty_ok = qty > 0

        # If qty is bad but match is otherwise fine, INVALID_QTY wins —
        # it's the actionable error.
        if outcome.status is RowStatus.READY and not qty_ok:
            status = RowStatus.INVALID_QTY
            detail = (
                f"Quantity {raw_quantity!r} is not a positive number."
                if raw_quantity else "Quantity is missing."
            )
        else:
            status = outcome.status
            detail = outcome.detail

        item = outcome.item
        out.append(PreviewRow(
            line_no=i,
            raw_item=raw_name or raw_sku or raw_material,
            raw_quantity=raw_quantity,
            item_id=item.id if item else None,
            item_name=(item.name if item else raw_name) or "",
            material_code=(item.material_code if item else raw_material) or "",
            expected_quantity=qty,
            unit=(unit or (item.unit if item else "") or "").strip()[:20],
            vendor_case_number=(case_no or "").strip()[:120],
            note=(note or "").strip()[:500],
            status=status,
            detail=detail,
        ))
    return PreviewReport(rows=out)
