"""Mologate: generated provider account names; automatic resets are switched team-wide only

Accounts are named 'Account #' followed by 6 random characters from [0-9A-Za-z]; members no
longer choose or edit names. Existing accounts get a generated name once. The old labels cannot
be restored on downgrade.

The per-account allow_team_resets switch is dropped: the global auto-reset setting alone decides
whether reset coupons are spent. A downgrade restores the column with every account allowed.

Revision ID: e5a3c1f8d2b7
Revises: d8e2b6c4a9f3
Create Date: 2026-10-11 18:00:00.000000

"""

import secrets
import string
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e5a3c1f8d2b7'
down_revision: str | None = 'd8e2b6c4a9f3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ALPHABET = string.digits + string.ascii_letters


def upgrade() -> None:
    accounts = sa.table('provider_account', sa.column('id', sa.Text), sa.column('label', sa.Text))
    conn = op.get_bind()
    taken = set()
    for (account_id,) in conn.execute(sa.select(accounts.c.id).order_by(accounts.c.id)).all():
        label = None
        while label is None or label in taken:
            label = 'Account #' + ''.join(secrets.choice(ALPHABET) for _ in range(6))
        taken.add(label)
        conn.execute(sa.update(accounts).where(accounts.c.id == account_id).values(label=label))

    with op.batch_alter_table('provider_account') as batch_op:
        batch_op.drop_column('allow_team_resets')


def downgrade() -> None:
    with op.batch_alter_table('provider_account') as batch_op:
        batch_op.add_column(sa.Column('allow_team_resets', sa.Boolean(), nullable=False, server_default=sa.true()))
