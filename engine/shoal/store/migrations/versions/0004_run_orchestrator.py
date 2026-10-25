"""runs record which orchestrator drives them

Existing runs belong to the built-in orchestrator.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-25 07:20:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('runs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('orchestrator', sa.String(length=20), server_default='builtin', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('runs', schema=None) as batch_op:
        batch_op.drop_column('orchestrator')
