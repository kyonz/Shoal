"""idempotent run starts and run event retention

Runs keep the client's Idempotency-Key (unique per tenant), and run events get
a created_at index so old events of finished runs can be pruned.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-25 12:10:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('runs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('idempotency_key', sa.String(length=200), nullable=True))
        batch_op.create_index('ix_runs_idempotency', ['tenant_id', 'idempotency_key'], unique=True)
    with op.batch_alter_table('run_events', schema=None) as batch_op:
        batch_op.create_index('ix_run_events_created', ['created_at'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('run_events', schema=None) as batch_op:
        batch_op.drop_index('ix_run_events_created')
    with op.batch_alter_table('runs', schema=None) as batch_op:
        batch_op.drop_index('ix_runs_idempotency')
        batch_op.drop_column('idempotency_key')
