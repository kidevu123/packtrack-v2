"""material_consumption_events

Revision ID: dc6c48337264
Revises: c1d2e3f4a5b6
Create Date: 2026-05-26 22:06:22.995031

NOTE: This migration was authored directly on the LXC and only the compiled
.pyc survived in git. It has been reconstructed here from the bytecode
metadata so the revision graph resolves cleanly. The upgrade is idempotent
because the table already exists on the production database where this
revision was applied.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes


revision = "dc6c48337264"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None


def _has_table(bind, name: str) -> bool:
    inspector = sa.inspect(bind)
    return inspector.has_table(name)


def upgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "material_consumption_events"):
        return
    op.create_table(
        "material_consumption_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("qty_consumed", sa.Float(), nullable=False),
        sa.Column("finished_lot_id", sa.Integer(), nullable=True),
        sa.Column("finished_lot_number",
                  sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("supplier_lot_number",
                  sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("packaging_lot_id", sa.Integer(), nullable=True),
        sa.Column("consumed_at", sa.DateTime(), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "finished_lot_id", "item_id", "supplier_lot_number",
            name="uq_consumption_lot_item",
        ),
    )
    op.create_index(
        op.f("ix_material_consumption_events_finished_lot_id"),
        "material_consumption_events", ["finished_lot_id"], unique=False,
    )
    op.create_index(
        op.f("ix_material_consumption_events_item_id"),
        "material_consumption_events", ["item_id"], unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_material_consumption_events_item_id"),
        table_name="material_consumption_events",
    )
    op.drop_index(
        op.f("ix_material_consumption_events_finished_lot_id"),
        table_name="material_consumption_events",
    )
    op.drop_table("material_consumption_events")
