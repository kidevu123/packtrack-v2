"""Add forecast_alert_sent_stock to items

Revision ID: f4a5b6c7d8e9
Revises: 58b071f4cab1
Create Date: 2026-05-26
"""
from alembic import op
import sqlalchemy as sa

revision = "f4a5b6c7d8e9"
down_revision = "58b071f4cab1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "items",
        sa.Column("forecast_alert_sent_stock", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("items", "forecast_alert_sent_stock")
