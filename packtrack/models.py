"""Domain model.

Intentionally smaller than v1 — see plan for what was dropped and why.
JSONB on POEvent.payload + ZohoMirror.line_items lets us query inside
those structures without parsing TEXT columns at the application layer.
"""
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import JSON, Column, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship, SQLModel


class Role(StrEnum):
    OWNER = "owner"
    AGENT = "agent"
    DESIGN = "design"
    RECEIVING = "receiving"


class POStatus(StrEnum):
    DRAFT = "draft"
    DESIGN_REVIEW = "design_review"
    DESIGN_REJECTED = "design_rejected"
    DESIGN_APPROVED = "design_approved"
    PI_RECEIVED = "pi_received"
    PI_APPROVED = "pi_approved"
    PRODUCTION = "production"
    SHIPPED = "shipped"
    RECEIVED = "received"
    CANCELLED = "cancelled"


class Urgency(StrEnum):
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class AttachmentKind(StrEnum):
    PI = "pi"
    ARTWORK = "artwork"
    OTHER = "other"
    # v2.5.0 — vendor's carton-by-carton packing list (PDF/CSV/XLSX).
    # Pointed at by Receive.packing_list_attachment_id; see
    # docs/design/2026-06-25-receiving-vnext.md § 0.6.
    PACKING_LIST = "packing_list"


class ShipMethod(StrEnum):
    EXPRESS = "express"
    SEA = "sea"


class ShipStatus(StrEnum):
    IN_TRANSIT = "in_transit"
    RECEIVED = "received"
    PARTIAL = "partial"


class Confidence(StrEnum):
    """How sure are we about the quantity on a ``BoxReceipt`` row?

    HIGH:    receiving team physically counted the box.
    MEDIUM:  declared quantity from the supplier label only — not verified.
    """
    HIGH = "high"
    MEDIUM = "medium"


