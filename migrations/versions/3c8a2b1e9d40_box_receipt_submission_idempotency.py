"""Add submission_id + submission_line_index for receiving-form idempotency.

Revision ID: 3c8a2b1e9d40
Revises: f4a5b6c7d8e9
Create Date: 2026-06-25

P0-1 (v2.4.1) — durable double-submit guard for the receiving form, using
schema-backed dedup that does NOT abuse ``box_number``. The previous
v2.4.1 attempt put a submission_id-derived prefix into ``box_number`` and
relied on ``uq_box_receipts_po_box`` — semantically wrong because
``box_number`` is the supplier-carton field in the operator-typed flow.

This migration adds the proper home for the idempotency key:

* ``submission_id`` (varchar 64, nullable) — hex token rendered into the
  receiving-form's hidden input. NULL for rows from any other flow
  (operator-typed supplier carton via /po/{id}/boxes, catchup, legacy).
* ``submission_line_index`` (int, nullable) — per-line position within
  one submission; lets two rows from the same submission coexist while
  still preventing a duplicate submit from creating two parallel sets.
* Partial UNIQUE index on (purchase_order_id, submission_id,
  submission_line_index) WHERE submission_id IS NOT NULL — the durable
  backstop. Rows with submission_id IS NULL (catchup, /po/{id}/boxes)
  are unaffected.

Postgres and SQLite both support partial indexes, so the raw-SQL
``CREATE UNIQUE INDEX ... WHERE`` works on either backend without
dialect splitting.
"""
from alembic import op
import sqlalchemy as sa


revision = "3c8a2b1e9d40"
down_revision = "f4a5b6c7d8e9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "box_receipts",
        sa.Column("submission_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "box_receipts",
        sa.Column("submission_line_index", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_box_receipts_submission_id",
        "box_receipts",
        ["submission_id"],
    )
    # Partial unique index — durable receiving-form idempotency guard.
    # Rows from any other flow (NULL submission_id) are unaffected.
    op.execute("""
        CREATE UNIQUE INDEX uq_box_receipts_po_submission
            ON box_receipts (purchase_order_id, submission_id, submission_line_index)
            WHERE submission_id IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_box_receipts_po_submission")
    op.drop_index("ix_box_receipts_submission_id", table_name="box_receipts")
    op.drop_column("box_receipts", "submission_line_index")
    op.drop_column("box_receipts", "submission_id")
