"""Slim Zoho Inventory client.

Sync (read) operations use the Zoho Integration Service gateway at
ZOHO_GATEWAY_URL (LXC 9503).  Push (write) operations — push_po,
adjust_stock — still use direct Zoho OAuth so they are gated on
zoho_configured; push migration to the gateway is P8.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import httpx
from sqlmodel import Session, select

from packtrack.config import settings
from packtrack.models import Item, POEvent, POStatus, PurchaseOrder, ZohoMirror
from packtrack.services.product_line import derive_product_line

logger = logging.getLogger("packtrack.zoho")

_token_cache: dict[str, object] = {"token": None, "expires_at": None}


# --------------------------------------------------------------------------
# OAuth
# --------------------------------------------------------------------------


def _get_access_token(force: bool = False) -> str:
    if (
        not force
        and _token_cache["token"]
        and _token_cache["expires_at"]
        and datetime.utcnow() < _token_cache["expires_at"]  # type: ignore[operator]
    ):
        return _token_cache["token"]  # type: ignore[return-value]
    if not settings.zoho_configured:
        raise RuntimeError("Zoho is not configured.")
    with httpx.Client(timeout=60.0) as client:
        r = client.post(
            settings.ZOHO_TOKEN_URL,
            data={
                "refresh_token": settings.ZOHO_REFRESH_TOKEN,
                "client_id": settings.ZOHO_CLIENT_ID,
                "client_secret": settings.ZOHO_CLIENT_SECRET,
                "grant_type": "refresh_token",
            },
        )
    if r.status_code >= 400:
        raise RuntimeError(f"Zoho OAuth failed: HTTP {r.status_code} {r.text[:300]}")
    data = r.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"Zoho OAuth: no access_token ({data!r})")
    _token_cache["token"] = token
    _token_cache["expires_at"] = datetime.utcnow() + timedelta(
        seconds=int(data.get("expires_in", 3600)) - 60
    )
    return token


def _headers() -> dict:
    return {
        "Authorization": f"Zoho-oauthtoken {_get_access_token()}",
        "Content-Type": "application/json",
    }


def _params() -> dict:
    return {"organization_id": settings.ZOHO_ORG_ID}


# --------------------------------------------------------------------------
# Gateway helpers (sync / read operations)
# --------------------------------------------------------------------------


def _gateway_headers() -> dict:
    return {
        "X-Brand": settings.ZOHO_GATEWAY_BRAND,
        "X-Internal-Token": settings.ZOHO_GATEWAY_TOKEN,
        "Accept": "application/json",
    }


def _is_packaging_po(detail: dict) -> bool:
    """True only when the Zoho 'Packaging?' checkbox is explicitly checked."""
    return (detail.get("custom_field_hash") or {}).get("cf_packaging_unformatted") is True


_ITEM_IMAGE_DIR_NAME = "items"  # under static/uploads/


def _items_image_dir() -> str:
    """Resolve and ensure the per-item image directory exists."""
    import os

    from packtrack.config import settings

    d = os.path.join(str(settings.UPLOAD_DIR), _ITEM_IMAGE_DIR_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def _is_image_bytes(blob: bytes) -> str | None:
    """Return a file extension if blob looks like an image, else None."""
    if not blob or len(blob) < 8:
        return None
    if blob[:2] == b"\xff\xd8":
        return "jpg"
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if blob[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if len(blob) >= 12 and blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "webp"
    return None


def _download_item_image(zoho_item_id: str) -> bytes | None:
    """Fetch item image via the gateway's binary image endpoint."""
    if not settings.gateway_configured:
        return None
    try:
        base = settings.ZOHO_GATEWAY_URL.rstrip("/")
        with httpx.Client(timeout=30.0) as client:
            r = client.get(
                f"{base}/zoho/items/image/{zoho_item_id}",
                headers=_gateway_headers(),
            )
        if r.status_code != 200:
            return None
        if "application/json" in (r.headers.get("Content-Type") or "").lower():
            return None
        return r.content
    except httpx.HTTPError:
        return None


def _sync_item_image(item: Item, zoho_item_id: str, image_id: str | None) -> None:
    """Download once per (item, image_id). Updates Item.image_path on success.

    Skipped silently when an image with the same ``image_id`` is already on
    disk — keeps repeat syncs cheap.
    """
    import os

    cache_key = f"{zoho_item_id}_{image_id or 'auto'}"
    if item.image_path and item.image_path.startswith(cache_key):
        return  # already cached
    blob = _download_item_image(zoho_item_id)
    if blob is None:
        return
    ext = _is_image_bytes(blob)
    if ext is None:
        return
    fname = f"{cache_key}.{ext}"
    path = os.path.join(_items_image_dir(), fname)
    try:
        with open(path, "wb") as f:
            f.write(blob)
    except OSError:
        return
    item.image_path = fname


