"""Receiving vNext Stage 1 — case-first tables above box_receipts.

Revision ID: e1f2a3b4c5d7
Revises: d5e6f7a8b9c0
Create Date: 2026-06-25

v2.5.0 Stage 1 of Receiving vNext (design doc:
``docs/design/2026-06-25-receiving-vnext.md`` § 2.2 + § 6 stage 1).

This is **additive only** — no existing table is dropped, no existing
column is rewritten, no existing row is touched. All new tables are
empty after migration. ``BoxReceipt`` and the legacy
``/receive/{zoho_po_id}`` flow continue to behave exactly as before.

Adds:

1. ``AttachmentKind`` enum gains a ``packing_list`` value at the
   database level. The Python ``StrEnum`` is updated in
   ``packtrack/models.py``; this migration only needs to widen the
   stored values, which we do by inserting via raw SQL where the type
   is ``VARCHAR`` (it is — see initial schema). No Postgres ENUM type
   alteration needed.
2. ``receives`` — receive header / one delivery event:
     id, receive_number (UNIQUE), purchase_order_id (nullable FK),
     shipment_id (nullable FK), shipment_kind enum, tracking_number,
     carrier, delivery_date, received_by_user_id (FK), finalized_by_user_id
     (nullable FK), status enum (default ``draft``), notes,
     submission_id (UNIQUE, nullable), packing_list_attachment_id
     (nullable FK to attachments), expected_case_count,
     expected_case_range, created_at, updated_at, finalized_at,
     pushed_at.
3. ``receive_cases`` — one vendor-labeled carton per row:
     id, receive_id FK ON DELETE CASCADE, vendor_case_number (nullable
     varchar(120) — required at finalize, not Stage 1), sequence,
     case_kind enum (nullable), notes, created_at, updated_at.
     Partial UNIQUE ``uq_receive_cases_receive_case_number`` on
     ``(receive_id, vendor_case_number) WHERE vendor_case_number IS NOT NULL``.
4. ``receive_case_lines`` — item-level qty within a case:
     id, receive_case_id FK ON DELETE CASCADE, purchase_order_id FK,
     po_line_id (nullable FK), item_id FK, declared_quantity,
     counted_quantity, accepted_quantity, unit_of_measure,
     supplier_lot_number, photo_paths JSON, notes, box_receipt_id
     (nullable FK — populated only at finalize, Stage 2),
     created_at, updated_at.
5. ``box_receipts.receive_id`` (nullable FK to receives) and
   ``box_receipts.receive_case_line_id`` (nullable FK to
   receive_case_lines). These are **DB-only** in Stage 1 — the
   ``BoxReceipt`` Python model does not declare them yet, so existing
   ORM queries continue to ignore them. Stage 2 (finalize) will add
   the Python attributes and populate the columns from the materialize
   path.

No backfill of historical ``BoxReceipt`` rows; they keep NULL for both
new columns. Receiving vNext is forward-only.
"""
from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel  # noqa: F401  -- alembic env imports sqlmodel types
from alembic import op

