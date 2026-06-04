"""Inbound webhooks from external systems.

Currently: Zoho sales order confirmation → SalesEvent log.
Auth: X-Zoho-Webhook-Secret header (ZOHO_WEBHOOK_SECRET env var).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

logger = logging.getLogger("packtrack.webhooks")
router = APIRouter()


def _check_webhook_secret(request: Request) -> bool:
    expected = os.environ.get("ZOHO_WEBHOOK_SECRET", "")
    return bool(expected) and request.headers.get("x-zoho-webhook-secret") == expected


@router.post("/api/webhooks/zoho-sales")
async def zoho_sales_webhook(request: Request) -> JSONResponse:
    """Receive Zoho sales order confirmation.

    Idempotent on zoho_order_id — safe to call multiple times.
    """
    if not _check_webhook_secret(request):
        return JSONResponse({"ok": False, "error": "Unauthorized."}, status_code=401)

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Body must be JSON."}, status_code=400)

    zoho_order_id = payload.get("zoho_order_id")
    product_sku = payload.get("product_sku")
    qty_sold = payload.get("qty_sold")
    sold_at_str = payload.get("sold_at")

    if not all([zoho_order_id, product_sku, qty_sold, sold_at_str]):
        return JSONResponse(
            {"ok": False, "error": "Missing: zoho_order_id, product_sku, qty_sold, sold_at"},
            status_code=400,
        )

    try:
        sold_at = datetime.fromisoformat(str(sold_at_str).rstrip("Z"))
    except ValueError:
        return JSONResponse({"ok": False, "error": f"Invalid sold_at: {sold_at_str}"}, status_code=400)

    from packtrack.db import engine
    from packtrack.models import SalesEvent

    with Session(engine) as session:
        try:
            event = SalesEvent(
                zoho_order_id=str(zoho_order_id),
                product_sku=str(product_sku),
                qty_sold=int(qty_sold),
                sold_at=sold_at,
            )
            session.add(event)
            session.commit()
            logger.info(
                "sales webhook: recorded %s qty=%s sku=%s",
                zoho_order_id, qty_sold, product_sku,
            )
            return JSONResponse({"ok": True, "created": True})
        except IntegrityError:
            session.rollback()
            logger.info(
                "sales webhook: duplicate zoho_order_id=%s — skipped", zoho_order_id
            )
            return JSONResponse({"ok": True, "created": False, "skipped": True})
