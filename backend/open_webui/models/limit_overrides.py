"""Mologate: extra pool points an admin grants a member, or the whole team (docs/limits-and-accounts.md §6).

An override adds `extra_points` percentage points of the pool to a member's extra (section 7.2), until
it ends or is revoked. Rows are never deleted: a revoked one keeps who revoked it and when, so the
table is also the audit log of every override ever granted.
"""

import time
import uuid

from sqlalchemy import BigInteger, Column, Float, Index, Text, or_, select

from open_webui.internal.db import Base, get_async_db_context


class LimitOverride(Base):
    __tablename__ = 'limit_override'

    id = Column(Text, primary_key=True)
    # Empty: the whole team, every member alike.
    user_id = Column(Text, nullable=True)
    granted_by = Column(Text, nullable=False)
    extra_points = Column(Float, nullable=False)
    # When it ends; empty: until revoked.
    until = Column(BigInteger, nullable=True)
    reason = Column(Text, nullable=False)
    revoked_at = Column(BigInteger, nullable=True)
    revoked_by = Column(Text, nullable=True)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)
    __table_args__ = (Index('ix_limit_override_user', 'user_id'),)


def as_dict(row: LimitOverride) -> dict:
    return {
        column: getattr(row, column)
        for column in (
            'id',
            'user_id',
            'granted_by',
            'extra_points',
            'until',
            'reason',
            'revoked_at',
            'revoked_by',
            'created_at',
        )
    }


class LimitOverridesTable:
    async def grant(self, user_id: str | None, granted_by: str, extra_points: float, until, reason: str) -> dict:
        now = int(time.time())
        row = LimitOverride(
            id=str(uuid.uuid4()),
            user_id=user_id,
            granted_by=granted_by,
            extra_points=extra_points,
            until=until,
            reason=reason,
            created_at=now,
            updated_at=now,
        )
        async with get_async_db_context() as db:
            db.add(row)
            await db.commit()
            return as_dict(row)

    async def revoke(self, override_id: str, revoked_by: str) -> dict | None:
        now = int(time.time())
        async with get_async_db_context() as db:
            row = await db.get(LimitOverride, override_id)
            if row is None:
                return None
            if row.revoked_at is None:
                row.revoked_at, row.revoked_by, row.updated_at = now, revoked_by, now
                await db.commit()
            return as_dict(row)

    async def list(self) -> list[dict]:
        query = select(LimitOverride).order_by(LimitOverride.created_at.desc())
        async with get_async_db_context() as db:
            return [as_dict(row) for row in (await db.execute(query)).scalars().all()]

    async def active_points(self, user_id: str, now: float | None = None) -> float:
        """The member's extra pool points from overrides in force: their own and the team's."""
        now = time.time() if now is None else now
        query = select(LimitOverride.extra_points).where(
            or_(LimitOverride.user_id == user_id, LimitOverride.user_id.is_(None)),
            LimitOverride.revoked_at.is_(None),
            or_(LimitOverride.until.is_(None), LimitOverride.until > now),
        )
        async with get_async_db_context() as db:
            return float(sum((await db.execute(query)).scalars().all()))


LimitOverrides = LimitOverridesTable()
