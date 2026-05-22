"""Global search.

Matches across PO numbers, item names, SKU codes, and vendors. Single-hit
collapses straight to the resource; multi-hit shows a results page.
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, or_, select

from packtrack.db import get_session
from packtrack.deps import require_user
from packtrack.models import Item, PurchaseOrder, User
from packtrack.services.scope import (
    filter_items_query,
    filter_pos_query,
    get_scope,
)

router = APIRouter()


@router.get("/search", response_class=HTMLResponse)
def search(
    request: Request,
    q: str | None = None,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    q = (q or "").strip()
    if not q:
        from packtrack.main import templates
        return templates.TemplateResponse(
            request, "search.html",
            {"user": user, "q": "", "po_hits": [], "item_hits": []},
        )

    like = f"%{q}%"
    scope = get_scope(session)

    direct = session.exec(
        select(PurchaseOrder).where(PurchaseOrder.po_number.ilike(q))
    ).first()
    if direct is not None:
        return RedirectResponse(url=f"/po/{direct.id}", status_code=303)

    po_hits = session.exec(
        filter_pos_query(
            select(PurchaseOrder)
            .where(PurchaseOrder.po_number.ilike(like))
            .order_by(PurchaseOrder.created_at.desc())
            .limit(25),
            session, scope,
        )
    ).all()
    item_hits = session.exec(
        filter_items_query(
            select(Item)
            .where(or_(
                Item.name.ilike(like),
                Item.sku_code.ilike(like),
                Item.vendor.ilike(like),
            ))
            .order_by(Item.name)
            .limit(25),
            scope,
        )
    ).all()

    # Single overall hit → straight there
    if len(po_hits) == 1 and not item_hits:
        return RedirectResponse(url=f"/po/{po_hits[0].id}", status_code=303)
    if len(item_hits) == 1 and not po_hits:
        return RedirectResponse(url=f"/inventory?q={q}", status_code=303)

    from packtrack.main import templates
    return templates.TemplateResponse(
        request, "search.html",
        {"user": user, "q": q, "po_hits": po_hits, "item_hits": item_hits},
    )
