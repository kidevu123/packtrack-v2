"""Receiving — record inbound packaging quantities per PO line item."""
from __future__ import annotations

import shutil
import uuid
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import or_
from sqlmodel import Session, select

from packtrack.config import settings
from packtrack.db import get_session
from packtrack.deps import require_user
from packtrack.models import (
    BoxReceipt,
    Item,
    LumaPushStatus,
    POEvent,
    PurchaseOrder,
    Role,
    User,
    ZohoMirror,
)
from packtrack.services.box_receipt import (
    compute_accepted,
    compute_confidence,
    compute_luma_readiness,
)
from packtrack.services.receiving import (
    ZohoReceiveSubmission,
    adopt_zoho_po,
    build_photo_url,
    ensure_material_code,
    push_luma_receipt,
    register_material_with_luma,
    submit_zoho_receives,
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
        {
            "user": user, "mirrors": mirrors, "linked": linked,
            "vnext_enabled": settings.RECEIVING_VNEXT_ENABLED,
        },
    )


# ---------------------------------------------------------------------------
# Receiving form — one Zoho PO
# ---------------------------------------------------------------------------


def _existing_boxes_for_submission(
    session: Session, po_id: int, submission_id: str,
) -> list[BoxReceipt]:
    """Return any BoxReceipts already created by this submission_id on this PO.

    Backed by the partial UNIQUE index
    ``uq_box_receipts_po_submission`` (migration 3c8a2b1e9d40,
    v2.4.1) — a race-losing concurrent POST hits the index and rolls
    back at the DB layer.

    Note: dedup is keyed on the ``submission_id`` column, NOT on
    ``box_number``. ``box_number`` is the supplier-carton field in the
    operator-typed flow and a Luma-compatibility mirror of
    ``packtrack_receipt_id`` in the receive-form flow; neither
    semantics belongs in an idempotency key.
    """
    if not submission_id:
        return []
    return session.exec(
        select(BoxReceipt).where(
            BoxReceipt.purchase_order_id == po_id,
            BoxReceipt.submission_id == submission_id,
        )
    ).all()


def _luma_compat_box_number(packtrack_receipt_id: str) -> str:
    """Synthetic ``box_number`` for receiving-form rows.

    Luma's ``/api/integrations/packtrack/receipts`` schema
    (``z.string().min(1)`` on ``box_number``) currently requires a
    non-empty value AND dedupes on
    ``(packtrack_receipt_id, box_number)``. Receiving forms do not
    collect a per-box supplier identifier, so we mirror the receipt id
    to satisfy both constraints with a stable, documented value.

    This is **not** PackTrack's idempotency key — that lives in
    ``submission_id`` / ``submission_line_index``. The ``PT-`` prefix
    makes it obvious in Luma logs that the value is PackTrack-
    generated compatibility content rather than a real carton string.

    See ``docs/PACKTRACK_LUMA_CONTRACT.md`` § 7 (P0-1) for the
    follow-up: a coordinated Luma change that makes ``box_number``
    optional would let us send empty here.
    """
    return f"PT-{packtrack_receipt_id}"


