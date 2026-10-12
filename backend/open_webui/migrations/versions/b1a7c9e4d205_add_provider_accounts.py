"""Mologate: provider accounts and short-lived device-login attempts.

Revision ID: b1a7c9e4d205
Revises: a6d4c8e2f1b3
"""

import sqlalchemy as sa
from alembic import op

revision = 'b1a7c9e4d205'
down_revision = 'a6d4c8e2f1b3'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'provider_account',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('owner_user_id', sa.Text(), nullable=False),
        sa.Column('provider', sa.String(32), nullable=False),
        sa.Column('plan', sa.Text(), nullable=False),
        sa.Column('external_account_id', sa.Text(), nullable=False),
        sa.Column('label', sa.Text(), nullable=False),
        sa.Column('secret', sa.Text(), nullable=False),
        sa.Column('sharing', sa.String(16), nullable=False),
        sa.Column('capacity_weight', sa.Float(), nullable=False),
        sa.Column('budget', sa.Text(), nullable=True),
        sa.Column('allow_team_resets', sa.Boolean(), nullable=False),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('models', sa.Text(), nullable=False),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.Column('updated_at', sa.BigInteger(), nullable=False),
        sa.UniqueConstraint('provider', 'external_account_id', name='uq_provider_account_external'),
    )
    op.create_index('ix_provider_account_owner', 'provider_account', ['owner_user_id'])
    op.create_table(
        'provider_login',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('owner_user_id', sa.Text(), nullable=False),
        sa.Column('secret', sa.Text(), nullable=False),
        sa.Column('label', sa.Text(), nullable=False),
        sa.Column('sharing', sa.String(16), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('account_id', sa.Text(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('expires_at', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.Column('updated_at', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_provider_login_owner', 'provider_login', ['owner_user_id'])


def downgrade():
    op.drop_table('provider_login')
    op.drop_table('provider_account')
