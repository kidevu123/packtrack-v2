"""Receiving — record inbound packaging quantities per PO line item."""
from __future__ import annotations

import shutil
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, select

from packtrack.config import settings
from packtrack.db import get_session
from packtrack.deps import require_user
from packtrack.models import (
    BoxReceipt,
    Confidence,
    Item,
    LumaPushStatus,
    POEvent,
    POStatus,
    PurchaseOrder,
    Role,
    User,
    ZohoMirror,
)
from packtrack.services.box_receipt import compute_accepted, compute_confidence, compute_luma_readiness
from packtrack.services.receiving import (
    adopt_zoho_po,
    build_photo_url,
    create_zoho_receive,
    push_luma_receipt,
)

router = APIRouter(prefix="/receive")

_ALLOWED_PHOTO_EXT = {"jpg", "jpeg", "png", "webp", "heic"}


def _photo_dir() -> Path:
    d = settings.UPLOAD_DIR / "receiving"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_photo(file: UploadFile) -> str | None:
    if not file or not file.filename:
        return None
    ext = (file.filename.rsplit(".", 1)[-1] or "").lower()
    if ext not in _ALLOWED_PHOTO_EXT:
        return None
    fname = f"{uuid.uuid4().hex}.{ext}"
    dest = _photo_dir() / fname
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    return fname


