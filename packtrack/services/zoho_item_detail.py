"""Read-only extended item detail + metadata reads (PackTrack -> integration service).

Phase A (v2.6.0): **display-only**. Fetches the richer Zoho item detail and the
item metadata (custom-field definitions, dropdown options, categories, reporting
tags, field policy) from the ``zoho-integration-service`` PackTrack item
endpoints (CT 9503, v1.31.0+):

* ``GET /zoho/pack_track/items/{item_id}``     — extended item detail
* ``GET /zoho/pack_track/items/metadata``      — field/option metadata

Boundary rule: PackTrack never calls Zoho directly. Auth is
``Authorization: Bearer <ZOHO_INTEGRATION_APP_TOKEN>`` + ``X-Brand`` (the same
scheme the receive / item-PATCH endpoints use).

Nothing here writes to Zoho and no custom-field PATCH is performed in this phase.
Every failure degrades gracefully: callers always get a usable (possibly empty)
view so the local PackTrack item detail still renders.

Naming caution: PackTrack's own ``Item.product_line`` is a *derived* browsing
group (FIX / FIX Beyond / Unassigned). Zoho's ``cf_product_line`` is a separate
custom-field dropdown (7OH / MIT A / MIT B). They are kept strictly distinct and
never merged here.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from packtrack.config import settings

logger = logging.getLogger("packtrack.zoho_item_detail")

_ITEMS_PATH = "/zoho/pack_track/items"
_DEFAULT_METADATA_TTL = 3600

# Preferred display order for packaging custom fields. The service decides what
# actually exists; this only orders the most useful fields near the top. Any
# field the service returns that is not listed here is appended afterwards.
_CF_ORDER: tuple[str, ...] = (
    "cf_item_type",
    "cf_product_line",
    "cf_delivery_method",
    "cf_item_size",
    "cf_unit_size",
    "cf_case_size",
    "cf_pack_count",
    "cf_pack_dimension",
    "cf_display_size",
    "cf_dosage",
    "cf_formulation",
    "cf_flavor_scent",
    "cf_package_type",
    "cf_market_value",
    "cf_description",
)


@dataclass
class CustomFieldRow:
    """One render-ready custom field (merge of metadata def + item value)."""

    api_name: str
    label: str
    field_type: str
    is_dropdown: bool
    value: str | None
    options: list[str]
    value_in_options: bool
    is_set: bool


@dataclass
class ExtendedItemDetail:
    """Read-only extended view assembled for the item detail template."""

    available: bool = False
    metadata_available: bool = False
    item: dict[str, Any] | None = None
    custom_fields: list[CustomFieldRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# HTTP plumbing (read-only)
# ---------------------------------------------------------------------------


def _base() -> str:
    return settings.ZOHO_INTEGRATION_BASE_URL.rstrip("/")


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.ZOHO_INTEGRATION_APP_TOKEN}",
        "X-Brand": settings.ZOHO_INTEGRATION_BRAND,
        "Accept": "application/json",
    }


def fetch_item_detail(
    zoho_item_id: str | None,
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any] | None:
    """GET the extended, normalized item. Returns the item dict or ``None``.

    Never raises — any failure (not configured, network, non-2xx, bad body)
    returns ``None`` so the local detail page still renders.
    """
    if not settings.zoho_integration_configured or not zoho_item_id:
        return None
    url = f"{_base()}{_ITEMS_PATH}/{zoho_item_id}"
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=settings.ZOHO_INTEGRATION_TIMEOUT_SECONDS)
    try:
        resp = client.get(url, headers=_headers())
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("extended item detail fetch failed for %s: %s", zoho_item_id, exc)
        return None
    finally:
        if owns_client:
            client.close()
    item = body.get("item") if isinstance(body, dict) else None
    return item if isinstance(item, dict) else None


# ---------------------------------------------------------------------------
# Metadata + simple TTL cache
# ---------------------------------------------------------------------------

_metadata_lock = threading.Lock()
_metadata_cache: dict[str, Any] = {"data": None, "expires_at": 0.0}


def reset_metadata_cache() -> None:
    """Clear the in-process metadata cache (used by tests)."""
    with _metadata_lock:
        _metadata_cache["data"] = None
        _metadata_cache["expires_at"] = 0.0


def fetch_metadata(
    *,
    client: httpx.Client | None = None,
    force: bool = False,
) -> dict[str, Any] | None:
    """GET item metadata with a simple in-process TTL cache.

    Honors ``meta.cache_ttl_seconds`` (default 3600). Returns the full metadata
    envelope or ``None``. On a fetch failure the last cached value is returned
    if present (even if expired), so the page keeps working during a blip.
    """
    if not settings.zoho_integration_configured:
        return None
    now = time.time()
    with _metadata_lock:
        cached = _metadata_cache["data"]
        if not force and cached is not None and now < _metadata_cache["expires_at"]:
            return cached
    url = f"{_base()}{_ITEMS_PATH}/metadata"
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=settings.ZOHO_INTEGRATION_TIMEOUT_SECONDS)
    try:
        resp = client.get(url, headers=_headers())
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("item metadata fetch failed: %s", exc)
        with _metadata_lock:
            return _metadata_cache["data"]  # stale-or-None fallback
    finally:
        if owns_client:
            client.close()
    ttl = _DEFAULT_METADATA_TTL
    meta = body.get("meta") if isinstance(body, dict) else None
    if isinstance(meta, dict):
        try:
            ttl = int(meta.get("cache_ttl_seconds") or _DEFAULT_METADATA_TTL)
        except (TypeError, ValueError):
            ttl = _DEFAULT_METADATA_TTL
    with _metadata_lock:
        _metadata_cache["data"] = body
        _metadata_cache["expires_at"] = time.time() + max(0, ttl)
    return body


# ---------------------------------------------------------------------------
# View assembly (merge item values with metadata definitions)
# ---------------------------------------------------------------------------


def _humanize(api_name: str) -> str:
    base = (api_name or "").removeprefix("cf_").replace("_", " ").strip()
    return base.title() or (api_name or "")


def _option_names(defn: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for opt in defn.get("options") or []:
        if isinstance(opt, dict):
            name = opt.get("name")
            if name not in (None, ""):
                out.append(str(name))
        elif opt not in (None, ""):
            out.append(str(opt))
    return out


def _custom_field_defs(metadata: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(metadata, dict):
        return []
    md = metadata.get("metadata")
    if not isinstance(md, dict):
        return []
    defs = md.get("custom_fields")
    return defs if isinstance(defs, list) else []


def build_custom_field_rows(
    item: dict[str, Any] | None,
    metadata: dict[str, Any] | None,
) -> list[CustomFieldRow]:
    """Merge metadata field definitions with the item's set custom-field values.

    The service only returns custom fields that are *set* on the item, so the
    metadata definitions provide the full, ordered, labeled field list (and the
    dropdown options). When metadata is unavailable we fall back to rendering
    only the fields actually present on the item, as plain read-only rows.
    """
    item = item or {}
    item_cfs = item.get("custom_fields")
    item_cfs = item_cfs if isinstance(item_cfs, dict) else {}

    defs = _custom_field_defs(metadata)
    def_by_name = {d.get("api_name"): d for d in defs if d.get("api_name")}

    if def_by_name:
        ordered = [n for n in _CF_ORDER if n in def_by_name]
        ordered += [n for n in def_by_name if n not in _CF_ORDER]
        source_names = ordered
    else:
        source_names = list(item_cfs.keys())

    rows: list[CustomFieldRow] = []
    for name in source_names:
        defn = def_by_name.get(name) or {}
        cf_val = item_cfs.get(name) or {}
        raw_value = cf_val.get("value")
        value = str(raw_value) if raw_value not in (None, "") else None
        options = _option_names(defn)
        is_dropdown = bool(defn.get("is_dropdown"))
        label = defn.get("label") or cf_val.get("label") or _humanize(name)
        field_type = defn.get("field_type") or cf_val.get("field_type") or "string"
        value_in_options = value is None or not options or value in options
        rows.append(
            CustomFieldRow(
                api_name=name,
                label=label,
                field_type=field_type,
                is_dropdown=is_dropdown,
                value=value,
                options=options,
                value_in_options=value_in_options,
                is_set=value is not None,
            )
        )
    return rows


def _metadata_warnings(metadata: dict[str, Any] | None) -> list[str]:
    out: list[str] = []
    if not isinstance(metadata, dict):
        return out
    meta = metadata.get("meta")
    if not isinstance(meta, dict):
        return out
    for warn in meta.get("warnings") or []:
        if isinstance(warn, dict):
            label = warn.get("label") or warn.get("source") or "metadata"
            out.append(f"Zoho metadata: “{label}” unavailable")
        elif warn:
            out.append(f"Zoho metadata: {warn}")
    return out


def build_extended_detail(
    zoho_item_id: str | None,
    *,
    client: httpx.Client | None = None,
) -> ExtendedItemDetail:
    """Assemble the read-only extended detail view for an item.

    Returns an :class:`ExtendedItemDetail`. When the item is not Zoho-synced or
    the service is unavailable, ``available`` is ``False`` and the caller simply
    renders the local PackTrack detail (plus any warning).
    """
    result = ExtendedItemDetail()
    if not settings.zoho_integration_configured or not zoho_item_id:
        return result

    item = fetch_item_detail(zoho_item_id, client=client)
    metadata = fetch_metadata(client=client)
    result.metadata_available = metadata is not None

    if item is None:
        result.warnings.append("Zoho extended details unavailable.")
        return result

    result.available = True
    result.item = item
    result.custom_fields = build_custom_field_rows(item, metadata)
    if metadata is not None:
        result.warnings.extend(_metadata_warnings(metadata))
    else:
        result.warnings.append(
            "Zoho field metadata unavailable — labels and dropdown options are limited."
        )
    return result
