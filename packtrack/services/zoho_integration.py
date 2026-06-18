"""Client for the zoho-integration-service Pack Track receive endpoints.

Pack Track never calls Zoho directly for receive writes. Every purchase-receive
that lands in Zoho must go through these endpoints. The service owns Zoho
credentials, rate-limit handling, and the ``ENABLE_LIVE_INVENTORY_WRITES`` gate.

Two operations:

* ``preview_receive`` — read-only preflight. Validates the payload against
  live PO state and returns the would-be Zoho body. Never writes.
* ``commit_receive`` — writes the receive through the service when live
  writes are enabled. Returns ``403 LIVE_WRITE_DISABLED`` when they are not,
  which the caller treats as "blocked" (the receipt stays in Pack Track,
  no Zoho write happens, no retry storm).

Auth: bearer app token + ``X-Brand``. The legacy ``X-Internal-Token`` header
is **not** valid here — sending it would return 401/403.

Idempotency: ``Idempotency-Key: PACK_TRACK_RECEIVE_<pack_track_receipt_id>``
is sent on every request so the same Pack Track receipt cannot create a
duplicate Zoho purchase receive even on retry.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from packtrack.config import settings

logger = logging.getLogger("packtrack.zoho_integration")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ZohoIntegrationError(Exception):
    """Base — every failure path raises a subclass of this."""


class ZohoIntegrationNotConfiguredError(ZohoIntegrationError):
    """One of ZOHO_INTEGRATION_BASE_URL / _APP_TOKEN / _BRAND is missing."""


class ZohoIntegrationLiveWriteDisabledError(ZohoIntegrationError):
    """403 LIVE_WRITE_DISABLED — service is in dry-run mode by ops decision."""


class ZohoIntegrationAuthError(ZohoIntegrationError):
    """403 ZOHO_AUTH_FORBIDDEN — service was reached but Zoho rejected auth."""


@dataclass
class ZohoIntegrationValidationError(ZohoIntegrationError):
    """4xx (other than auth/idempotency) with a structured error code.

    Wraps codes like ITEM_PO_MISMATCH, INSUFFICIENT_PO_REMAINING,
    PO_LINE_ITEM_NOT_FOUND, BRAND_REQUIRED, etc.
    """

    code: str
    detail: str
    status_code: int

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


@dataclass
class ZohoIntegrationConfigError(ZohoIntegrationError):
    """404 — service knows the brand/org/product/credential is misconfigured.

    Distinct from validation because operator/admin needs to fix it on the
    *service* side, not retry with a different payload.
    """

    code: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


class ZohoIntegrationIdempotencyConflictError(ZohoIntegrationError):
    """409 — the Idempotency-Key was reused with a different payload.

    Surfaced loudly because it almost always indicates a data-consistency
    bug (a Pack Track receipt id was reused with different line data).
    """


class ZohoIntegrationRateLimitedError(ZohoIntegrationError):
    """429 — caller should back off; not retried inside this client."""


class ZohoIntegrationGatewayError(ZohoIntegrationError):
    """5xx, network error, or otherwise opaque failure from the service."""


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReceivePayload:
    """One line item of a purchase receive — preview and commit take this shape."""

    pack_track_receipt_id: str
    purchaseorder_id: str
    purchaseorder_line_item_id: str
    item_id: str
    received_quantity: float
    received_date: str  # ISO YYYY-MM-DD
    warehouse_id: str | None = None
    notes: str | None = None
    pack_track_operator_id: str | None = None
    pack_track_workflow_session_id: str | None = None

    def as_body(self) -> dict[str, Any]:
        """Serialize to the JSON body the service expects.

        ``None`` values are kept as ``null`` because the service treats
        ``"warehouse_id": null`` as "no warehouse override". Empty strings
        are dropped to avoid spurious validation hits on optional fields.
        """
        body = asdict(self)
        return {k: v for k, v in body.items() if v != ""}


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


_PREVIEW_PATH = "/zoho/pack_track/receive/preview"
_COMMIT_PATH = "/zoho/pack_track/receive/commit"


def _require_config() -> None:
    if not settings.zoho_integration_configured:
        raise ZohoIntegrationNotConfiguredError(
            "ZOHO_INTEGRATION_BASE_URL / ZOHO_INTEGRATION_APP_TOKEN / "
            "ZOHO_INTEGRATION_BRAND must be set."
        )


def _headers(payload: ReceivePayload) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.ZOHO_INTEGRATION_APP_TOKEN}",
        "X-Brand": settings.ZOHO_INTEGRATION_BRAND,
        "Idempotency-Key": f"PACK_TRACK_RECEIVE_{payload.pack_track_receipt_id}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _url(path: str) -> str:
    return settings.ZOHO_INTEGRATION_BASE_URL.rstrip("/") + path


def _parse_error(body: Any, fallback: str) -> tuple[str, str]:
    """Pull ``(code, detail)`` out of a service error body.

    The service returns ``{"error": "CODE", "detail": "human text"}`` for
    typed errors. Falls back to ``UNKNOWN`` / raw text when the body is not
    a dict (e.g. an HTML 502 from a reverse proxy).
    """
    if isinstance(body, dict):
        code = str(body.get("error") or body.get("code") or "UNKNOWN")
        detail = str(body.get("detail") or body.get("message") or fallback)
        return code, detail
    return "UNKNOWN", fallback or "no body"


def _raise_for_status(resp: httpx.Response) -> None:
    """Translate HTTP status + service error body into a typed exception.

    On 2xx, returns silently. On any 4xx/5xx, raises the matching subclass.
    """
    if resp.status_code < 400:
        return

    try:
        body = resp.json()
    except ValueError:
        body = None
    code, detail = _parse_error(body, resp.text[:300] if resp.text else "")

    sc = resp.status_code

    if sc == 403 and code == "LIVE_WRITE_DISABLED":
        raise ZohoIntegrationLiveWriteDisabledError(detail or "Live writes disabled.")
    if sc == 403 and code == "ZOHO_AUTH_FORBIDDEN":
        raise ZohoIntegrationAuthError(detail or "Zoho auth forbidden.")
    if sc == 409:
        raise ZohoIntegrationIdempotencyConflictError(detail or "Idempotency conflict.")
    if sc == 429:
        raise ZohoIntegrationRateLimitedError(detail or "Rate limited.")
    if sc == 404 and code in {
        "BRAND_NOT_FOUND",
        "ORG_NOT_CONFIGURED",
        "PRODUCT_NOT_CONFIGURED",
        "CREDENTIAL_NOT_FOUND",
    }:
        raise ZohoIntegrationConfigError(code=code, detail=detail)
    if 400 <= sc < 500:
        raise ZohoIntegrationValidationError(code=code, detail=detail, status_code=sc)
    raise ZohoIntegrationGatewayError(f"HTTP {sc} {code}: {detail}")


def _post(
    path: str,
    payload: ReceivePayload,
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Execute a single POST. Returns parsed JSON; raises a typed error otherwise."""
    _require_config()
    url = _url(path)
    body = payload.as_body()
    headers = _headers(payload)

    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=settings.ZOHO_INTEGRATION_TIMEOUT_SECONDS)
    try:
        try:
            resp = client.post(url, json=body, headers=headers)
        except httpx.HTTPError as e:
            logger.warning(
                "zoho_integration: network error calling %s for %s: %s",
                path, payload.pack_track_receipt_id, e,
            )
            raise ZohoIntegrationGatewayError(f"network error: {e}") from e
    finally:
        if owns_client:
            client.close()

    _raise_for_status(resp)
    try:
        return resp.json()
    except ValueError as e:
        raise ZohoIntegrationGatewayError(f"non-JSON response: {resp.text[:300]}") from e


def preview_receive(
    payload: ReceivePayload,
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Validate + preflight. Never writes to Zoho. Returns the service's preview body."""
    return _post(_PREVIEW_PATH, payload, client=client)


def commit_receive(
    payload: ReceivePayload,
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Commit the purchase receive through the service.

    Raises ``ZohoIntegrationLiveWriteDisabledError`` while the service is in
    dry-run mode — caller should treat that as a non-failure "blocked"
    state and not retry until ops enables live writes.
    """
    return _post(_COMMIT_PATH, payload, client=client)