class LumaPushStatus(StrEnum):
    """State of a single ``BoxReceipt`` w.r.t. the Luma webhook.

    PENDING:    valid for push but no attempt made yet (P5 will fire it).
    NOT_READY:  blocked from push because the row's material_code is empty.
                Cleanup happens via ``audit_material_codes.py`` plus owner
                edits; once material_code lands, the row flips to PENDING
                via the receiving UI / a background reaper (TBD in P5).
    DRY_RUN_OK: P4 dry-run returned 200; not yet pushed live.
    PUSHED:     P5 live push acknowledged by Luma.
    FAILED:     P5 live push errored — retry-eligible.
    DUPLICATE:  Luma replied with idempotency-hit on (packtrack_receipt_id,
                box_number); the row is treated as PUSHED for our purposes.
    """
    PENDING = "pending"
    NOT_READY = "not_ready"
    DRY_RUN_OK = "dry_run_ok"
    PUSHED = "pushed"
    FAILED = "failed"
    DUPLICATE = "duplicate"


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True, max_length=200)
    name: str = Field(max_length=120)
    role: Role
    password_hash: str
    is_active: bool = True
    telegram_chat_id: str | None = Field(default=None, max_length=40, index=True)
    # Pending Telegram action (upload_pi, upload_artwork, reject_pi_reason). 30-min TTL.
    tg_pending_action: str | None = None
    tg_pending_po_id: int | None = None
    tg_pending_set_at: datetime | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Item(SQLModel, table=True):
    __tablename__ = "items"

    id: int | None = Field(default=None, primary_key=True)
    zoho_item_id: str | None = Field(default=None, index=True, unique=True, max_length=80)
    name: str = Field(max_length=240, index=True)
    sku_code: str | None = Field(default=None, max_length=120, index=True)
    # ``material_code`` is an OPTIONAL, PackTrack-owned internal identity for
    # future Luma / BOM / consumption workflows. It is NOT required for normal
    # inventory usage and a missing code is not a blocker — ``zoho_item_id`` is
    # the required external sync key. Owner-controlled and decoupled from Zoho
    # ids so a Zoho re-keying does not break Luma maps. Nullable; a partial
    # unique index in Postgres enforces no-duplicates among populated values.
    material_code: str | None = Field(default=None, max_length=120, index=True)
    # Derived brand / product line for grouped browsing on /inventory.
    # Recomputed from ``name`` on every Zoho sync (see services/product_line.py)
    # and backfilled for existing rows. Nullable so legacy rows are safe until
    # the first sync/backfill runs; the UI coalesces null to the generic group.
    product_line: str | None = Field(default=None, max_length=120, index=True)
    vendor: str | None = Field(default=None, max_length=200)
    description: str | None = None
    unit: str = Field(default="units", max_length=40)
    current_stock: float = 0.0
    daily_usage_rate: float = 0.0
    forecast_alert_sent_stock: float | None = Field(default=None)
    reorder_point: float = 0.0
    reorder_point_locked: bool = False
    critical_point: float = 0.0
    sea_lead_days: int = 45
    express_lead_days: int = 7
    image_url: str | None = Field(default=None, max_length=500)
    image_path: str | None = Field(default=None, max_length=300)  # under static/uploads/items/
    last_unit_cost: float | None = None  # most recent purchase unit price
    last_synced_at: datetime | None = None
    # Outbound (PackTrack -> Zoho) item-update sync state. No item-write path
    # to Zoho exists yet (only PO push + receive go outbound), so an owner edit
    # to a Zoho-owned field (name/description/vendor/unit) is saved locally and
    # parked as ``pending`` until a write path is wired. See
    # services/zoho_item_sync.py. ``None`` = nothing to push / in sync.
    zoho_push_status: str | None = None  # None | pending | synced | failed
    zoho_push_error: str | None = None
    zoho_push_attempted_at: datetime | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class PurchaseOrder(SQLModel, table=True):
    __tablename__ = "purchase_orders"

    id: int | None = Field(default=None, primary_key=True)
    po_number: str = Field(unique=True, index=True, max_length=40)
    status: POStatus = POStatus.DRAFT
    urgency: Urgency = Urgency.NORMAL
    notes: str | None = None
    currency: str = Field(default="USD", max_length=10)
    created_by_id: int = Field(foreign_key="users.id")
    zoho_po_id: str | None = Field(default=None, index=True, max_length=80)
    push_status: str | None = None  # success | failed | None
    push_error: str | None = None
    push_attempted_at: datetime | None = None
    last_production_reminder_at: datetime | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    lines: list["POLine"] = Relationship(
        back_populates="po", sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )

    @property
    def total(self) -> float:
        return sum((line.line_total for line in self.lines), 0.0)

    @property
    def item_summary(self) -> list[dict]:
        """Roll up lines by item — one entry per distinct ``item_id``.

        Used by the board card, PO detail header, and print page to show "8
        lines, but really 5 distinct items at these quantities" instead of
        making the operator do the math from a flat table.

        Each entry: ``{item, total_qty, total_value, line_count, image_path, name, unit}``.
        Sorted by total_value desc so the highest-impact items come first.
        """
        bucket: dict[int, dict] = {}
        for line in self.lines:
            it = line.item
            if it is None:
                continue
            row = bucket.setdefault(
                it.id,
                {
                    "item": it,
                    "name": it.name,
                    "unit": it.unit,
                    "image_path": it.image_path,
                    "total_qty": 0.0,
                    "total_value": 0.0,
                    "line_count": 0,
                },
            )
            row["total_qty"] += float(line.quantity or 0)
            row["total_value"] += float(line.line_total or 0)
            row["line_count"] += 1
        return sorted(
            bucket.values(),
            key=lambda r: (-r["total_value"], r["name"]),
        )

    @property
    def lines_sorted(self) -> list["POLine"]:
        """Lines sorted by item name then unit_price — keeps duplicate-item
        rows clustered in the detail table."""
        return sorted(
            self.lines,
            key=lambda li: ((li.item.name if li.item else "").lower(), li.unit_price or 0),
        )
    events: list["POEvent"] = Relationship(
        back_populates="po", sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
    attachments: list["Attachment"] = Relationship(
        back_populates="po", sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
    shipments: list["Shipment"] = Relationship(
        back_populates="po", sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )

    @property
    def status_label(self) -> str:
        labels = {
            POStatus.DRAFT: "Draft",
            POStatus.DESIGN_REVIEW: "Design Review",
            POStatus.DESIGN_REJECTED: "Design Rejected",
            POStatus.DESIGN_APPROVED: "Design Approved",
            POStatus.PI_RECEIVED: "PI Received",
            POStatus.PI_APPROVED: "PI Approved",
            POStatus.PRODUCTION: "Production",
            POStatus.SHIPPED: "Shipped",
            POStatus.RECEIVED: "Received",
            POStatus.CANCELLED: "Cancelled",
        }
        return labels.get(self.status, self.status.value.replace("_", " ").title())

    @property
    def is_placeholder_number(self) -> bool:
        """True only when the PT- placeholder has outlived a push attempt —
        i.e., we tried to push to Zoho and failed (or are still waiting). For
        a brand-new draft we DON'T show "awaiting Zoho number" because we
        haven't tried pushing yet."""
        if not self.po_number.startswith("PT-"):
            return False
        return self.push_attempted_at is not None and self.zoho_po_id is None


class POLine(SQLModel, table=True):
    __tablename__ = "po_lines"

    id: int | None = Field(default=None, primary_key=True)
    po_id: int = Field(foreign_key="purchase_orders.id", index=True)
    item_id: int = Field(foreign_key="items.id", index=True)
    quantity: float
    unit_price: float = 0.0
    target_arrival: date | None = None
    received_quantity: float = 0.0
    line_notes: str | None = None

    po: PurchaseOrder = Relationship(back_populates="lines")
    item: Item = Relationship()

    @property
    def line_total(self) -> float:
        return float(self.quantity or 0) * float(self.unit_price or 0)


class POEvent(SQLModel, table=True):
    __tablename__ = "po_events"

    id: int | None = Field(default=None, primary_key=True)
    po_id: int = Field(foreign_key="purchase_orders.id", index=True)
    kind: str = Field(max_length=40)  # status_change | comment | attachment | sync | system
    message: str
    actor_id: int | None = Field(default=None, foreign_key="users.id")
    payload: dict | None = Field(default=None, sa_column=Column(JSONB().with_variant(JSON(), "sqlite")))
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)

    po: PurchaseOrder = Relationship(back_populates="events")


class Attachment(SQLModel, table=True):
    __tablename__ = "attachments"

    id: int | None = Field(default=None, primary_key=True)
    po_id: int = Field(foreign_key="purchase_orders.id", index=True)
    kind: AttachmentKind
    version: int = 1
    filename: str = Field(max_length=255)
    file_path: str | None = Field(default=None, max_length=500)
    external_url: str | None = Field(default=None, max_length=1000)
    source: str | None = Field(default=None, max_length=20)  # web | telegram | link
    uploaded_by_id: int | None = Field(default=None, foreign_key="users.id")
    notes: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

    po: PurchaseOrder = Relationship(back_populates="attachments")


class Shipment(SQLModel, table=True):
    __tablename__ = "shipments"

    id: int | None = Field(default=None, primary_key=True)
    po_id: int = Field(foreign_key="purchase_orders.id", index=True)
    item_id: int | None = Field(default=None, foreign_key="items.id")
    method: ShipMethod
    status: ShipStatus = ShipStatus.IN_TRANSIT
    quantity: float
    received_quantity: float | None = None
    shipped_date: date | None = None
    eta: date | None = None
    received_date: date | None = None
    tracking_number: str | None = Field(default=None, max_length=120)
    carrier: str | None = Field(default=None, max_length=120)
    notes: str | None = None
    discrepancy_notes: str | None = None

    po: PurchaseOrder = Relationship(back_populates="shipments")


class BoxReceipt(SQLModel, table=True):
    """One supplier carton/box, received and recorded.

    The smallest unit PackTrack tracks for receipts. Each box is its own
    row with its own ``packtrack_receipt_id`` (the shared identifier we
    hand to Luma). ``material_code``, ``material_name``, and ``supplier``
    are **snapshotted at receive time** so a later rename of the
    underlying ``Item`` does not retroactively change receiving history
    (or break Luma reconciliation).

    Field-level rules (enforced in ``services/box_receipt.py``):

    * ``accepted_quantity = counted_quantity if counted_quantity is not None
      else declared_quantity``.
    * ``confidence = HIGH`` iff a counted quantity was provided, else
      ``MEDIUM``.
    * ``luma_push_status = NOT_READY`` iff ``material_code`` is null/blank
      at receive time, else ``PENDING``. NOT_READY rows are excluded from
      every Luma push path (P3 builder will refuse them).

    ``box_number`` semantics depend on the flow that created the row:

    * ``POST /po/{id}/boxes`` (supplier-carton flow) — operator-typed
      real supplier/manufacturer carton identifier; UNIQUE per PO.
    * ``POST /receive/{po}`` (receiving form, post-v2.4.1) — a stable
      Luma-compatibility value derived from ``packtrack_receipt_id``,
      because Luma's current ``/api/integrations/packtrack/receipts``
      endpoint requires a non-empty ``box_number`` (``z.string().min(1)``).
      Receiving forms do not collect a per-box supplier identifier, so
      mirroring the receipt id is the documented contract until Luma
      relaxes that requirement.
    * ``services/receive_catchup.py`` (legacy/back-fill) —
      ``f"CATCHUP-{po_zoho_id}-{zoho_item_id}"``.
    * ``UNIQUE(purchase_order_id, box_number)`` — enforced for back-
      compat with the supplier-carton flow. Receive-form rows satisfy
      it by inheriting the globally-unique ``packtrack_receipt_id``.

    PackTrack's receiving-form idempotency is enforced by the partial
    unique index ``uq_box_receipts_po_submission`` on
    ``(purchase_order_id, submission_id, submission_line_index)``,
    **NOT** by ``box_number``. See ``submission_id`` below.

    Schema-level rules (enforced in migrations):

    * ``UNIQUE(packtrack_receipt_id)`` — every row has its own globally-
      unique receipt id (UUID4 by default). This is the key Luma pairs
      with ``box_number`` for its own idempotency.
    * ``UNIQUE(purchase_order_id, box_number)`` — see above.
    * Partial ``UNIQUE(purchase_order_id, submission_id,
      submission_line_index) WHERE submission_id IS NOT NULL`` —
      receiving-form double-submit guard (v2.4.1).
    """

    __tablename__ = "box_receipts"

    id: int | None = Field(default=None, primary_key=True)
    # The integer ``id`` is the local PK. ``packtrack_receipt_id`` is the
    # *external* identity we give Luma — independent of the int sequence so
    # a future re-key, a backup restore, or a multi-instance setup cannot
    # accidentally re-use a Luma-known id.
    packtrack_receipt_id: str = Field(
        index=True, unique=True, max_length=40,
        description="Stable external receipt id (UUID4). Sent to Luma as packtrack_receipt_id.",
    )

    purchase_order_id: int = Field(foreign_key="purchase_orders.id", index=True)
    shipment_id: int | None = Field(default=None, foreign_key="shipments.id", index=True)
    item_id: int = Field(foreign_key="items.id", index=True)

    # Snapshots at receive time — DO NOT update these from the underlying
    # Item later, even on Zoho re-sync. Receiving history must be
    # reproducible.
    material_code: str | None = Field(default=None, max_length=120, index=True)
    material_name: str = Field(max_length=240)
    supplier: str | None = Field(default=None, max_length=200)

    supplier_lot_number: str | None = Field(default=None, max_length=120)
    box_number: str = Field(max_length=120)

    # Receiving-form idempotency (added in migration 3c8a2b1e9d40, v2.4.1).
    # NULL on rows from any other flow — operator-typed supplier carton,
    # catchup, legacy. Together with ``submission_line_index`` and a
    # partial UNIQUE index on (po, submission_id, submission_line_index)
    # they make repeat POSTs of the same form a no-op without leaning on
    # ``box_number`` for dedup.
    submission_id: str | None = Field(default=None, max_length=64, index=True)
    submission_line_index: int | None = Field(default=None)

    declared_quantity: float
    counted_quantity: float | None = None
    accepted_quantity: float
    unit_of_measure: str = Field(default="EACH", max_length=20)
    confidence: Confidence

    received_by_user_id: int = Field(foreign_key="users.id")
    received_at: datetime = Field(default_factory=datetime.utcnow, index=True)

    luma_push_status: LumaPushStatus = Field(default=LumaPushStatus.PENDING)
    luma_pushed_at: datetime | None = None
    luma_response: dict | None = Field(default=None, sa_column=Column(JSONB().with_variant(JSON(), "sqlite")))

    # List of filenames under uploads/receiving/ — stored at receive time.
    photo_paths: list[str] | None = Field(default=None, sa_column=Column(JSONB().with_variant(JSON(), "sqlite")))

    notes: str | None = None

    # Receiving vNext Stage 2 (v2.6.0) — upward FKs into the case-first
    # model. The DB columns + indexes ship in Stage 1 migration
    # ``e1f2a3b4c5d7_receive_vnext_stage1``; this is the Python-side
    # declaration. NULL on rows from the legacy ``/receive/{zoho_po_id}``
    # flow, ``POST /po/{id}/boxes``, and catchup — only populated when
    # a Receive's finalize materializes a leaf. Legacy paths untouched.
    receive_id: int | None = Field(default=None, foreign_key="receives.id", index=True)
    receive_case_line_id: int | None = Field(default=None, foreign_key="receive_case_lines.id")

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    @property
    def is_luma_ready(self) -> bool:
        """True iff this row could be pushed to Luma right now (material_code
        present and the row is not already pushed or in a permanent block)."""
        if self.luma_push_status == LumaPushStatus.NOT_READY:
            return False
        return bool((self.material_code or "").strip())


class MaterialConsumptionEvent(SQLModel, table=True):
    """Audit log of packaging consumption pushes from Luma.

    One row per (finished_lot_id, item) pair — idempotent by unique constraint.
    Drives auto-maintenance of Item.current_stock and Item.daily_usage_rate.
    """
    __tablename__ = "material_consumption_events"
    __table_args__ = (
        UniqueConstraint(
            "finished_lot_id", "item_id",
            name="uq_consumption_lot_item",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    item_id: int = Field(foreign_key="items.id", index=True)
    qty_consumed: float
    finished_lot_id: str = Field(max_length=128, index=True)
    finished_lot_number: str = Field(max_length=128, default="")
    supplier_lot_number: str | None = Field(default=None, max_length=128)
    packaging_lot_id: str | None = Field(default=None, max_length=128)
    consumed_at: datetime
    received_at: datetime = Field(default_factory=datetime.utcnow)


class ZohoMirror(SQLModel, table=True):
    __tablename__ = "zoho_mirror"

    id: int | None = Field(default=None, primary_key=True)
    zoho_purchaseorder_id: str = Field(unique=True, index=True, max_length=80)
    purchaseorder_number: str | None = Field(default=None, max_length=80)
    vendor_name: str | None = Field(default=None, max_length=240)
    status: str | None = Field(default=None, max_length=60)
    date: str | None = Field(default=None, max_length=30)
    delivery_date: str | None = Field(default=None, max_length=30)
    total: float | None = None
    currency_code: str | None = Field(default=None, max_length=10)
    line_items: list[dict] | None = Field(default=None, sa_column=Column(JSONB().with_variant(JSON(), "sqlite")))
    synced_at: datetime = Field(default_factory=datetime.utcnow)


class SyncRun(SQLModel, table=True):
    __tablename__ = "sync_runs"

    id: int | None = Field(default=None, primary_key=True)
    started_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    finished_at: datetime | None = None
    status: str = Field(default="running", max_length=20)
    items_updated: int = 0
    items_created: int = 0
    po_mirrored: int = 0
    error_message: str | None = None
    triggered_by_id: int | None = Field(default=None, foreign_key="users.id")


class AppSetting(SQLModel, table=True):
    __tablename__ = "app_settings"

    key: str = Field(primary_key=True, max_length=80)
    value: str | None = None
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    updated_by_id: int | None = Field(default=None, foreign_key="users.id")


class SalesEvent(SQLModel, table=True):
    """Audit log of Zoho sales confirmed events.

    One row per zoho_order_id — idempotent by unique constraint.
    Feeds daily_usage_rate recomputation and forecasting.
    """
    __tablename__ = "sales_events"
    __table_args__ = (
        UniqueConstraint("zoho_order_id", name="uq_sales_event_order"),
    )

    id: int | None = Field(default=None, primary_key=True)
    zoho_order_id: str = Field(max_length=128, index=True)
    product_sku: str = Field(max_length=128, index=True)
    qty_sold: int
    sold_at: datetime
    received_at: datetime = Field(default_factory=datetime.utcnow)


# ──────────────────────────────────────────────────────────────────────
# Receiving vNext (v2.5.0 Stage 1) — case-first model. See
# docs/design/2026-06-25-receiving-vnext.md. Gated by
# settings.RECEIVING_VNEXT_ENABLED at the route layer; data layer is
# always loaded so migrations + tests work, but no UI/path materializes
# rows unless the flag is on.
#
# Stage 1 is draft + counting only:
#   * Receive: header / one physical delivery event
#   * ReceiveCase: one vendor-labeled carton in this receive
#   * ReceiveCaseLine: item rows inside each case
# Finalize, BoxReceipt materialization, Zoho push, and Luma push are
# deferred to Stage 2 (v2.6.0).
# ──────────────────────────────────────────────────────────────────────


class ShipmentKind(StrEnum):
    PARCEL = "parcel"
    PALLETIZED = "palletized"


class ReceiveStatus(StrEnum):
    DRAFT = "draft"
    COUNTING = "counting"
    REVIEW = "review"
    # Stage 2 (v2.6.0) terminal states — defined here so the column enum
    # is forward-compatible without a follow-up migration.
    FINALIZED = "finalized"
    PUSHED_OK = "pushed_ok"
    PUSH_FAILED = "push_failed"
    CANCELLED = "cancelled"


class CaseKind(StrEnum):
    MASTER_CASE = "master_case"
    DISPLAY_CASE = "display_case"
    PALLET = "pallet"
    LOOSE = "loose"
    OTHER = "other"


class Receive(SQLModel, table=True):
    """One physical delivery event from a vendor.

    A Receive groups vendor-labeled cases (``ReceiveCase``) and the
    item-level quantity rows inside them (``ReceiveCaseLine``). It is
    the case-first replacement for the legacy receive form's one-row-
    per-PO-line flow. Stage 1 (v2.5.0) supports draft + counting only;
    finalize / push are Stage 2.

    ``purchase_order_id`` is nullable in the schema (future multi-PO
    support — see design § 2.3) but the route layer requires it for v1.

    ``receive_number`` is a human-friendly server-generated identifier
    ``R-YYYY-NNNN`` (yearly sequence). It is what operators quote when
    talking about a receive.

    ``submission_id`` carries the v2.4.1 idempotency token upward from
    the receive layer; it is generated at creation time and used by
    finalize (Stage 2) to drive ``BoxReceipt.submission_id`` so a
    re-submitted finalize is a no-op at the DB layer.

    ``packing_list_attachment_id`` is a direct FK to the single primary
    packing-list ``Attachment`` for this receive (decision § 0.6); the
    schema for multi-attachment will follow a join table when needed.
    """

    __tablename__ = "receives"

    id: int | None = Field(default=None, primary_key=True)
    receive_number: str = Field(max_length=40, unique=True)
    purchase_order_id: int | None = Field(default=None, foreign_key="purchase_orders.id", index=True)
    shipment_id: int | None = Field(default=None, foreign_key="shipments.id")
    shipment_kind: ShipmentKind = ShipmentKind.PARCEL
    tracking_number: str | None = Field(default=None, max_length=120)
    carrier: str | None = Field(default=None, max_length=120)
    delivery_date: date
    received_by_user_id: int = Field(foreign_key="users.id")
    finalized_by_user_id: int | None = Field(default=None, foreign_key="users.id")
    status: ReceiveStatus = Field(default=ReceiveStatus.DRAFT, index=True)
    notes: str | None = None
    submission_id: str | None = Field(default=None, max_length=64, unique=True)
    packing_list_attachment_id: int | None = Field(default=None, foreign_key="attachments.id")
    expected_case_count: int | None = None
    expected_case_range: str | None = Field(default=None, max_length=40)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    finalized_at: datetime | None = None
    pushed_at: datetime | None = None


class ReceiveCase(SQLModel, table=True):
    """One vendor-labeled carton/case in a Receive.

    ``vendor_case_number`` is permissive free text (``"1"``,
    ``"C-001"``, ``"BOX-A-7"``). Nullable while drafting; required at
    finalize (Stage 2). A partial UNIQUE index on
    ``(receive_id, vendor_case_number) WHERE vendor_case_number IS NOT NULL``
    prevents accidental duplicate case numbers within one receive.
    """

    __tablename__ = "receive_cases"

    id: int | None = Field(default=None, primary_key=True)
    receive_id: int = Field(foreign_key="receives.id", index=True)
    vendor_case_number: str | None = Field(default=None, max_length=120)
    sequence: int
    case_kind: CaseKind | None = None
    notes: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ReceiveCaseLine(SQLModel, table=True):
    """Item-level quantity within a single ReceiveCase.

    ``purchase_order_id`` is required (so a future multi-PO Receive can
    attribute each line to the right PO). ``po_line_id`` is the
    specific PO line the operator picked, if any. ``item_id`` is
    required.

    ``box_receipt_id`` is populated only at finalize (Stage 2), when
    the case-line materializes a leaf ``BoxReceipt``.
    """

    __tablename__ = "receive_case_lines"

    id: int | None = Field(default=None, primary_key=True)
    receive_case_id: int = Field(foreign_key="receive_cases.id", index=True)
    purchase_order_id: int = Field(foreign_key="purchase_orders.id")
    po_line_id: int | None = Field(default=None, foreign_key="po_lines.id")
    item_id: int = Field(foreign_key="items.id", index=True)
    declared_quantity: float
    counted_quantity: float | None = None
    accepted_quantity: float | None = None
    unit_of_measure: str = Field(default="EACH", max_length=20)
    supplier_lot_number: str | None = Field(default=None, max_length=120)
    photo_paths: list[str] | None = Field(
        default=None, sa_column=Column(JSONB().with_variant(JSON(), "sqlite"))
    )
    notes: str | None = None
    box_receipt_id: int | None = Field(default=None, foreign_key="box_receipts.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
