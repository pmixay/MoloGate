"""Mologate: where each chat's container lives.

Revision ID: b5d9e1f3a7c2
Revises: a8d4e2c7f915
"""

import sqlalchemy as sa
from alembic import op

revision = 'b5d9e1f3a7c2'
down_revision = 'a8d4e2c7f915'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'chat_container',
        sa.Column('context', sa.Text(), primary_key=True),
        sa.Column('chat_id', sa.Text(), nullable=True),
        sa.Column('user_id', sa.Text(), nullable=True),
        sa.Column('node_id', sa.Text(), nullable=True),
        sa.Column('network', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('files', sa.JSON(), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.Column('updated_at', sa.BigInteger(), nullable=False),
        sa.Column('last_active_at', sa.BigInteger(), nullable=False),
        sa.Column('moved_at', sa.BigInteger(), nullable=True),
    )
    op.create_index('ix_chat_container_chat_id', 'chat_container', ['chat_id'])


def downgrade():
    op.drop_table('chat_container')
