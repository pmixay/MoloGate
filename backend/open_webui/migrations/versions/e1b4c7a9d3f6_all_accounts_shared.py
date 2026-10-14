"""Mologate: every provider account is shared with the team; "Only me" is gone.

Private accounts and pending private sign-ins become shared. Their owners can still pause them.

Revision ID: e1b4c7a9d3f6
Revises: d7a3f9c2e5b1
"""

from alembic import op

revision = 'e1b4c7a9d3f6'
down_revision = 'd7a3f9c2e5b1'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("UPDATE provider_account SET sharing = 'shared' WHERE sharing = 'private'")
    op.execute("UPDATE provider_login SET sharing = 'shared' WHERE sharing = 'private'")


def downgrade():
    # Which accounts were private is not kept; they stay shared.
    pass
