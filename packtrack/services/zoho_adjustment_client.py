"""zoho-integration-service v1.34.0 adjustment client (v2.10.0).

This is the ONLY module in PackTrack that may make an HTTP call related
to inventory adjustments. It hits exactly one endpoint:

    POST {ZOHO_INTEGRATION_BASE_URL}/zoho/pack_track/items/{zoho_item_id}/inventory-adjustments

It carries Bearer + X-Brand + Idempotency-Key headers, sends Decimal
quantities as 4-decimal-place strings (never floats), and never sends
item master-data fields (vendor, price, sku, account, tags, category,
stock_override).

PackTrack v2.10.0 still NEVER calls Zoho directly. The integration
service is the single seam through which any Zoho write travels.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

import httpx

from packtrack.config import settings
from packtrack.models import InventoryAdjustment, Item, ZohoSyncStatus

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Outcome shape
# ---------------------------------------------------------------------------


class OutcomeKind(StrEnum):
    """How the sync attempt resolved. Maps 1:1 to ZohoSyncStatus
    values that the orchestrator then writes to the row."""

    SYNCED = "synced"
    SYNCED_IDEMPOTENT = "synced_idempotent"  # service responded "already done"
    FAILED = "failed"
    SKIPPED = "skipped"  # item has no zoho_item_id, etc.
    NOT_CONFIGURED = "not_configured"


@dataclass(frozen=True)
class SyncOutcome:
    kind: OutcomeKind
    zoho_adjustment_id: str | None = None
    zoho_reference: str | None = None
    warning: str | None = None  # e.g. STOCK_DRIFT_DETECTED
    error_message: str | None = None  # truncated, safe to show in UI
    http_status: int | None = None
    raw_response: dict[str, Any] | None = None  # for tests / debug

    def to_status(self) -> ZohoSyncStatus:
        return {
            OutcomeKind.SYNCED: ZohoSyncStatus.SYNCED,
            OutcomeKind.SYNCED_IDEMPOTENT: ZohoSyncStatus.SYNCED,
            OutcomeKind.FAILED: ZohoSyncStatus.FAILED,
            OutcomeKind.SKIPPED: ZohoSyncStatus.SKIPPED,
            OutcomeKind.NOT_CONFIGURED: ZohoSyncStatus.NOT_CONFIGURED,
        }[self.kind]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_ERROR_MESSAGE_MAX = 500


def _truncate_error(msg: str) -> str:
    """Truncate so a verbose 500-line traceback can't bloat the DB row
    or smuggle secrets via a returned token-like header."""
    if not msg:
        return ""
    msg = msg.strip()
    if len(msg) > _ERROR_MESSAGE_MAX:
        return msg[: _ERROR_MESSAGE_MAX - 1] + "…"
    return msg


def _decimal_str(value: Decimal) -> str:
    """4-decimal-place string, matching the service contract.

    Service v1.34.0 parses these as Decimal too — we send strings so
    no float drift can creep in over JSON."""
    # ``Decimal('1') * Decimal('1.0000')`` keeps the scale we want
    # without changing the value; quantize is the explicit form.
    return f"{Decimal(value).quantize(Decimal('0.0001')):f}"


def _iso_utc(ts: datetime) -> str:
    """ISO-8601 UTC, always with a Z suffix."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC).isoformat().replace("+00:00", "Z")


def build_payload(adjustment: InventoryAdjustment, *, created_by: str) -> dict[str, Any]:
    """The exact JSON body the service expects.

    Intentionally narrow — NO master-data fields, NO account / vendor /
    price / SKU / tags / category / stock_override. A future contributor
    extending this dict must add the field to the service's accept-list
    first; the spec for the service is explicit about which fields it
    accepts."""
    return {
        "adjustment_number": adjustment.adjustment_number,
        "idempotency_key": adjustment.idempotency_key,
        "mode": adjustment.mode.value,
        "quantity_before": _decimal_str(adjustment.quantity_before),
        "quantity_delta": _decimal_str(adjustment.quantity_delta),
        "quantity_after": _decimal_str(adjustment.quantity_after),
        "reason_code": adjustment.reason_code.value,
        "notes": adjustment.notes or "",
        "source": adjustment.source.value,
        "created_by": created_by,
        "created_at": _iso_utc(adjustment.created_at),
    }