def _render_already_processed(
    request: Request,
    user: User,
    mirror: ZohoMirror,
    po: PurchaseOrder,
    existing_boxes: list[BoxReceipt],
):
    """Render the result template using the rows the first POST created.

    No new BoxReceipts, no fresh Luma push, no Zoho commit — the
    original submission already did all of that. The template shows a
    'submission already processed' banner.
    """
    from packtrack.main import templates

    results: list[dict] = []
    for box in sorted(existing_boxes, key=lambda b: b.box_number):
        pushed = box.luma_push_status == LumaPushStatus.PUSHED
        results.append({
            "name": box.material_name,
            "declared": box.declared_quantity,
            "counted": box.counted_quantity,
            "accepted": box.accepted_quantity,
            "confidence": box.confidence.value,
            "luma_ok": pushed,
            "luma_err": None if pushed else ((box.luma_response or {}).get("error") if box.luma_response else None),
            "ok": True,
            "_box_receipt_id": box.id,
        })
    return templates.TemplateResponse(
        request, "receiving_result.html",
        {
            "user": user,
            "mirror": mirror,
            "po": po,
            "results": results,
            "zoho_committed": 0,
            "zoho_blocked": 0,
            "zoho_failed": 0,
            "zoho_total": 0,
            "is_retry": False,
            "already_processed": True,
        },
        status_code=200,
    )


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

    # Fresh idempotency key per form render. The form embeds this as a
    # hidden field; the POST handler refuses missing IDs and rejects any
    # POST whose ID already produced BoxReceipts on this PO.
    submission_id = uuid.uuid4().hex

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

    # Count distinct items with FAILED/NOT_READY BoxReceipts so the banner
    # shows the number of items to push (not raw row count from repeat submissions).
    luma_configured = bool(settings.LUMA_RECEIPT_WEBHOOK_URL and settings.LUMA_PACKTRACK_SECRET)
    failed_boxes_count = 0
    if luma_configured:
        po_row = session.exec(
            select(PurchaseOrder).where(PurchaseOrder.zoho_po_id == zoho_po_id)
        ).first()
        if po_row is not None:
            all_failed = session.exec(
                select(BoxReceipt).where(
                    BoxReceipt.purchase_order_id == po_row.id,
                    or_(
                        BoxReceipt.luma_push_status == LumaPushStatus.FAILED,
                        BoxReceipt.luma_push_status == LumaPushStatus.NOT_READY,
                    ),
                )
            ).all()
            # Count distinct items (deduplication mirrors what retry-luma does).
            failed_boxes_count = len({b.item_id for b in all_failed})

    from packtrack.main import templates
    return templates.TemplateResponse(
        request, "receiving_form.html",
        {
            "user": user,
            "mirror": mirror,
            "line_items": line_items,
            "luma_configured": luma_configured,
            "failed_boxes_count": failed_boxes_count,
            "submission_id": submission_id,
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

    # ── P0-1 idempotency guard ────────────────────────────────────────
    # Every receiving form render embeds a hex submission_id. We require
    # it on POST so accidental double-submits (browser back+submit, lost
    # connection retries, repeat click) cannot create duplicate
    # BoxReceipts or duplicate Luma pushes. The durable backstop is the
    # ``uq_box_receipts_po_box`` constraint — even if the in-flight
    # check below missed a row, the second insert would fail at the DB.
    submission_id = (form.get("submission_id") or "").strip()
    if not submission_id:
        raise HTTPException(
            status_code=400,
            detail="Missing submission_id — refresh the receiving form and try again.",
        )

    # Look up the PT PO before the dedup check so we have an id to bind
    # the lookup to. Do NOT call adopt_zoho_po yet (it would create a row
    # even for empty submissions and dedup re-renders).
    po_existing = session.exec(
        select(PurchaseOrder).where(PurchaseOrder.zoho_po_id == zoho_po_id)
    ).first()
    if po_existing is not None:
        already = _existing_boxes_for_submission(
            session, po_existing.id, submission_id,
        )
        if already:
            return _render_already_processed(
                request, user, mirror, po_existing, already,
            )

    # Parse per-line submissions.
    # Form fields are named: qty_{zoho_item_id}, counted_{zoho_item_id},
    #                         lot_{zoho_item_id}, photo_{zoho_item_id}
    zoho_item_ids = form.getlist("zoho_item_id[]")
    zoho_line_item_ids = form.getlist("zoho_line_item_id[]")

    results: list[dict] = []
    zoho_submissions: list[ZohoReceiveSubmission] = []  # for zoho-integration-service
    po: PurchaseOrder | None = None
    # The Zoho integration service uses this as its session_id; reusing
    # submission_id means a re-submit with the same token also dedups at
    # the Zoho service layer (its Idempotency-Key is derived from the
    # per-line packtrack_receipt_id, but the session_id provides extra
    # tracing).
    luma_operation_id = submission_id

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
            with suppress(ValueError):
                counted = float(counted_str)

        lot_number = (form.get(f"lot_{zid}") or "").strip() or None
        photo_file = form.get(f"photo_{zid}")

        # Look up item.
        item = session.exec(select(Item).where(Item.zoho_item_id == zid)).first()
        if item is None:
            results.append({"name": zid, "ok": False, "error": "Item not found in PackTrack."})
            continue

        # Ensure item has a material_code before snapshotting into BoxReceipt.
        # ensure_material_code() tries sku_code first, then auto-generates
        # PT-{id:05d} so Luma always receives a stable, non-empty code.
        ensure_material_code(session, item)

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
        # P0-1 (v2.4.1): submission_id + submission_line_index are the
        # PackTrack-side idempotency keys. box_number is a Luma
        # compatibility mirror of packtrack_receipt_id — NOT the dedup
        # key (see model docstring and _luma_compat_box_number).
        receipt_id = str(uuid.uuid4())
        box_number = _luma_compat_box_number(receipt_id)

        box = BoxReceipt(
            packtrack_receipt_id=receipt_id,
            purchase_order_id=po.id,
            item_id=item.id,
            material_code=(item.material_code or "").strip() or None,
            material_name=item.name[:240],
            supplier=item.vendor,
            supplier_lot_number=lot_number,
            box_number=box_number,
            submission_id=submission_id,
            submission_line_index=i + 1,
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

        # Luma push — pre-register the material first so the mapping exists.
        luma_ok = luma_err = luma_resp = None
        if settings.LUMA_RECEIPT_WEBHOOK_URL and settings.LUMA_PACKTRACK_SECRET:
            register_material_with_luma(item)   # best-effort; failures logged
            photo_urls = [build_photo_url(photo_fname)] if photo_fname else []
            luma_ok, luma_err, luma_resp = push_luma_receipt(
                box, mirror.purchaseorder_number or zoho_po_id, photo_urls,
                received_by=user.name,
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
            "_box_receipt_id": box.id,  # joined to zoho results after submission
        })

        zlid = zoho_line_item_ids[i] if i < len(zoho_line_item_ids) else ""
        zoho_submissions.append(ZohoReceiveSubmission(
            box_receipt_id=box.id,
            packtrack_receipt_id=box.packtrack_receipt_id,
            zoho_item_id=zid,
            zoho_line_item_id=zlid,
            quantity=declared,
            item_name=item.name,
        ))

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

    # Zoho purchase receives — one commit per line via zoho-integration-service.
    zoho_results = submit_zoho_receives(
        mirror, zoho_submissions,
        operator=user, session_id=luma_operation_id, notes=global_notes,
    )
    committed = sum(1 for r in zoho_results if r.status == "committed")
    blocked = sum(1 for r in zoho_results if r.blocked)
    failed = sum(1 for r in zoho_results if not r.ok and not r.blocked and r.status != "skipped")

    # POEvent per non-success line so admins can audit without paging through
    # uvicorn logs. Successful commits don't get a per-line event — the
    # "received" event above already summarises the operator's submission.
    for r in zoho_results:
        if r.status in ("committed", "skipped"):
            continue
        prefix = {
            "blocked": "Zoho receive blocked (live writes disabled)",
            "disabled": "Zoho receive submission disabled by ops",
            "not_configured": "Zoho receive skipped — integration not configured",
            "validation_failed": f"Zoho receive validation failed ({r.error_code})",
            "config_error": f"Zoho integration config error ({r.error_code})",
            "auth_failed": "Zoho integration auth failed",
            "idempotency_conflict": "Zoho receive idempotency conflict (data inconsistency)",
            "rate_limited": "Zoho integration rate-limited",
            "gateway_error": "Zoho integration gateway error",
        }.get(r.status, f"Zoho receive status={r.status}")
        session.add(POEvent(
            po_id=po.id,
            kind="zoho_receive",
            message=f"{prefix}: {r.submission.item_name} qty={r.submission.quantity:g}"
                    + (f" — {r.message}" if r.message else ""),
        ))
    if zoho_results:
        session.commit()

    # Attach a per-line zoho status to results so the template can render it.
    by_box_id = {r.submission.box_receipt_id: r for r in zoho_results}
    for entry in results:
        zr = by_box_id.get(entry.get("_box_receipt_id"))
        if zr is None:
            continue
        entry["zoho_status"] = zr.status
        entry["zoho_msg"] = zr.message
        entry["zoho_error_code"] = zr.error_code

    from packtrack.main import templates
    return templates.TemplateResponse(
        request, "receiving_result.html",
        {
            "user": user,
            "mirror": mirror,
            "po": po,
            "results": results,
            "zoho_committed": committed,
            "zoho_blocked": blocked,
            "zoho_failed": failed,
            "zoho_total": len(zoho_results),
            "is_retry": False,
        },
        status_code=200,
    )


# ---------------------------------------------------------------------------
# Retry Luma push — for already-recorded BoxReceipts that failed or were
# NOT_READY because the item had no material_code at receive time.
# ---------------------------------------------------------------------------


@router.post("/{zoho_po_id}/retry-luma")
def retry_luma_push(
    zoho_po_id: str,
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    """Re-push all FAILED / NOT_READY BoxReceipts on this PO to Luma.

    For NOT_READY boxes the item's material_code is resolved (or auto-
    generated) first so that every box gets a stable code before the push.
    No new BoxReceipts or Zoho receives are created — this is a Luma-only
    sync operation on existing recorded data.
    """
    if user.role not in (Role.RECEIVING, Role.OWNER):
        raise HTTPException(status_code=403)

    mirror = session.exec(
        select(ZohoMirror).where(ZohoMirror.zoho_purchaseorder_id == zoho_po_id)
    ).first()
    if mirror is None:
        raise HTTPException(status_code=404, detail="PO not found in mirror.")

    po = session.exec(
        select(PurchaseOrder).where(PurchaseOrder.zoho_po_id == zoho_po_id)
    ).first()
    if po is None:
        # No receipts have ever been recorded for this PO — send back to form.
        return RedirectResponse(url=f"/receive/{zoho_po_id}", status_code=303)

    if not (settings.LUMA_RECEIPT_WEBHOOK_URL and settings.LUMA_PACKTRACK_SECRET):
        raise HTTPException(status_code=400, detail="Luma is not configured on this server.")

    boxes = session.exec(
        select(BoxReceipt).where(
            BoxReceipt.purchase_order_id == po.id,
            or_(
                BoxReceipt.luma_push_status == LumaPushStatus.FAILED,
                BoxReceipt.luma_push_status == LumaPushStatus.NOT_READY,
            ),
        )
    ).all()

    if not boxes:
        # Nothing to retry.
        return RedirectResponse(url=f"/receive/{zoho_po_id}", status_code=303)

    # Deduplicate: when multiple failed attempts exist for the same item
    # (e.g. the operator clicked "Record receipt" several times), only push
    # the most-recent BoxReceipt per item_id and silently retire the older
    # ones as DUPLICATE so Luma never receives inflated quantities.
    latest_per_item: dict[int, BoxReceipt] = {}
    for box in boxes:
        existing = latest_per_item.get(box.item_id)
        if existing is None or box.created_at > existing.created_at:
            latest_per_item[box.item_id] = box

    now = datetime.utcnow()
    for box in boxes:
        if box is not latest_per_item.get(box.item_id):
            box.luma_push_status = LumaPushStatus.DUPLICATE
            box.updated_at = now
            session.add(box)

    retry_results: list[dict] = []
    po_number = mirror.purchaseorder_number or zoho_po_id

    for box in latest_per_item.values():
        item = session.get(Item, box.item_id)

        # Resolve / generate material_code on the Item if the snapshot is empty.
        if not box.material_code and item:
            mc = ensure_material_code(session, item)
            if mc:
                box.material_code = mc
                box.updated_at = now
                session.add(box)
                session.flush()

        if not box.material_code:
            retry_results.append({
                "name": box.material_name,
                "ok": True,
                "declared": box.declared_quantity,
                "accepted": box.accepted_quantity,
                "confidence": box.confidence.value,
                "luma_ok": False,
                "luma_err": "Could not resolve a material code for this item.",
            })
            continue

        # Pre-register material with Luma so the mapping exists before pushing.
        if item:
            register_material_with_luma(item)   # best-effort; failures logged

        photo_urls = [build_photo_url(p) for p in (box.photo_paths or []) if p]
        received_by = ""
        if box.received_by_user_id:
            usr = session.get(User, box.received_by_user_id)
            if usr:
                received_by = usr.name

        luma_ok, luma_err, luma_resp = push_luma_receipt(
            box, po_number, photo_urls, received_by=received_by,
        )
        if luma_ok:
            box.luma_push_status = LumaPushStatus.PUSHED
            box.luma_pushed_at = now
            box.luma_response = luma_resp
        else:
            box.luma_push_status = LumaPushStatus.FAILED
            box.luma_response = {"error": luma_err}
        box.updated_at = now
        session.add(box)

        retry_results.append({
            "name": box.material_name,
            "ok": True,
            "declared": box.declared_quantity,
            "accepted": box.accepted_quantity,
            "confidence": box.confidence.value,
            "luma_ok": luma_ok,
            "luma_err": luma_err,
        })

    session.commit()

    from packtrack.main import templates
    return templates.TemplateResponse(
        request, "receiving_result.html",
        {
            "user": user,
            "mirror": mirror,
            "po": po,
            "results": retry_results,
            # No Zoho receives are being submitted on a Luma retry — collapse
            # to neutral counters so the template hides the Zoho summary block.
            "zoho_committed": 0,
            "zoho_blocked": 0,
            "zoho_failed": 0,
            "zoho_total": 0,
            "is_retry": True,
        },
        status_code=200,
    )
