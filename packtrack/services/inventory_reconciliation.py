"""Inventory reconciliation & sync-exceptions dashboard service (v2.17.0).

This module computes the data for ``GET /inventory/reconciliation``. It
is **strictly read-only**: no DB writes, no Zoho calls, no integration-
service calls, no mutation of ``Item.current_stock`` or any
``InventoryAdjustment`` field. Every helper takes a session and returns
plain dataclasses the route layer hands straight to the template.

Three sections + a summary:

  * ``compute_variance_rows()``       — items with a snapshot whose
                                        ``current_stock != snapshot``
  * ``compute_stale_snapshot_rows()`` — items with no snapshot OR a
                                        snapshot older than the configured
                                        staleness threshold
  * ``compute_sync_exception_rows()`` — adjustments not yet SYNCED,
                                        each tagged with the v2.16.3
                                        retry-eligibility decision so
                                        the UI shows Retry only when
                                        the route would accept it
  * ``compute_summary()``             — single-pass counts used by the
                                        summary cards at the top of the
                                        dashboard

The router layer applies the operator's filters (``variance_only``,
``stale_only``, ``failed_only``, ``retryable_only``, ``q`` for search,
``product_line``) to the section lists below — keeping filter logic
adjacent to the data shape makes the route file trivial and the
filtering testable in isolation.

PackTrack policy reminder: PackTrack is the source of truth for
``Item.current_stock``. Zoho snapshots are informational only. Nothing
in this module ever rewrites either side.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from sqlmodel import Session, select

from packtrack.models import (
    InventoryAdjustment,
    Item,
    ZohoSyncStatus,
)
from packtrack.services.inventory_adjustment_sync import (
    RetryBlockReason,
    RetryEligibility,
    retry_eligibility,
)

# ---------------------------------------------------------------------------
# Status enums + dataclasses
# ---------------------------------------------------------------------------


class VarianceStatus(StrEnum):
    """Per-row badge for the variance section.

    ``IN_SYNC`` is included so the summary can count items that ARE
    aligned (it shouldn't appear as a row in the variance table since
    the table only lists items with non-zero variance, but the enum is
    used by the summary counter)."""

    IN_SYNC = "in_sync"
    PACKTRACK_HIGHER = "packtrack_higher"
    ZOHO_HIGHER = "zoho_higher"
    SNAPSHOT_STALE = "snapshot_stale"


class StaleSnapshotStatus(StrEnum):
    """Per-row badge for the stale/missing snapshot section."""

    MISSING = "missing_snapshot"
    STALE = "stale_snapshot"


# Operator-facing copy for each status code. Kept here so the template
# isn't full of conditional jinja for label translation.
VARIANCE_LABELS: dict[VarianceStatus, str] = {
    VarianceStatus.IN_SYNC: "In sync",
    VarianceStatus.PACKTRACK_HIGHER: "PackTrack higher",
    VarianceStatus.ZOHO_HIGHER: "Zoho higher",
    VarianceStatus.SNAPSHOT_STALE: "Snapshot stale",
}

STALE_LABELS: dict[StaleSnapshotStatus, str] = {
    StaleSnapshotStatus.MISSING: "Missing",
    StaleSnapshotStatus.STALE: "Stale",
}


@dataclass(frozen=True)
class VarianceRow:
    item_id: int
    name: str
    sku_code: str | None
    material_code: str | None
    product_line: str | None
    packtrack_qty: Decimal
    zoho_qty: Decimal
    variance: Decimal       # packtrack_qty - zoho_qty (signed)
    snapshot_at: datetime | None
    snapshot_stale: bool
    status: VarianceStatus
    # v2.17.1 — short operator-facing copy for "what should I do next?"
    # rendered next to the row. Computed deterministically from status;
    # never implies Zoho should overwrite PackTrack.
    recommended_action: str = ""


@dataclass(frozen=True)
class StaleSnapshotRow:
    item_id: int
    name: str
    sku_code: str | None
    material_code: str | None
    product_line: str | None
    packtrack_qty: Decimal
    snapshot_at: datetime | None
    zoho_item_id: str | None
    status: StaleSnapshotStatus
    # v2.17.1 — operator guidance. Differentiates "we have a zoho id but
    # no snapshot yet (integration concern)" from "item is local-only
    # and needs linking" so the recommended action is precise.
    recommended_action: str = ""


@dataclass(frozen=True)
class SyncExceptionRow:
    adjustment_id: int
    adjustment_number: str
    created_at: datetime
    item_id: int
    item_name: str
    item_sku: str | None
    item_material_code: str | None
    quantity_delta: Decimal
    reason_label: str
    zoho_sync_status: ZohoSyncStatus
    zoho_sync_error: str | None
    zoho_sync_warning: str | None
    sync_attempt_count: int
    eligibility: RetryEligibility
    # v2.17.1 — operator copy for "what should I do next?". Distinguishes
    # eligibility-blocked rows (already compensated / drift risk / voided)
    # from config-class statuses (not_configured / skipped) so the
    # operator's first move is obvious.
    recommended_action: str = ""


@dataclass(frozen=True)
class Summary:
    total_items: int
    items_in_sync: int          # snapshot exists AND PT == Zoho AND not stale
    items_with_variance: int    # snapshot exists AND PT != Zoho
    items_stale_or_missing: int # no snapshot OR snapshot older than threshold
    failed_sync_rows: int
    pending_sync_rows: int
    not_configured_sync_rows: int
    skipped_sync_rows: int
    retryable_sync_rows: int    # of the non-synced rows above, count eligible
    blocked_sync_rows: int      # of the non-synced rows above, count blocked

    # Counts for the dashboard "totals" strip
    @property
    def exception_total(self) -> int:
        return (
            self.failed_sync_rows
            + self.pending_sync_rows
            + self.not_configured_sync_rows
            + self.skipped_sync_rows
        )


@dataclass(frozen=True)
class Filters:
    """Operator filter inputs from the dashboard's query string."""
    variance_only: bool = False
    stale_only: bool = False
    failed_only: bool = False
    retryable_only: bool = False
    q: str = ""                  # case-insensitive substring across name/SKU/material_code
    product_line: str = ""       # exact match on Item.product_line

    @property
    def any_active(self) -> bool:
        return (
            self.variance_only or self.stale_only or self.failed_only
            or self.retryable_only or bool(self.q) or bool(self.product_line)
        )