def is_configured() -> bool:
    """True only when all three settings are populated AND the operator
    has explicitly enabled adjustment sync."""
    return bool(
        getattr(settings, "ZOHO_INTEGRATION_ADJUST_ENABLED", False)
        and (getattr(settings, "ZOHO_INTEGRATION_BASE_URL", "") or "").strip()
        and (getattr(settings, "ZOHO_INTEGRATION_APP_TOKEN", "") or "").strip()
        and (getattr(settings, "ZOHO_INTEGRATION_BRAND", "") or "").strip()
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def push_adjustment_to_zoho(
    adjustment: InventoryAdjustment,
    item: Item,
    *,
    created_by: str,
    http_client: httpx.Client | None = None,
) -> SyncOutcome:
    """Push one adjustment row through zoho-integration-service.

    Returns a ``SyncOutcome`` — the caller (orchestrator) decides what
    to persist on the row. Never raises for an upstream HTTP error;
    only raises for genuine programmer bugs (e.g. passing an item with
    a different id than the adjustment references).

    Why no auto-retry here: the integration service is idempotent on
    ``Idempotency-Key``, but a 5xx / timeout / 409 each have different
    operational meanings. We surface the FAILED outcome with the
    error message and let the operator (or a future cron) hit the
    retry route. That keeps the request path deterministic.
    """
    if adjustment.item_id != item.id:
        raise ValueError(
            f"Item id mismatch: adjustment.item_id={adjustment.item_id} "
            f"vs item.id={item.id}"
        )

    if not is_configured():
        return SyncOutcome(kind=OutcomeKind.NOT_CONFIGURED)

    if not (item.zoho_item_id or "").strip():
        return SyncOutcome(
            kind=OutcomeKind.SKIPPED,
            error_message=(
                "Item has no zoho_item_id — re-sync the item from "
                "Settings → Sync, then retry."
            ),
        )

    base_url = settings.ZOHO_INTEGRATION_BASE_URL.rstrip("/")
    url = (
        f"{base_url}/zoho/pack_track/items/"
        f"{item.zoho_item_id}/inventory-adjustments"
    )
    headers = {
        "Authorization": f"Bearer {settings.ZOHO_INTEGRATION_APP_TOKEN}",
        "X-Brand": settings.ZOHO_INTEGRATION_BRAND,
        "Idempotency-Key": adjustment.idempotency_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = build_payload(adjustment, created_by=created_by)
    timeout = float(getattr(settings, "ZOHO_INTEGRATION_TIMEOUT_SECONDS", 30.0))

    # Never log the Bearer token; log only the payload + the URL.
    log.info(
        "adjustment.sync.attempt adjustment=%s item_zoho_id=%s",
        adjustment.adjustment_number, item.zoho_item_id,
    )

    own_client = http_client is None
    client = http_client or httpx.Client(timeout=timeout)
    try:
        try:
            resp = client.post(url, headers=headers, json=payload, timeout=timeout)
        except httpx.TimeoutException as exc:
            return SyncOutcome(
                kind=OutcomeKind.FAILED,
                error_message=_truncate_error(f"Timeout calling integration service: {exc}"),
            )
        except httpx.HTTPError as exc:
            return SyncOutcome(
                kind=OutcomeKind.FAILED,
                error_message=_truncate_error(f"Network error: {exc}"),
            )
    finally:
        if own_client:
            client.close()

    return _interpret_response(resp)


def _interpret_response(resp: httpx.Response) -> SyncOutcome:
    """Map an HTTP response into a SyncOutcome.

    Status mapping per the spec:
      200 ok=true                       → SYNCED
      200 ok=true + meta.idempotent     → SYNCED (idempotent replay)
      200 ok=true + STOCK_DRIFT_DETECTED→ SYNCED + warning
      401                               → FAILED (auth)
      403                               → FAILED (capability)
      404                               → FAILED (item not found upstream)
      409                               → FAILED (idempotency conflict — do NOT auto-retry)
      422                               → FAILED (validation)
      5xx / other                       → FAILED with retry visible
    """
    try:
        body = resp.json() if resp.content else {}
    except ValueError:
        body = {}

    meta = body.get("meta") or {} if isinstance(body, dict) else {}

    if resp.status_code == 200 and isinstance(body, dict) and body.get("ok") is True:
        warning = _extract_warning(body)
        idempotent = bool(meta.get("idempotent"))
        return SyncOutcome(
            kind=OutcomeKind.SYNCED_IDEMPOTENT if idempotent else OutcomeKind.SYNCED,
            zoho_adjustment_id=str(body.get("zoho_adjustment_id") or "") or None,
            zoho_reference=str(body.get("zoho_reference") or "") or None,
            warning=warning,
            http_status=200,
            raw_response=body,
        )

    # Failure paths — extract any service-supplied message for the UI.
    error_msg = _extract_error_message(resp, body)
    return SyncOutcome(
        kind=OutcomeKind.FAILED,
        error_message=_truncate_error(error_msg),
        http_status=resp.status_code,
        raw_response=body if isinstance(body, dict) else None,
    )


def _extract_warning(body: dict[str, Any]) -> str | None:
    """Walk the typical shapes that v1.34.0 may emit for a warning.

    Tries (in order):
      * top-level ``warning`` (string)
      * ``meta.warning``
      * ``meta.warnings`` (list)
      * top-level ``warnings``
    """
    warning = body.get("warning")
    if isinstance(warning, str) and warning.strip():
        return warning.strip()
    meta = body.get("meta") or {}
    if isinstance(meta, dict):
        single = meta.get("warning")
        if isinstance(single, str) and single.strip():
            return single.strip()
        many = meta.get("warnings")
        if isinstance(many, list) and many:
            return "; ".join(str(w).strip() for w in many if str(w).strip())
    many_top = body.get("warnings")
    if isinstance(many_top, list) and many_top:
        return "; ".join(str(w).strip() for w in many_top if str(w).strip())
    return None


def _extract_error_message(resp: httpx.Response, body: Any) -> str:
    """Prefer the service's structured ``error.message`` when present.

    Falls back to ``error`` (string), the raw body text, or a
    ``HTTP <code>`` line. Never returns the request headers or any
    settings — those are the only places a token could leak.
    """
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("detail") or err.get("code")
            if msg:
                return f"HTTP {resp.status_code}: {msg}"
        if isinstance(err, str) and err.strip():
            return f"HTTP {resp.status_code}: {err.strip()}"
        detail = body.get("detail")
        if isinstance(detail, str) and detail.strip():
            return f"HTTP {resp.status_code}: {detail.strip()}"
    text = (resp.text or "").strip()
    if text:
        return f"HTTP {resp.status_code}: {text[:200]}"
    return f"HTTP {resp.status_code}"
