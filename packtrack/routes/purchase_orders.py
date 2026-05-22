"""PO list + detail + create + transitions + uploads + receiving.

One blueprint covers the full PO lifecycle. Every state-mutating endpoint
calls into ``services.workflow.allowed_move`` so role rules can't drift.
"""
from __future__ import annotations

import os
import shutil
import uuid
from datetime import date, datetime

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlmodel import Session, select

from packtrack import zoho
from packtrack.config import settings
from packtrack.db import get_session
from packtrack.deps import require_user
from packtrack.models import (
    Attachment,
    AttachmentKind,
    BoxReceipt,
    Item,
    POEvent,
    POLine,
    POStatus,
    PurchaseOrder,
    Role,
    ShipMethod,
    Shipment,
    ShipStatus,
    Urgency,
    User,
)
from packtrack.notifications import notify
from packtrack.services.workflow import (
    action_label,
    allowed_move,
    is_destructive,
    primary_action,
    suggested_moves,
)

router = APIRouter(prefix="/po")


def _next_po_number(session: Session) -> str:
    last = session.exec(
        select(PurchaseOrder).order_by(PurchaseOrder.id.desc())
    ).first()
    n = (last.id + 1) if last and last.id else 1
    return f"PT-{datetime.utcnow().strftime('%Y%m')}-{n:04d}"


def _log(session: Session, po: PurchaseOrder, kind: str, message: str, user: User, payload: dict | None = None) -> None:
    session.add(POEvent(
        po_id=po.id,
        kind=kind,
        message=message,
        actor_id=user.id,
        payload=payload,
    ))