@dataclass(frozen=True)
class Dashboard:
    """The full payload the route hands to the template — filtered."""
    summary: Summary
    variance_rows: list[VarianceRow]
    stale_rows: list[StaleSnapshotRow]
    exception_rows: list[SyncExceptionRow]
    filters: Filters
    stale_threshold_hours: int
    product_lines: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_decimal(value) -> Decimal:
    """Coerce mixed numeric types (float current_stock, Decimal snapshot,
    None) into a Decimal. Treats None as 0 so callers can do arithmetic."""
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _is_snapshot_stale(
    snapshot_at: datetime | None,
    stale_threshold: timedelta,
    now: datetime,
) -> bool:
    """A snapshot is stale when it exists and is older than the
    threshold. Missing-snapshot is its own category — not "stale"."""
    if snapshot_at is None:
        return False
    return (now - snapshot_at) > stale_threshold


def _variance_status(variance: Decimal, stale: bool) -> VarianceStatus:
    if stale:
        return VarianceStatus.SNAPSHOT_STALE
    if variance == 0:
        return VarianceStatus.IN_SYNC
    if variance > 0:
        return VarianceStatus.PACKTRACK_HIGHER
    return VarianceStatus.ZOHO_HIGHER


# ---------------------------------------------------------------------------
# v2.17.1 — operator-facing "what should I do?" copy
# ---------------------------------------------------------------------------
#
# Pure functions. Deterministic from the row's state. Render-time only;
# never imply Zoho should overwrite PackTrack. Tested independently of
# the route layer so future copy tweaks don't need to re-test rendering.


def recommended_variance_action(status: VarianceStatus) -> str:
    if status is VarianceStatus.PACKTRACK_HIGHER:
        return "Review recent adjustments / confirm Zoho sync"
    if status is VarianceStatus.ZOHO_HIGHER:
        return "Cycle count or review PackTrack movements"
    if status is VarianceStatus.SNAPSHOT_STALE:
        return "Wait for next sync or review Zoho sync health"
    return ""  # IN_SYNC — nothing to do


def recommended_stale_action(
    status: StaleSnapshotStatus, zoho_item_id: str | None,
) -> str:
    if status is StaleSnapshotStatus.MISSING:
        if zoho_item_id:
            return "Await snapshot sync / check integration"
        return "Link Zoho item or mark as local-only"
    # STALE — snapshot exists but is old. Same guidance as a stale
    # variance row: it's a sync-cadence issue, not an item issue.
    return "Wait for next sync or review Zoho sync health"


