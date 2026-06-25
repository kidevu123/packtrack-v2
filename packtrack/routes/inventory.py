import csv
from io import StringIO
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlmodel import Session, select

from packtrack.db import get_session
from packtrack.deps import require_user
from packtrack.models import Item, Role, User
from packtrack.services.inventory import (
    count_inventory_items,
    coverage_for_items,
    filter_inventory_items,
    suggested_reorder_qty,
)
from packtrack.services.scope import get_scope

router = APIRouter()

PAGE_SIZE = 50


@router.get("/inventory", response_class=HTMLResponse)
def inventory(
    request: Request,
    q: str | None = None,
    vendor: str | None = None,
    stock_status: str | None = None,
    missing_material_code: bool = False,
    page: int = 1,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    page = max(1, page)
    offset = (page - 1) * PAGE_SIZE
    filters = {
        "q": q, "vendor": vendor, "stock_status": stock_status,
        "missing_material_code": missing_material_code,
    }
    total = count_inventory_items(session, **filters)
    items = filter_inventory_items(session, **filters, limit=PAGE_SIZE, offset=offset)
    # Summary cards use full-dataset counts (not filtered page totals).
    count_total = count_inventory_items(session)
    count_missing_code = count_inventory_items(session, missing_material_code=True)
    count_critical = count_inventory_items(session, stock_status="critical")
    count_low_or_critical = count_inventory_items(session, stock_status="low")
    # Low-only excludes critical so the cards don't double-count.
    count_low_only = max(count_low_or_critical - count_critical, 0)
    coverage = coverage_for_items(session, items)
    suggested = {it.id: suggested_reorder_qty(it) for it in items}
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    # Pagination links need to carry the active filters through. Build it
    # here (not in Jinja) so it round-trips as a regular HTML-escaped string.
    pager_qs_parts: list[tuple[str, str]] = []
    if q:
        pager_qs_parts.append(("q", q))
    if vendor:
        pager_qs_parts.append(("vendor", vendor))
    if stock_status:
        pager_qs_parts.append(("stock_status", stock_status))
    if missing_material_code:
        pager_qs_parts.append(("missing_material_code", "true"))
    filter_qs = ("&" + urlencode(pager_qs_parts)) if pager_qs_parts else ""
    from packtrack.main import templates
    return templates.TemplateResponse(
        request, "inventory.html",
        {
            "user": user, "items": items, "q": q or "",
            "vendor": vendor or "",
            "stock_status": stock_status or "",
            "missing_material_code": missing_material_code,
            "coverage": coverage,
            "suggested": suggested,
            "scope": get_scope(session),
            "page": page,
            "page_size": PAGE_SIZE,
            "total": total,
            "total_pages": total_pages,
            "filter_qs": filter_qs,
            "count_total": count_total,
            "count_missing_code": count_missing_code,
            "count_critical": count_critical,
            "count_low_only": count_low_only,
        },
    )


@router.post("/inventory/{item_id}/edit")
def edit_item_thresholds(
    item_id: int,
    request: Request,
    reorder_point: float = Form(0),
    critical_point: float = Form(0),
    daily_usage_rate: float = Form(0),
    material_code: str = Form(""),
    vendor: str = Form(""),
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
    item.material_code = material_code.strip() or None
    item.vendor = vendor.strip() or None
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
