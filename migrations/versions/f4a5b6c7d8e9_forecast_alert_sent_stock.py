"""Add forecast_alert_sent_stock to items

Revision ID: f4a5b6c7d8e9
Revises: 58b071f4cab1
Create Date: 2026-05-26

NOTE: Reconstructed from compiled bytecode metadata after the source .py
file was lost. Upgrade is idempotent because the column already exists on
the production database.
"""
from alembic import op
import sqlalchemy as sa


revision = "f4a5b6c7d8e9"
down_revision = "58b071f4cab1"
branch_labels = None
depends_on = None


def _has_column(bind, table: str, column: str) -> bool:
    insp = sa.inspect(bind)
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "items", "forecast_alert_sent_stock"):
        return
    op.add_column(
        "items",
        sa.Column("forecast_alert_sent_stock", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("items", "forecast_alert_sent_stock")
