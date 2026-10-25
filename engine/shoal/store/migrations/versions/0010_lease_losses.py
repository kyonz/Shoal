"""count leases lost to dead workers

runs.lease_losses counts the passes in a row whose worker never released the
lease (it crashed or ran out of memory), so such a run fails instead of taking
down worker after worker.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-25 20:15:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('runs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('lease_losses', sa.Integer(), server_default='0', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('runs', schema=None) as batch_op:
        batch_op.drop_column('lease_losses')
