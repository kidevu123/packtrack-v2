"""box_receipts — supplier-box-level receiving rows

Revision ID: b7c2d8e1f4a9
Revises: a3f1b2c4d5e6
Create Date: 2026-05-09 17:30:00

P2 of the PackTrack ↔ Luma integration. Adds the table that records each
supplier box as its own row, with declared/counted quantities,
``packtrack_receipt_id`` (stable external id for Luma), and Luma push state.

Design choices:

* Enum-typed columns (``confidence``, ``luma_push_status``) are stored as
  plain ``VARCHAR`` rather than Postgres ``ENUM`` types. Reasons: (a) the
  StrEnum classes in Python already validate writes, (b) extending an
  enum (e.g. adding a new ``LumaPushStatus`` value) is a one-line model
  change with VARCHAR vs. an ``ALTER TYPE ... ADD VALUE`` migration, and
  (c) the project's earlier migrations are mixed on this — VARCHAR stays
  out of the way.

* ``luma_response`` is ``JSONB`` so we can query it (e.g. find rows where
  Luma returned a specific error code) without re-parsing TEXT.

* Two unique constraints:
    - ``(packtrack_receipt_id)`` — globally unique receipt id.
    - ``(purchase_order_id, box_number)`` — a supplier carton cannot be
      entered twice on the same PO.

* All datetimes default in Python (``datetime.utcnow``); we don't rely on
  Postgres ``now()`` so tests can override clocks deterministically.
"""
from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel  # noqa: F401  -- alembic env imports sqlmodel types
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b7c2d8e1f4a9"
down_revision: str | None = "a3f1b2c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "box_receipts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("packtrack_receipt_id", sa.String(length=40), nullable=False),
        sa.Column(
            "purchase_order_id",
            sa.Integer(),
            sa.ForeignKey("purchase_orders.id"),
            nullable=False,
        ),
        sa.Column(
            "shipment_id",
            sa.Integer(),
            sa.ForeignKey("shipments.id"),
            nullable=True,
        ),
        sa.Column(
            "item_id",
            sa.Integer(),
            sa.ForeignKey("items.id"),
            nullable=False,
        ),
        sa.Column("material_code", sa.String(length=120), nullable=True),
        sa.Column("material_name", sa.String(length=240), nullable=False),
        sa.Column("supplier", sa.String(length=200), nullable=True),
        sa.Column("supplier_lot_number", sa.String(length=120), nullable=True),
        sa.Column("box_number", sa.String(length=120), nullable=False),
        sa.Column("declared_quantity", sa.Float(), nullable=False),
        sa.Column("counted_quantity", sa.Float(), nullable=True),
        sa.Column("accepted_quantity", sa.Float(), nullable=False),
        sa.Column(
            "unit_of_measure",
            sa.String(length=20),
            nullable=False,
            server_default="EACH",
        ),
        sa.Column("confidence", sa.String(length=20), nullable=False),
        sa.Column(
            "received_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column(
            "luma_push_status",
            sa.String(length=40),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("luma_pushed_at", sa.DateTime(), nullable=True),
        sa.Column("luma_response", postgresql.JSONB, nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    op.create_index(
        "ix_box_receipts_packtrack_receipt_id",
        "box_receipts",
        ["packtrack_receipt_id"],
        unique=True,
    )
    op.create_index(
        "ix_box_receipts_purchase_order_id",
        "box_receipts",
        ["purchase_order_id"],
    )
    op.create_index(
        "ix_box_receipts_shipment_id",
        "box_receipts",
        ["shipment_id"],
    )
    op.create_index(
        "ix_box_receipts_item_id",
        "box_receipts",
        ["item_id"],
    )
    op.create_index(
        "ix_box_receipts_material_code",
        "box_receipts",
        ["material_code"],
    )
    op.create_index(
        "ix_box_receipts_received_at",
        "box_receipts",
        ["received_at"],
    )
    # A supplier box on the same PO is a hard duplicate.
    op.create_unique_constraint(
        "uq_box_receipts_po_box",
        "box_receipts",
        ["purchase_order_id", "box_number"],
    )

    # Drop the server_defaults — they were only needed at insert time so
    # existing-row backfill would succeed had this been a non-empty table.
    # The application is the source of truth for new rows.
    op.alter_column("box_receipts", "unit_of_measure", server_default=None)
    op.alter_column("box_receipts", "luma_push_status", server_default=None)


def downgrade() -> None:
    op.drop_constraint("uq_box_receipts_po_box", "box_receipts", type_="unique")
    op.drop_index("ix_box_receipts_received_at", table_name="box_receipts")
    op.drop_index("ix_box_receipts_material_code", table_name="box_receipts")
    op.drop_index("ix_box_receipts_item_id", table_name="box_receipts")
    op.drop_index("ix_box_receipts_shipment_id", table_name="box_receipts")
    op.drop_index("ix_box_receipts_purchase_order_id", table_name="box_receipts")
    op.drop_index("ix_box_receipts_packtrack_receipt_id", table_name="box_receipts")
    op.drop_table("box_receipts")
