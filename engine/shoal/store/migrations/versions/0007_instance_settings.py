"""instance settings (the orchestrator, set from the instance console)

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-25 17:30:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

import shoal.store.tables

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('instance_settings',
    sa.Column('key', sa.String(length=100), nullable=False),
    sa.Column('value', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('updated_by', sa.String(length=200), nullable=True),
    sa.Column('updated_at', shoal.store.tables.UTCDateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )


def downgrade() -> None:
    op.drop_table('instance_settings')
