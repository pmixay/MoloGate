"""Mologate: automatic provider limit resets.

Revision ID: c3d9e7a5b1f0
Revises: b1a7c9e4d205
"""

import sqlalchemy as sa
from alembic import op

revision = 'c3d9e7a5b1f0'
down_revision = 'b1a7c9e4d205'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'limit_reset_event',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('account_id', sa.Text(), nullable=False),
        sa.Column('provider', sa.String(32), nullable=False),
        sa.Column('triggered_by', sa.Text(), nullable=True),
        sa.Column('credit_id', sa.Text(), nullable=True),
        sa.Column('idempotency_key', sa.Text(), nullable=False),
        sa.Column('outcome', sa.String(32), nullable=False),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.Column('updated_at', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_limit_reset_event_provider', 'limit_reset_event', ['provider', 'created_at'])


def downgrade():
    op.drop_table('limit_reset_event')
