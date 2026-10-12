"""Mologate: the usage ledger, one row per provider request.

Revision ID: a8d4e2c7f915
Revises: f7c2a9d4b8e1
"""

import sqlalchemy as sa
from alembic import op

revision = 'a8d4e2c7f915'
down_revision = 'f7c2a9d4b8e1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'usage_event',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('ts', sa.BigInteger(), nullable=False),
        sa.Column('user_id', sa.Text(), nullable=False),
        sa.Column('api_key_id', sa.Text(), nullable=True),
        sa.Column('account_id', sa.Text(), nullable=True),
        sa.Column('provider', sa.String(32), nullable=False),
        sa.Column('model_id', sa.Text(), nullable=False),
        sa.Column('source', sa.String(16), nullable=False),
        sa.Column('tier', sa.String(16), nullable=True),
        sa.Column('input_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('output_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('cached_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('reasoning_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('cost', sa.Float(), nullable=True),
        sa.Column('cost_source', sa.String(16), nullable=True),
        sa.Column('est_units', sa.Float(), nullable=True),
        sa.Column('units', sa.Float(), nullable=True),
        sa.Column('credits_charged', sa.Float(), nullable=False, server_default='0'),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('chat_id', sa.Text(), nullable=True),
        sa.Column('request_id', sa.Text(), nullable=True),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.Column('updated_at', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_usage_event_user_ts', 'usage_event', ['user_id', 'ts'])
    op.create_index('ix_usage_event_account_ts', 'usage_event', ['account_id', 'ts'])


def downgrade():
    op.drop_table('usage_event')
