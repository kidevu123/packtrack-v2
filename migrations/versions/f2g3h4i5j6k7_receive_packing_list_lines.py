"""Receiving MVP v2.7.5 — manual packing-list expected lines.

Revision ID: f2g3h4i5j6k7
Revises: e1f2a3b4c5d7
Create Date: 2026-06-29

Adds ``receive_packing_list_lines`` so operators can record the vendor's
declared packing-list contents alongside the actual receive counts. The
existing ``receives.packing_list_attachment_id`` file pointer is
unchanged — these rows live independently of the uploaded file and v2.7.5
does no parsing (``source`` is always ``"manual"``).

Additive only — no existing table or column is touched.
"""
from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel  # noqa: F401  -- alembic env imports sqlmodel types
from alembic import op

revision: str = "f2g3h4i5j6k7"
down_revision: str | None = "e1f2a3b4c5d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "receive_packing_list_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "receive_id",
            sa.Integer(),
            sa.ForeignKey("receives.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "item_id",
            sa.Integer(),
            sa.ForeignKey("items.id"),
            nullable=False,
        ),
        sa.Column("vendor_case_number", sa.String(length=120), nullable=True),
        sa.Column("expected_quantity", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(length=20), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False, server_default="manual"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column(
            "created_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_receive_packing_list_lines_receive_id",
        "receive_packing_list_lines",
        ["receive_id"],
    )
    op.create_index(
        "ix_receive_packing_list_lines_item_id",
        "receive_packing_list_lines",
        ["item_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_receive_packing_list_lines_item_id",
        table_name="receive_packing_list_lines",
    )
    op.drop_index(
        "ix_receive_packing_list_lines_receive_id",
        table_name="receive_packing_list_lines",
    )
    op.drop_table("receive_packing_list_lines")