def _vendor_name(item: dict) -> str:
    v = (item.get("vendor_name") or "").strip()
    if v:
        return v
    pv = item.get("preferred_vendors") or []
    if pv and isinstance(pv, list) and isinstance(pv[0], dict):
        return (pv[0].get("vendor_name") or "").strip()
    if isinstance(item.get("vendor"), dict):
        for k in ("vendor_name", "name", "contact_name"):
            x = (item["vendor"].get(k) or "").strip()
            if x:
                return x
    return ""


def _reorder_level(d: dict) -> float | None:
    for key in ("reorder_level", "reorderLevel", "reorder_point"):
        v = d.get(key)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None


def _apply_item_sync_fields(record: Item, raw: dict, *, is_new: bool = False) -> None:
    """Apply one Zoho item payload onto an ``Item`` record (mutates in place).

    Stock ownership (v2.11.0): PackTrack is the operational source of
    truth for packaging counts. The inbound Zoho item sync NEVER
    overwrites ``current_stock`` on an existing item — it records the
    upstream value as a snapshot (``last_zoho_stock_snapshot`` +
    ``last_zoho_stock_snapshot_at``) for reconciliation only. The
    ``is_new`` flag (set by the caller when this is the very first
    insert for a previously-unknown ``zoho_item_id``) allows a one-time
    seed of ``current_stock`` from Zoho so a brand-new SKU shows the
    right opening number. See ``services/inventory_stock_policy`` for
    the full allowlist + rationale.

    Loop / honesty protection: when an owner has edited a Zoho-owned, pushable
    field (name/description/unit) and the edit is parked ``pending`` (waiting on
    the integration service), the inbound sync must NOT revert those three —
    otherwise the UI would silently lose the edit before it syncs. ``vendor`` is
    Zoho-read-only in PackTrack (the service rejects vendor writes), so it
    always tracks Zoho here regardless of pending state. sku_code, reorder
    level, and image always track Zoho since they are not owner-editable.

    Outbound is only ever triggered by an explicit owner edit, never from here,
    so a sync pulling back identical values can never re-trigger an outbound
    push (no echo loop).
    """
    from packtrack.services.inventory_stock_policy import (
        parse_zoho_stock,
        record_zoho_stock_snapshot,
    )

    owner_edit_pending = record.zoho_push_status == "pending"
    if not owner_edit_pending:
        record.name = (raw.get("name") or "")[:240]
        record.description = (raw.get("description") or "")[:50000] or None
        unit = (raw.get("unit") or "").strip()
        if unit:
            record.unit = unit[:40]
    # Vendor is read-only in PackTrack → always reflect Zoho.
    record.vendor = (_vendor_name(raw) or "")[:200] or record.vendor
    record.sku_code = (raw.get("sku") or "")[:120] or None
    # v2.11.0 stock-ownership policy: snapshot upstream stock on every
    # sync; only seed current_stock on the very first insert.
    zoho_stock = parse_zoho_stock(raw)
    record_zoho_stock_snapshot(record, zoho_stock)
    if is_new and zoho_stock is not None:
        record.current_stock = float(zoho_stock)
    rl = _reorder_level(raw)
    if rl is not None and not record.reorder_point_locked:
        record.reorder_point = rl
    # Keep the brand grouping in step with the effective (possibly
    # owner-preserved) name on every sync.
    record.product_line = derive_product_line(record.name)
    record.last_synced_at = datetime.utcnow()


def sync_items(session: Session) -> tuple[int, int]:
    """Pull packaging items from Zoho via the gateway and upsert into ``items``.

    Filters to cf_item_type == 'Packaging' so non-packaging SKUs never land here.
    """
    base = settings.ZOHO_GATEWAY_URL.rstrip("/")
    hdrs = _gateway_headers()
    raw_items: list[dict] = []
    page = 1
    with httpx.Client(timeout=120.0) as client:
        while True:
            r = client.get(
                f"{base}/zoho/items/list",
                headers=hdrs,
                params={"page": page, "per_page": 200},
            )
            r.raise_for_status()
            payload = r.json()
            data = payload.get("data") or payload
            for item in data.get("items") or []:
                name = item.get("name") or ""
                cf = (item.get("cf_item_type") or "").strip()
                if cf == "Packaging" or "[Packaging]" in name:
                    raw_items.append(item)
            if not (data.get("page_context") or {}).get("has_more_page"):
                break
            page += 1
            if page > 100:
                logger.warning("sync_items: hit 100-page safety limit")
                break

    updated = created = 0
    just_created: list[Item] = []
    for raw in raw_items:
        zoho_id = str(raw.get("item_id") or "")
        if not zoho_id:
            continue
        record = session.exec(select(Item).where(Item.zoho_item_id == zoho_id)).first()
        if record is None:
            record = Item(zoho_item_id=zoho_id, name=(raw.get("name") or "")[:240])
            session.add(record)
            session.flush()
            created += 1
            just_created.append(record)
            _apply_item_sync_fields(record, raw, is_new=True)
        else:
            updated += 1
            _apply_item_sync_fields(record, raw, is_new=False)
        try:
            _sync_item_image(record, zoho_id, str(raw.get("image_id") or "") or None)
        except Exception as e:
            logger.debug("Image sync skipped for %s: %s", zoho_id, e)
    session.commit()

    # Opportunistic Luma registration for freshly created items. Most will
    # short-circuit (no material_code yet — assigned later at receive time)
    # but the ones that do have a code get their Luma mapping primed without
    # waiting for a receipt push. Failures are logged inside the helper.
    if just_created:
        from packtrack.services.receiving import maybe_register_with_luma
        for it in just_created:
            try:
                maybe_register_with_luma(it)
            except Exception:
                logger.exception("post-sync Luma registration raised for item %s", it.id)

    return updated, created


