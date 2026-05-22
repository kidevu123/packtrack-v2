"""item_material_code — add nullable material_code with a partial unique index

Revision ID: a3f1b2c4d5e6
Revises: 58274d5910dc
Create Date: 2026-05-09 16:11:48

P1 of the PackTrack ↔ Luma integration. We need a stable, owner-controlled
material identity that maps to Luma ``material_item.code``. The candidates we
already have on ``items`` are unsuitable as-is:

* ``zoho_item_id`` is stable but opaque (numeric Zoho-internal); humans
  don't read or speak it. Bad as a shared key for printed POs and Luma UX.
* ``sku_code`` is indexed but **not** unique at the column level today, and
  may be empty for hand-created items.

So we add a dedicated ``items.material_code``:
  * Nullable — existing items predate it; backfill is a separate, auditable
    step (``scripts/audit_material_codes.py --apply-safe-defaults``).
  * Partial unique index where the value is not null — Postgres-native way
    to say "all populated codes must be distinct" without forcing the
    column NOT NULL on legacy rows.
  * No default. The owner picks codes deliberately.

A regular non-unique btree index is also created for filter / lookup. The
partial unique index sits next to it.
"""
from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel  # noqa: F401  -- alembic env imports sqlmodel types
from alembic import op

revision: str = "a3f1b2c4d5e6"
down_revision: str | None = "58274d5910dc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "items",
        sa.Column("material_code", sa.String(length=120), nullable=True),
    )
    # Filter/lookup index — non-unique, all rows including NULL.
    op.create_index(
        "ix_items_material_code",
        "items",
        ["material_code"],
    )
    # Uniqueness only among populated values; null rows are excluded.
    # Postgres-only — Alembic surfaces this via ``postgresql_where``.
    op.create_index(
        "ix_items_material_code_unique",
        "items",
        ["material_code"],
        unique=True,
        postgresql_where=sa.text("material_code IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_items_material_code_unique", table_name="items")
    op.drop_index("ix_items_material_code", table_name="items")
    op.drop_column("items", "material_code")
