"""sales_events

Revision ID: 58b071f4cab1
Revises: dc6c48337264
Create Date: 2026-05-27 02:24:20.138673

NOTE: Reconstructed from compiled bytecode metadata after the source .py file
was lost. Upgrade is idempotent because the table already exists on the
production database.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes


revision = "58b071f4cab1"
down_revision = "dc6c48337264"
branch_labels = None
depends_on = None


def _has_table(bind, name: str) -> bool:
    return sa.inspect(bind).has_table(name)


def upgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "sales_events"):
        return
    op.create_table(
        "sales_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("zoho_order_id",
                  sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("product_sku",
                  sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("qty_sold", sa.Integer(), nullable=False),
        sa.Column("sold_at", sa.DateTime(), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("zoho_order_id", "product_sku",
                            name="uq_sales_event_order"),
    )
    op.create_index(
        op.f("ix_sales_events_product_sku"),
        "sales_events", ["product_sku"], unique=False,
    )
    op.create_index(
        op.f("ix_sales_events_zoho_order_id"),
        "sales_events", ["zoho_order_id"], unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_sales_events_zoho_order_id"), table_name="sales_events")
    op.drop_index(op.f("ix_sales_events_product_sku"), table_name="sales_events")
    op.drop_table("sales_events")