# --------------------------------------------------------------------------
# Open PO mirror
# --------------------------------------------------------------------------


_TERMINAL_STATUSES = {
    "cancelled", "void",
}


def _open_status(s: str | None) -> bool:
    return (s or "").strip().lower().replace(" ", "_") not in _TERMINAL_STATUSES


def _po_id(d: dict) -> str:
    return str(d.get("purchaseorder_id") or d.get("purchase_order_id") or "")


def _po_vendor(d: dict) -> str:
    v = (d.get("vendor_name") or "").strip()
    if v:
        return v
    if isinstance(d.get("vendor"), dict):
        for k in ("vendor_name", "name", "contact_name", "company_name"):
            x = (d["vendor"].get(k) or "").strip()
            if x:
                return x
    return ""


def sync_open_pos(session: Session) -> int:
    """Pull open packaging POs from Zoho via the gateway into ``zoho_mirror``.

    Two-pass:
      1. List all POs (fast, no custom fields) and filter to open statuses.
      2. Fetch detail for each open PO via the generic dispatcher, which
         returns ``custom_field_hash`` including ``cf_packaging_unformatted``.
         Only POs with that flag explicitly True are saved.

    Wipes and replaces ``zoho_mirror`` on every run.
    """
    base = settings.ZOHO_GATEWAY_URL.rstrip("/")
    hdrs = _gateway_headers()

    # Pass 1 — paginate list endpoint
    all_pos: list[dict] = []
    page = 1
    with httpx.Client(timeout=120.0) as client:
        while True:
            r = client.get(
                f"{base}/zoho/purchaseorders_inv/list",
                headers=hdrs,
                params={"page": page, "per_page": 200},
            )
            r.raise_for_status()
            payload = r.json()
            batch = (payload.get("data") or {}).get("purchaseorders") or []
            all_pos.extend(batch)
            if not (payload.get("meta") or {}).get("has_more"):
                break
            page += 1
            if page > 20:
                logger.warning("sync_open_pos: hit 20-page safety limit")
                break

    open_pos = [p for p in all_pos if _open_status(p.get("status")) and _po_id(p)]
    logger.info("sync_open_pos: %d total, %d open status", len(all_pos), len(open_pos))

    # Pass 2 — fetch detail, filter by packaging flag
    packaging_details: list[dict] = []
    with httpx.Client(timeout=120.0) as client:
        for po in open_pos:
            zid = _po_id(po)
            try:
                r = client.get(
                    f"{base}/zoho/purchaseorders/get/{zid}",
                    headers=hdrs,
                )
                r.raise_for_status()
                detail = (r.json().get("data") or {}).get("purchaseorder") or {}
                if _is_packaging_po(detail):
                    packaging_details.append(detail)
            except httpx.HTTPError as e:
                logger.warning("Detail fetch failed for PO %s: %s", zid, e)

    logger.info("sync_open_pos: %d packaging POs imported", len(packaging_details))

    # Wipe + replace
    session.exec(ZohoMirror.__table__.delete())  # type: ignore[arg-type]
    now = datetime.utcnow()
    for detail in packaging_details:
        zid = str(detail.get("purchaseorder_id") or "")
        line_items_payload: list[dict] = []
        for li in detail.get("line_items") or []:
            line_items_payload.append({
                "name": (li.get("name") or "")[:160],
                "quantity": float(li.get("quantity") or 0),
                "quantity_received": float(li.get("quantity_received") or 0),
                "item_id": str(li.get("item_id") or ""),
                # Needed for zoho-integration-service Pack Track receive calls;
                # without it the service returns 400 PO_LINE_ITEM_NOT_FOUND.
                "line_item_id": str(li.get("line_item_id") or ""),
            })
        session.add(ZohoMirror(
            zoho_purchaseorder_id=zid,
            purchaseorder_number=detail.get("purchaseorder_number"),
            vendor_name=_po_vendor(detail) or None,
            status=detail.get("status"),
            date=detail.get("date"),
            delivery_date=detail.get("delivery_date") or None,
            total=float(detail["total"]) if detail.get("total") is not None else None,
            currency_code=detail.get("currency_code"),
            line_items=line_items_payload,
            synced_at=now,
        ))
    session.commit()
    return len(packaging_details)


