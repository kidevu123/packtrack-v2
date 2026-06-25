"""Outbound item-update sync (PackTrack -> zoho-integration-service).

Boundary rule
-------------
PackTrack never calls Zoho directly. Item writes go through the
``zoho-integration-service`` PackTrack item endpoints (CT 9503, v1.30.0+):

* ``GET   /zoho/pack_track/items/{item_id}``
* ``GET   /zoho/pack_track/items/list``
* ``PATCH /zoho/pack_track/items/{item_id}``

Only the service's writable allowlist is ever sent: ``name``, ``description``,
``unit``. The service does **not** support free-text vendor writes (a vendor
PATCH returns ``422 VENDOR_UPDATE_NOT_SUPPORTED``), so vendor is treated as
Zoho-read-only in PackTrack and is never included in an outbound payload.

State machine on ``Item``
-------------------------
* ``synced``  — a PATCH succeeded; local name/description/unit match Zoho.
* ``failed``  — a PATCH was attempted and the service returned 4xx/5xx (or a
  network error). The local edit is kept; the error is stored truncated and
  the owner can retry from the detail page.
* ``pending`` — the edit is saved locally but no push happened yet because the
  integration service is not configured, or the item has no ``zoho_item_id``
  (a manual/local-only item). Pending edits to Zoho-owned fields are protected
  from inbound-sync clobber (see ``zoho.sync_items``). ``pending`` is NOT a
  failure.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from sqlmodel import Session

from packtrack.config import settings
from packtrack.models import Item

logger = logging.getLogger("packtrack.zoho_item_sync")

# Zoho-owned fields an owner can edit in PackTrack and that we push outbound.
# Editing one of these is what makes an item "dirty" for outbound sync. This is
# exactly the service's PATCH writable allowlist. Vendor is intentionally NOT
# here: the service rejects vendor writes, so vendor is Zoho-read-only in
# PackTrack. PackTrack-owned fields (material_code, thresholds, lead days,
# daily usage) are never pushed and never change push state.
ZOHO_OWNED_EDITABLE_FIELDS: frozenset[str] = frozenset(
    {"name", "description", "unit"}
)

# The service's PATCH writable allowlist — the only keys we ever send.
_PATCH_FIELDS: tuple[str, ...] = ("name", "description", "unit")

_ITEMS_PATH = "/zoho/pack_track/items"

PUSH_PENDING = "pending"
PUSH_SYNCED = "synced"
PUSH_FAILED = "failed"


class ItemSyncError(Exception):
    """Any failure talking to the integration-service item endpoints."""


@dataclass
class ItemPushResult:
    """Outcome of an outbound item-update attempt.

    ``pending`` is NOT a failure — it means the edit is safely stored locally
    and is waiting for the integration service / a Zoho item id. The UI shows
    it as "Saved locally · Zoho sync pending".
    """

    status: str
    error: str | None = None

    @property
    def ok_local(self) -> bool:
        return self.status in (PUSH_SYNCED, PUSH_PENDING)


def item_write_path_available() -> bool:
    """Whether a safe Zoho item-update write path is wired and configured.

    True once the integration service is configured (base URL + app token +
    brand). All three are required; see ``settings.zoho_integration_configured``.
    """
    return settings.zoho_integration_configured


# ---------------------------------------------------------------------------
# HTTP plumbing (integration service item endpoints)
# ---------------------------------------------------------------------------


def _item_url(zoho_item_id: str) -> str:
    return f"{settings.ZOHO_INTEGRATION_BASE_URL.rstrip('/')}{_ITEMS_PATH}/{zoho_item_id}"


def _item_headers() -> dict[str, str]:
    # The PackTrack item endpoints authenticate with X-Internal-Token + X-Brand
    # (distinct from the receive endpoints, which use a bearer token).
    return {
        "X-Internal-Token": settings.ZOHO_INTEGRATION_APP_TOKEN,
        "X-Brand": settings.ZOHO_INTEGRATION_BRAND,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _format_http_error(resp: httpx.Response) -> str:
    """Build a compact, useful error string from a service error response."""
    code = ""
    detail = ""
    try:
        body: Any = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        detail_field = body.get("detail")
        if isinstance(detail_field, dict):
            inner = (
                detail_field.get("error")
                if isinstance(detail_field.get("error"), dict)
                else detail_field
            )
            code = str(inner.get("code") or inner.get("error") or "")
            detail = str(inner.get("message") or inner.get("detail") or "")
        else:
            code = str(body.get("error") or body.get("code") or "")
            detail = str(detail_field or body.get("message") or "")
    if not detail and resp.text:
        detail = resp.text[:300]
    parts = [f"HTTP {resp.status_code}"]
    if code:
        parts.append(code)
    msg = " ".join(parts)
    return f"{msg}: {detail}".strip().rstrip(":") if detail else msg


def _normalized_item(body: Any) -> dict[str, Any]:
    """Pull the normalized item dict out of a service response, best-effort."""
    if not isinstance(body, dict):
        return {}
    for key in ("item", "data", "pack_track_item"):
        nested = body.get(key)
        if isinstance(nested, dict):
            return nested
    return body


def _patch_item(
    zoho_item_id: str,
    payload: dict[str, Any],
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """PATCH the item through the integration service. Raises ItemSyncError."""
    url = _item_url(zoho_item_id)
    headers = _item_headers()
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=settings.ZOHO_INTEGRATION_TIMEOUT_SECONDS)
    try:
        try:
            resp = client.patch(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise ItemSyncError(f"network error: {exc}") from exc
    finally:
        if owns_client:
            client.close()
    if resp.status_code >= 400:
        raise ItemSyncError(_format_http_error(resp))
    try:
        return resp.json()
    except ValueError:
        return {}


def fetch_item(
    zoho_item_id: str,
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """GET the normalized item from the integration service (read-after-write)."""
    url = _item_url(zoho_item_id)
    headers = _item_headers()
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=settings.ZOHO_INTEGRATION_TIMEOUT_SECONDS)
    try:
        try:
            resp = client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise ItemSyncError(f"network error: {exc}") from exc
    finally:
        if owns_client:
            client.close()
    if resp.status_code >= 400:
        raise ItemSyncError(_format_http_error(resp))
    try:
        return _normalized_item(resp.json())
    except ValueError:
        return {}


def _align_from_service(item: Item, normalized: dict[str, Any]) -> None:
    """Align local Zoho-owned fields from a normalized service item.

    Only ever touches name/description/unit (the writable allowlist). Never
    touches PackTrack-owned fields (thresholds, material_code, stock, etc.).
    """
    if not normalized:
        return
    name = normalized.get("name")
    if isinstance(name, str) and name.strip():
        item.name = name[:240]
    if "description" in normalized:
        desc = normalized.get("description")
        item.description = (desc or "").strip()[:50000] or None
    unit = normalized.get("unit")
    if isinstance(unit, str) and unit.strip():
        item.unit = unit[:40]


def _outbound_payload(item: Item) -> dict[str, Any]:
    """Build the PATCH body from the writable allowlist (never vendor)."""
    return {
        "name": item.name,
        "description": item.description or "",
        "unit": item.unit,
    }


def push_item_update(
    session: Session,
    item: Item,
    *,
    client: httpx.Client | None = None,
) -> ItemPushResult:
    """Push an owner's item edit to Zoho via the integration service.

    Always records ``zoho_push_attempted_at`` and never raises:

    * No ``zoho_item_id`` (manual/local-only item) or service not configured
      → park ``pending`` (error cleared); the edit stays local and protected.
    * PATCH succeeds → ``synced``; optionally aligns name/description/unit from
      the service's normalized response (falling back to a GET when the PATCH
      body is empty). Never rolls back the local edit.
    * PATCH returns 4xx/5xx or a network error → ``failed`` with a truncated
      error; the local edit is kept so the owner can retry.
    """
    item.zoho_push_attempted_at = datetime.utcnow()

    if not item.zoho_item_id or not item_write_path_available():
        item.zoho_push_status = PUSH_PENDING
        item.zoho_push_error = None
        session.add(item)
        session.commit()
        reason = "no zoho_item_id" if not item.zoho_item_id else "service not configured"
        logger.info(
            "item %s edit saved locally; parked pending (%s)", item.id, reason
        )
        return ItemPushResult(PUSH_PENDING)

    payload = _outbound_payload(item)
    try:
        body = _patch_item(item.zoho_item_id, payload, client=client)
    except ItemSyncError as exc:
        item.zoho_push_status = PUSH_FAILED
        item.zoho_push_error = str(exc)[:1000]
        session.add(item)
        session.commit()
        logger.warning("item %s Zoho item PATCH failed: %s", item.id, exc)
        return ItemPushResult(PUSH_FAILED, item.zoho_push_error)

    # Read-after-write: prefer the PATCH response; if it carried no usable item
    # body, do a best-effort GET to confirm/align. A failed verification GET
    # must not flip an otherwise-successful write to "failed".
    normalized = _normalized_item(body)
    if not normalized.get("name"):
        try:
            normalized = fetch_item(item.zoho_item_id, client=client)
        except ItemSyncError as exc:
            logger.info("item %s post-PATCH verify GET failed (ignored): %s", item.id, exc)
            normalized = {}
    _align_from_service(item, normalized)

    item.zoho_push_status = PUSH_SYNCED
    item.zoho_push_error = None
    session.add(item)
    session.commit()
    logger.info("item %s synced to Zoho via integration service", item.id)
    return ItemPushResult(PUSH_SYNCED)


def mark_in_sync(session: Session, item: Item) -> None:
    """Clear outbound push state — used when local values now match Zoho."""
    if item.zoho_push_status is not None or item.zoho_push_error is not None:
        item.zoho_push_status = None
        item.zoho_push_error = None
        session.add(item)
        session.commit()
