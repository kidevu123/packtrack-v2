"""Dashboard data builders.

The home page composes four sections — pipeline, needs-you, stock alerts,
recent activity. This module is the only place that knows how to assemble
them, so the route handler stays a thin shell.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from math import ceil

from sqlmodel import Session, col, desc, select

from packtrack.models import (
    Item,
    POEvent,
    POStatus,
    PurchaseOrder,
    Role,
    ShipMethod,
    ShipStatus,
    Shipment,
    User,
)
from packtrack.services.inbox import InboxItem, build_inbox
from packtrack.services.scope import (
    filter_items_query,
    filter_pos_query,
    get_scope,
)

# Visible stages on the pipeline strip + board, in display order.
PIPELINE_COLUMNS: list[tuple[str, str, list[POStatus]]] = [
    ("draft", "Draft", [POStatus.DRAFT]),
    ("design", "Design review", [POStatus.DESIGN_REVIEW, POStatus.DESIGN_REJECTED]),
    ("awaiting_pi", "Awaiting PI", [POStatus.DESIGN_APPROVED]),
    ("pi_review", "PI review", [POStatus.PI_RECEIVED]),
    ("production", "Production", [POStatus.PI_APPROVED, POStatus.PRODUCTION]),
    ("shipped", "Shipped", [POStatus.SHIPPED]),
]

# When the user drops a card into a column, this is the canonical status it
# becomes. Multi-status columns ('design', 'production') always land on the
# entry status — dragging back into 'design' from 'design_rejected' returns
# the PO to 'design_review'.
COLUMN_TARGET_STATUS: dict[str, POStatus] = {
    "draft": POStatus.DRAFT,
    "design": POStatus.DESIGN_REVIEW,
    "awaiting_pi": POStatus.DESIGN_APPROVED,
    "pi_review": POStatus.PI_RECEIVED,
    "production": POStatus.PRODUCTION,
    "shipped": POStatus.SHIPPED,
}

# How many days a PO can stay in a stage before it shows a "stale" badge.
# Tuned for typical packaging-from-China lead times: design and PI should be
# fast (small office tasks), production is naturally slow (~30+ days).
STALE_THRESHOLDS_DAYS: dict[str, int] = {
    "draft": 2,
    "design": 3,
    "awaiting_pi": 4,
    "pi_review": 2,
    "production": 35,
    "shipped": 60,
}


@dataclass
class POCard:
    po: PurchaseOrder
    column_key: str
    days_in_stage: int
    is_stale: bool
    top_item: str
    item_count: int


@dataclass
class PipelineColumn:
    key: str
    label: str
    cards: list[POCard] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.cards)


@dataclass
class StockAlert:
    item: Item
    severity: str  # 'critical' | 'amber'
    days_left: float | None
    suggested_qty: int


@dataclass
class ActivityItem:
    when: datetime
    po_id: int
    po_number: str
    actor_name: str | None
    message: str
    kind: str


@dataclass
class Pulse:
    open_spend: float
    open_spend_currency: str
    this_month_spend: float
    last_30d_delivered_value: float
    avg_cycle_days: float | None  # design_review → received, last 90 days


@dataclass
class MonthlyBar:
    label: str   # "May"
    iso: str     # "2026-05"
    value: float


@dataclass
class LandingItem:
    eta: date
    po_id: int
    po_number: str
    method: str
    quantity: float
    unit: str
    item_name: str
    item_image: str | None
    carrier: str | None


@dataclass
class Dashboard:
    user: User
    pipeline: list[PipelineColumn]
    inbox: list[InboxItem]
    stock_alerts: list[StockAlert]
    activity: list[ActivityItem]
    pipeline_total: int
    pulse: Pulse
    spend_trend: list[MonthlyBar]
    landing: list[LandingItem]


# ---------------------------------------------------------------------------
# Pipeline (with age-in-stage)
# ---------------------------------------------------------------------------


def _column_key_for(status: POStatus) -> str | None:
    for key, _label, statuses in PIPELINE_COLUMNS:
        if status in statuses:
            return key
    return None


def _last_status_change_at(session: Session, po_ids: list[int]) -> dict[int, datetime]:
    """For each PO, find the most recent status_change event timestamp.

    POs without any status_change event fall back to ``created_at`` in the
    caller. Done in one query so the dashboard stays cheap.
    """
    if not po_ids:
        return {}
    rows = session.exec(
        select(POEvent.po_id, POEvent.created_at)
        .where(POEvent.kind == "status_change", col(POEvent.po_id).in_(po_ids))
        .order_by(POEvent.po_id, desc(POEvent.created_at))
    ).all()
    out: dict[int, datetime] = {}
    for po_id, created_at in rows:
        if po_id not in out:
            out[po_id] = created_at
    return out


def build_pipeline(
    session: Session,
    *,
    only_stale: bool = False,
    urgency_filter: str | None = None,  # 'high' | 'critical'
    mine_user_id: int | None = None,
) -> list[PipelineColumn]:
    """Pipeline columns, optionally filtered.

    Filters compose: ``only_stale=True`` AND ``urgency_filter='critical'``
    AND ``mine_user_id=42`` is "show me my critical-and-stale POs".

    Vendor scope (``AppSetting.vendor_scope``) is applied unconditionally.
    """
    stmt = (
        select(PurchaseOrder)
        .where(col(PurchaseOrder.status).notin_([POStatus.RECEIVED, POStatus.CANCELLED]))
        .order_by(desc(PurchaseOrder.urgency), desc(PurchaseOrder.created_at))
    )
    if urgency_filter in ("high", "critical"):
        from packtrack.models import Urgency
        if urgency_filter == "critical":
            stmt = stmt.where(PurchaseOrder.urgency == Urgency.CRITICAL)
        else:  # high — include critical too, since critical >= high
            stmt = stmt.where(col(PurchaseOrder.urgency).in_([Urgency.HIGH, Urgency.CRITICAL]))
    if mine_user_id is not None:
        stmt = stmt.where(PurchaseOrder.created_by_id == mine_user_id)
    stmt = filter_pos_query(stmt, session, get_scope(session))
    open_pos = session.exec(stmt).all()
    if not open_pos:
        return [PipelineColumn(key=k, label=lbl) for k, lbl, _ in PIPELINE_COLUMNS]

    last_change = _last_status_change_at(session, [p.id for p in open_pos])
    now = datetime.utcnow()
    columns = {k: PipelineColumn(key=k, label=lbl) for k, lbl, _ in PIPELINE_COLUMNS}

    for po in open_pos:
        ck = _column_key_for(po.status)
        if ck is None:
            continue
        anchor = last_change.get(po.id, po.created_at) or po.created_at
        days = max(0, (now - anchor).days)
        threshold = STALE_THRESHOLDS_DAYS.get(ck, 9999)
        top_item = "(no items)"
        item_count = len(po.lines)
        if po.lines:
            top_item = po.lines[0].item.name
            if item_count > 1:
                top_item += f" + {item_count - 1} more"
        is_stale = days >= threshold
        if only_stale and not is_stale:
            continue
        columns[ck].cards.append(POCard(
            po=po,
            column_key=ck,
            days_in_stage=days,
            is_stale=is_stale,
            top_item=top_item,
            item_count=item_count,
        ))
    return [columns[k] for k, _, _ in PIPELINE_COLUMNS]


# ---------------------------------------------------------------------------
# Stock alerts (with suggested reorder qty)
# ---------------------------------------------------------------------------


def _suggested_reorder_qty(item: Item, buffer_days: int = 14) -> int:
    """Cover sea_lead_days + buffer at current daily usage. If no usage rate,
    fall back to 2× reorder_point to give a sane default."""
    if item.daily_usage_rate and item.daily_usage_rate > 0:
        days = max(item.sea_lead_days or 45, 30) + buffer_days
        target_stock = item.daily_usage_rate * days
        gap = max(0.0, target_stock - max(item.current_stock, 0))
        if gap > 0:
            return int(ceil(gap))
    if item.reorder_point and item.reorder_point > 0:
        return int(ceil(item.reorder_point * 2))
    return 100


def build_stock_alerts(session: Session) -> list[StockAlert]:
    stmt = filter_items_query(select(Item), get_scope(session))
    items = session.exec(stmt).all()
    alerts: list[StockAlert] = []
    for it in items:
        if not (it.reorder_point or it.critical_point):
            continue
        if it.critical_point and it.current_stock <= it.critical_point:
            severity = "critical"
        elif it.reorder_point and it.current_stock <= it.reorder_point:
            severity = "amber"
        else:
            continue
        days_left = (
            it.current_stock / it.daily_usage_rate
            if it.daily_usage_rate and it.daily_usage_rate > 0
            else None
        )
        alerts.append(StockAlert(
            item=it,
            severity=severity,
            days_left=round(days_left, 1) if days_left is not None else None,
            suggested_qty=_suggested_reorder_qty(it),
        ))
    alerts.sort(key=lambda a: (0 if a.severity == "critical" else 1,
                               a.days_left if a.days_left is not None else 9999))
    return alerts


# ---------------------------------------------------------------------------
# Activity feed
# ---------------------------------------------------------------------------


_HUMAN_STATUS = {
    "draft": "draft",
    "design_review": "design review",
    "design_rejected": "design rejected",
    "design_approved": "design approved",
    "pi_received": "PI uploaded",
    "pi_approved": "PI approved",
    "production": "production",
    "shipped": "shipped",
    "received": "received",
    "cancelled": "cancelled",
}


def _humanize(message: str) -> str:
    """Tidy raw event messages for the activity feed.

    Drops the seed-data ``(demo)`` marker, swaps ``Status: pi_received`` →
    ``Status: PI uploaded``, and strips trailing periods so consecutive items
    don't all end the same.
    """
    msg = (message or "").replace(" (demo)", "").replace("(demo)", "").strip()
    for code, human in _HUMAN_STATUS.items():
        msg = msg.replace(f"Status: {code}", f"status: {human}")
        msg = msg.replace(f"to {code.replace('_', ' ')}", f"to {human}")
    if msg.endswith("."):
        msg = msg[:-1]
    return msg


def build_activity(session: Session, *, limit: int = 8) -> list[ActivityItem]:
    """Recent events across all POs.

    Pulls a few extras then dedupes consecutive events for the same PO whose
    timestamps are within 5 seconds — those are usually a single status change
    that happened to log two near-identical rows (e.g. seed inserts).
    """
    rows = session.exec(
        select(POEvent, PurchaseOrder, User)
        .join(PurchaseOrder, POEvent.po_id == PurchaseOrder.id)
        .join(User, POEvent.actor_id == User.id, isouter=True)
        .order_by(desc(POEvent.created_at))
        .limit(limit * 3)
    ).all()

    out: list[ActivityItem] = []
    last_key: tuple[int, int] | None = None  # (po_id, second-bucket)
    for ev, po, user in rows:
        bucket = (po.id, int(ev.created_at.timestamp() / 5))
        if last_key == bucket:
            continue
        last_key = bucket
        out.append(ActivityItem(
            when=ev.created_at,
            po_id=po.id,
            po_number=po.po_number,
            actor_name=user.name if user else None,
            message=_humanize(ev.message),
            kind=ev.kind,
        ))
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def build_pulse(session: Session) -> Pulse:
    """Top-of-dashboard KPIs.

    open_spend: sum of totals for non-closed POs (running commitments).
    this_month_spend: POs created since the 1st of the current month.
    last_30d_delivered_value: POs that became received in the last 30 days.
    avg_cycle_days: median(received_at - created_at) over the last 90 days.

    All sums respect ``vendor_scope`` so the operator sees the numbers for
    the vendor they're focused on, not all-vendor totals.
    """
    now = datetime.utcnow()
    scope = get_scope(session)
    open_stmt = filter_pos_query(
        select(PurchaseOrder).where(
            col(PurchaseOrder.status).notin_([POStatus.RECEIVED, POStatus.CANCELLED])
        ),
        session, scope,
    )
    open_pos = session.exec(open_stmt).all()
    open_spend = sum(po.total for po in open_pos)
    currency = "USD"
    if open_pos:
        currencies = {(po.currency or "USD") for po in open_pos}
        if len(currencies) == 1:
            currency = next(iter(currencies))

    month_start = datetime(now.year, now.month, 1)
    this_month_spend = sum(
        po.total for po in session.exec(
            filter_pos_query(
                select(PurchaseOrder).where(PurchaseOrder.created_at >= month_start),
                session, scope,
            )
        ).all()
    )

    cutoff_30 = now - timedelta(days=30)
    delivered_30d = session.exec(
        filter_pos_query(
            select(PurchaseOrder)
            .where(PurchaseOrder.status == POStatus.RECEIVED)
            .where(PurchaseOrder.updated_at >= cutoff_30),
            session, scope,
        )
    ).all()
    last_30d_delivered_value = sum(po.total for po in delivered_30d)

    cutoff_90 = now - timedelta(days=90)
    cycles = []
    for po in session.exec(
        filter_pos_query(
            select(PurchaseOrder)
            .where(PurchaseOrder.status == POStatus.RECEIVED)
            .where(PurchaseOrder.updated_at >= cutoff_90),
            session, scope,
        )
    ).all():
        if po.created_at and po.updated_at:
            d = (po.updated_at - po.created_at).total_seconds() / 86400.0
            if d >= 0:
                cycles.append(d)
    avg_cycle = sum(cycles) / len(cycles) if cycles else None

    return Pulse(
        open_spend=open_spend,
        open_spend_currency=currency,
        this_month_spend=this_month_spend,
        last_30d_delivered_value=last_30d_delivered_value,
        avg_cycle_days=round(avg_cycle, 1) if avg_cycle is not None else None,
    )


def build_spend_trend(session: Session, *, months: int = 6) -> list[MonthlyBar]:
    """Total PO value created in each of the last ``months`` months. Used for
    the bar-chart on the dashboard. Currency-aware sums are out of scope —
    callers display in the dominant currency only."""
    now = datetime.utcnow()
    # Compute month buckets going back ``months - 1`` months
    buckets: list[tuple[int, int]] = []
    y, m = now.year, now.month
    for _ in range(months):
        buckets.append((y, m))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    buckets.reverse()

    earliest = datetime(buckets[0][0], buckets[0][1], 1)
    pos = session.exec(
        filter_pos_query(
            select(PurchaseOrder).where(PurchaseOrder.created_at >= earliest),
            session, get_scope(session),
        )
    ).all()

    sums: dict[tuple[int, int], float] = defaultdict(float)
    for po in pos:
        key = (po.created_at.year, po.created_at.month)
        sums[key] += po.total

    month_names = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ]
    return [
        MonthlyBar(
            label=month_names[m - 1],
            iso=f"{y:04d}-{m:02d}",
            value=sums.get((y, m), 0.0),
        )
        for y, m in buckets
    ]


def build_landing(session: Session, *, days: int = 7) -> list[LandingItem]:
    """In-transit shipments ETA-ing in the next ``days`` days."""
    today = date.today()
    horizon = today + timedelta(days=days)
    rows = session.exec(
        select(Shipment, PurchaseOrder)
        .join(PurchaseOrder, Shipment.po_id == PurchaseOrder.id)
        .where(Shipment.status == ShipStatus.IN_TRANSIT)
        .where(Shipment.eta.is_not(None))
        .where(Shipment.eta <= horizon)
        .order_by(Shipment.eta)
        .limit(20)
    ).all()
    out: list[LandingItem] = []
    for sh, po in rows:
        item_name = ""
        item_image = None
        item_unit = ""
        if sh.item_id is not None:
            it = session.get(Item, sh.item_id)
            if it is not None:
                item_name = it.name
                item_image = it.image_path
                item_unit = it.unit
        if not item_name and po.lines:
            item_name = po.lines[0].item.name
            item_image = po.lines[0].item.image_path
            item_unit = po.lines[0].item.unit
        out.append(LandingItem(
            eta=sh.eta,
            po_id=po.id,
            po_number=po.po_number,
            method=sh.method.value,
            quantity=float(sh.quantity or 0),
            unit=item_unit,
            item_name=item_name,
            item_image=item_image,
            carrier=sh.carrier,
        ))
    return out


def build_dashboard(session: Session, user: User) -> Dashboard:
    pipeline = build_pipeline(session)
    pipeline_total = sum(c.count for c in pipeline)
    inbox = build_inbox(session, user)
    stock_alerts: list[StockAlert] = []
    if user.role == Role.OWNER:
        stock_alerts = build_stock_alerts(session)
    activity = build_activity(session, limit=10)
    pulse = build_pulse(session) if user.role == Role.OWNER else Pulse(0, "USD", 0, 0, None)
    spend_trend = build_spend_trend(session) if user.role == Role.OWNER else []
    landing = build_landing(session)
    return Dashboard(
        user=user,
        pipeline=pipeline,
        inbox=inbox,
        stock_alerts=stock_alerts,
        activity=activity,
        pipeline_total=pipeline_total,
        pulse=pulse,
        spend_trend=spend_trend,
        landing=landing,
    )
