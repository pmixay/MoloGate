"""Mologate: send uploaded documents to the model as full text, for existing installs

Uploads skip embeddings and the vector database by default now: a file's extracted text goes to the
model whole, like a plain attachment, until per-chat sandboxes receive the files themselves.
Installs that stored the old default get it switched once; admins can turn retrieval back on in
Admin Settings > Documents.

Revision ID: d8e2b6c4a9f3
Revises: c3d9e7a5b1f0
Create Date: 2026-10-11 15:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd8e2b6c4a9f3'
down_revision: str | None = 'c3d9e7a5b1f0'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    config = sa.table('config', sa.column('key', sa.Text), sa.column('value', sa.JSON))
    conn = op.get_bind()
    key = 'rag.bypass_embedding_and_retrieval'
    stored = conn.execute(sa.select(config.c.value).where(config.c.key == key)).first()
    if stored is not None and stored[0] in (False, 'false'):
        conn.execute(sa.update(config).where(config.c.key == key).values(value=True))


def downgrade() -> None:
    pass
