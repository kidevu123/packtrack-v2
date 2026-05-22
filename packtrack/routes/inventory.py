import csv
from io import StringIO

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlmodel import Session, or_, select

from packtrack.db import get_session
from packtrack.deps import require_user
from packtrack.models import Item, Role, User
from packtrack.services.scope import filter_items_query, get_scope

router = APIRouter()


@router.get("/inventory", response_class=HTMLResponse)
def inventory(
    request: Request,
    q: str | None = None,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    stmt = (
        select(Item)
        .where(
            or_(
                Item.material_code.is_not(None),
                Item.name.contains("[Packaging]"),
                Item.name.contains("[packaging]"),
            )
        )
        .order_by(Item.name)
    )
    stmt = filter_items_query(stmt, get_scope(session))
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            or_(
                Item.name.ilike(like),
                Item.sku_code.ilike(like),
                Item.vendor.ilike(like),
            )
        )
    items = session.exec(stmt).all()
    from packtrack.main import templates
    return templates.TemplateResponse(
        request, "inventory.html",
        {
            "user": user, "items": items, "q": q or "",
            "scope": get_scope(session),
        },
    )


@router.post("/inventory/{item_id}/edit")
def edit_item_thresholds(
    item_id: int,
    request: Request,
    reorder_point: float = Form(0),
    critical_point: float = Form(0),
    daily_usage_rate: float = Form(0),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    if user.role != Role.OWNER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    item = session.get(Item, item_id)
    if item is None:
        raise HTTPException(status_code=404)
    item.reorder_point = max(0.0, float(reorder_point or 0))
    item.critical_point = max(0.0, float(critical_point or 0))
    item.daily_usage_rate = max(0.0, float(daily_usage_rate or 0))
    item.reorder_point_locked = True  # owner overrode → lock from Zoho overwrite
    session.commit()
    # HTMX swap returns the updated row fragment
    if request.headers.get("hx-request") == "true":
        from packtrack.main import templates
        return templates.TemplateResponse(
            request, "_partials/inventory_row.html", {"it": item, "user": user}
        )
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/inventory", status_code=303)


@router.get("/inventory.csv")
def inventory_csv(
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    items = session.exec(select(Item).order_by(Item.name)).all()
    out = StringIO()
    w = csv.writer(out)
    w.writerow([
        "name", "sku_code", "vendor", "unit", "current_stock",
        "daily_usage_rate", "reorder_point", "critical_point",
        "sea_lead_days", "express_lead_days", "last_unit_cost",
        "zoho_item_id",
    ])
    for it in items:
        w.writerow([
            it.name, it.sku_code or "", it.vendor or "", it.unit,
            it.current_stock, it.daily_usage_rate,
            it.reorder_point, it.critical_point,
            it.sea_lead_days, it.express_lead_days,
            it.last_unit_cost or "",
            it.zoho_item_id or "",
        ])
    out.seek(0)
    return StreamingResponse(
        iter([out.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="inventory.csv"'},
    )
