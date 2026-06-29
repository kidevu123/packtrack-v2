"""Receiving vNext MVP (v2.7.5) — manual packing-list reconciliation.

Compares ``ReceivePackingListLine`` (operator-entered "what the vendor
said is in the shipment") against ``ReceiveCaseLine`` (operator-counted
"what we actually received") and produces a per-item reconciliation
report. Warnings only — finalize is never blocked by reconciliation,
and Zoho/Luma payloads still use actual counted lines.

No file parsing here. Expected lines are entered manually in v2.7.5;
CSV/PDF/OCR happens later, after real vendor packing-list samples land.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from sqlmodel import Session, select

from packtrack.models import (
    Item,
    Receive,
    ReceiveCase,
    ReceiveCaseLine,
    ReceivePackingListLine,
)


class ReconcileStatus(StrEnum):
    MATCH = "match"
    SHORT = "short"
    OVER = "over"
    UNEXPECTED = "unexpected"  # counted but not on expected list
    MISSING = "missing"  # expected but not counted


@dataclass(frozen=True)
class ReconcileRow:
    """One per item_id reconciliation."""

    item_id: int
    item_name: str
    unit: str
    expected_quantity: float
    counted_quantity: float
    difference: float  # counted - expected (negative = short)
    status: ReconcileStatus

    @property
    def message(self) -> str:
        """Operator-facing one-liner for warning lists."""
        u = self.unit or ""
        u_sp = f" {u}" if u else ""
        if self.status is ReconcileStatus.MATCH:
            return (
                f"{self.item_name}: packing list and count match at "
                f"{_fmt_qty(self.expected_quantity)}{u_sp}."
            )
        if self.status is ReconcileStatus.SHORT:
            return (
                f"{self.item_name}: packing list expected "
                f"{_fmt_qty(self.expected_quantity)}{u_sp}, "
                f"you counted {_fmt_qty(self.counted_quantity)}{u_sp} — "
                f"short {_fmt_qty(abs(self.difference))}{u_sp}."
            )
        if self.status is ReconcileStatus.OVER:
            return (
                f"{self.item_name}: packing list expected "
                f"{_fmt_qty(self.expected_quantity)}{u_sp}, "
                f"you counted {_fmt_qty(self.counted_quantity)}{u_sp} — "
                f"over by {_fmt_qty(self.difference)}{u_sp}."
            )
        if self.status is ReconcileStatus.UNEXPECTED:
            return (
                f"{self.item_name}: counted {_fmt_qty(self.counted_quantity)}{u_sp} "
                f"but not on packing list."
            )
        # MISSING
        return (
            f"{self.item_name}: packing list expected "
            f"{_fmt_qty(self.expected_quantity)}{u_sp} but nothing counted."
        )


@dataclass
class ReconciliationReport:
    """All-item rollup used by review + the per-case "expected" hint."""

    rows: list[ReconcileRow] = field(default_factory=list)
    has_expected_lines: bool = False

    @property
    def differences(self) -> list[ReconcileRow]:
        return [r for r in self.rows if r.status is not ReconcileStatus.MATCH]

    def expected_for_item(self, item_id: int) -> float | None:
        """Expected quantity for one item, or None if not on the packing
        list. Used by the per-case-line "Expected from packing list" hint
        next to case entry on the receive page."""
        for r in self.rows:
            if r.item_id == item_id and r.status is not ReconcileStatus.UNEXPECTED:
                return r.expected_quantity
        return None


def _fmt_qty(q: float) -> str:
    if q == int(q):
        return f"{int(q):,}"
    return f"{q:,g}"


def build_reconciliation_report(
    session: Session, receive: Receive,
) -> ReconciliationReport:
    """Group expected and counted by item_id and classify each.

    "Counted" is the per-item sum of ``ReceiveCaseLine.declared_quantity``
    (which is what the operator actually keyed in — the same field
    finalize uses via ``compute_accepted``).
    """
    expected_rows = session.exec(
        select(ReceivePackingListLine).where(
            ReceivePackingListLine.receive_id == receive.id
        )
    ).all()

    counted_lines = session.exec(
        select(ReceiveCaseLine)
        .join(ReceiveCase, ReceiveCase.id == ReceiveCaseLine.receive_case_id)
        .where(ReceiveCase.receive_id == receive.id)
    ).all()

    expected_by_item: dict[int, float] = {}
    expected_unit: dict[int, str] = {}
    for el in expected_rows:
        expected_by_item[el.item_id] = expected_by_item.get(el.item_id, 0.0) + float(
            el.expected_quantity or 0
        )
        if el.unit and el.item_id not in expected_unit:
            expected_unit[el.item_id] = el.unit

    counted_by_item: dict[int, float] = {}
    counted_unit: dict[int, str] = {}
    for cl in counted_lines:
        counted_by_item[cl.item_id] = counted_by_item.get(cl.item_id, 0.0) + float(
            cl.declared_quantity or 0
        )
        if cl.unit_of_measure and cl.item_id not in counted_unit:
            counted_unit[cl.item_id] = cl.unit_of_measure

    item_ids = sorted(set(expected_by_item) | set(counted_by_item))
    rows: list[ReconcileRow] = []
    for iid in item_ids:
        item = session.get(Item, iid)
        name = item.name if item else f"item {iid}"
        unit = expected_unit.get(iid) or counted_unit.get(iid) or (item.unit if item else "")
        exp = expected_by_item.get(iid, 0.0)
        cnt = counted_by_item.get(iid, 0.0)
        if iid not in expected_by_item:
            status = ReconcileStatus.UNEXPECTED
        elif iid not in counted_by_item or cnt == 0:
            status = ReconcileStatus.MISSING
        elif abs(cnt - exp) < 1e-9:
            status = ReconcileStatus.MATCH
        elif cnt < exp:
            status = ReconcileStatus.SHORT
        else:
            status = ReconcileStatus.OVER
        rows.append(
            ReconcileRow(
                item_id=iid,
                item_name=name,
                unit=unit or "",
                expected_quantity=exp,
                counted_quantity=cnt,
                difference=cnt - exp,
                status=status,
            )
        )
    rows.sort(key=lambda r: (r.status is ReconcileStatus.MATCH, r.item_name.lower()))
    return ReconciliationReport(
        rows=rows,
        has_expected_lines=bool(expected_rows),
    )
