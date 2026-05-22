"""Receiving orchestration.

Three responsibilities:
  1. adopt_zoho_po   — ensure a PackTrack PurchaseOrder + POLines exist for a
                       Zoho-mirrored PO so BoxReceipts can be attached.
  2. create_zoho_receive — POST a purchase-receive to Zoho via the gateway.
  3. push_luma_receipt   — POST one BoxReceipt to Luma's webhook.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime

import httpx
from sqlmodel import Session, select

from packtrack.config import settings
from packtrack.models import (
    BoxReceipt,
    Item,
    LumaPushStatus,
    POEvent,
    POLine,
    POStatus,
    PurchaseOrder,
    User,
    ZohoMirror,
)

logger = logging.getLogger("packtrack.receiving")


# ---------------------------------------------------------------------------
# 0. Material-code resolution helper
# ---------------------------------------------------------------------------


def ensure_material_code(session: Session, item: Item) -> str | None:
    """Guarantee ``item.material_code`` is set — generating one if needed.

    Priority order:
    1. Already has ``material_code`` → return it, no DB write.
    2. Has ``sku_code`` not already claimed → promote ``sku_code``.
    3. Neither → generate deterministic ``PT-{id:05d}`` (stable per item).

    Writes back to the Item row and flushes so the caller's BoxReceipt
    snapshot picks up the new value.  Caller must commit.
    """
    if item.material_code:
        return item.material_code

    # Try sku_code first — preferred because it matches Zoho identity.
    if item.sku_code:
        collision = session.exec(
            select(Item)
            .where(Item.material_code == item.sku_code)
            .where(Item.id != item.id)
        ).first()
        if collision is None:
            item.material_code = item.sku_code
            session.add(item)
            session.flush()
            logger.info("material_code: item %s assigned from sku_code → %s", item.id, item.material_code)
            return item.material_code
        # sku_code already claimed by another item — fall through.

    # Auto-generate: PT-{id:05d} is deterministic and stable per item id.
    generated = f"PT-{item.id:05d}"
    collision = session.exec(
        select(Item)
        .where(Item.material_code == generated)
        .where(Item.id != item.id)
    ).first()
    item.material_code = generated if collision is None else f"PT-{uuid.uuid4().hex[:8].upper()}"
    session.add(item)
    session.flush()
    logger.info("material_code: item %s auto-generated → %s", item.id, item.material_code)
    return item.material_code


# ---------------------------------------------------------------------------
# 1. Adopt Zoho PO → PackTrack PurchaseOrder
# ---------------------------------------------------------------------------


def adopt_zoho_po(session: Session, mirror: ZohoMirror, created_by: User) -> PurchaseOrder:
    """Return the PackTrack PO linked to this Zoho mirror row, creating it if
    it does not exist yet.

    Lines are synced from ``mirror.line_items`` keyed on ``zoho_item_id``.
    Lines whose Zoho item_id cannot be found in the local items table are
    skipped (they are non-packaging items on a partially-packaging PO).
    """
    po = session.exec(
        select(PurchaseOrder).where(PurchaseOrder.zoho_po_id == mirror.zoho_purchaseorder_id)
    ).first()

    if po is None:
        po_number = (mirror.purchaseorder_number or mirror.zoho_purchaseorder_id)[:40]
        # Guard against duplicate po_number from a previous import attempt.
        existing = session.exec(
            select(PurchaseOrder).where(PurchaseOrder.po_number == po_number)
        ).first()
        if existing and existing.zoho_po_id is None:
            # Orphaned PackTrack PO with the same number — link it.
            existing.zoho_po_id = mirror.zoho_purchaseorder_id
            po = existing
        else:
            po = PurchaseOrder(
                po_number=po_number,
                status=POStatus.SHIPPED,
                zoho_po_id=mirror.zoho_purchaseorder_id,
                currency=mirror.currency_code or "USD",
                created_by_id=created_by.id,
            )
            session.add(po)
            session.flush()
            session.add(POEvent(
                po_id=po.id,
                kind="sync",
                message=f"Adopted from Zoho ({mirror.zoho_purchaseorder_id}) at receiving time.",
                actor_id=created_by.id,
            ))

    # Sync lines from ZohoMirror — adds missing, never removes existing.
    existing_item_ids = {line.item_id for line in po.lines}
    for li in (mirror.line_items or []):
        zoho_item_id = str(li.get("item_id") or "")
        if not zoho_item_id:
            continue
        item = session.exec(
            select(Item).where(Item.zoho_item_id == zoho_item_id)
        ).first()
        if item is None or item.id in existing_item_ids:
            continue
        session.add(POLine(
            po_id=po.id,
            item_id=item.id,
            quantity=float(li.get("quantity") or 0),
            unit_price=0.0,
        ))
        existing_item_ids.add(item.id)

    session.commit()
    session.refresh(po)
    return po


# ---------------------------------------------------------------------------
# 2. Push to Zoho via gateway
# ---------------------------------------------------------------------------


def create_zoho_receive(
    mirror: ZohoMirror,
    received_lines: list[dict],  # [{zoho_item_id, zoho_line_item_id, quantity, unit}]
    luma_operation_id: str,
    notes: str | None = None,
) -> tuple[bool, str | None]:
    """POST a purchase receive to Zoho via the gateway.

    Returns ``(ok, error_message)``. Never raises — failures are logged and
    surfaced to the caller to record in the POEvent stream.
    """
    if not settings.gateway_configured:
        return False, "Gateway not configured."
    if not received_lines:
        return False, "No line items to receive."

    line_items = [
        {
            "item_id": li["zoho_item_id"],
            "quantity": li["quantity"],
            **({"line_item_id": li["zoho_line_item_id"]} if li.get("zoho_line_item_id") else {}),
            **({"unit": li["unit"]} if li.get("unit") else {}),
        }
        for li in received_lines
        if li.get("quantity", 0) > 0
    ]
    if not line_items:
        return False, "All received quantities are zero."

    base = settings.ZOHO_GATEWAY_URL.rstrip("/")
    payload = {
        "dry_run": False,
        "luma_operation_id": luma_operation_id,
        "purchaseorder_id": mirror.zoho_purchaseorder_id,
        "date": date.today().isoformat(),
        "line_items": line_items,
        **({"notes": notes} if notes else {}),
    }
    try:
        with httpx.Client(timeout=30.0) as client:
            r = client.post(
                f"{base}/zoho/purchase_receives/create",
                headers={
                    "X-Brand": settings.ZOHO_GATEWAY_BRAND,
                    "X-Internal-Token": settings.ZOHO_GATEWAY_TOKEN,
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        if r.status_code >= 400:
            msg = f"HTTP {r.status_code}: {r.text[:300]}"
            logger.warning("Zoho receive failed for %s: %s", mirror.zoho_purchaseorder_id, msg)
            return False, msg
        return True, None
    except Exception as e:
        logger.exception("Zoho receive error for %s", mirror.zoho_purchaseorder_id)
        return False, str(e)[:500]


# ---------------------------------------------------------------------------
# 3. Push one BoxReceipt to Luma
# ---------------------------------------------------------------------------


def push_luma_receipt(
    box: BoxReceipt,
    po_number: str,
    photo_urls: list[str],
    *,
    received_by: str = "",
    dry_run: bool = False,
) -> tuple[bool, str | None, dict | None]:
    """POST one BoxReceipt to the Luma webhook.

    Returns ``(ok, error_message, response_body)``.
    Photos are passed in the free-form ``payload`` field so Luma can store
    them without a schema change on their side.
    """
    if not settings.LUMA_RECEIPT_WEBHOOK_URL:
        return False, "LUMA_RECEIPT_WEBHOOK_URL not configured.", None

    payload = {
        "source_system": "PACKTRACK",
        "packtrack_po_id": po_number,
        "packtrack_receipt_id": box.packtrack_receipt_id,
        "material_code": box.material_code or "",
        "material_name": box.material_name,
        "supplier": box.supplier,
        "supplier_lot_number": box.supplier_lot_number,
        "box_number": box.box_number,
        "declared_quantity": int(box.declared_quantity),
        "counted_quantity": int(box.counted_quantity) if box.counted_quantity is not None else None,
        "unit_of_measure": box.unit_of_measure,
        "received_at": box.received_at.isoformat() + "Z",
        "received_by": received_by or None,
        **({"payload": {"photo_urls": photo_urls}} if photo_urls else {}),
    }
    headers = {
        "Content-Type": "application/json",
        "x-packtrack-secret": settings.LUMA_PACKTRACK_SECRET,
    }
    if dry_run:
        headers["x-packtrack-dry-run"] = "true"

    try:
        with httpx.Client(timeout=20.0) as client:
            r = client.post(settings.LUMA_RECEIPT_WEBHOOK_URL, json=payload, headers=headers)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code >= 400:
            return False, f"HTTP {r.status_code}: {body.get('error', r.text[:200])}", body
        return True, None, body
    except Exception as e:
        logger.exception("Luma push failed for receipt %s", box.packtrack_receipt_id)
        return False, str(e)[:500], None


def build_photo_url(filename: str) -> str:
    """Public URL for a photo stored under uploads/receiving/."""
    base = (settings.APP_BASE_URL or "").rstrip("/")
    return f"{base}/uploads/receiving/{filename}"
