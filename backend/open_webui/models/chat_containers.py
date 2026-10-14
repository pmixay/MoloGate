"""Mologate: where each chat's container lives (docs/container-nodes.md).

One row per container context (``chat:<id>`` or ``automation:<id>``): the node holding it, whether
its network is on, and the attachments already copied into it. The container itself, its files
and its running state live on the node.
"""

import time

from sqlalchemy import JSON, BigInteger, Boolean, Column, Text, delete, select, update

from open_webui.internal.db import Base, get_async_db_context


class ChatContainer(Base):
    __tablename__ = 'chat_container'

    context = Column(Text, primary_key=True)
    chat_id = Column(Text, nullable=True, index=True)
    user_id = Column(Text, nullable=True)
    node_id = Column(Text, nullable=True)
    network = Column(Boolean, nullable=False, default=True)
    # {file_id: {"path", "name", "size", "at"}}
    files = Column(JSON, nullable=True)
    # One-time notice for the agent, e.g. that the container had to start over on another node.
    note = Column(Text, nullable=True)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)
    last_active_at = Column(BigInteger, nullable=False)
    moved_at = Column(BigInteger, nullable=True)


def _as_dict(row: ChatContainer | None) -> dict | None:
    if row is None:
        return None
    return {
        'context': row.context,
        'chat_id': row.chat_id,
        'user_id': row.user_id,
        'node_id': row.node_id,
        'network': bool(row.network),
        'files': dict(row.files or {}),
        'note': row.note,
        'created_at': row.created_at,
        'updated_at': row.updated_at,
        'last_active_at': row.last_active_at,
        'moved_at': row.moved_at,
    }


class ChatContainersTable:
    async def get(self, context: str) -> dict | None:
        async with get_async_db_context() as db:
            return _as_dict(await db.get(ChatContainer, context))

    async def create(
        self, context: str, node_id: str, network: bool, chat_id: str | None = None, user_id: str | None = None
    ) -> dict:
        now = int(time.time())
        row = ChatContainer(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            node_id=node_id,
            network=network,
            files={},
            created_at=now,
            updated_at=now,
            last_active_at=now,
        )
        async with get_async_db_context() as db:
            existing = await db.get(ChatContainer, context)
            if existing is not None:
                return _as_dict(existing)
            db.add(row)
            await db.commit()
        return _as_dict(row)

    async def update(self, context: str, **values) -> None:
        values['updated_at'] = int(time.time())
        async with get_async_db_context() as db:
            await db.execute(update(ChatContainer).where(ChatContainer.context == context).values(**values))
            await db.commit()

    async def touch(self, context: str) -> None:
        now = int(time.time())
        async with get_async_db_context() as db:
            await db.execute(update(ChatContainer).where(ChatContainer.context == context).values(last_active_at=now))
            await db.commit()

    async def delete(self, context: str) -> None:
        async with get_async_db_context() as db:
            await db.execute(delete(ChatContainer).where(ChatContainer.context == context))
            await db.commit()

    async def all(self) -> list[dict]:
        async with get_async_db_context() as db:
            rows = (await db.execute(select(ChatContainer))).scalars().all()
        return [_as_dict(row) for row in rows]


ChatContainers = ChatContainersTable()
