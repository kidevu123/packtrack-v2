"""Material-code identity audit.

Pure helpers — they take a list of ``ItemView`` and return findings. No DB
access, no I/O. The CLI (``scripts/audit_material_codes.py``) is the only
caller that touches the database; tests target these functions directly.

The intent is keeping the audit logic decoupled from SQLModel / FastAPI so we
can verify behaviour without a Postgres test fixture.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ItemView:
    """A read-only snapshot of an ``Item`` row, restricted to the columns the
    audit needs. Anything else (stock, prices, images) is irrelevant here and
    deliberately excluded so tests don't have to mock half the schema."""

    id: int
    zoho_item_id: str | None
    name: str
    sku_code: str | None
    vendor: str | None
    material_code: str | None


def _norm(value: str | None) -> str | None:
    """Treat empty/whitespace strings as null. Sku code ``"  "`` should not
    count as populated."""
    if value is None:
        return None
    s = value.strip()
    return s or None


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def find_blank_sku_codes(items: list[ItemView]) -> list[ItemView]:
    """Items where ``sku_code`` is null or blank."""
    return [it for it in items if _norm(it.sku_code) is None]


def find_duplicate_sku_codes(items: list[ItemView]) -> dict[str, list[ItemView]]:
    """SKU codes shared by 2+ items. Comparison is case-sensitive on the
    stripped value — Zoho is case-sensitive on its side too, so we do not
    fold case here."""
    buckets: dict[str, list[ItemView]] = defaultdict(list)
    for it in items:
        sku = _norm(it.sku_code)
        if sku is None:
            continue
        buckets[sku].append(it)
    return {k: v for k, v in buckets.items() if len(v) > 1}


def find_missing_zoho_ids(items: list[ItemView]) -> list[ItemView]:
    """Items with no ``zoho_item_id`` — usually hand-created or pre-sync."""
    return [it for it in items if _norm(it.zoho_item_id) is None]


def find_missing_material_codes(items: list[ItemView]) -> list[ItemView]:
    """Items with no ``material_code`` — they need either a safe-default
    backfill or manual review before the Luma push can route them."""
    return [it for it in items if _norm(it.material_code) is None]


def find_duplicate_material_codes(items: list[ItemView]) -> dict[str, list[ItemView]]:
    """Items that share a populated material_code. The partial unique index
    in Postgres also prevents this, but the audit reports it independently
    so the operator sees it before alembic complains on insert."""
    buckets: dict[str, list[ItemView]] = defaultdict(list)
    for it in items:
        code = _norm(it.material_code)
        if code is None:
            continue
        buckets[code].append(it)
    return {k: v for k, v in buckets.items() if len(v) > 1}


def find_vendor_material_conflicts(
    items: list[ItemView],
) -> dict[tuple[str, str], list[ItemView]]:
    """``(vendor, material_code)`` collisions — the same vendor shouldn't
    have two items with the same material code. Different vendors sharing a
    code is a separate question; this helper only flags the within-vendor
    case so report output stays focused."""
    buckets: dict[tuple[str, str], list[ItemView]] = defaultdict(list)
    for it in items:
        v = _norm(it.vendor)
        c = _norm(it.material_code)
        if v is None or c is None:
            continue
        buckets[(v, c)].append(it)
    return {k: v for k, v in buckets.items() if len(v) > 1}


# ---------------------------------------------------------------------------
# Safe-default proposal
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SafeDefault:
    item_id: int
    proposed_material_code: str
    source: str  # always "sku_code" today; reserved for future heuristics


def compute_safe_defaults(items: list[ItemView]) -> list[SafeDefault]:
    """Propose ``material_code`` values to copy in from ``sku_code``.

    Rules — every one must hold:

    1. ``material_code`` is currently null/blank (we never overwrite).
    2. ``sku_code`` is non-empty after stripping.
    3. ``sku_code`` is unique across the entire item set under audit. A
       value that already collides cannot be safely promoted; the operator
       must rename one side first.

    Returns the per-item proposal list. Empty list = nothing safe to do.
    """
    duplicate_skus = set(find_duplicate_sku_codes(items).keys())
    proposals: list[SafeDefault] = []
    for it in items:
        if _norm(it.material_code) is not None:
            continue  # never overwrite
        sku = _norm(it.sku_code)
        if sku is None:
            continue  # nothing to copy from
        if sku in duplicate_skus:
            continue  # ambiguous — refuse
        proposals.append(SafeDefault(
            item_id=it.id,
            proposed_material_code=sku,
            source="sku_code",
        ))
    return proposals


# ---------------------------------------------------------------------------
# Aggregate report — what the CLI prints, structured so tests can assert
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AuditReport:
    total: int
    with_sku_code: int
    blank_sku_code: list[ItemView]
    duplicate_sku_code: dict[str, list[ItemView]]
    missing_zoho_id: list[ItemView]
    missing_material_code: list[ItemView]
    duplicate_material_code: dict[str, list[ItemView]]
    vendor_material_conflicts: dict[tuple[str, str], list[ItemView]]
    safe_defaults: list[SafeDefault]


def audit(items: list[ItemView]) -> AuditReport:
    """Run every detector + the safe-default proposer. Pure: same input
    always yields the same report."""
    return AuditReport(
        total=len(items),
        with_sku_code=sum(1 for it in items if _norm(it.sku_code) is not None),
        blank_sku_code=find_blank_sku_codes(items),
        duplicate_sku_code=find_duplicate_sku_codes(items),
        missing_zoho_id=find_missing_zoho_ids(items),
        missing_material_code=find_missing_material_codes(items),
        duplicate_material_code=find_duplicate_material_codes(items),
        vendor_material_conflicts=find_vendor_material_conflicts(items),
        safe_defaults=compute_safe_defaults(items),
    )
