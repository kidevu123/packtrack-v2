"""material_consumption_events

Revision ID: dc6c48337264
Revises: c1d2e3f4a5b6
Create Date: 2026-05-26 22:06:22.995031

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = 'dc6c48337264'
down_revision: Union[str, None] = 'c1d2e3f4a5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'material_consumption_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('item_id', sa.Integer(), nullable=False),
        sa.Column('qty_consumed', sa.Float(), nullable=False),
        sa.Column('finished_lot_id', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
        sa.Column('finished_lot_number', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
        sa.Column('supplier_lot_number', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=True),
        sa.Column('packaging_lot_id', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=True),
        sa.Column('consumed_at', sa.DateTime(), nullable=False),
        sa.Column('received_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['item_id'], ['items.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('finished_lot_id', 'item_id', name='uq_consumption_lot_item'),
    )
    op.create_index(
        op.f('ix_material_consumption_events_finished_lot_id'),
        'material_consumption_events', ['finished_lot_id'], unique=False,
    )
    op.create_index(
        op.f('ix_material_consumption_events_item_id'),
        'material_consumption_events', ['item_id'], unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f('ix_material_consumption_events_item_id'),
        table_name='material_consumption_events',
    )
    op.drop_index(
        op.f('ix_material_consumption_events_finished_lot_id'),
        table_name='material_consumption_events',
    )
    op.drop_table('material_consumption_events')
