"""Mologate: turn web search on, through Exa, for existing installs

Web search is on by default now, with Exa as the engine. Installs that stored the old defaults get
it switched on once: an empty engine becomes Exa, and Exa's page text per result gets the new cap.
An engine an admin already picked is kept, and admins can still turn search off afterwards.

Revision ID: f3b7d2a9c4e1
Revises: e5a2f9c1d7b4
Create Date: 2026-10-11 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f3b7d2a9c4e1'
down_revision: str | None = 'e5a2f9c1d7b4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EXA_MAX_CONTENT_LENGTH = 3000


def upgrade() -> None:
    config = sa.table('config', sa.column('key', sa.Text), sa.column('value', sa.JSON))
    conn = op.get_bind()
    keys = ('web.search.enable', 'web.search.engine', 'web.search.exa_max_content_length')
    stored = dict(conn.execute(sa.select(config.c.key, config.c.value).where(config.c.key.in_(keys))).all())

    updates = {}
    if 'web.search.enable' in stored and stored['web.search.enable'] in (False, 'false'):
        updates['web.search.enable'] = True
    if 'web.search.engine' in stored and not stored['web.search.engine']:
        updates['web.search.engine'] = 'exa'
    if 'web.search.exa_max_content_length' in stored and stored['web.search.exa_max_content_length'] is None:
        updates['web.search.exa_max_content_length'] = EXA_MAX_CONTENT_LENGTH

    for key, value in updates.items():
        conn.execute(sa.update(config).where(config.c.key == key).values(value=value))


def downgrade() -> None:
    pass
