"""shared limits, queued webhooks and worker lanes

Adds the limiter lease and webhook inbox tables, runs.lane (existing runs go to
the standard lane), indexes for tenant quotas and, on Postgres, a fillfactor
that leaves room for HOT updates of runs and steps.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-25 12:40:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

import shoal.store.tables

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'limiter_leases',
        sa.Column('id', sa.String(length=40), nullable=False),
        sa.Column('key', sa.String(length=200), nullable=False),
        sa.Column('weight', sa.Integer(), nullable=False),
        sa.Column('expires_at', shoal.store.tables.UTCDateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('limiter_leases', schema=None) as batch_op:
        batch_op.create_index('ix_limiter_key_expires', ['key', 'expires_at'], unique=False)
    op.create_table(
        'webhook_inbox',
        sa.Column('id', sa.String(length=40), nullable=False),
        sa.Column('tenant_id', sa.String(length=40), nullable=False),
        sa.Column('webhook_id', sa.String(length=40), nullable=False),
        sa.Column('headers', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('next_attempt_at', shoal.store.tables.UTCDateTime(timezone=True), nullable=False),
        sa.Column('lease_expires_at', shoal.store.tables.UTCDateTime(timezone=True), nullable=True),
        sa.Column('result', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
        sa.Column('created_at', shoal.store.tables.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('webhook_inbox', schema=None) as batch_op:
        batch_op.create_index('ix_webhook_inbox_due', ['status', 'next_attempt_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_webhook_inbox_tenant_id'), ['tenant_id'], unique=False)
    with op.batch_alter_table('runs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('lane', sa.String(length=20), server_default='standard', nullable=False))
        batch_op.create_index('ix_runs_tenant_status', ['tenant_id', 'status'], unique=False)
        batch_op.create_index('ix_runs_lease', ['lease_expires_at'], unique=False)
    if op.get_bind().dialect.name == 'postgresql':
        op.execute('ALTER TABLE runs SET (fillfactor = 80)')
        op.execute('ALTER TABLE step_runs SET (fillfactor = 80)')


def downgrade() -> None:
    with op.batch_alter_table('webhook_inbox', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_webhook_inbox_tenant_id'))
        batch_op.drop_index('ix_webhook_inbox_due')
    op.drop_table('webhook_inbox')
    if op.get_bind().dialect.name == 'postgresql':
        op.execute('ALTER TABLE runs RESET (fillfactor)')
        op.execute('ALTER TABLE step_runs RESET (fillfactor)')
    with op.batch_alter_table('runs', schema=None) as batch_op:
        batch_op.drop_index('ix_runs_lease')
        batch_op.drop_index('ix_runs_tenant_status')
        batch_op.drop_column('lane')
    with op.batch_alter_table('limiter_leases', schema=None) as batch_op:
        batch_op.drop_index('ix_limiter_key_expires')
    op.drop_table('limiter_leases')
