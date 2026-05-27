"""Phase D — /inventory/forecast page."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from packtrack.db import get_session
from packtrack.deps import require_user
from packtrack.models import User
from packtrack.services.forecast import compute_forecast
from sqlmodel import Session

router = APIRouter()


@router.get("/inventory/forecast", response_class=HTMLResponse)
def inventory_forecast(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    rows = compute_forecast(session)
    order_now = [r for r in rows if r.panel == "order_now"]
    watch = [r for r in rows if r.panel == "watch"]
    ok = [r for r in rows if r.panel == "ok"]
    no_velocity = [r for r in rows if r.panel == "no_velocity"]

    # Fire Telegram alerts for order_now items (deduplicated in notify_forecast_alert)
    from packtrack.notifications import notify_forecast_alert
    for r in order_now:
        try:
            notify_forecast_alert(session, r)
        except Exception:
            import logging
            logging.getLogger("packtrack.forecast").exception(
                "Failed to send forecast alert for %s", r.item.material_code
            )

    from packtrack.main import templates
    return templates.TemplateResponse(
        request,
        "forecast.html",
        {
            "user": user,
            "order_now": order_now,
            "watch": watch,
            "ok": ok,
            "no_velocity": no_velocity,
            "all_rows": rows,
        },
    )


@router.get("/inventory/forecast/detail/{item_id}", response_class=HTMLResponse)
def forecast_detail(
    item_id: int,
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    rows = compute_forecast(session)
    row = next((r for r in rows if r.item.id == item_id), None)
    if row is None:
        return HTMLResponse("<p class='text-sm text-stone-400 px-4 py-3'>Item not found in forecast.</p>")
    from packtrack.main import templates
    return templates.TemplateResponse(
        request, "_partials/forecast_row.html", {"row": row, "user": user}
    )
