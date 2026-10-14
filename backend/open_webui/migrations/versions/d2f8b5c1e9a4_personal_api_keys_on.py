"""Mologate: personal API keys on for existing installs (fair use, phase 6)

API keys and the permission for members to make them are on by default now (docs/limits-and-
accounts.md §11). Installs that stored the old defaults get them switched on once; an admin can
still turn them off afterwards. Environment variables set at start still win.

Revision ID: d2f8b5c1e9a4
Revises: b6d1e8f3a2c7
"""

import sqlalchemy as sa
from alembic import op

revision = 'd2f8b5c1e9a4'
down_revision = 'b6d1e8f3a2c7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    config = sa.table('config', sa.column('key', sa.Text), sa.column('value', sa.JSON))
    conn = op.get_bind()
    keys = ('auth.enable_api_keys', 'user.permissions')
    stored = dict(conn.execute(sa.select(config.c.key, config.c.value).where(config.c.key.in_(keys))).all())
    if stored.get('auth.enable_api_keys') in (False, 'false'):
        conn.execute(sa.update(config).where(config.c.key == 'auth.enable_api_keys').values(value=True))
    permissions = stored.get('user.permissions')
    if isinstance(permissions, dict) and isinstance(permissions.get('features'), dict):
        if permissions['features'].get('api_keys') is False:
            permissions = {**permissions, 'features': {**permissions['features'], 'api_keys': True}}
            conn.execute(sa.update(config).where(config.c.key == 'user.permissions').values(value=permissions))


def downgrade() -> None:
    pass
