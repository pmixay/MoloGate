"""Mologate: provider accounts without an owner (team accounts)

An admin can connect a key or a ChatGPT account that belongs to no member: it serves the whole
team, only admins manage it, and all of its use is pool use. Admin connections to a supported
provider become such accounts. A downgrade removes team accounts, since the old schema requires
an owner.

Revision ID: f7c2a9d4b8e1
Revises: e5a3c1f8d2b7
Create Date: 2026-10-12 08:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f7c2a9d4b8e1'
down_revision: str | None = 'e5a3c1f8d2b7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('provider_account') as batch_op:
        batch_op.alter_column('owner_user_id', existing_type=sa.Text(), nullable=True)


def downgrade() -> None:
    accounts = sa.table('provider_account', sa.column('owner_user_id', sa.Text))
    op.get_bind().execute(sa.delete(accounts).where(accounts.c.owner_user_id.is_(None)))
    with op.batch_alter_table('provider_account') as batch_op:
        batch_op.alter_column('owner_user_id', existing_type=sa.Text(), nullable=False)