# ---------------------------------------------------------------------------
# List — all open packaging POs
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
def receiving_list(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    if user.role not in (Role.RECEIVING, Role.OWNER):
        raise HTTPException(status_code=403)

    mirrors = session.exec(select(ZohoMirror).order_by(ZohoMirror.date.desc())).all()
    # Annotate with linked PackTrack PO (if already adopted).
    linked: dict[str, PurchaseOrder] = {}
    po_rows = session.exec(
        select(PurchaseOrder).where(PurchaseOrder.zoho_po_id.is_not(None))
    ).all()
    for po in po_rows:
        if po.zoho_po_id:
            linked[po.zoho_po_id] = po

    from packtrack.main import templates
    return templates.TemplateResponse(
        request, "receiving_list.html",
        {"user": user, "mirrors": mirrors, "linked": linked},
    )


# ---------------------------------------------------------------------------
# Receiving form — one Zoho PO
# ---------------------------------------------------------------------------


@router.get("/{zoho_po_id}", response_class=HTMLResponse)
def receiving_form(
    zoho_po_id: str,
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    if user.role not in (Role.RECEIVING, Role.OWNER):
        raise HTTPException(status_code=403)

    mirror = session.exec(
        select(ZohoMirror).where(ZohoMirror.zoho_purchaseorder_id == zoho_po_id)
    ).first()
    if mirror is None:
        raise HTTPException(status_code=404, detail="PO not found in mirror.")

    # Resolve each line item to an Item row (by zoho_item_id).
    line_items = []
    for li in (mirror.line_items or []):
        zid = str(li.get("item_id") or "")
        item = None
        if zid:
            item = session.exec(select(Item).where(Item.zoho_item_id == zid)).first()
        line_items.append({
            "zoho_item_id": zid,
            "zoho_line_item_id": str(li.get("line_item_id") or ""),
            "name": li.get("name") or (item.name if item else "Unknown item"),
            "ordered": float(li.get("quantity") or 0),
            "already_received": float(li.get("quantity_received") or 0),
            "item": item,
        })

    from packtrack.main import templates
    return templates.TemplateResponse(
        request, "receiving_form.html",
        {
            "user": user,
            "mirror": mirror,
            "line_items": line_items,
            "luma_configured": bool(settings.LUMA_RECEIPT_WEBHOOK_URL and settings.LUMA_PACKTRACK_SECRET),
        },
    )


# ---------------------------------------------------------------------------
# Submit receiving
# ---------------------------------------------------------------------------


@router.post("/{zoho_po_id}")
async def submit_receiving(
    zoho_po_id: str,
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    if user.role not in (Role.RECEIVING, Role.OWNER):
        raise HTTPException(status_code=403)

    mirror = session.exec(
        select(ZohoMirror).where(ZohoMirror.zoho_purchaseorder_id == zoho_po_id)
    ).first()
    if mirror is None:
        raise HTTPException(status_code=404, detail="PO not found.")

    form = await request.form()
    global_notes = (form.get("notes") or "").strip() or None

    # Parse per-line submissions.
    # Form fields are named: qty_{zoho_item_id}, counted_{zoho_item_id},
    #                         lot_{zoho_item_id}, photo_{zoho_item_id}
    zoho_item_ids = form.getlist("zoho_item_id[]")
    zoho_line_item_ids = form.getlist("zoho_line_item_id[]")

    results: list[dict] = []
    push_line_items: list[dict] = []  # for Zoho purchase receive
    po: PurchaseOrder | None = None
    luma_operation_id = str(uuid.uuid4())

    for i, zid in enumerate(zoho_item_ids):
        qty_str = (form.get(f"qty_{zid}") or "").strip()
        if not qty_str:
            continue
        try:
            declared = float(qty_str)
        except ValueError:
            continue
        if declared <= 0:
            continue

        counted_str = (form.get(f"counted_{zid}") or "").strip()
        counted: float | None = None
        if counted_str:
            try:
                counted = float(counted_str)
            except ValueError:
                pass

        lot_number = (form.get(f"lot_{zid}") or "").strip() or None
        photo_file = form.get(f"photo_{zid}")

        # Look up item.
        item = session.exec(select(Item).where(Item.zoho_item_id == zid)).first()
        if item is None:
            results.append({"name": zid, "ok": False, "error": "Item not found in PackTrack."})
            continue

        # Adopt PO once.
        if po is None:
            po = adopt_zoho_po(session, mirror, user)

        # Save photo if provided.
        photo_fname: str | None = None
        if hasattr(photo_file, "filename") and photo_file.filename:
            photo_fname = _save_photo(photo_file)

        # Create BoxReceipt.
        accepted = compute_accepted(declared, counted)
        confidence = compute_confidence(counted)
        luma_status = compute_luma_readiness(item.material_code)
        now = datetime.utcnow()
        box_number = f"RCPT-{luma_operation_id[:8]}-{i+1}"

        box = BoxReceipt(
            packtrack_receipt_id=str(uuid.uuid4()),
            purchase_order_id=po.id,
            item_id=item.id,
            material_code=(item.material_code or "").strip() or None,
            material_name=item.name[:240],
            supplier=item.vendor,
            supplier_lot_number=lot_number,
            box_number=box_number,
            declared_quantity=declared,
            counted_quantity=counted,
            accepted_quantity=accepted,
            unit_of_measure=item.unit or "EACH",
            confidence=confidence,
            received_by_user_id=user.id,
            received_at=now,
            luma_push_status=luma_status,
            photo_paths=[photo_fname] if photo_fname else None,
            notes=global_notes,
            created_at=now,
            updated_at=now,
        )
        session.add(box)
        session.flush()

        # Luma push.
        luma_ok = luma_err = luma_resp = None
        if settings.LUMA_RECEIPT_WEBHOOK_URL and settings.LUMA_PACKTRACK_SECRET:
            photo_urls = [build_photo_url(photo_fname)] if photo_fname else []
            luma_ok, luma_err, luma_resp = push_luma_receipt(
                box, mirror.purchaseorder_number or zoho_po_id, photo_urls
            )
            if luma_ok:
                box.luma_push_status = LumaPushStatus.PUSHED
                box.luma_pushed_at = datetime.utcnow()
                box.luma_response = luma_resp
            else:
                box.luma_push_status = LumaPushStatus.FAILED
                box.luma_response = {"error": luma_err}

        results.append({
            "name": item.name,
            "declared": declared,
            "counted": counted,
            "accepted": accepted,
            "confidence": confidence.value,
            "luma_ok": luma_ok,
            "luma_err": luma_err,
            "ok": True,
        })

        zlid = zoho_line_item_ids[i] if i < len(zoho_line_item_ids) else ""
        push_line_items.append({
            "zoho_item_id": zid,
            "zoho_line_item_id": zlid,
            "quantity": declared,
            "unit": item.unit,
        })

    if po is None:
        raise HTTPException(status_code=400, detail="No lines with a quantity > 0 were submitted.")

    # Log receiving event on the PO.
    summary_parts = [f"{r['name']}: {r['accepted']:g}" for r in results if r.get("ok")]
    session.add(POEvent(
        po_id=po.id,
        kind="received",
        message="Received: " + "; ".join(summary_parts) + (f" — {global_notes}" if global_notes else ""),
        actor_id=user.id,
    ))
    session.commit()

    # Zoho purchase receive (fire-and-forget; failure recorded in session).
    zoho_ok, zoho_err = create_zoho_receive(mirror, push_line_items, luma_operation_id, global_notes)
    if not zoho_ok:
        session.add(POEvent(
            po_id=po.id,
            kind="system",
            message=f"Zoho purchase receive failed: {zoho_err}",
        ))
        session.commit()

    from packtrack.main import templates
    return templates.TemplateResponse(
        request, "receiving_result.html",
        {
            "user": user,
            "mirror": mirror,
            "po": po,
            "results": results,
            "zoho_ok": zoho_ok,
            "zoho_err": zoho_err,
        },
        status_code=200,
    )
