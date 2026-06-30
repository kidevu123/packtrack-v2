"""Zoho stock snapshot columns (v2.11.0).

Revision ID: i5j6k7l8m9n0
Revises: h4i5j6k7l8m9
Create Date: 2026-06-30

Adds two nullable columns to ``items`` so the inbound Zoho item sync
can record what Zoho currently reports without overwriting
PackTrack's local ``current_stock``:

* ``last_zoho_stock_snapshot`` — Decimal NUMERIC(18, 4). What Zoho said
  PackTrack's stock was at the last sync. Informational only.
* ``last_zoho_stock_snapshot_at`` — when that snapshot was taken.

These columns are NOT a source of truth. PackTrack adjustments remain
the only writer of ``current_stock`` for existing items. New items
seeded via the very first Zoho sync still get ``current_stock`` from
the upstream value (one-time initialization), but every subsequent
sync only updates the snapshot fields.

Additive only — no existing column or row is touched.
"""
from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel  # noqa: F401  -- alembic env imports sqlmodel types
from alembic import op

revision: str = "i5j6k7l8m9n0"
down_revision: str | None = "h4i5j6k7l8m9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "items",
        sa.Column("last_zoho_stock_snapshot", sa.Numeric(18, 4), nullable=True),
    )
    op.add_column(
        "items",
        sa.Column("last_zoho_stock_snapshot_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("items", "last_zoho_stock_snapshot_at")
    op.drop_column("items", "last_zoho_stock_snapshot")
