"""Receiving orchestration.

Three responsibilities:
  1. adopt_zoho_po   — ensure a PackTrack PurchaseOrder + POLines exist for a
                       Zoho-mirrored PO so BoxReceipts can be attached.
  2. submit_zoho_receives — commit per-line purchase receives through
                       zoho-integration-service. Pack Track never calls Zoho
                       directly for receive writes.
  3. push_luma_receipt   — POST one BoxReceipt to Luma's webhook.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Literal

import httpx
from sqlmodel import Session, select

from packtrack.config import settings
from packtrack.models import (
    BoxReceipt,
    Item,
    LumaPushStatus,
    POEvent,
    POLine,
    POStatus,
    PurchaseOrder,
    User,
    ZohoMirror,
)
from packtrack.services.zoho_integration import (
    ReceivePayload,
    ZohoIntegrationConfigError,
    ZohoIntegrationGatewayError,
    ZohoIntegrationIdempotencyConflictError,
    ZohoIntegrationLiveWriteDisabledError,
    ZohoIntegrationNotConfiguredError,
    ZohoIntegrationRateLimitedError,
    ZohoIntegrationValidationError,
    commit_receive,
)

logger = logging.getLogger("packtrack.receiving")


# ---------------------------------------------------------------------------
# 0. Material-code resolution helper
# ---------------------------------------------------------------------------


def maybe_register_with_luma(item: Item) -> LumaRegistrationResult | None:
    """Fire registration when the item has BOTH material_code AND
    zoho_item_id; return ``None`` otherwise.

    Best-effort wrapper for trigger points other than the receive flow
    (item sync, ensure_material_code). Failures are logged inside the
    helper; this never raises.
    """
    if not (item.material_code and item.zoho_item_id):
        return None
    try:
        return register_item_with_luma(item)
    except Exception:  # never let registration crash a caller
        logger.exception(
            "Luma registration helper raised unexpectedly for item %s (material_code=%s)",
            item.id, item.material_code,
        )
        return None


def ensure_material_code(session: Session, item: Item) -> str | None:
    """Guarantee ``item.material_code`` is set — generating one if needed.

    Priority order:
    1. Already has ``material_code`` → return it, no DB write.
    2. Has ``sku_code`` not already claimed → promote ``sku_code``.
    3. Neither → generate deterministic ``PT-{id:05d}`` (stable per item).

    Writes back to the Item row and flushes so the caller's BoxReceipt
    snapshot picks up the new value.  Caller must commit.

    Side effect: when material_code is set for the first time AND the
    item has a zoho_item_id, the item is opportunistically registered
    with Luma so Luma can fill in/refresh its zoho_item_id without
    waiting for a receipt push. Best-effort — failures are logged, never
    raised, so the receive flow stays in control of its own error path.
    """
    if item.material_code:
        return item.material_code

    # Try sku_code first — preferred because it matches Zoho identity.
    if item.sku_code:
        collision = session.exec(
            select(Item)
            .where(Item.material_code == item.sku_code)
            .where(Item.id != item.id)
        ).first()
        if collision is None:
            item.material_code = item.sku_code
            session.add(item)
            session.flush()
            logger.info("material_code: item %s assigned from sku_code → %s", item.id, item.material_code)
            maybe_register_with_luma(item)
            return item.material_code
        # sku_code already claimed by another item — fall through.

    # Auto-generate: PT-{id:05d} is deterministic and stable per item id.
    generated = f"PT-{item.id:05d}"
    collision = session.exec(
        select(Item)
        .where(Item.material_code == generated)
        .where(Item.id != item.id)
    ).first()
    item.material_code = generated if collision is None else f"PT-{uuid.uuid4().hex[:8].upper()}"
    session.add(item)
    session.flush()
    logger.info("material_code: item %s auto-generated → %s", item.id, item.material_code)
    maybe_register_with_luma(item)
    return item.material_code


# ---------------------------------------------------------------------------
# 1. Adopt Zoho PO → PackTrack PurchaseOrder
# ---------------------------------------------------------------------------


def adopt_zoho_po(session: Session, mirror: ZohoMirror, created_by: User) -> PurchaseOrder:
    """Return the PackTrack PO linked to this Zoho mirror row, creating it if
    it does not exist yet.

    Lines are synced from ``mirror.line_items`` keyed on ``zoho_item_id``.
    Lines whose Zoho item_id cannot be found in the local items table are
    skipped (they are non-packaging items on a partially-packaging PO).
    """
    po = session.exec(
        select(PurchaseOrder).where(PurchaseOrder.zoho_po_id == mirror.zoho_purchaseorder_id)
    ).first()

    if po is None:
        po_number = (mirror.purchaseorder_number or mirror.zoho_purchaseorder_id)[:40]
        # Guard against duplicate po_number from a previous import attempt.
        existing = session.exec(
            select(PurchaseOrder).where(PurchaseOrder.po_number == po_number)
        ).first()
        if existing and existing.zoho_po_id is None:
            # Orphaned PackTrack PO with the same number — link it.
            existing.zoho_po_id = mirror.zoho_purchaseorder_id
            po = existing
        else:
            po = PurchaseOrder(
                po_number=po_number,
                status=POStatus.SHIPPED,
                zoho_po_id=mirror.zoho_purchaseorder_id,
                currency=mirror.currency_code or "USD",
                created_by_id=created_by.id,
            )
            session.add(po)
            session.flush()
            session.add(POEvent(
                po_id=po.id,
                kind="sync",
                message=f"Adopted from Zoho ({mirror.zoho_purchaseorder_id}) at receiving time.",
                actor_id=created_by.id,
            ))

    # Sync lines from ZohoMirror — adds missing, never removes existing.
    existing_item_ids = {line.item_id for line in po.lines}
    for li in (mirror.line_items or []):
        zoho_item_id = str(li.get("item_id") or "")
        if not zoho_item_id:
            continue
        item = session.exec(
            select(Item).where(Item.zoho_item_id == zoho_item_id)
        ).first()
        if item is None or item.id in existing_item_ids:
            continue
        session.add(POLine(
            po_id=po.id,
            item_id=item.id,
            quantity=float(li.get("quantity") or 0),
            unit_price=0.0,
        ))
        existing_item_ids.add(item.id)

    session.commit()
    session.refresh(po)
    return po


# ---------------------------------------------------------------------------
# 2. Submit purchase receives via zoho-integration-service
# ---------------------------------------------------------------------------


LineStatus = Literal[
    "committed",
    "blocked",            # 403 LIVE_WRITE_DISABLED — recorded locally, no Zoho write
    "validation_failed",  # 4xx with a typed code (ITEM_PO_MISMATCH, INSUFFICIENT_*, etc.)
    "config_error",       # service knows brand/org/product/credential is missing
    "auth_failed",        # 403 ZOHO_AUTH_FORBIDDEN
    "idempotency_conflict",
    "rate_limited",
    "gateway_error",
    "skipped",            # nothing to send (zero qty)
    "not_configured",     # client env not set — operator decision
    "disabled",           # off-switch flipped in settings
]


@dataclass(frozen=True)
class ZohoReceiveSubmission:
    """One Pack Track line being sent to zoho-integration-service.

    ``zoho_line_item_id`` is required by the service (400 PO_LINE_ITEM_NOT_FOUND
    otherwise) — callers must source it from the synced ZohoMirror, not guess.
    """

    box_receipt_id: int
    packtrack_receipt_id: str
    zoho_item_id: str
    zoho_line_item_id: str
    quantity: float
    item_name: str  # used for log/event messages, not sent in the payload


@dataclass(frozen=True)
class ZohoReceiveResult:
    submission: ZohoReceiveSubmission
    status: LineStatus
    message: str | None
    error_code: str | None = None  # service error code (ITEM_PO_MISMATCH, …)
    response: dict | None = None   # parsed service response on success

    @property
    def ok(self) -> bool:
        return self.status == "committed"

    @property
    def blocked(self) -> bool:
        """True iff the line did not fail per se but no Zoho write happened
        (LIVE_WRITE_DISABLED / disabled). Receipt remains in Pack Track."""
        return self.status in ("blocked", "disabled", "not_configured")


def submit_zoho_receives(
    mirror: ZohoMirror,
    submissions: list[ZohoReceiveSubmission],
    *,
    operator: User,
    session_id: str,
    notes: str | None = None,
) -> list[ZohoReceiveResult]:
    """Commit each ``ZohoReceiveSubmission`` through zoho-integration-service.

    One HTTP call per submission; the service is authoritative for ordering
    and idempotency. Returns per-line results — caller writes POEvents and
    updates the receiving response template from them.

    Never raises — every failure is captured in a ``ZohoReceiveResult`` so
    Pack Track's local state (BoxReceipt rows) stays consistent regardless
    of service availability.
    """
    if not submissions:
        return []
    if not settings.ZOHO_INTEGRATION_RECEIVE_ENABLED:
        return [
            ZohoReceiveResult(
                submission=s,
                status="disabled",
                message="Zoho integration receive submission is disabled.",
            )
            for s in submissions
        ]
    if not settings.zoho_integration_configured:
        return [
            ZohoReceiveResult(
                submission=s,
                status="not_configured",
                message="Zoho integration service is not configured.",
            )
            for s in submissions
        ]

    today = date.today().isoformat()
    results: list[ZohoReceiveResult] = []

    # One client for the whole batch — pools the TCP connection across lines.
    with httpx.Client(timeout=settings.ZOHO_INTEGRATION_TIMEOUT_SECONDS) as client:
        for sub in submissions:
            if sub.quantity <= 0:
                results.append(
                    ZohoReceiveResult(
                        submission=sub, status="skipped", message="Zero quantity.",
                    )
                )
                continue
            if not sub.zoho_line_item_id:
                # Without this Zoho can't map the line — fail loudly so the
                # operator knows the mirror needs to be re-synced.
                results.append(
                    ZohoReceiveResult(
                        submission=sub,
                        status="validation_failed",
                        error_code="PO_LINE_ITEM_NOT_FOUND",
                        message=(
                            "Mirror has no Zoho line_item_id for this item — "
                            "re-run Zoho sync to refresh, then retry."
                        ),
                    )
                )
                continue

            payload = ReceivePayload(
                pack_track_receipt_id=sub.packtrack_receipt_id,
                purchaseorder_id=mirror.zoho_purchaseorder_id,
                purchaseorder_line_item_id=sub.zoho_line_item_id,
                item_id=sub.zoho_item_id,
                received_quantity=sub.quantity,
                received_date=today,
                notes=notes,
                pack_track_operator_id=str(operator.id) if operator.id is not None else None,
                pack_track_workflow_session_id=session_id,
            )
            try:
                body = commit_receive(payload, client=client)
            except ZohoIntegrationLiveWriteDisabledError as e:
                results.append(
                    ZohoReceiveResult(
                        submission=sub, status="blocked", message=str(e) or
                        "Live writes disabled on zoho-integration-service.",
                    )
                )
            except ZohoIntegrationIdempotencyConflictError as e:
                logger.error(
                    "zoho_receive: idempotency conflict for receipt %s — "
                    "Pack Track id was reused with different payload",
                    sub.packtrack_receipt_id,
                )
                results.append(
                    ZohoReceiveResult(
                        submission=sub,
                        status="idempotency_conflict",
                        message=str(e) or "Idempotency conflict.",
                    )
                )
            except ZohoIntegrationValidationError as e:
                results.append(
                    ZohoReceiveResult(
                        submission=sub,
                        status="validation_failed",
                        error_code=e.code,
                        message=e.detail,
                    )
                )
            except ZohoIntegrationConfigError as e:
                results.append(
                    ZohoReceiveResult(
                        submission=sub,
                        status="config_error",
                        error_code=e.code,
                        message=e.detail,
                    )
                )
            except ZohoIntegrationRateLimitedError as e:
                results.append(
                    ZohoReceiveResult(
                        submission=sub, status="rate_limited", message=str(e),
                    )
                )
            except ZohoIntegrationNotConfiguredError as e:
                results.append(
                    ZohoReceiveResult(
                        submission=sub, status="not_configured", message=str(e),
                    )
                )
            except ZohoIntegrationGatewayError as e:
                logger.warning(
                    "zoho_receive: gateway error for receipt %s: %s",
                    sub.packtrack_receipt_id, e,
                )
                results.append(
                    ZohoReceiveResult(
                        submission=sub, status="gateway_error", message=str(e),
                    )
                )
            else:
                results.append(
                    ZohoReceiveResult(
                        submission=sub,
                        status="committed",
                        message=None,
                        response=body if isinstance(body, dict) else None,
                    )
                )
    return results


# ---------------------------------------------------------------------------
# 3. Push one BoxReceipt to Luma
# ---------------------------------------------------------------------------


def push_luma_receipt(
    box: BoxReceipt,
    po_number: str,
    photo_urls: list[str],
    *,
    received_by: str = "",
    dry_run: bool = False,
) -> tuple[bool, str | None, dict | None]:
    """POST one BoxReceipt to the Luma webhook.

    Returns ``(ok, error_message, response_body)``.
    Photos are passed in the free-form ``payload`` field so Luma can store
    them without a schema change on their side.
    """
    if not settings.LUMA_RECEIPT_WEBHOOK_URL:
        return False, "LUMA_RECEIPT_WEBHOOK_URL not configured.", None

    payload = {
        "source_system": "PACKTRACK",
        "packtrack_po_id": po_number,
        "packtrack_receipt_id": box.packtrack_receipt_id,
        "material_code": box.material_code or "",
        "material_name": box.material_name,
        "supplier": box.supplier,
        "supplier_lot_number": box.supplier_lot_number,
        "box_number": box.box_number,
        "declared_quantity": int(box.declared_quantity),
        "counted_quantity": int(box.counted_quantity) if box.counted_quantity is not None else None,
        "unit_of_measure": box.unit_of_measure,
        "received_at": box.received_at.isoformat() + "Z",
        "received_by": received_by or None,
        **({"payload": {"photo_urls": photo_urls}} if photo_urls else {}),
    }
    headers = {
        "Content-Type": "application/json",
        "x-packtrack-secret": settings.LUMA_PACKTRACK_SECRET,
    }
    if dry_run:
        headers["x-packtrack-dry-run"] = "true"

    try:
        with httpx.Client(timeout=20.0) as client:
            r = client.post(settings.LUMA_RECEIPT_WEBHOOK_URL, json=payload, headers=headers)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code >= 400:
            return False, f"HTTP {r.status_code}: {body.get('error', r.text[:200])}", body
        return True, None, body
    except Exception as e:
        logger.exception("Luma push failed for receipt %s", box.packtrack_receipt_id)
        return False, str(e)[:500], None


def build_photo_url(filename: str) -> str:
    """Public URL for a photo stored under uploads/receiving/."""
    base = (settings.APP_BASE_URL or "").rstrip("/")
    return f"{base}/uploads/receiving/{filename}"


# ---------------------------------------------------------------------------
# 4. Luma material pre-registration
# ---------------------------------------------------------------------------


def _infer_luma_kind(item_name: str) -> str:
    """Derive Luma ``packaging_material.kind`` from the item name.

    Luma enum values (packaging_material_kind): BLISTER_CARD, DISPLAY, CASE,
    LABEL, INSERT, BOTTLE, CAP, INDUCTION_SEAL, HEAT_SEAL_FILM, BLISTER_FOIL,
    DESICCANT, COTTON, OTHER.
    """
    n = (item_name or "").lower()
    if "blister" in n:
        return "BLISTER_CARD"
    if "display" in n:
        return "DISPLAY"
    if "case" in n:
        return "CASE"
    if "label" in n:
        return "LABEL"
    if "insert" in n:
        return "INSERT"
    if "bottle" in n:
        return "BOTTLE"
    if "cap" in n:
        return "CAP"
    if "seal" in n:
        return "INDUCTION_SEAL"
    return "OTHER"


class LumaRegistrationOutcome(StrEnum):
    """Result of a single Luma item-registration call.

    Mirrors the ``outcome`` field returned by Luma's
    ``/api/integrations/packtrack/items`` endpoint, plus the local
    short-circuit cases (skipped/failed) so callers can log every code
    path uniformly.
    """

    REGISTERED = "registered"                     # 201 — new material + mapping
    UPDATED = "updated"                            # 200 — backfilled zoho_item_id
    ALREADY_MAPPED = "already_mapped"              # 200 — no-op
    CONFLICT = "conflict"                          # 409 — incoming zoho_item_id ≠ existing
    SKIPPED_NO_CONFIG = "skipped_no_config"        # Luma webhook/secret not set
    SKIPPED_NO_MATERIAL_CODE = "skipped_no_material_code"
    FAILED = "failed"                              # transport / 5xx / parse


@dataclass(frozen=True)
class LumaRegistrationResult:
    outcome: LumaRegistrationOutcome
    luma_material_id: str | None = None
    status_code: int | None = None
    message: str | None = None
    existing_zoho_item_id: str | None = None  # populated on CONFLICT for audit
    incoming_zoho_item_id: str | None = None  # populated on CONFLICT for audit

    @property
    def ok(self) -> bool:
        return self.outcome in (
            LumaRegistrationOutcome.REGISTERED,
            LumaRegistrationOutcome.UPDATED,
            LumaRegistrationOutcome.ALREADY_MAPPED,
        )

    @property
    def needs_review(self) -> bool:
        return self.outcome is LumaRegistrationOutcome.CONFLICT


def register_item_with_luma(
    item: Item,
    *,
    client: httpx.Client | None = None,
) -> LumaRegistrationResult:
    """Register ``item.material_code`` with Luma, including ``zoho_item_id``
    when present, and return a structured outcome.

    Calls ``POST {luma_base}/api/integrations/packtrack/items``. Luma's
    endpoint upserts a ``packaging_materials`` row (sku = material_code),
    fills ``zoho_item_id`` when it was previously NULL, and surfaces
    ``ZOHO_ID_CONFLICT_REVIEW_REQUIRED`` if an incoming id contradicts an
    existing non-NULL one.

    Idempotent — safe to call on every receive, every Zoho item sync,
    and from the backfill script.
    """
    if not settings.LUMA_RECEIPT_WEBHOOK_URL or not settings.LUMA_PACKTRACK_SECRET:
        return LumaRegistrationResult(
            outcome=LumaRegistrationOutcome.SKIPPED_NO_CONFIG,
            message="Luma not configured (LUMA_RECEIPT_WEBHOOK_URL/SECRET missing).",
        )
    if not item.material_code:
        return LumaRegistrationResult(
            outcome=LumaRegistrationOutcome.SKIPPED_NO_MATERIAL_CODE,
            message=f"Item {item.id} has no material_code to register.",
        )

    items_url = settings.LUMA_RECEIPT_WEBHOOK_URL.rsplit("/", 1)[0] + "/items"
    payload = {
        "material_code": item.material_code,
        "material_name": (item.name or item.material_code)[:240],
        "kind": _infer_luma_kind(item.name or ""),
        "unit_of_measure": (item.unit or "each"),
        **({"zoho_item_id": item.zoho_item_id} if item.zoho_item_id else {}),
    }
    headers = {
        "Content-Type": "application/json",
        "x-packtrack-secret": settings.LUMA_PACKTRACK_SECRET,
    }

    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=20.0)
    try:
        try:
            r = client.post(items_url, json=payload, headers=headers)
        except httpx.HTTPError as e:
            logger.warning(
                "Luma item registration network error for %s: %s",
                item.material_code, e,
            )
            return LumaRegistrationResult(
                outcome=LumaRegistrationOutcome.FAILED,
                message=f"network error: {e}",
            )
    finally:
        if owns_client:
            client.close()

    body: dict = {}
    if r.headers.get("content-type", "").startswith("application/json"):
        try:
            body = r.json() or {}
        except ValueError:
            body = {}

    # Conflict — Luma already has a different zoho_item_id for this code.
    # NEVER auto-overwritten on either side; surfaced for operator review.
    if r.status_code == 409 or body.get("outcome") == "ZOHO_ID_CONFLICT_REVIEW_REQUIRED":
        logger.warning(
            "Luma zoho_item_id CONFLICT for material_code=%s — existing=%r incoming=%r",
            item.material_code,
            body.get("existing_zoho_item_id"),
            body.get("incoming_zoho_item_id"),
        )
        return LumaRegistrationResult(
            outcome=LumaRegistrationOutcome.CONFLICT,
            status_code=r.status_code,
            luma_material_id=body.get("luma_material_id"),
            message=body.get("error") or "Zoho id conflict, review required.",
            existing_zoho_item_id=body.get("existing_zoho_item_id"),
            incoming_zoho_item_id=body.get("incoming_zoho_item_id"),
        )

    if r.status_code >= 400:
        msg = f"HTTP {r.status_code}: {body.get('error', r.text[:200])}"
        logger.warning("Luma item registration failed for %s: %s", item.material_code, msg)
        return LumaRegistrationResult(
            outcome=LumaRegistrationOutcome.FAILED,
            status_code=r.status_code,
            message=msg,
        )

    # 2xx: trust Luma's explicit outcome field; fall back to the legacy
    # ``created`` flag for older deploys that don't return ``outcome``.
    outcome_raw = (body.get("outcome") or "").upper()
    if outcome_raw == "REGISTERED":
        outcome = LumaRegistrationOutcome.REGISTERED
    elif outcome_raw == "UPDATED":
        outcome = LumaRegistrationOutcome.UPDATED
    elif outcome_raw == "ALREADY_MAPPED":
        outcome = LumaRegistrationOutcome.ALREADY_MAPPED
    elif body.get("created") is True:
        outcome = LumaRegistrationOutcome.REGISTERED
    else:
        outcome = LumaRegistrationOutcome.ALREADY_MAPPED

    logger.info(
        "Luma item %s -> %s (luma_id=%s, zoho_item_id=%s)",
        item.material_code, outcome.value,
        body.get("luma_material_id"), item.zoho_item_id or "(none)",
    )
    return LumaRegistrationResult(
        outcome=outcome,
        status_code=r.status_code,
        luma_material_id=body.get("luma_material_id"),
    )


def register_material_with_luma(item: Item) -> tuple[bool, str | None]:
    """Back-compat shim — receiving routes use this tuple signature.

    Prefer :func:`register_item_with_luma` in new code; it surfaces
    UPDATED / CONFLICT outcomes the tuple form folds away.
    """
    r = register_item_with_luma(item)
    if r.ok:
        return True, None
    return False, r.message or r.outcome.value