def recommended_exception_action(
    status: ZohoSyncStatus, eligibility: RetryEligibility,
) -> str:
    # Eligibility-blocked rows: surface why no action is needed.
    if not eligibility.allowed:
        reason = eligibility.reason
        if reason is RetryBlockReason.REVERSED_LOCALLY:
            return "No action — already compensated locally"
        if reason is RetryBlockReason.REVERSAL_OF_UNSYNCED:
            return "No action — retry blocked to prevent drift"
        if reason is RetryBlockReason.VOIDED:
            return "No action — voided locally"
        if reason is RetryBlockReason.ALREADY_SYNCED:
            return "Already synced"
        return eligibility.detail  # defensive fallback

    # Eligible — distinguish config-class statuses from a clean retry so
    # the operator's first move is precise.
    if status is ZohoSyncStatus.NOT_CONFIGURED:
        return "Check integration configuration"
    if status is ZohoSyncStatus.SKIPPED:
        return "Link item to Zoho before syncing"
    return "Retry sync"


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def compute_variance_rows(
    session: Session, *,
    stale_threshold: timedelta,
    now: datetime | None = None,
) -> list[VarianceRow]:
    """All items whose Zoho snapshot exists and disagrees with PackTrack
    (or whose snapshot is stale even if it currently matches — the
    operator still needs to confirm the snapshot is fresh).

    Sorted by absolute variance descending so the worst drift floats to
    the top. Stale-but-equal rows fall to the bottom of the list.
    """
    now = now or datetime.utcnow()
    rows: list[VarianceRow] = []

    items = session.exec(
        select(Item).where(Item.last_zoho_stock_snapshot.is_not(None))
    ).all()
    for it in items:
        pt = _to_decimal(it.current_stock)
        zoho = _to_decimal(it.last_zoho_stock_snapshot)
        variance = pt - zoho
        stale = _is_snapshot_stale(it.last_zoho_stock_snapshot_at, stale_threshold, now)
        if variance == 0 and not stale:
            # Aligned + fresh — nothing for the operator to act on here.
            # (Summary counts this row as IN_SYNC separately.)
            continue
        status = _variance_status(variance, stale)
        rows.append(VarianceRow(
            item_id=it.id, name=it.name, sku_code=it.sku_code,
            material_code=it.material_code, product_line=it.product_line,
            packtrack_qty=pt, zoho_qty=zoho, variance=variance,
            snapshot_at=it.last_zoho_stock_snapshot_at,
            snapshot_stale=stale,
            status=status,
            recommended_action=recommended_variance_action(status),
        ))

    rows.sort(
        key=lambda r: (abs(r.variance), r.snapshot_stale),
        reverse=True,
    )
    return rows


def compute_stale_snapshot_rows(
    session: Session, *,
    stale_threshold: timedelta,
    now: datetime | None = None,
) -> list[StaleSnapshotRow]:
    """Items with no snapshot at all, OR a snapshot older than
    ``stale_threshold``. Missing-snapshot rows sort first (more
    actionable — "we've never seen Zoho's value")."""
    now = now or datetime.utcnow()
    rows: list[StaleSnapshotRow] = []
    items = session.exec(select(Item)).all()
    for it in items:
        snap_at = it.last_zoho_stock_snapshot_at
        if it.last_zoho_stock_snapshot is None:
            status = StaleSnapshotStatus.MISSING
        elif _is_snapshot_stale(snap_at, stale_threshold, now):
            status = StaleSnapshotStatus.STALE
        else:
            continue
        rows.append(StaleSnapshotRow(
            item_id=it.id, name=it.name, sku_code=it.sku_code,
            material_code=it.material_code, product_line=it.product_line,
            packtrack_qty=_to_decimal(it.current_stock),
            snapshot_at=snap_at, zoho_item_id=it.zoho_item_id,
            status=status,
            recommended_action=recommended_stale_action(status, it.zoho_item_id),
        ))
    rows.sort(key=lambda r: (
        0 if r.status is StaleSnapshotStatus.MISSING else 1,
        r.snapshot_at or datetime.min,
    ))
    return rows


