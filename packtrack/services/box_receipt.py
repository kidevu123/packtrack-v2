"""Box-level receiving service.

Pure helpers (``compute_*``, ``find_box_collision``) plus the DB-aware
``create_box_receipt`` factory. The compute helpers exist as separate
functions so tests can verify the rules without standing up Postgres.

Hard rule: this module never calls Luma. P2 is structure-only.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlmodel import Session, select

from packtrack.models import (
    BoxReceipt,
    Confidence,
    Item,
    LumaPushStatus,
    PurchaseOrder,
    User,
)

# ---------------------------------------------------------------------------
# Pure rule helpers
# ---------------------------------------------------------------------------


def compute_accepted(declared: float, counted: float | None) -> float:
    """Counted-takes-precedence rule.

    Counted exists → that's what we accept.
    Counted is None → fall back to declared (supplier label).
    Counted explicitly 0 → still accept 0 (the box was empty); zero is a
    valid count, not a "missing".
    """
    if counted is not None:
        return float(counted)
    return float(declared)


def compute_confidence(counted: float | None) -> Confidence:
    """HIGH iff someone physically counted; MEDIUM otherwise."""
    return Confidence.HIGH if counted is not None else Confidence.MEDIUM


def compute_luma_readiness(material_code: str | None) -> LumaPushStatus:
    """Initial Luma push state for a brand-new BoxReceipt.

    NOT_READY when the snapshot material_code is empty — those rows are
    excluded from any future Luma payload by the P3 builder. PENDING
    otherwise; P5 picks them up.
    """
    code = (material_code or "").strip()
    return LumaPushStatus.PENDING if code else LumaPushStatus.NOT_READY


@dataclass(frozen=True)
class _BoxRow:
    """Minimal shape of a row for collision checks. Real ``BoxReceipt``
    instances satisfy this duck type; tests pass plain dataclasses."""
    box_number: str


def find_box_collision(
    existing: list[_BoxRow], box_number: str,
) -> _BoxRow | None:
    """Return the row that already uses ``box_number`` on this PO, if any.

    Whitespace-stripped comparison so ``"BOX-1"`` and ``"BOX-1 "`` are the
    same supplier carton. Empty / whitespace-only ``box_number`` never
    collides (caller validates emptiness separately).
    """
    norm = (box_number or "").strip()
    if not norm:
        return None
    for row in existing:
        if (row.box_number or "").strip() == norm:
            return row
    return None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BoxReceiptError(Exception):
    """Base for receiving errors that should be surfaced to the user."""


class DuplicateBoxNumber(BoxReceiptError):
    """A row with this ``(po, box_number)`` already exists."""


class InvalidQuantity(BoxReceiptError):
    """``declared_quantity`` <= 0, or counted_quantity is negative."""


class MissingMaterialName(BoxReceiptError):
    """The Item snapshot has no usable name — refuse to record the row."""


# ---------------------------------------------------------------------------
# DB-aware factory
# ---------------------------------------------------------------------------


def create_box_receipt(
    session: Session,
    *,
    po: PurchaseOrder,
    item: Item,
    user: User,
    box_number: str,
    declared_quantity: float,
    counted_quantity: float | None,
    supplier_lot_number: str | None,
    unit_of_measure: str | None,
    notes: str | None,
    shipment_id: int | None = None,
) -> BoxReceipt:
    """Build, validate, and persist a single ``BoxReceipt``.

    * Snapshots ``material_code``, ``material_name``, and ``supplier`` from
      the ``Item`` at receive time. Later edits to the Item never alter
      these — receiving history is the integration's source of truth.
    * Pre-checks the ``(po, box_number)`` uniqueness so we can return a
      clean error before the DB constraint fires; the constraint stays as
      defense in depth in case of a race or an out-of-band insert.
    * Sets ``luma_push_status`` to NOT_READY when material_code is empty.
    * Generates ``packtrack_receipt_id`` as a UUID4 — independent of the
      integer PK so it's stable across restores / re-keys.
    """
    box_number = (box_number or "").strip()
    if not box_number:
        raise BoxReceiptError("box_number is required.")

    if declared_quantity is None or declared_quantity <= 0:
        raise InvalidQuantity("declared_quantity must be > 0.")
    if counted_quantity is not None and counted_quantity < 0:
        raise InvalidQuantity("counted_quantity cannot be negative.")

    if not (item.name or "").strip():
        raise MissingMaterialName(
            f"Item id={item.id} has no name — cannot snapshot material_name."
        )

    # Collision check against this PO's existing rows.
    existing = list(session.exec(
        select(BoxReceipt).where(BoxReceipt.purchase_order_id == po.id)
    ).all())
    if find_box_collision(existing, box_number) is not None:
        raise DuplicateBoxNumber(
            f"Box {box_number!r} is already recorded on PO {po.po_number}."
        )

    accepted = compute_accepted(declared_quantity, counted_quantity)
    confidence = compute_confidence(counted_quantity)
    luma_status = compute_luma_readiness(item.material_code)

    now = datetime.utcnow()
    row = BoxReceipt(
        packtrack_receipt_id=str(uuid.uuid4()),
        purchase_order_id=po.id,
        shipment_id=shipment_id,
        item_id=item.id,
        material_code=(item.material_code or "").strip() or None,
        material_name=item.name[:240],
        supplier=(item.vendor or None),
        supplier_lot_number=(supplier_lot_number or "").strip() or None,
        box_number=box_number,
        declared_quantity=float(declared_quantity),
        counted_quantity=float(counted_quantity) if counted_quantity is not None else None,
        accepted_quantity=accepted,
        unit_of_measure=(unit_of_measure or "EACH")[:20],
        confidence=confidence,
        received_by_user_id=user.id,
        received_at=now,
        luma_push_status=luma_status,
        luma_pushed_at=None,
        luma_response=None,
        notes=(notes or None),
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row
