"""Mologate: the allowance engine's decision on each ledger row.

Revision ID: d7a3f9c2e5b1
Revises: c4e8a2d6f1b9
"""

import sqlalchemy as sa
from alembic import op

revision = 'd7a3f9c2e5b1'
down_revision = 'c4e8a2d6f1b9'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('usage_event', sa.Column('decision', sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table('usage_event') as batch:
        batch.drop_column('decision')
