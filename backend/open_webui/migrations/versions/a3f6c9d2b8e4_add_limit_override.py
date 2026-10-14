"""Mologate: admin and team overrides of the fair-use share (phase 4).

Revision ID: a3f6c9d2b8e4
Revises: e1b4c7a9d3f6
"""

import sqlalchemy as sa
from alembic import op

revision = 'a3f6c9d2b8e4'
down_revision = 'e1b4c7a9d3f6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'limit_override',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('user_id', sa.Text(), nullable=True),
        sa.Column('granted_by', sa.Text(), nullable=False),
        sa.Column('extra_points', sa.Float(), nullable=False),
        sa.Column('until', sa.BigInteger(), nullable=True),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('revoked_at', sa.BigInteger(), nullable=True),
        sa.Column('revoked_by', sa.Text(), nullable=True),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.Column('updated_at', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_limit_override_user', 'limit_override', ['user_id'])


def downgrade():
    op.drop_table('limit_override')
