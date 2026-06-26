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
from packtrack.services.zoho_item_detail import (
    WRITABLE_CUSTOM_FIELDS,
    build_extended_detail,
    build_item_editor,
    fetch_metadata,
    resolve_master_data_changes,
)
from packtrack.services.zoho_item_sync import (
    PUSH_FAILED,
    PUSH_SYNCED,
    push_item_update,
    scalar_payload,
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
    # Read-only extended Zoho detail (v2.6.0). Never raises — a service blip
    # leaves ``extended.available`` False and the local detail still renders.
    extended = build_extended_detail(item.zoho_item_id)
    return _render_item_detail(
        request, session, item, user, saved=saved or "", extended=extended
    )


def _local_values(item: Item) -> dict[str, str]:
    """The PackTrack-mirrored Zoho scalar trio (authoritative for these three)."""
    return {
        "name": item.name or "",
        "unit": item.unit or "",
        "description": item.description or "",
    }


def _render_item_detail(
    request: Request,
    session: Session,
    item: Item,
    user: User,
    *,
    saved: str,
    extended,
    submitted: dict[str, str] | None = None,
    errors: dict[str, str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Render the item detail page (also reused to re-show a form with errors)."""
    coverage = coverage_for_items(session, [item]).get(item.id)
    suggested = suggested_reorder_qty(item)
    can_edit = user.role == Role.OWNER
    editor = build_item_editor(
        extended,
        can_edit=can_edit,
        local_values=_local_values(item),
        submitted=submitted,
        errors=errors,
    )
    from packtrack.main import templates
    return templates.TemplateResponse(
        request, "inventory_detail.html",
        {
            "user": user,
            "it": item,
            "coverage": coverage,
            "suggested": suggested,
            "can_edit": can_edit,
            "saved": saved,
            "extended": extended,
            "editor": editor,
        },
        status_code=status_code,
    )


def _clean(value: str, limit: int) -> str:
    return (value or "").strip()[:limit]


def _form_float(form, key: str) -> float:
    try:
        return float(str(form.get(key) or 0))
    except (TypeError, ValueError):
        return 0.0


def _form_int(form, key: str) -> int:
    try:
        return int(float(str(form.get(key) or 0)))
    except (TypeError, ValueError):
        return 0


@router.post("/inventory/{item_id:int}")
async def update_item(
    item_id: int,
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    """Owner-only metadata-driven item master-data edit (v2.8.0).

    Saves PackTrack-owned operational fields locally, then validates the changed
    Zoho master-data fields against live metadata and pushes a single combined,
    changed-only PATCH through the integration service.

    Editable Zoho fields (per the v1.33.0 service contract): name, unit, brand,
    manufacturer, category_id, description, and the allowlisted custom fields.
    Everything else (vendor, sku, pricing, accounts, valuation, stock, tags,
    images, status/type) stays read-only and is never sent — read-only keys are
    not even collected from the form. ``current_stock`` is not editable.

    Validation is all-or-nothing: if any changed field is invalid, nothing is
    written to Zoho and the form is re-rendered with inline errors and the
    submitted values preserved. PackTrack's derived ``product_line`` browsing
    group is recomputed from the name and is never conflated with Zoho's
    ``cf_product_line`` custom field.
    """
    if user.role != Role.OWNER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    item = session.get(Item, item_id)
    if item is None:
        raise HTTPException(status_code=404)

    form = await request.form()

    # --- PackTrack-owned operational fields (local only; never sent to Zoho) ---
    item.material_code = _clean(str(form.get("material_code", "")), _MAX_MATERIAL_CODE) or None
    item.daily_usage_rate = max(0.0, _form_float(form, "daily_usage_rate"))
    item.reorder_point = max(0.0, _form_float(form, "reorder_point"))
    item.critical_point = max(0.0, _form_float(form, "critical_point"))
    item.sea_lead_days = max(0, _form_int(form, "sea_lead_days"))
    item.express_lead_days = max(0, _form_int(form, "express_lead_days"))
    # Owner override locks reorder_point from being overwritten by Zoho sync.
    item.reorder_point_locked = True
    # Vendor: Zoho-read-only for synced items; only editable for manual items.
    if not item.zoho_item_id:
        item.vendor = _clean(str(form.get("vendor", "")), _MAX_VENDOR) or None
    session.add(item)
    session.commit()

    # --- Collect only the allowlisted Zoho fields + their hidden originals ---
    extended = build_extended_detail(item.zoho_item_id)
    std_keys = ("name", "unit", "brand", "manufacturer", "category_id", "description")
    submitted: dict[str, str] = {k: str(form.get(k, "")) for k in std_keys}
    originals: dict[str, str] = {k: str(form.get(f"{k}__orig", "")) for k in std_keys}
    for api in WRITABLE_CUSTOM_FIELDS:
        submitted[api] = str(form.get(api, ""))
        originals[api] = str(form.get(f"{api}__orig", ""))

    metadata = fetch_metadata()
    res = resolve_master_data_changes(
        metadata=metadata,
        categories=extended.categories,
        submitted=submitted,
        originals=originals,
    )

    # All-or-nothing: any invalid change re-renders the form with inline errors
    # and the user's submitted values — nothing is written to Zoho.
    if res.errors:
        return _render_item_detail(
            request, session, item, user, saved="invalid", extended=extended,
            submitted=submitted, errors=res.errors, status_code=200,
        )

    if not res.payload:
        # Only local operational fields changed (or nothing did).
        return RedirectResponse(url=f"/inventory/{item_id}?saved=ok", status_code=303)

    # Mirror the validated Zoho scalar trio locally before pushing so a failed
    # push keeps the local edit (and the derived browsing group stays in step).
    if "name" in res.payload:
        item.name = _clean(res.payload["name"], _MAX_NAME) or item.name
        res.payload["name"] = item.name
    if "unit" in res.payload:
        item.unit = _clean(res.payload["unit"], _MAX_UNIT) or item.unit
        res.payload["unit"] = item.unit
    if "description" in res.payload:
        item.description = (res.payload["description"] or "").strip() or None
    item.product_line = derive_product_line(item.name)
    session.add(item)
    session.commit()

    saved = _saved_token(push_item_update(session, item, payload=res.payload).status)
    return RedirectResponse(url=f"/inventory/{item_id}?saved={saved}", status_code=303)


def _saved_token(push_status: str) -> str:
    """Map an outbound push status to the detail-page ``?saved=`` flash token."""
    if push_status == PUSH_SYNCED:
        return "synced"
    if push_status == PUSH_FAILED:
        return "failed"
    return "local"  # pending


@router.post("/inventory/{item_id:int}/sync/retry")
def retry_item_sync(
    item_id: int,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    """Owner-only: retry pushing this item's locally-mirrored scalars to Zoho.

    Honest retry: only ``name``/``description``/``unit`` are stored locally, so
    those are the only fields this can re-assert. Brand/manufacturer/category and
    custom-field edits that failed must be re-submitted from the edit form (they
    aren't persisted locally). Redirects with a ``saved=synced|failed|local``
    flash. Intentionally minimal — no outbox UI.
    """
    if user.role != Role.OWNER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    item = session.get(Item, item_id)
    if item is None:
        raise HTTPException(status_code=404)
    saved = _saved_token(
        push_item_update(session, item, payload=scalar_payload(item)).status
    )
    return RedirectResponse(url=f"/inventory/{item_id}?saved={saved}", status_code=303)


@router.post("/inventory/{item_id}/edit")
def edit_item_thresholds(
    item_id: int,
    request: Request,
    reorder_point: float = Form(0),
    critical_point: float = Form(0),
    daily_usage_rate: float = Form(0),
    material_code: str = Form(""),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    """Inline row editor — PackTrack-owned operational fields ONLY.

    Limited to daily usage, reorder point, critical point, and material_code
    (all PackTrack-owned, never pushed to Zoho). Zoho-owned fields (name,
    description, unit — see ZOHO_OWNED_EDITABLE_FIELDS) are edited on the detail
    page so they go through the integration-service push path. Vendor is
    Zoho-read-only and is never editable from the inline row.
    """
    if user.role != Role.OWNER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    item = session.get(Item, item_id)
    if item is None:
        raise HTTPException(status_code=404)
    item.reorder_point = max(0.0, float(reorder_point or 0))
    item.critical_point = max(0.0, float(critical_point or 0))
    item.daily_usage_rate = max(0.0, float(daily_usage_rate or 0))
    item.material_code = material_code.strip() or None
    item.reorder_point_locked = True  # owner overrode → lock from Zoho overwrite
    session.commit()
    # HTMX swap returns the updated row fragment
    if request.headers.get("hx-request") == "true":
        from packtrack.main import templates
        return templates.TemplateResponse(
            request, "_partials/inventory_row.html", {"it": item, "user": user}
        )
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