def compute_sync_exception_rows(
    session: Session, *,
    reason_labels: dict | None = None,
    limit: int = 500,
) -> list[SyncExceptionRow]:
    """All InventoryAdjustment rows whose ``zoho_sync_status`` is not
    SYNCED, newest first, each tagged with the v2.16.3 retry-eligibility
    decision so the UI can show Retry vs the right blocked reason.

    ``reason_labels`` is the human-copy dict from
    ``services.inventory_adjustments.REASON_LABELS`` — passed in to
    avoid a circular import at module load time. Falls back to the raw
    enum value when not provided.
    """
    reason_labels = reason_labels or {}
    item_cache: dict[int, Item] = {}

    adjustments = session.exec(
        select(InventoryAdjustment)
        .where(InventoryAdjustment.zoho_sync_status != ZohoSyncStatus.SYNCED)
        .order_by(
            InventoryAdjustment.created_at.desc(),
            InventoryAdjustment.id.desc(),
        )
        .limit(limit)
    ).all()

    rows: list[SyncExceptionRow] = []
    for adj in adjustments:
        item = item_cache.get(adj.item_id)
        if item is None:
            item = session.get(Item, adj.item_id)
            if item is not None:
                item_cache[adj.item_id] = item
        eligibility = retry_eligibility(session, adj)
        rows.append(SyncExceptionRow(
            adjustment_id=adj.id,
            adjustment_number=adj.adjustment_number,
            created_at=adj.created_at,
            item_id=adj.item_id,
            item_name=item.name if item else f"item #{adj.item_id}",
            item_sku=item.sku_code if item else None,
            item_material_code=item.material_code if item else None,
            quantity_delta=adj.quantity_delta,
            reason_label=reason_labels.get(adj.reason_code, str(adj.reason_code.value)),
            zoho_sync_status=adj.zoho_sync_status,
            zoho_sync_error=adj.zoho_sync_error,
            zoho_sync_warning=adj.zoho_sync_warning,
            sync_attempt_count=adj.sync_attempt_count or 0,
            eligibility=eligibility,
            recommended_action=recommended_exception_action(
                adj.zoho_sync_status, eligibility,
            ),
        ))
    return rows


def compute_summary(
    session: Session, *,
    stale_threshold: timedelta,
    exception_rows: list[SyncExceptionRow] | None = None,
    now: datetime | None = None,
) -> Summary:
    """Single-pass counts for the dashboard's summary cards.

    Pass ``exception_rows`` to avoid re-querying — the route already
    computes them for the table below; reusing the list keeps
    eligibility evaluation cost flat at one pass per request."""
    now = now or datetime.utcnow()
    items = session.exec(select(Item)).all()

    total_items = len(items)
    items_in_sync = items_with_variance = items_stale_or_missing = 0

    for it in items:
        snap = it.last_zoho_stock_snapshot
        snap_at = it.last_zoho_stock_snapshot_at
        if snap is None:
            items_stale_or_missing += 1
            continue
        if _is_snapshot_stale(snap_at, stale_threshold, now):
            items_stale_or_missing += 1
            # Stale + variance both count — the operator cares about both.
            if _to_decimal(it.current_stock) != _to_decimal(snap):
                items_with_variance += 1
            continue
        if _to_decimal(it.current_stock) == _to_decimal(snap):
            items_in_sync += 1
        else:
            items_with_variance += 1

    if exception_rows is None:
        exception_rows = compute_sync_exception_rows(session)

    failed = pending = not_conf = skipped = retryable = blocked = 0
    for r in exception_rows:
        if r.zoho_sync_status is ZohoSyncStatus.FAILED:
            failed += 1
        elif r.zoho_sync_status is ZohoSyncStatus.PENDING:
            pending += 1
        elif r.zoho_sync_status is ZohoSyncStatus.NOT_CONFIGURED:
            not_conf += 1
        elif r.zoho_sync_status is ZohoSyncStatus.SKIPPED:
            skipped += 1
        if r.eligibility.allowed:
            retryable += 1
        else:
            blocked += 1

    return Summary(
        total_items=total_items,
        items_in_sync=items_in_sync,
        items_with_variance=items_with_variance,
        items_stale_or_missing=items_stale_or_missing,
        failed_sync_rows=failed,
        pending_sync_rows=pending,
        not_configured_sync_rows=not_conf,
        skipped_sync_rows=skipped,
        retryable_sync_rows=retryable,
        blocked_sync_rows=blocked,
    )


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def _matches_search(needle: str, *fields: str | None) -> bool:
    """Case-insensitive substring match across any of the given fields.
    Empty needle matches everything."""
    if not needle:
        return True
    n = needle.lower().strip()
    if not n:
        return True
    return any(f and n in f.lower() for f in fields)


