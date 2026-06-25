import csv
from io import StringIO
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlmodel import Session, select

from packtrack.db import get_session
from packtrack.deps import require_user
from packtrack.models import Item, Role, User
from packtrack.services.inventory import (
    count_inventory_items,
    coverage_for_items,
    filter_inventory_items,
    group_counts,
    suggested_reorder_qty,
)
from packtrack.services.product_line import (
    GENERIC_GROUP,
    derive_product_line,
    group_sort_key,
)
from packtrack.services.scope import get_scope
from packtrack.services.zoho_item_sync import (
    ZOHO_OWNED_EDITABLE_FIELDS,
    push_item_update,
)

router = APIRouter()

PAGE_SIZE = 50

# Field length guards mirror the Item column definitions in models.py.
_MAX_NAME = 240
_MAX_VENDOR = 200
_MAX_MATERIAL_CODE = 120
_MAX_UNIT = 40


@router.get("/inventory", response_class=HTMLResponse)
def inventory(
    request: Request,
    q: str | None = None,
    vendor: str | None = None,
    stock_status: str | None = None,
    missing_material_code: bool = False,
    group: str | None = None,
    page: int = 1,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    page = max(1, page)
    offset = (page - 1) * PAGE_SIZE
    group = (group or "").strip() or None
    filters = {
        "q": q, "vendor": vendor, "stock_status": stock_status,
        "missing_material_code": missing_material_code,
    }
    total = count_inventory_items(session, **filters, group=group)
    items = filter_inventory_items(
        session, **filters, group=group, limit=PAGE_SIZE, offset=offset
    )
    # Group chips count the full (non-group) filtered set so users can always
    # jump between product lines regardless of the active group.
    groups = group_counts(session, **filters)
    # Summary cards use full-dataset counts (not filtered page totals).
    count_total = count_inventory_items(session)
    count_missing_code = count_inventory_items(session, missing_material_code=True)
    count_critical = count_inventory_items(session, stock_status="critical")
    count_low_or_critical = count_inventory_items(session, stock_status="low")
    # Low-only excludes critical so the cards don't double-count.
    count_low_only = max(count_low_or_critical - count_critical, 0)
    coverage = coverage_for_items(session, items)
    suggested = {it.id: suggested_reorder_qty(it) for it in items}
    # Group the page's items by product line (coalescing nulls) so the template
    # can render brand sub-headers without sorting None values itself.
    buckets: dict[str, list[Item]] = {}
    for it in items:
        buckets.setdefault(it.product_line or GENERIC_GROUP, []).append(it)
    grouped_items = sorted(buckets.items(), key=lambda kv: group_sort_key(kv[0]))
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    # Pagination links need to carry the active filters through. Build it
    # here (not in Jinja) so it round-trips as a regular HTML-escaped string.
    base_qs_parts: list[tuple[str, str]] = []
    if q:
        base_qs_parts.append(("q", q))
    if vendor:
        base_qs_parts.append(("vendor", vendor))
    if stock_status:
        base_qs_parts.append(("stock_status", stock_status))
    if missing_material_code:
        base_qs_parts.append(("missing_material_code", "true"))
    # base_filter_qs drives the group-chip links (everything except group).
    base_filter_qs = ("&" + urlencode(base_qs_parts)) if base_qs_parts else ""
    # filter_qs drives pagination links (everything including the active group).
    pager_qs_parts = list(base_qs_parts)
    if group:
        pager_qs_parts.append(("group", group))
    filter_qs = ("&" + urlencode(pager_qs_parts)) if pager_qs_parts else ""
    from packtrack.main import templates
    return templates.TemplateResponse(
        request, "inventory.html",
        {
            "user": user, "items": items, "q": q or "",
            "grouped_items": grouped_items,
            "vendor": vendor or "",
            "stock_status": stock_status or "",
            "missing_material_code": missing_material_code,
            "groups": groups,
            "active_group": group or "",
            "coverage": coverage,
            "suggested": suggested,
            "scope": get_scope(session),
            "page": page,
            "page_size": PAGE_SIZE,
            "total": total,
            "total_pages": total_pages,
            "filter_qs": filter_qs,
            "base_filter_qs": base_filter_qs,
            "count_total": count_total,
            "count_missing_code": count_missing_code,
            "count_critical": count_critical,
            "count_low_only": count_low_only,
        },
    )


@router.get("/inventory/{item_id:int}", response_class=HTMLResponse)
def item_detail(
    item_id: int,
    request: Request,
    saved: str | None = None,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    item = session.get(Item, item_id)
    if item is None:
        raise HTTPException(status_code=404)
    coverage = coverage_for_items(session, [item]).get(item.id)
    suggested = suggested_reorder_qty(item)
    from packtrack.main import templates
    return templates.TemplateResponse(
        request, "inventory_detail.html",
        {
            "user": user,
            "it": item,
            "coverage": coverage,
            "suggested": suggested,
            "can_edit": user.role == Role.OWNER,
            "saved": saved or "",
        },
    )


def _clean(value: str, limit: int) -> str:
    return (value or "").strip()[:limit]


@router.post("/inventory/{item_id:int}")
def update_item(
    item_id: int,
    request: Request,
    name: str = Form(""),
    description: str = Form(""),
    material_code: str = Form(""),
    vendor: str = Form(""),
    unit: str = Form(""),
    daily_usage_rate: float = Form(0),
    reorder_point: float = Form(0),
    critical_point: float = Form(0),
    sea_lead_days: int = Form(0),
    express_lead_days: int = Form(0),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    """Owner-only full item edit from the detail page.

    ``current_stock`` is intentionally NOT editable — stock comes from Zoho /
    receipts, and the repo has no manual inventory-adjustment pattern that we
    could safely reuse from the UI. Editing a Zoho-owned field parks the item
    as ``pending`` outbound sync (no Zoho item-write path exists yet).
    """
    if user.role != Role.OWNER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    item = session.get(Item, item_id)
    if item is None:
        raise HTTPException(status_code=404)

    new_name = _clean(name, _MAX_NAME)
    new_values = {
        "name": new_name or item.name,  # never blank out the name
        "description": (description or "").strip() or None,
        "vendor": _clean(vendor, _MAX_VENDOR) or None,
        "unit": _clean(unit, _MAX_UNIT) or item.unit,
    }
    # Did any Zoho-owned field actually change? Only then do we touch push state.
    zoho_dirty = any(
        getattr(item, fld) != new_values[fld]
        for fld in ZOHO_OWNED_EDITABLE_FIELDS
    )

    item.name = new_values["name"]
    item.description = new_values["description"]
    item.vendor = new_values["vendor"]
    item.unit = new_values["unit"]
    item.material_code = _clean(material_code, _MAX_MATERIAL_CODE) or None
    item.daily_usage_rate = max(0.0, float(daily_usage_rate or 0))
    item.reorder_point = max(0.0, float(reorder_point or 0))
    item.critical_point = max(0.0, float(critical_point or 0))
    item.sea_lead_days = max(0, int(sea_lead_days or 0))
    item.express_lead_days = max(0, int(express_lead_days or 0))
    # Owner override locks reorder_point from being overwritten by Zoho sync,
    # matching the existing inline-edit behavior.
    item.reorder_point_locked = True
    # Keep the brand grouping in step with the (possibly edited) name.
    item.product_line = derive_product_line(item.name)
    session.add(item)
    session.commit()

    saved = "ok"
    if zoho_dirty:
        result = push_item_update(session, item)
        saved = "synced" if result.status == "synced" else "local"

    return RedirectResponse(url=f"/inventory/{item_id}?saved={saved}", status_code=303)


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