revision: str = "e1f2a3b4c5d7"
down_revision: str | None = "d5e6f7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── 1. receives ────────────────────────────────────────────────────
    op.create_table(
        "receives",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("receive_number", sa.String(length=40), nullable=False),
        sa.Column(
            "purchase_order_id",
            sa.Integer(),
            sa.ForeignKey("purchase_orders.id"),
            nullable=True,
        ),
        sa.Column(
            "shipment_id",
            sa.Integer(),
            sa.ForeignKey("shipments.id"),
            nullable=True,
        ),
        sa.Column("shipment_kind", sa.String(length=20), nullable=False, server_default="parcel"),
        sa.Column("tracking_number", sa.String(length=120), nullable=True),
        sa.Column("carrier", sa.String(length=120), nullable=True),
        sa.Column("delivery_date", sa.Date(), nullable=False),
        sa.Column(
            "received_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "finalized_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("submission_id", sa.String(length=64), nullable=True),
        sa.Column(
            "packing_list_attachment_id",
            sa.Integer(),
            sa.ForeignKey("attachments.id"),
            nullable=True,
        ),
        sa.Column("expected_case_count", sa.Integer(), nullable=True),
        sa.Column("expected_case_range", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("finalized_at", sa.DateTime(), nullable=True),
        sa.Column("pushed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("receive_number", name="uq_receives_receive_number"),
        sa.UniqueConstraint("submission_id", name="uq_receives_submission_id"),
    )
    op.create_index("ix_receives_purchase_order_id", "receives", ["purchase_order_id"])
    op.create_index("ix_receives_status", "receives", ["status"])

    # ── 2. receive_cases ──────────────────────────────────────────────
    op.create_table(
        "receive_cases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "receive_id",
            sa.Integer(),
            sa.ForeignKey("receives.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("vendor_case_number", sa.String(length=120), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("case_kind", sa.String(length=20), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_receive_cases_receive_id", "receive_cases", ["receive_id"])
    # Partial UNIQUE — duplicate vendor_case_number within the same receive is
    # rejected; NULLs (drafting placeholder) are allowed. Postgres + SQLite
    # both support WHERE clauses on UNIQUE indexes.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_receive_cases_receive_case_number
            ON receive_cases (receive_id, vendor_case_number)
            WHERE vendor_case_number IS NOT NULL
        """
    )

    # ── 3. receive_case_lines ─────────────────────────────────────────
    op.create_table(
        "receive_case_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "receive_case_id",
            sa.Integer(),
            sa.ForeignKey("receive_cases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "purchase_order_id",
            sa.Integer(),
            sa.ForeignKey("purchase_orders.id"),
            nullable=False,
        ),
        sa.Column(
            "po_line_id",
            sa.Integer(),
            sa.ForeignKey("po_lines.id"),
            nullable=True,
        ),
        sa.Column(
            "item_id",
            sa.Integer(),
            sa.ForeignKey("items.id"),
            nullable=False,
        ),
        sa.Column("declared_quantity", sa.Float(), nullable=False),
        sa.Column("counted_quantity", sa.Float(), nullable=True),
        sa.Column("accepted_quantity", sa.Float(), nullable=True),
        sa.Column("unit_of_measure", sa.String(length=20), nullable=False, server_default="EACH"),
        sa.Column("supplier_lot_number", sa.String(length=120), nullable=True),
        sa.Column(
            "photo_paths",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "box_receipt_id",
            sa.Integer(),
            sa.ForeignKey("box_receipts.id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_receive_case_lines_receive_case_id", "receive_case_lines", ["receive_case_id"])
    op.create_index("ix_receive_case_lines_item_id", "receive_case_lines", ["item_id"])

    # ── 4. box_receipts: nullable upward FKs (Stage 2 will populate) ──
    op.add_column(
        "box_receipts",
        sa.Column(
            "receive_id",
            sa.Integer(),
            sa.ForeignKey("receives.id"),
            nullable=True,
        ),
    )
    op.add_column(
        "box_receipts",
        sa.Column(
            "receive_case_line_id",
            sa.Integer(),
            sa.ForeignKey("receive_case_lines.id"),
            nullable=True,
        ),
    )
    op.create_index("ix_box_receipts_receive_id", "box_receipts", ["receive_id"])


def downgrade() -> None:
    op.drop_index("ix_box_receipts_receive_id", table_name="box_receipts")
    op.drop_column("box_receipts", "receive_case_line_id")
    op.drop_column("box_receipts", "receive_id")
    op.drop_index("ix_receive_case_lines_item_id", table_name="receive_case_lines")
    op.drop_index("ix_receive_case_lines_receive_case_id", table_name="receive_case_lines")
    op.drop_table("receive_case_lines")
    op.execute("DROP INDEX IF EXISTS uq_receive_cases_receive_case_number")
    op.drop_index("ix_receive_cases_receive_id", table_name="receive_cases")
    op.drop_table("receive_cases")
    op.drop_index("ix_receives_status", table_name="receives")
    op.drop_index("ix_receives_purchase_order_id", table_name="receives")
    op.drop_table("receives")