def apply_filters(
    *,
    variance_rows: list[VarianceRow],
    stale_rows: list[StaleSnapshotRow],
    exception_rows: list[SyncExceptionRow],
    filters: Filters,
) -> tuple[list[VarianceRow], list[StaleSnapshotRow], list[SyncExceptionRow]]:
    """Apply the operator's filter set to each section.

    The filters cross sections deliberately: ``variance_only`` HIDES the
    stale + exceptions sections; ``stale_only`` HIDES variance +
    exceptions; ``failed_only`` / ``retryable_only`` HIDE variance +
    stale. ``q`` and ``product_line`` narrow within sections (search/
    product-line match) but do not hide sections.
    """
    v_out: list[VarianceRow] = list(variance_rows)
    s_out: list[StaleSnapshotRow] = list(stale_rows)
    e_out: list[SyncExceptionRow] = list(exception_rows)

    if filters.variance_only:
        s_out, e_out = [], []
    if filters.stale_only:
        v_out, e_out = [], []
    if filters.failed_only:
        v_out, s_out = [], []
        e_out = [
            r for r in e_out if r.zoho_sync_status is ZohoSyncStatus.FAILED
        ]
    if filters.retryable_only:
        v_out, s_out = [], []
        e_out = [r for r in e_out if r.eligibility.allowed]

    if filters.q:
        v_out = [
            r for r in v_out
            if _matches_search(filters.q, r.name, r.sku_code, r.material_code)
        ]
        s_out = [
            r for r in s_out
            if _matches_search(filters.q, r.name, r.sku_code, r.material_code)
        ]
        e_out = [
            r for r in e_out
            if _matches_search(filters.q, r.item_name, r.item_sku, r.item_material_code)
        ]

    if filters.product_line:
        v_out = [r for r in v_out if (r.product_line or "") == filters.product_line]
        s_out = [r for r in s_out if (r.product_line or "") == filters.product_line]
        # Exception rows don't carry product_line; the row's item is the
        # source. We don't re-load items here — product-line filter
        # currently scopes the item-side sections only. Documented in
        # the dashboard help text.

    return v_out, s_out, e_out


def list_product_lines(session: Session) -> list[str]:
    """Distinct, non-null product_line values across items, for the
    filter dropdown. Sorted for stable rendering."""
    items = session.exec(
        select(Item.product_line).where(Item.product_line.is_not(None))
    ).all()
    return sorted({pl for pl in items if pl})


# ---------------------------------------------------------------------------
# Top-level builder
# ---------------------------------------------------------------------------


def build_dashboard(
    session: Session, *,
    filters: Filters | None = None,
    stale_threshold_hours: int,
    reason_labels: dict | None = None,
    now: datetime | None = None,
) -> Dashboard:
    """Compose a complete Dashboard payload — sections + summary +
    filter echo. The route layer calls this once and renders.

    Summary is computed against the UNFILTERED universe so the cards
    stay stable as the operator narrows the table below. Filters apply
    only to the per-section row lists.
    """
    filters = filters or Filters()
    stale_threshold = timedelta(hours=stale_threshold_hours)

    variance = compute_variance_rows(
        session, stale_threshold=stale_threshold, now=now,
    )
    stale = compute_stale_snapshot_rows(
        session, stale_threshold=stale_threshold, now=now,
    )
    exceptions = compute_sync_exception_rows(
        session, reason_labels=reason_labels,
    )
    summary = compute_summary(
        session, stale_threshold=stale_threshold,
        exception_rows=exceptions, now=now,
    )
    product_lines = list_product_lines(session)

    v_out, s_out, e_out = apply_filters(
        variance_rows=variance,
        stale_rows=stale,
        exception_rows=exceptions,
        filters=filters,
    )

    return Dashboard(
        summary=summary,
        variance_rows=v_out,
        stale_rows=s_out,
        exception_rows=e_out,
        filters=filters,
        stale_threshold_hours=stale_threshold_hours,
        product_lines=product_lines,
    )
