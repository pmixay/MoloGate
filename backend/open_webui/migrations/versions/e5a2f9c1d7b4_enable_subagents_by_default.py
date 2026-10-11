"""Mologate: turn sub-agents on for existing installs

Sub-agents (background ones included) are on by default now. Installs that stored the old default
get them switched on once; admins can still turn them off afterwards.

Revision ID: e5a2f9c1d7b4
Revises: d4c1a8e37b62
Create Date: 2026-10-11 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e5a2f9c1d7b4'
down_revision: str | None = 'd4c1a8e37b62'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KEYS = ('subagents.enable', 'subagents.background_enabled')


def upgrade() -> None:
    config = sa.table('config', sa.column('key', sa.Text), sa.column('value', sa.JSON))
    conn = op.get_bind()
    rows = conn.execute(sa.select(config.c.key, config.c.value).where(config.c.key.in_(KEYS))).all()
    for key, value in rows:
        if value is False or value == 'false':
            conn.execute(sa.update(config).where(config.c.key == key).values(value=True))


def downgrade() -> None:
    pass
