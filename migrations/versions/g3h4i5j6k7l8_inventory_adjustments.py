"""Inventory adjustments ledger (v2.9.0).

Revision ID: g3h4i5j6k7l8
Revises: f2g3h4i5j6k7
Create Date: 2026-06-29

Adds ``inventory_adjustments`` — the immutable movement / stock-correction
ledger that becomes the only sanctioned way to mutate
``items.current_stock`` from the UI under v2.9.0+. Adjustment rows are
append-only by convention (no UPDATE route, no DELETE route); a
correction is a NEW row whose ``reversal_of_adjustment_id`` points at the
row it cancels.

Quantity columns are ``NUMERIC(18, 4)`` so the math is Decimal-safe.
``items.current_stock`` itself stays ``DOUBLE PRECISION`` for now — that
column lives on the master-data editor's surface area and is being
reshaped on a different branch.

Additive only. No existing table or column is touched.
"""
from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel  # noqa: F401  -- alembic env imports sqlmodel types
from alembic import op

revision: str = "g3h4i5j6k7l8"
down_revision: str | None = "f2g3h4i5j6k7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "inventory_adjustments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "item_id",
            sa.Integer(),
            sa.ForeignKey("items.id"),
            nullable=False,
        ),
        sa.Column("adjustment_number", sa.String(length=40), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("direction", sa.String(length=20), nullable=False),
        sa.Column("quantity_before", sa.Numeric(18, 4), nullable=False),
        sa.Column("quantity_delta", sa.Numeric(18, 4), nullable=False),
        sa.Column("quantity_after", sa.Numeric(18, 4), nullable=False),
        sa.Column("reason_code", sa.String(length=40), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column(
            "source", sa.String(length=30), nullable=False,
            server_default="manual_adjustment",
        ),
        sa.Column(
            "zoho_sync_status", sa.String(length=20), nullable=False,
            server_default="not_configured",
        ),
        sa.Column("zoho_sync_error", sa.Text(), nullable=True),
        sa.Column("zoho_synced_at", sa.DateTime(), nullable=True),
        sa.Column("zoho_reference", sa.String(length=120), nullable=True),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("voided_at", sa.DateTime(), nullable=True),
        sa.Column(
            "voided_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("void_reason", sa.Text(), nullable=True),
        sa.Column(
            "reversal_of_adjustment_id",
            sa.Integer(),
            sa.ForeignKey("inventory_adjustments.id"),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "adjustment_number", name="uq_inventory_adjustments_adjustment_number",
        ),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_inventory_adjustments_idempotency_key",
        ),
    )
    op.create_index(
        "ix_inventory_adjustments_item_id", "inventory_adjustments", ["item_id"],
    )
    op.create_index(
        "ix_inventory_adjustments_created_at",
        "inventory_adjustments", ["created_at"],
    )
    op.create_index(
        "ix_inventory_adjustments_zoho_sync_status",
        "inventory_adjustments", ["zoho_sync_status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_inventory_adjustments_zoho_sync_status",
        table_name="inventory_adjustments",
    )
    op.drop_index(
        "ix_inventory_adjustments_created_at",
        table_name="inventory_adjustments",
    )
    op.drop_index(
        "ix_inventory_adjustments_item_id",
        table_name="inventory_adjustments",
    )
    op.drop_table("inventory_adjustments")