# --------------------------------------------------------------------------
# Push PO + adjust stock
# --------------------------------------------------------------------------


def push_po(session: Session, po: PurchaseOrder) -> tuple[bool, str | None, str | None]:
    """Create a PO in Zoho. Adopts the assigned purchaseorder_number.

    Returns (ok, zoho_po_id, error). Updates push_status / push_error /
    push_attempted_at on the PO and commits.
    """
    po.push_attempted_at = datetime.utcnow()
    if po.zoho_po_id:
        po.push_status = "success"
        po.push_error = None
        session.commit()
        return True, po.zoho_po_id, None
    if not settings.zoho_configured:
        po.push_status = "failed"
        po.push_error = "Zoho not configured"
        session.commit()
        return False, None, po.push_error

    line_items: list[dict] = []
    for line in po.lines:
        if not line.item.zoho_item_id:
            continue
        li = {
            "item_id": line.item.zoho_item_id,
            "quantity": line.quantity,
            "description": line.line_notes or "",
        }
        # Zoho uses ``rate`` (per-unit) — only send if we set one, so a $0
        # placeholder doesn't overwrite a price set in Zoho directly.
        if line.unit_price and line.unit_price > 0:
            li["rate"] = line.unit_price
        line_items.append(li)
    if not line_items:
        po.push_status = "failed"
        po.push_error = "No line items have a Zoho item id"
        session.commit()
        return False, None, po.push_error

    payload = {
        "date": po.created_at.strftime("%Y-%m-%d"),
        "notes": po.notes or "",
        "line_items": line_items,
    }
    if po.currency and po.currency != "USD":
        # Zoho expects a currency_id rather than a code — leave currency_code
        # for the operator to set in Zoho if non-USD orgs need it; logging the
        # intent so a future migration can map codes → ids.
        pass
    try:
        with httpx.Client(timeout=60.0) as client:
            r = client.post(
                f"{settings.ZOHO_API_BASE}/purchaseorders",
                headers=_headers(),
                params=_params(),
                json=payload,
            )
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
        body = r.json()
        zpo = body.get("purchaseorder") or body.get("purchase_order") or {}
        zid = zpo.get("purchaseorder_id")
        zno = (zpo.get("purchaseorder_number") or "").strip()[:40]
    except Exception as e:
        po.push_status = "failed"
        po.push_error = str(e)[:1000]
        session.commit()
        return False, None, po.push_error

    po.zoho_po_id = str(zid) if zid else None
    if zno and zno != po.po_number:
        # Adopt the Zoho number so both systems agree.
        existing = session.exec(
            select(PurchaseOrder).where(
                PurchaseOrder.po_number == zno, PurchaseOrder.id != po.id
            )
        ).first()
        if existing is None:
            po.po_number = zno
    po.push_status = "success"
    po.push_error = None
    session.add(POEvent(
        po_id=po.id,
        kind="sync",
        message=f"Pushed to Zoho ({po.zoho_po_id}).",
    ))
    session.commit()
    return True, po.zoho_po_id, None


def adjust_stock(item_id: str, quantity: float, reason: str) -> dict:
    payload = {
        "date": datetime.utcnow().strftime("%Y-%m-%d"),
        "reason": reason,
        "line_items": [{"item_id": item_id, "quantity_adjusted": quantity}],
    }
    with httpx.Client(timeout=60.0) as client:
        r = client.post(
            f"{settings.ZOHO_API_BASE}/inventoryadjustments",
            headers=_headers(),
            params=_params(),
            json=payload,
        )
        r.raise_for_status()
        return r.json()


def retry_unpushed(session: Session, limit: int = 10) -> dict:
    rows = session.exec(
        select(PurchaseOrder)
        .where(PurchaseOrder.zoho_po_id.is_(None))
        .where(PurchaseOrder.status != POStatus.CANCELLED)
        .order_by(PurchaseOrder.created_at)
        .limit(limit)
    ).all()
    tried = ok = failed = 0
    for po in rows:
        tried += 1
        success, _, _ = push_po(session, po)
        if success:
            ok += 1
        else:
            failed += 1
    return {"tried": tried, "ok": ok, "failed": failed}
