"""Zoho receive catch-up: detect receives already recorded in Zoho and
backfill PackTrack BoxReceipts + push to Luma.

Runs after every sync_open_pos.  Handles POs received directly in Zoho
(e.g. PO-00220) without going through PackTrack's receiving form.

Per line item:
  1. Skip if quantity_received == 0.
  2. Resolve zoho_item_id → Item row.  Skip if unknown (not yet synced).
  3. Adopt ZohoMirror → PackTrack PO (adopt_zoho_po).
  4. Sum existing BoxReceipts for (po, item) → already_covered.
  5. If delta = quantity_received - already_covered > 0, create a BoxReceipt.
  6. Push to Luma (best-effort; PENDING_MATERIAL_CODE items queued for retry).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlmodel import Session, col, select

from packtrack.config import settings
from packtrack.models import (
    BoxReceipt,
    Confidence,
    Item,
    LumaPushStatus,
    POEvent,
    POStatus,
    PurchaseOrder,
    Role,
    User,
    ZohoMirror,
)
from packtrack.services.box_receipt import compute_luma_readiness
from packtrack.services.receiving import adopt_zoho_po, ensure_material_code, push_luma_receipt

logger = logging.getLogger("packtrack.receive_catchup")


def _get_system_user(session: Session) -> User | None:
    """Return the first owner account to attribute catch-up receipts to."""
    return session.exec(
        select(User).where(col(User.role) == Role.OWNER)
    ).first()


def catchup_zoho_receives(session: Session) -> dict:
    """Scan all ZohoMirrors and backfill BoxReceipts for already-received lines.

    Returns a summary dict: mirrors_scanned, receipts_created, luma_pushed, errors.
    """
    mirrors = session.exec(select(ZohoMirror)).all()
    summary = {"mirrors_scanned": 0, "receipts_created": 0, "luma_pushed": 0, "errors": 0}

    system_user = _get_system_user(session)
    if system_user is None:
        logger.warning("catchup: no owner user found — aborting catch-up")
        return summary

    for mirror in mirrors:
        summary["mirrors_scanned"] += 1

        # Resolve the PackTrack PO once per mirror, not per line item.
        po_row = session.exec(
            select(PurchaseOrder).where(
                PurchaseOrder.zoho_po_id == mirror.zoho_purchaseorder_id
            )
        ).first()
        if po_row is None:
            po_row = adopt_zoho_po(session, mirror, system_user)

        for li in (mirror.line_items or []):
            zoho_item_id = str(li.get("item_id") or "")
            qty_received = float(li.get("quantity_received") or 0)

            if not zoho_item_id or qty_received <= 0:
                continue

            item = session.exec(
                select(Item).where(Item.zoho_item_id == zoho_item_id)
            ).first()
            if item is None:
                logger.debug(
                    "catchup: zoho_item_id=%s not in local DB (PO %s) — skip",
                    zoho_item_id, mirror.purchaseorder_number,
                )
                continue

            # How much do existing BoxReceipts already cover?
            existing = session.exec(
                select(BoxReceipt).where(
                    BoxReceipt.purchase_order_id == po_row.id,
                    BoxReceipt.item_id == item.id,
                )
            ).all()
            already_covered = sum(
                float(r.accepted_quantity or r.declared_quantity or 0)
                for r in existing
            )

            delta = qty_received - already_covered
            if delta <= 0:
                continue

            # Deterministic box number prevents duplicate rows on concurrent runs.
            box_number = f"CATCHUP-{mirror.zoho_purchaseorder_id}-{zoho_item_id}"
            if session.exec(
                select(BoxReceipt).where(BoxReceipt.box_number == box_number)
            ).first():
                continue
            # Ensure item has a material_code before snapshotting — auto-
            # generates PT-{id:05d} when neither material_code nor sku_code
            # is set so every catch-up receipt reaches Luma with a stable code.
            ensure_material_code(session, item)
            luma_status = compute_luma_readiness(item.material_code)
            now = datetime.utcnow()

            box = BoxReceipt(
                packtrack_receipt_id=str(uuid.uuid4()),
                purchase_order_id=po_row.id,
                item_id=item.id,
                material_code=(item.material_code or "").strip() or None,
                material_name=item.name[:240],
                supplier=item.vendor,
                box_number=box_number,
                declared_quantity=delta,
                counted_quantity=None,
                accepted_quantity=delta,
                unit_of_measure=item.unit or "EACH",
                confidence=Confidence.MEDIUM,
                received_by_user_id=system_user.id,
                luma_push_status=luma_status,
                notes=f"Auto-created from Zoho receive sync (qty_received={qty_received:g})",
                received_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(box)
            session.flush()

            session.add(POEvent(
                po_id=po_row.id,
                kind="system",
                message=(
                    f"Catch-up: {item.name} — {delta:g} units from Zoho receive "
                    f"(total received in Zoho: {qty_received:g})."
                ),
            ))

            summary["receipts_created"] += 1
            logger.info(
                "catchup: BoxReceipt %s — item=%s po=%s delta=%g (Luma push deferred to manual per-PO sync)",
                box.packtrack_receipt_id, item.name, mirror.purchaseorder_number, delta,
            )
            # NOTE: Luma push is intentionally NOT done here.
            # The background sync records receipts in PackTrack so the data is
            # captured, but pushing to Luma is a manual, per-PO action triggered
            # by the operator from the receiving form.  This prevents the sync
            # job from flooding Luma with every historical PO at once.

        # Flip SHIPPED → RECEIVED if every line item with qty_received > 0
        # is now fully covered by BoxReceipts.
        if po_row.status == POStatus.SHIPPED:
            fully_covered = True
            for li in (mirror.line_items or []):
                zoho_item_id = str(li.get("item_id") or "")
                qty_received = float(li.get("quantity_received") or 0)
                if not zoho_item_id or qty_received <= 0:
                    continue
                item = session.exec(
                    select(Item).where(Item.zoho_item_id == zoho_item_id)
                ).first()
                if item is None:
                    continue
                covered = sum(
                    float(r.accepted_quantity or r.declared_quantity or 0)
                    for r in session.exec(
                        select(BoxReceipt).where(
                            BoxReceipt.purchase_order_id == po_row.id,
                            BoxReceipt.item_id == item.id,
                        )
                    ).all()
                )
                if covered < qty_received:
                    fully_covered = False
                    break
            if fully_covered:
                po_row.status = POStatus.RECEIVED
                session.add(POEvent(
                    po_id=po_row.id,
                    kind="system",
                    message="All Zoho-received items now covered — PO marked received.",
                ))
                logger.info("catchup: PO %s → RECEIVED", mirror.purchaseorder_number)

        session.commit()

    return summary
