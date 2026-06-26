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


# Custom fields PackTrack may edit (v2.8.0), matching the service v1.33.0
# allowlist. Editability is still gated on the live metadata ``policy`` being
# ``writable``; this set just bounds what PackTrack will ever render/send.
WRITABLE_CUSTOM_FIELDS: frozenset[str] = frozenset({
    "cf_item_type", "cf_delivery_method", "cf_item_size", "cf_dosage",
    "cf_pack_count", "cf_market_value", "cf_display_size", "cf_product_line",
    "cf_flavor_scent", "cf_package_type", "cf_formulation", "cf_pack_dimension",
    "cf_unit_size", "cf_case_size", "cf_description",
})

# Standard Zoho fields PackTrack may edit (v1.33.0). Brand/manufacturer/category
# are Zoho-only (never stored in a local Item column).
WRITABLE_STANDARD_FIELDS: tuple[str, ...] = (
    "name", "unit", "brand", "manufacturer", "category_id", "description",
)

# Metadata field_types that require numeric parsing before a write.
_NUMERIC_FIELD_TYPES: frozenset[str] = frozenset(
    {"number", "decimal", "amount", "percent", "integer"}
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
    policy: str = "read_only"
    is_numeric: bool = False
    is_writable: bool = False


@dataclass
class ExtendedItemDetail:
    """Read-only extended view assembled for the item detail template."""

    available: bool = False
    metadata_available: bool = False
    item: dict[str, Any] | None = None
    custom_fields: list[CustomFieldRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    categories: list[dict[str, Any]] = field(default_factory=list)
    field_policy: dict[str, str] = field(default_factory=dict)


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


def _categories(metadata: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(metadata, dict):
        return []
    md = metadata.get("metadata")
    if not isinstance(md, dict):
        return []
    cats = md.get("categories")
    return [c for c in cats if isinstance(c, dict) and c.get("category_id")] if isinstance(cats, list) else []


def _field_policy(metadata: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(metadata, dict):
        return {}
    md = metadata.get("metadata")
    if not isinstance(md, dict):
        return {}
    fp = md.get("field_policy")
    return {str(k): str(v) for k, v in fp.items()} if isinstance(fp, dict) else {}


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
        policy = str(defn.get("policy") or "read_only")
        # Only fields that are both metadata-writable and in our allowlist are
        # editable; a missing metadata def (no policy) is never editable.
        is_writable = bool(defn) and policy == "writable" and name in WRITABLE_CUSTOM_FIELDS
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
                policy=policy,
                is_numeric=field_type in _NUMERIC_FIELD_TYPES,
                is_writable=is_writable,
            )
        )
    return rows


CF_PRODUCT_LINE = "cf_product_line"


def product_line_options(*, client: httpx.Client | None = None) -> list[str] | None:
    """Live, valid option names for the Zoho ``cf_product_line`` dropdown.

    Returns the ordered list of option *names* (e.g. ``["7OH", "MIT A",
    "MIT B"]``) from cached metadata, ``[]`` when metadata is reachable but the
    field/options aren't defined, or ``None`` when metadata is unavailable (so
    callers can fall back to read-only and avoid posting an unvalidated value).

    This is the authoritative server-side allowlist used to validate an owner's
    ``cf_product_line`` edit before it is sent to the integration service.
    """
    metadata = fetch_metadata(client=client)
    if metadata is None:
        return None
    for defn in _custom_field_defs(metadata):
        if defn.get("api_name") == CF_PRODUCT_LINE:
            return _option_names(defn)
    return []


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


# ---------------------------------------------------------------------------
# Master-data editor view-model + validation (v2.8.0)
# ---------------------------------------------------------------------------

# Standard fields the owner may clear (send empty string). name/unit are
# non-empty-required; category_id is not clearable.
_CLEARABLE_STANDARD: frozenset[str] = frozenset({"description", "brand", "manufacturer"})

_STD_LABELS: dict[str, str] = {
    "name": "Item name",
    "unit": "Unit",
    "brand": "Brand",
    "manufacturer": "Manufacturer",
    "category_id": "Category",
    "description": "Standard description",
}


@dataclass
class EditField:
    """One render-ready editable (or read-only) field for the editor template."""

    key: str            # form field name (standard key or custom api_name)
    label: str
    kind: str           # text | textarea | number | select | category
    value: str          # display value (submitted on re-render, else current)
    original: str       # true current value (hidden field for change-detection)
    editable: bool
    clearable: bool
    options: list[str] = field(default_factory=list)            # custom dropdown
    category_options: list[dict[str, Any]] = field(default_factory=list)
    value_in_options: bool = True
    error: str | None = None
    help: str | None = None


@dataclass
class ItemEditor:
    available: bool
    metadata_available: bool
    can_edit: bool
    primary: list[EditField] = field(default_factory=list)
    custom: list[EditField] = field(default_factory=list)


@dataclass
class MasterDataResolution:
    """Outcome of validating + change-detecting a submitted edit form."""

    payload: dict[str, Any] = field(default_factory=dict)  # changed + valid fields
    errors: dict[str, str] = field(default_factory=dict)   # key -> message
    changed: bool = False                                  # any change attempted


def _category_label(categories: list[dict[str, Any]], category_id: str) -> str | None:
    for c in categories:
        if str(c.get("category_id")) == str(category_id):
            return str(c.get("name") or c.get("category_name") or category_id)
    return None


def build_item_editor(
    extended: ExtendedItemDetail,
    *,
    can_edit: bool,
    local_values: dict[str, str],
    submitted: dict[str, str] | None = None,
    errors: dict[str, str] | None = None,
) -> ItemEditor:
    """Assemble the metadata-driven editor view-model for the template.

    ``local_values`` carries the PackTrack-mirrored ``name``/``unit``/
    ``description`` (authoritative for those three). Zoho-only fields
    (brand/manufacturer/category + custom fields) come from ``extended``. When
    ``submitted`` is provided (re-render after a validation error) its values
    override the displayed value so the owner doesn't lose their input.
    """
    submitted = submitted or {}
    errors = errors or {}
    item = extended.item or {}
    editor = ItemEditor(
        available=extended.available,
        metadata_available=extended.metadata_available,
        can_edit=can_edit,
    )

    def val(key: str, current: str) -> str:
        return submitted.get(key, current)

    # --- Primary details ---------------------------------------------------
    name_cur = local_values.get("name", "")
    unit_cur = local_values.get("unit", "")
    desc_cur = local_values.get("description", "")
    editor.primary.append(EditField(
        key="name", label=_STD_LABELS["name"], kind="text",
        value=val("name", name_cur), original=name_cur,
        editable=can_edit, clearable=False, error=errors.get("name"),
    ))
    editor.primary.append(EditField(
        key="unit", label=_STD_LABELS["unit"], kind="text",
        value=val("unit", unit_cur), original=unit_cur,
        editable=can_edit, clearable=False, error=errors.get("unit"),
    ))

    # Brand / manufacturer are Zoho-only free text; only meaningful once the
    # extended item loaded (so we know the current value to diff against).
    if extended.available:
        brand_cur = str(item.get("brand") or "")
        manuf_cur = str(item.get("manufacturer") or "")
        editor.primary.append(EditField(
            key="brand", label=_STD_LABELS["brand"], kind="text",
            value=val("brand", brand_cur), original=brand_cur,
            editable=can_edit, clearable=True, error=errors.get("brand"),
            help="Free text in this Zoho org.",
        ))
        editor.primary.append(EditField(
            key="manufacturer", label=_STD_LABELS["manufacturer"], kind="text",
            value=val("manufacturer", manuf_cur), original=manuf_cur,
            editable=can_edit, clearable=True, error=errors.get("manufacturer"),
            help="Free text in this Zoho org.",
        ))

        cat = item.get("category") if isinstance(item.get("category"), dict) else {}
        cur_cat_id = str(cat.get("category_id") or "") if cat else ""
        cat_editable = can_edit and extended.metadata_available and bool(extended.categories)
        cur_in_opts = (not cur_cat_id) or any(
            str(c.get("category_id")) == cur_cat_id for c in extended.categories
        )
        editor.primary.append(EditField(
            key="category_id", label=_STD_LABELS["category_id"], kind="category",
            value=val("category_id", cur_cat_id), original=cur_cat_id,
            editable=cat_editable, clearable=False,
            category_options=extended.categories,
            value_in_options=cur_in_opts, error=errors.get("category_id"),
            help="Category can't be cleared." if cat_editable else (
                None if extended.metadata_available else "Categories unavailable — read-only."
            ),
        ))

    editor.primary.append(EditField(
        key="description", label=_STD_LABELS["description"], kind="textarea",
        value=val("description", desc_cur), original=desc_cur,
        editable=can_edit, clearable=True, error=errors.get("description"),
    ))

    # --- Custom fields -----------------------------------------------------
    for row in extended.custom_fields:
        editable = can_edit and row.is_writable
        if row.is_dropdown:
            kind = "select"
        elif row.is_numeric:
            kind = "number"
        elif row.field_type == "multiline":
            kind = "textarea"
        else:
            kind = "text"
        cur = row.value or ""
        editor.custom.append(EditField(
            key=row.api_name, label=row.label, kind=kind,
            value=val(row.api_name, cur), original=cur,
            editable=editable, clearable=True,
            options=row.options, value_in_options=row.value_in_options,
            error=errors.get(row.api_name),
            help=("Zoho Product Line (custom field — not the PackTrack browsing group)."
                  if row.api_name == "cf_product_line" else None),
        ))
    return editor


def resolve_master_data_changes(
    *,
    metadata: dict[str, Any] | None,
    categories: list[dict[str, Any]],
    submitted: dict[str, str],
    originals: dict[str, str],
) -> MasterDataResolution:
    """Validate + change-detect a submitted edit; build the changed-only payload.

    All-or-nothing: any error means the route must not call the service. Only
    fields whose submitted value differs from the hidden original are considered;
    read-only fields are never present in ``submitted`` (the route only collects
    allowlisted keys), so they can never be sent.
    """
    res = MasterDataResolution()
    defs = {d.get("api_name"): d for d in _custom_field_defs(metadata) if d.get("api_name")}
    cat_ids = {str(c.get("category_id")) for c in categories}

    def changed(key: str) -> bool:
        return (submitted.get(key, "") or "").strip() != (originals.get(key, "") or "").strip()

    # --- standard non-empty-required text (name, unit) ---------------------
    for key in ("name", "unit"):
        if changed(key):
            res.changed = True
            value = (submitted.get(key, "") or "").strip()
            if not value:
                res.errors[key] = f"{_STD_LABELS[key]} can't be empty."
            else:
                res.payload[key] = value

    # --- standard clearable free text (description, brand, manufacturer) ---
    for key in ("description", "brand", "manufacturer"):
        if changed(key):
            res.changed = True
            res.payload[key] = (submitted.get(key, "") or "").strip()

    # --- category_id (validated, not clearable) ----------------------------
    if changed("category_id"):
        res.changed = True
        value = (submitted.get("category_id", "") or "").strip()
        if not value:
            res.errors["category_id"] = "Category can't be cleared."
        elif metadata is None or not cat_ids:
            res.errors["category_id"] = "Categories unavailable — can't change category."
        elif value not in cat_ids:
            res.errors["category_id"] = "Choose a valid category."
        else:
            res.payload["category_id"] = value

    # --- custom fields -----------------------------------------------------
    custom_payload: dict[str, str] = {}
    for api in WRITABLE_CUSTOM_FIELDS:
        if not changed(api):
            continue
        res.changed = True
        value = (submitted.get(api, "") or "").strip()
        defn = defs.get(api)
        if metadata is None or not defn:
            res.errors[api] = "Zoho metadata unavailable — can't edit this field."
            continue
        if value == "":
            custom_payload[api] = ""  # clear
            continue
        field_type = str(defn.get("field_type") or "string")
        if defn.get("is_dropdown"):
            options = _option_names(defn)
            match = next((o for o in options if o.lower() == value.lower()), None)
            if match is None:
                res.errors[api] = "Choose a valid option."
            else:
                custom_payload[api] = match
        elif field_type in _NUMERIC_FIELD_TYPES:
            try:
                float(value)
            except (TypeError, ValueError):
                res.errors[api] = "Must be a number."
            else:
                custom_payload[api] = value
        else:
            custom_payload[api] = value
    if custom_payload:
        res.payload["custom_fields"] = custom_payload
    return res


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
    result.categories = _categories(metadata)
    result.field_policy = _field_policy(metadata)
    if metadata is not None:
        result.warnings.extend(_metadata_warnings(metadata))
    else:
        result.warnings.append(
            "Zoho field metadata unavailable — labels and dropdown options are limited."
        )
    return result
