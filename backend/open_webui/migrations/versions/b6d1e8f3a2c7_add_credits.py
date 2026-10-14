"""Mologate: credits for accounts added to the pool (phase 5, decision 7).

Revision ID: b6d1e8f3a2c7
Revises: a3f6c9d2b8e4
"""

import sqlalchemy as sa
from alembic import op

revision = 'b6d1e8f3a2c7'
down_revision = 'a3f6c9d2b8e4'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'credit_grant',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('user_id', sa.Text(), nullable=False),
        sa.Column('provider', sa.String(32), nullable=False),
        sa.Column('external_account_id', sa.Text(), nullable=False),
        sa.Column('plan', sa.String(32), nullable=False),
        sa.Column('amount', sa.Float(), nullable=False),
        sa.Column('granted_at', sa.BigInteger(), nullable=False),
        sa.Column('forfeited_at', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.Column('updated_at', sa.BigInteger(), nullable=False),
        sa.UniqueConstraint('provider', 'external_account_id', name='uq_credit_grant_account'),
    )
    op.create_table(
        'credit_entry',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('user_id', sa.Text(), nullable=False),
        sa.Column('delta', sa.Float(), nullable=False),
        sa.Column('reason', sa.String(16), nullable=False),
        sa.Column('usage_event_id', sa.Text(), nullable=True),
        sa.Column('grant_id', sa.Text(), nullable=True),
        sa.Column('ts', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_credit_entry_user_ts', 'credit_entry', ['user_id', 'ts'])


def downgrade():
    op.drop_table('credit_entry')
    op.drop_table('credit_grant')
