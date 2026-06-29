"""Inventory adjustment sync metadata (v2.10.0).

Revision ID: h4i5j6k7l8m9
Revises: g3h4i5j6k7l8
Create Date: 2026-06-29

Two nullable columns on ``inventory_adjustments``:

* ``zoho_sync_warning`` — free-text. zoho-integration-service v1.34.0
  may return a ``STOCK_DRIFT_DETECTED`` warning in the response when its
  read of Zoho's current quantity disagrees with PackTrack's
  ``quantity_before``. The adjustment is still posted and marked
  SYNCED, but the warning needs to surface in the UI so the operator
  can investigate the drift.
* ``sync_attempt_count`` — small integer. Increments each time the
  orchestrator calls the integration service for this row (initial
  attempt + every retry). Drives the "tried N times" hint in history
  and helps spot a row that's failing repeatedly.

Additive only — no existing column or row is touched.
"""
from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel  # noqa: F401  -- alembic env imports sqlmodel types
from alembic import op

revision: str = "h4i5j6k7l8m9"
down_revision: str | None = "g3h4i5j6k7l8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "inventory_adjustments",
        sa.Column("zoho_sync_warning", sa.Text(), nullable=True),
    )
    op.add_column(
        "inventory_adjustments",
        sa.Column(
            "sync_attempt_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("inventory_adjustments", "sync_attempt_count")
    op.drop_column("inventory_adjustments", "zoho_sync_warning")
