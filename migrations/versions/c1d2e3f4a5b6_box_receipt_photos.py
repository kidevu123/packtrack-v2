"""box_receipt_photos — photo_paths JSONB on box_receipts

Revision ID: c1d2e3f4a5b6
Revises: b7c2d8e1f4a9
Create Date: 2026-05-21

Adds a nullable JSONB column to store a list of photo filenames
(relative to uploads/receiving/) captured at receive time.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "c1d2e3f4a5b6"
down_revision = "b7c2d8e1f4a9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("box_receipts", sa.Column("photo_paths", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("box_receipts", "photo_paths")
