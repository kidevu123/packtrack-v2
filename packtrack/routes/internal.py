"""Internal machine-to-machine API routes.

Header-secret auth only — not behind HTML session auth.
Designed for service-to-service calls (Luma → PackTrack).
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session

from packtrack.db import get_session
from packtrack.notifications import notify_stock_alert
from packtrack.services.consumption import process_luma_consumption

logger = logging.getLogger("packtrack.internal")
router = APIRouter()

_REQUIRED = {"finished_lot_id", "consumed_materials", "released_at"}


def _check_luma_secret(request: Request) -> bool:
    expected = os.environ.get("LUMA_PACKTRACK_SECRET", "")
    return bool(expected) and request.headers.get("x-luma-packtrack-secret") == expected


@router.post("/api/internal/luma-consumption")
async def luma_consumption(
    request: Request,
    session: Session = Depends(get_session),
) -> JSONResponse:
    """Receive packaging consumption from Luma on finishedLot RELEASED.

    Idempotent: re-sending the same finished_lot_id is a no-op.
    """
    if not _check_luma_secret(request):
        return JSONResponse({"ok": False, "error": "Unauthorized."}, status_code=401)

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Body must be JSON."}, status_code=400)

    missing = _REQUIRED - set(payload.keys())
    if missing:
        return JSONResponse(
            {"ok": False, "error": f"Missing: {sorted(missing)}"}, status_code=400
        )

    result = process_luma_consumption(session, payload)

    # Fire Telegram alerts for any threshold crossings
    for entry in result["processed"]:
        if entry.get("threshold_crossed") and entry.get("item_id"):
            from packtrack.models import Item
            item = session.get(Item, entry["item_id"])
            if item:
                notify_stock_alert(session, item, entry["threshold_crossed"])

    return JSONResponse({"ok": True, **result})
