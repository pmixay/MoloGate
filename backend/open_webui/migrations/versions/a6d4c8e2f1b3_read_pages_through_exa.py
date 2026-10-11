"""Mologate: read web pages through Exa for existing installs

Pages (fetch_url, links added to a chat) are read through Exa's Contents API by default now.
Installs still on the built-in reader switch to Exa once; another reader an admin picked is kept.
Without an Exa key, pages are still read directly.

Revision ID: a6d4c8e2f1b3
Revises: f3b7d2a9c4e1
Create Date: 2026-10-11 11:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a6d4c8e2f1b3'
down_revision: str | None = 'f3b7d2a9c4e1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    config = sa.table('config', sa.column('key', sa.Text), sa.column('value', sa.JSON))
    conn = op.get_bind()
    stored = conn.execute(sa.select(config.c.value).where(config.c.key == 'web.loader.engine')).first()
    if stored is not None and stored[0] in (None, '', 'safe_web'):
        conn.execute(sa.update(config).where(config.c.key == 'web.loader.engine').values(value='exa'))


def downgrade() -> None:
    pass
