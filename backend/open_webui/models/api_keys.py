"""Mologate: personal API keys, several named ones per member (docs/limits-and-accounts.md §11).

Keys live in Open WebUI's ``api_key`` table. A personal key is stored as a SHA-256 hash and shown to
its owner once, when it is made; ``data`` holds its name, a short hint to recognise it by, its
limits and when it was revoked. A revoked key is kept, so the ledger's rows still name it. The one
key Open WebUI made per member before (id ``key_<user id>``, stored as it is) keeps working and is
listed as the default key.
"""

import hashlib
import time
import uuid

from sqlalchemy import or_, select

from open_webui.internal.db import get_async_db_context
from open_webui.models.users import ApiKey

HASH_PREFIX = 'sha256:'
LEGACY_PREFIX = 'key_'
# last_used_at is written at most this often per key.
TOUCH_SECONDS = 60


def hash_key(key: str) -> str:
    return HASH_PREFIX + hashlib.sha256(key.encode()).hexdigest()


def new_key() -> str:
    return 'sk-' + uuid.uuid4().hex + uuid.uuid4().hex[:16]


def hint(key: str) -> str:
    """Enough of a key to tell it apart, never enough to use it."""
    return f'{key[:5]}…{key[-4:]}'


def is_legacy(row) -> bool:
    return row.id.startswith(LEGACY_PREFIX)


def view(row) -> dict:
    """A key as its owner sees it. Never the key itself."""
    data = row.data if isinstance(row.data, dict) else {}
    legacy = is_legacy(row)
    return {
        'id': row.id,
        'name': data.get('name') or ('Default key' if legacy else 'Key'),
        'hint': data.get('hint') or (hint(row.key) if legacy and not row.key.startswith(HASH_PREFIX) else None),
        'limits': data.get('limits') or {},
        'legacy': legacy,
        'created_at': row.created_at,
        'last_used_at': row.last_used_at,
        'expires_at': row.expires_at,
        'revoked_at': data.get('revoked_at'),
    }


def usable(row, now: float | None = None) -> str | None:
    """Why a key cannot be used now, or None."""
    now = time.time() if now is None else now
    data = row.data if isinstance(row.data, dict) else {}
    if data.get('revoked_at'):
        return 'This API key was revoked.'
    if row.expires_at and row.expires_at <= now:
        return 'This API key has expired.'
    return None


class PersonalKeysTable:
    async def find(self, key: str):
        """The row of a key presented by a client: a personal key by its hash, the default key as stored."""
        if not key or key.startswith(HASH_PREFIX):
            # A stored hash is never a key.
            return None
        async with get_async_db_context() as db:
            return (
                (await db.execute(select(ApiKey).where(or_(ApiKey.key == hash_key(key), ApiKey.key == key))))
                .scalars()
                .first()
            )

    async def get(self, user_id: str, key_id: str):
        async with get_async_db_context() as db:
            row = await db.get(ApiKey, key_id)
            return row if row is not None and row.user_id == user_id else None

    async def list(self, user_id: str) -> list:
        query = select(ApiKey).where(ApiKey.user_id == user_id).order_by(ApiKey.created_at)
        async with get_async_db_context() as db:
            return list((await db.execute(query)).scalars().all())

    async def create(self, user_id: str, name: str, limits: dict, expires_at: int | None) -> tuple[dict, str]:
        """A new key: its view, and the key itself, which is shown this once."""
        key = new_key()
        now = int(time.time())
        row = ApiKey(
            id=str(uuid.uuid4()),
            user_id=user_id,
            key=hash_key(key),
            data={'name': name, 'hint': hint(key), 'limits': limits, 'revoked_at': None},
            expires_at=expires_at,
            created_at=now,
            updated_at=now,
        )
        async with get_async_db_context() as db:
            db.add(row)
            await db.commit()
            return view(row), key

    async def update(self, user_id: str, key_id: str, values: dict):
        async with get_async_db_context() as db:
            row = await db.get(ApiKey, key_id)
            if row is None or row.user_id != user_id:
                return None
            data = dict(row.data or {})
            for name in ('name', 'limits', 'revoked_at'):
                if name in values:
                    data[name] = values[name]
            row.data = data
            if 'expires_at' in values:
                row.expires_at = values['expires_at']
            row.updated_at = int(time.time())
            await db.commit()
            await db.refresh(row)
            return row

    async def revoke(self, user_id: str, key_id: str):
        return await self.update(user_id, key_id, {'revoked_at': int(time.time())})

    async def touch(self, row) -> None:
        now = int(time.time())
        if row.last_used_at and now - row.last_used_at < TOUCH_SECONDS:
            return
        async with get_async_db_context() as db:
            stored = await db.get(ApiKey, row.id)
            if stored is not None:
                stored.last_used_at = now
                await db.commit()


PersonalKeys = PersonalKeysTable()
