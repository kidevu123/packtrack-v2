"""item product_line + outbound zoho push state

Revision ID: d5e6f7a8b9c0
Revises: 3c8a2b1e9d40
Create Date: 2026-06-25 16:30:00

v2.5.0 inventory improvements:

* ``items.product_line`` — derived brand/product-line group for grouped
  browsing on /inventory. Nullable; backfilled here from existing names via
  the same pure helper the Zoho sync uses, so legacy rows are grouped
  immediately without waiting for the next sync. Indexed for GROUP BY counts
  and group filtering.
* ``items.zoho_push_status`` / ``zoho_push_error`` / ``zoho_push_attempted_at``
  — outbound (PackTrack -> Zoho) item-update sync state. No Zoho item-write
  path exists yet, so owner edits to Zoho-owned fields park as ``pending``
  until one is wired. Mirrors the ``purchase_orders.push_*`` columns.

All additions are nullable with no server default, so the change is safe for
existing production data (no table rewrite, no NOT NULL backfill lock).
"""
from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel  # noqa: F401  -- alembic env imports sqlmodel types
from alembic import op

revision: str = "d5e6f7a8b9c0"
down_revision: str | None = "3c8a2b1e9d40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "items",
        sa.Column("product_line", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "items",
        sa.Column("zoho_push_status", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "items",
        sa.Column("zoho_push_error", sa.Text(), nullable=True),
    )
    op.add_column(
        "items",
        sa.Column("zoho_push_attempted_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_items_product_line", "items", ["product_line"])

    # Backfill product_line from existing names using the shared pure helper,
    # so the value matches exactly what the next Zoho sync would compute.
    from packtrack.services.product_line import derive_product_line

    bind = op.get_bind()
    items = sa.table(
        "items",
        sa.column("id", sa.Integer),
        sa.column("name", sa.String),
        sa.column("product_line", sa.String),
    )
    rows = bind.execute(sa.select(items.c.id, items.c.name)).fetchall()
    for row in rows:
        bind.execute(
            items.update()
            .where(items.c.id == row.id)
            .values(product_line=derive_product_line(row.name))
        )


def downgrade() -> None:
    op.drop_index("ix_items_product_line", table_name="items")
    op.drop_column("items", "zoho_push_attempted_at")
    op.drop_column("items", "zoho_push_error")
    op.drop_column("items", "zoho_push_status")
    op.drop_column("items", "product_line")