# --------------------------------------------------------------------------
# List + detail + new
# --------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
def board(
    request: Request,
    show_closed: bool = False,
    stale: bool = False,
    urgency: str | None = None,  # 'high' | 'critical'
    mine: bool = False,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    from packtrack.services.dashboard import build_pipeline

    pipeline = build_pipeline(
        session,
        only_stale=stale,
        urgency_filter=urgency if urgency in ("high", "critical") else None,
        mine_user_id=user.id if mine else None,
    )
    visible_count = sum(c.count for c in pipeline)
    closed = []
    if show_closed:
        closed = session.exec(
            select(PurchaseOrder)
            .where(PurchaseOrder.status.in_([POStatus.RECEIVED, POStatus.CANCELLED]))
            .order_by(PurchaseOrder.updated_at.desc())
            .limit(50)
        ).all()
    from packtrack.main import templates
    return templates.TemplateResponse(
        request, "po_board.html",
        {
            "user": user, "pipeline": pipeline,
            "closed": closed, "show_closed": show_closed,
            "filter_stale": stale, "filter_urgency": urgency or "",
            "filter_mine": mine, "visible_count": visible_count,
            "POStatus": POStatus,
        },
    )


@router.get("/new", response_class=HTMLResponse)
def new_po_form(
    request: Request,
    item_id: int | None = None,
    qty: float | None = None,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    if user.role != Role.OWNER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    from packtrack.services.scope import filter_items_query, get_scope
    item_rows = session.exec(
        filter_items_query(select(Item).order_by(Item.name), get_scope(session))
    ).all()
    items_for_template = [
        {
            "id": it.id,
            "name": it.name,
            "unit": it.unit,
            "current_stock": it.current_stock,
            "last_unit_cost": it.last_unit_cost or 0,
            "image_path": it.image_path,
        }
        for it in item_rows
    ]
    preselect = session.get(Item, item_id) if item_id else None
    from packtrack.main import templates
    return templates.TemplateResponse(
        request, "po_new.html",
        {
            "user": user, "items": items_for_template,
            "preselect": preselect, "preselect_qty": qty,
            "Urgency": Urgency,
        },
    )


@router.post("/new")
async def create_po(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    if user.role != Role.OWNER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    form = await request.form()
    urgency = form.get("urgency", "normal")
    notes = form.get("notes", "")
    currency = (form.get("currency") or "USD").upper()[:10]
    item_ids = form.getlist("item_id[]")
    quantities = form.getlist("quantity[]")
    prices = form.getlist("unit_price[]")
    arrivals = form.getlist("arrival[]")
    line_notes = form.getlist("line_notes[]")
    if not item_ids:
        raise HTTPException(status_code=400, detail="Select at least one item.")

    po = PurchaseOrder(
        po_number=_next_po_number(session),
        status=POStatus.DESIGN_REVIEW,
        urgency=Urgency(urgency),
        notes=notes or None,
        currency=currency,
        created_by_id=user.id,
    )
    session.add(po)
    session.flush()

    for i, item_id in enumerate(item_ids):
        try:
            qty = float(quantities[i] or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        item = session.get(Item, int(item_id))
        if item is None:
            continue
        try:
            price = float(prices[i] or 0) if i < len(prices) else 0.0
        except (TypeError, ValueError):
            price = 0.0
        arrival_str = arrivals[i] if i < len(arrivals) else ""
        ln = line_notes[i] if i < len(line_notes) else ""
        session.add(POLine(
            po_id=po.id,
            item_id=item.id,
            quantity=qty,
            unit_price=price,
            target_arrival=date.fromisoformat(arrival_str) if arrival_str else None,
            line_notes=ln or None,
        ))
        # Track most recent unit cost on the item — feeds the next PO's
        # default price suggestion and powers cost-trend reports later.
        if price > 0:
            item.last_unit_cost = price

    _log(session, po, "status_change", f"PO created. Urgency: {urgency}.", user)
    session.commit()

    # Push to Zoho synchronously; failure persisted on PO and retried by scheduler.
    if settings.zoho_configured:
        zoho.push_po(session, po)

    notify(session, "po.created", po, actor=user)
    return RedirectResponse(url=f"/po/{po.id}", status_code=303)


@router.get("/{po_id}", response_class=HTMLResponse)
def po_detail(
    po_id: int,
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    po = session.get(PurchaseOrder, po_id)
    if po is None:
        raise HTTPException(status_code=404)
    events = session.exec(
        select(POEvent).where(POEvent.po_id == po.id).order_by(POEvent.created_at.desc())
    ).all()
    primary = primary_action(user, po.status)
    suggested = suggested_moves(user, po.status)
    secondary = [s for s in suggested if s != primary]
    from packtrack.main import templates
    return templates.TemplateResponse(
        request, "po_detail.html",
        {
            "user": user,
            "po": po,
            "events": events,
            "primary": primary,
            "primary_label": action_label(po.status, primary) if primary else None,
            "secondary": [
                {
                    "target": s,
                    "label": action_label(po.status, s),
                    "destructive": is_destructive(s),
                }
                for s in secondary
            ],
            "POStatus": POStatus,
            "Role": Role,
        },
    )


# --------------------------------------------------------------------------
# Status transitions
# --------------------------------------------------------------------------


@router.post("/{po_id}/move")
async def move_status(
    po_id: int,
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    """Move a PO. Accepts either:
      - ``target`` (status string) — for click-driven moves from PO detail
      - ``column`` (board column key) — for drag-and-drop on the board

    Both forms route through ``allowed_move`` so role rules can't drift.
    Returns JSON for HTMX/board callers, redirects for plain HTML POSTs.
    """
    from packtrack.services.dashboard import COLUMN_TARGET_STATUS
    from fastapi.responses import JSONResponse

    po = session.get(PurchaseOrder, po_id)
    if po is None:
        raise HTTPException(status_code=404)

    form = await request.form()
    target = (form.get("target") or "").strip()
    column = (form.get("column") or "").strip()
    reason = (form.get("reason") or "").strip()

    target_status: POStatus | None = None
    if target:
        try:
            target_status = POStatus(target)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid status")
    elif column:
        target_status = COLUMN_TARGET_STATUS.get(column)
        if target_status is None:
            raise HTTPException(status_code=400, detail="Invalid column")
    else:
        raise HTTPException(status_code=400, detail="Need target or column")

    wants_json = (
        request.headers.get("hx-request") == "true"
        or "application/json" in (request.headers.get("accept") or "")
    )
    ok, err = allowed_move(user, po.status, target_status)
    if not ok:
        if wants_json:
            return JSONResponse({"ok": False, "error": err}, status_code=403)
        raise HTTPException(status_code=403, detail=err)

    old_status = po.status
    if old_status == target_status:
        if wants_json:
            return JSONResponse({"ok": True, "status": target_status.value, "unchanged": True})
        return RedirectResponse(url=f"/po/{po.id}", status_code=303)
    po.status = target_status
    po.updated_at = datetime.utcnow()
    _log(
        session, po, "status_change",
        f"{old_status.value.replace('_', ' ')} → {target_status.value.replace('_', ' ')}"
        + (f" — {reason}" if reason else ""),
        user,
    )
    session.commit()

    # Fire notifications matched to the transition.
    if old_status == POStatus.DESIGN_REVIEW and target_status == POStatus.DESIGN_APPROVED:
        notify(session, "po.design_approved", po)
    elif old_status == POStatus.DESIGN_REVIEW and target_status == POStatus.DESIGN_REJECTED:
        notify(session, "po.design_rejected", po, reason=reason or "No reason given.")
    elif target_status == POStatus.DESIGN_REVIEW and old_status in (POStatus.DESIGN_REJECTED, POStatus.DESIGN_APPROVED):
        notify(session, "po.returned_to_design", po)
    elif old_status == POStatus.PI_RECEIVED and target_status == POStatus.PI_APPROVED:
        notify(session, "po.pi_approved", po)
    elif old_status == POStatus.PI_RECEIVED and target_status == POStatus.DESIGN_APPROVED:
        notify(session, "po.pi_rejected", po, reason=reason or "No reason given.")
    elif target_status == POStatus.PRODUCTION and old_status != POStatus.PRODUCTION:
        notify(session, "po.production_started", po)
    elif target_status == POStatus.SHIPPED and old_status != POStatus.SHIPPED:
        notify(session, "po.shipped", po, summary=reason or "")

    if wants_json:
        return JSONResponse({
            "ok": True, "status": target_status.value,
            "status_label": po.status.value.replace('_', ' ').title(),
        })
    return RedirectResponse(url=f"/po/{po.id}", status_code=303)


@router.get(".csv")
def po_csv(
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    """All POs as CSV — for finance / spreadsheet exports."""
    import csv
    from io import StringIO
    from fastapi.responses import StreamingResponse

    pos = session.exec(
        select(PurchaseOrder).order_by(PurchaseOrder.created_at.desc())
    ).all()
    out = StringIO()
    w = csv.writer(out)
    w.writerow([
        "po_number", "status", "urgency", "currency", "total",
        "created_at", "updated_at", "zoho_po_id", "lines", "items",
    ])
    for po in pos:
        items = " | ".join(f"{line.item.name} x{line.quantity:g}" for line in po.lines)
        w.writerow([
            po.po_number, po.status.value, po.urgency.value, po.currency,
            f"{po.total:.2f}",
            po.created_at.isoformat(timespec="seconds"),
            po.updated_at.isoformat(timespec="seconds"),
            po.zoho_po_id or "",
            len(po.lines), items,
        ])
    out.seek(0)
    return StreamingResponse(
        iter([out.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="purchase_orders.csv"'},
    )


@router.get("/{po_id}/print", response_class=HTMLResponse)
def po_print(
    po_id: int,
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    po = session.get(PurchaseOrder, po_id)
    if po is None:
        raise HTTPException(status_code=404)
    from packtrack.main import templates
    return templates.TemplateResponse(
        request, "po_print.html", {"po": po, "user": user},
    )


@router.post("/{po_id}/duplicate")
def duplicate_po(
    po_id: int,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    """Create a fresh draft with the same lines (qty + price + notes).

    Most repeat orders are 80%+ identical to a previous PO; this saves the
    operator from re-entering 8 line items by hand. The new PO starts in
    DRAFT so the operator can adjust before sending it through.
    """
    if user.role != Role.OWNER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    src = session.get(PurchaseOrder, po_id)
    if src is None:
        raise HTTPException(status_code=404)

    new_po = PurchaseOrder(
        po_number=_next_po_number(session),
        status=POStatus.DRAFT,
        urgency=src.urgency,
        notes=src.notes,
        currency=src.currency,
        created_by_id=user.id,
    )
    session.add(new_po)
    session.flush()

    for line in src.lines:
        session.add(POLine(
            po_id=new_po.id,
            item_id=line.item_id,
            quantity=line.quantity,
            unit_price=line.unit_price,
            line_notes=line.line_notes,
        ))

    _log(
        session, new_po, "comment",
        f"Duplicated from {src.po_number}",
        user,
        payload={"source_po_id": src.id, "source_po_number": src.po_number},
    )
    session.commit()
    return RedirectResponse(url=f"/po/{new_po.id}", status_code=303)


@router.post("/{po_id}/comment")
def add_comment(
    po_id: int,
    text: str = Form(...),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    po = session.get(PurchaseOrder, po_id)
    if po is None:
        raise HTTPException(status_code=404)
    text = (text or "").strip()
    if text:
        _log(session, po, "comment", text, user)
        session.commit()
    return RedirectResponse(url=f"/po/{po.id}", status_code=303)


# --------------------------------------------------------------------------
# Attachments (PI + artwork — one endpoint, kind disambiguates)
# --------------------------------------------------------------------------


_ALLOWED_PI_EXT = {"pdf", "jpg", "jpeg", "png"}
_ALLOWED_ART_EXT = {"pdf", "jpg", "jpeg", "png", "webp", "zip", "ai", "eps"}


def _save_upload(file: UploadFile, po_number: str, kind: AttachmentKind) -> tuple[str, str]:
    if not file.filename or "." not in file.filename:
        raise HTTPException(status_code=400, detail="Invalid filename.")
    ext = file.filename.rsplit(".", 1)[1].lower()
    allowed = _ALLOWED_PI_EXT if kind == AttachmentKind.PI else _ALLOWED_ART_EXT
    if ext not in allowed:
        raise HTTPException(status_code=400, detail=f"Allowed: {', '.join(sorted(allowed))}")
    safe_name = f"{po_number}_{uuid.uuid4().hex[:10]}.{ext}"
    target_dir = settings.UPLOAD_DIR / kind.value
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / safe_name
    with target_path.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    return safe_name, str(target_path.relative_to(settings.UPLOAD_DIR))


def _next_attachment_version(session: Session, po_id: int, kind: AttachmentKind) -> int:
    rows = session.exec(
        select(Attachment).where(Attachment.po_id == po_id, Attachment.kind == kind)
    ).all()
    return (max((a.version for a in rows), default=0)) + 1


@router.post("/{po_id}/upload-pi")
def upload_pi(
    po_id: int,
    file: UploadFile = File(...),
    notes: str = Form(""),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    po = session.get(PurchaseOrder, po_id)
    if po is None:
        raise HTTPException(status_code=404)
    if user.role not in (Role.AGENT, Role.OWNER):
        raise HTTPException(status_code=403)
    if po.status != POStatus.DESIGN_APPROVED:
        raise HTTPException(status_code=409, detail="PO is not awaiting a PI right now.")

    safe_name, rel = _save_upload(file, po.po_number, AttachmentKind.PI)
    version = _next_attachment_version(session, po.id, AttachmentKind.PI)
    session.add(Attachment(
        po_id=po.id, kind=AttachmentKind.PI, version=version,
        filename=file.filename, file_path=rel, source="web",
        uploaded_by_id=user.id, notes=notes or None,
    ))
    po.status = POStatus.PI_RECEIVED
    po.updated_at = datetime.utcnow()
    _log(session, po, "attachment", f"PI v{version} uploaded ({safe_name}).", user)
    session.commit()
    notify(session, "po.pi_uploaded", po)
    return RedirectResponse(url=f"/po/{po.id}", status_code=303)


@router.post("/{po_id}/upload-artwork")
def upload_artwork(
    po_id: int,
    file: UploadFile | None = File(default=None),
    external_url: str = Form(""),
    notes: str = Form(""),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    po = session.get(PurchaseOrder, po_id)
    if po is None:
        raise HTTPException(status_code=404)
    if user.role not in (Role.DESIGN, Role.OWNER):
        raise HTTPException(status_code=403)
    if po.status not in (POStatus.DESIGN_REVIEW, POStatus.DESIGN_APPROVED, POStatus.PI_RECEIVED, POStatus.PI_APPROVED):
        raise HTTPException(status_code=409, detail="PO not accepting artwork now.")

    rel: str | None = None
    safe_name: str | None = None
    has_file = file is not None and file.filename
    has_url = bool(external_url.strip())
    if not has_file and not has_url:
        raise HTTPException(status_code=400, detail="Provide a file or external link.")
    if has_file:
        safe_name, rel = _save_upload(file, po.po_number, AttachmentKind.ARTWORK)
    if has_url and not external_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="External URL must be http:// or https://")

    version = _next_attachment_version(session, po.id, AttachmentKind.ARTWORK)
    session.add(Attachment(
        po_id=po.id, kind=AttachmentKind.ARTWORK, version=version,
        filename=safe_name or f"link-v{version}",
        file_path=rel, external_url=external_url[:1000] or None,
        source="link" if has_url and not has_file else "web",
        uploaded_by_id=user.id, notes=notes or None,
    ))
    _log(session, po, "attachment", f"Artwork v{version} added.", user)
    session.commit()
    notify(session, "po.artwork_uploaded", po, notes=notes)
    return RedirectResponse(url=f"/po/{po.id}", status_code=303)


@router.get("/{po_id}/file/{attachment_id}")
def download_attachment(
    po_id: int,
    attachment_id: int,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    a = session.get(Attachment, attachment_id)
    if a is None or a.po_id != po_id or not a.file_path:
        raise HTTPException(status_code=404)
    full = settings.UPLOAD_DIR / a.file_path
    if not full.exists():
        raise HTTPException(status_code=404)
    return FileResponse(path=full, filename=a.filename)


# --------------------------------------------------------------------------
# Shipping + receiving
# --------------------------------------------------------------------------


@router.post("/{po_id}/ship")
def mark_shipped(
    po_id: int,
    express_qty: float = Form(0),
    sea_qty: float = Form(0),
    express_eta: str = Form(""),
    sea_eta: str = Form(""),
    tracking: str = Form(""),
    carrier: str = Form(""),
    item_id: int | None = Form(None),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    po = session.get(PurchaseOrder, po_id)
    if po is None:
        raise HTTPException(status_code=404)
    if user.role not in (Role.OWNER, Role.AGENT):
        raise HTTPException(status_code=403)
    if po.status not in (POStatus.PI_APPROVED, POStatus.PRODUCTION):
        raise HTTPException(status_code=409)
    if express_qty <= 0 and sea_qty <= 0:
        raise HTTPException(status_code=400, detail="Enter express or sea quantity.")
    if express_qty > 0 and not express_eta:
        raise HTTPException(status_code=400, detail="Express ETA required.")
    if sea_qty > 0 and not sea_eta:
        raise HTTPException(status_code=400, detail="Sea ETA required.")

    today = date.today()
    if express_qty > 0:
        session.add(Shipment(
            po_id=po.id, item_id=item_id, method=ShipMethod.EXPRESS, quantity=express_qty,
            shipped_date=today, eta=date.fromisoformat(express_eta),
            tracking_number=tracking or None, carrier=carrier or None,
        ))
    if sea_qty > 0:
        session.add(Shipment(
            po_id=po.id, item_id=item_id, method=ShipMethod.SEA, quantity=sea_qty,
            shipped_date=today, eta=date.fromisoformat(sea_eta),
        ))
    po.status = POStatus.SHIPPED
    summary = f"Express: {express_qty:g}, Sea: {sea_qty:g}, Carrier: {carrier or '—'}"
    _log(session, po, "status_change", f"Shipped. {summary}", user)
    session.commit()
    notify(session, "po.shipped", po, summary=summary)
    return RedirectResponse(url=f"/po/{po.id}", status_code=303)


@router.post("/{po_id}/receive/{shipment_id}")
def receive_shipment(
    po_id: int,
    shipment_id: int,
    actual_qty: float = Form(...),
    discrepancy_notes: str = Form(""),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    if user.role not in (Role.RECEIVING, Role.OWNER):
        raise HTTPException(status_code=403)
    sh = session.get(Shipment, shipment_id)
    if sh is None or sh.po_id != po_id:
        raise HTTPException(status_code=404)
    po = session.get(PurchaseOrder, po_id)
    if po is None:
        raise HTTPException(status_code=404)

    sh.received_date = date.today()
    sh.received_quantity = actual_qty
    full = actual_qty >= sh.quantity
    sh.status = ShipStatus.RECEIVED if full else ShipStatus.PARTIAL
    if not full:
        sh.discrepancy_notes = discrepancy_notes or None

    summary = (
        f"{sh.method.value}: {actual_qty:g} of {sh.quantity:g} units received"
        + (" (full)" if full else f" (short by {sh.quantity - actual_qty:g})")
    )

    # Push stock adjustment to Zoho if linked.
    if sh.item_id is not None:
        item = session.get(Item, sh.item_id)
        if item and item.zoho_item_id and settings.zoho_configured:
            try:
                zoho.adjust_stock(
                    item.zoho_item_id, actual_qty,
                    f"Received via PackTrack — PO {po.po_number}",
                )
                summary += " · Zoho adjusted"
                item.current_stock += actual_qty
            except Exception as e:
                summary += f" · Zoho push failed: {e}"

    _log(session, po, "received", summary, user)

    # Close PO if all shipments are settled.
    all_shipments = session.exec(select(Shipment).where(Shipment.po_id == po.id)).all()
    if all(s.status in (ShipStatus.RECEIVED, ShipStatus.PARTIAL) for s in all_shipments):
        po.status = POStatus.RECEIVED
        _log(session, po, "status_change", "All shipments received. PO closed.", user)
    session.commit()

    if not full:
        notify(session, "po.discrepancy", po, notes=summary)
    else:
        notify(session, "po.received", po, summary=summary)
    return RedirectResponse(url=f"/po/{po.id}", status_code=303)


# ---------------------------------------------------------------------------
# Box-level receiving (P2). Lives alongside the existing shipment-level
# receive_shipment route — that flow remains usable for back-compat.
# ---------------------------------------------------------------------------


@router.post("/{po_id}/boxes")
def add_box_receipt(
    po_id: int,
    box_number: str = Form(...),
    item_id: int = Form(...),
    declared_quantity: float = Form(...),
    counted_quantity: str = Form(""),  # empty string = no count
    supplier_lot_number: str = Form(""),
    unit_of_measure: str = Form("EACH"),
    notes: str = Form(""),
    shipment_id: int | None = Form(None),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    """Record one supplier box on this PO.

    Intentionally additive to the existing ``/po/{po_id}/receive/{shipment_id}``
    flow — the older shipment-level receive remains, and the operator picks
    which one suits a given delivery. Box receipts power the eventual Luma
    push (P5); shipment receipts continue to push to Zoho stock as before.
    """
    from packtrack.services.box_receipt import (
        BoxReceiptError,
        create_box_receipt,
    )

    if user.role not in (Role.RECEIVING, Role.OWNER):
        raise HTTPException(status_code=403)
    po = session.get(PurchaseOrder, po_id)
    if po is None:
        raise HTTPException(status_code=404)
    item = session.get(Item, item_id)
    if item is None:
        raise HTTPException(status_code=400, detail="Unknown item.")

    counted_val: float | None
    counted_str = (counted_quantity or "").strip()
    if counted_str == "":
        counted_val = None
    else:
        try:
            counted_val = float(counted_str)
        except ValueError:
            raise HTTPException(status_code=400, detail="counted_quantity must be a number.")

    try:
        row = create_box_receipt(
            session,
            po=po,
            item=item,
            user=user,
            box_number=box_number,
            declared_quantity=declared_quantity,
            counted_quantity=counted_val,
            supplier_lot_number=supplier_lot_number or None,
            unit_of_measure=unit_of_measure or "EACH",
            notes=notes or None,
            shipment_id=shipment_id,
        )
    except BoxReceiptError as e:
        raise HTTPException(status_code=400, detail=str(e))

    _log(
        session, po, "received",
        f"Box {row.box_number} recorded · {row.accepted_quantity:g} {row.unit_of_measure} "
        f"({row.confidence.value}) · luma={row.luma_push_status.value}",
        user,
        payload={
            "box_receipt_id": row.id,
            "packtrack_receipt_id": row.packtrack_receipt_id,
        },
    )
    session.commit()
    return RedirectResponse(url=f"/po/{po.id}#boxes", status_code=303)
