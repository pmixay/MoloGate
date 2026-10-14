"""Mologate: credits, a one-time reward for each account a member adds to the pool (decision 7).

``credit_grant`` holds one row per provider account ever connected, keyed by the provider's own
account ID, so an account earns its subscription credits once, however often it is re-added
(decision 8). An API key's grant grows with the money spent through it. ``credit_entry`` is the
append-only ledger of balance changes: grants, spending in tier 3, forfeits on disconnect and
admin corrections. A member's balance is the sum of their entries.
"""

import time
import uuid

from sqlalchemy import BigInteger, Column, Float, Index, String, Text, UniqueConstraint, func, select

from open_webui.internal.db import Base, get_async_db_context


class CreditGrant(Base):
    __tablename__ = 'credit_grant'

    id = Column(Text, primary_key=True)
    user_id = Column(Text, nullable=False)
    provider = Column(String(32), nullable=False)
    external_account_id = Column(Text, nullable=False)
    plan = Column(String(32), nullable=False)
    # Credits granted so far: a subscription's once, an API key's as money is spent through it.
    amount = Column(Float, nullable=False, default=0)
    granted_at = Column(BigInteger, nullable=False)
    forfeited_at = Column(BigInteger, nullable=True)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)
    __table_args__ = (UniqueConstraint('provider', 'external_account_id', name='uq_credit_grant_account'),)


class CreditEntry(Base):
    __tablename__ = 'credit_entry'

    id = Column(Text, primary_key=True)
    user_id = Column(Text, nullable=False)
    delta = Column(Float, nullable=False)
    # grant | spend | forfeit | admin
    reason = Column(String(16), nullable=False)
    usage_event_id = Column(Text, nullable=True)
    # The grant a grant or forfeit entry belongs to.
    grant_id = Column(Text, nullable=True)
    ts = Column(BigInteger, nullable=False)
    created_at = Column(BigInteger, nullable=False)
    __table_args__ = (Index('ix_credit_entry_user_ts', 'user_id', 'ts'),)


def _entry(user_id: str, delta: float, reason: str, usage_event_id=None, grant_id=None) -> CreditEntry:
    now = int(time.time())
    return CreditEntry(
        id=str(uuid.uuid4()),
        user_id=user_id,
        delta=delta,
        reason=reason,
        usage_event_id=usage_event_id,
        grant_id=grant_id,
        ts=now,
        created_at=now,
    )


class CreditsTable:
    async def grant(self, user_id: str, provider: str, external_account_id: str, plan: str, amount: float):
        """A subscription's one-time grant. None when this provider account was ever granted before."""
        now = int(time.time())
        async with get_async_db_context() as db:
            existing = (
                await db.execute(
                    select(CreditGrant).where(
                        CreditGrant.provider == provider, CreditGrant.external_account_id == external_account_id
                    )
                )
            ).scalar()
            if existing is not None:
                return None
            grant = CreditGrant(
                id=str(uuid.uuid4()),
                user_id=user_id,
                provider=provider,
                external_account_id=external_account_id,
                plan=plan,
                amount=amount,
                granted_at=now,
                created_at=now,
                updated_at=now,
            )
            db.add(grant)
            db.add(_entry(user_id, amount, 'grant', grant_id=grant.id))
            await db.commit()
            return grant.id

    async def earn(self, user_id: str, provider: str, external_account_id: str, amount: float, usage_event_id=None):
        """An API key's credits for money spent through it, added to its grant as they are earned."""
        now = int(time.time())
        async with get_async_db_context() as db:
            grant = (
                await db.execute(
                    select(CreditGrant).where(
                        CreditGrant.provider == provider, CreditGrant.external_account_id == external_account_id
                    )
                )
            ).scalar()
            if grant is None:
                grant = CreditGrant(
                    id=str(uuid.uuid4()),
                    user_id=user_id,
                    provider=provider,
                    external_account_id=external_account_id,
                    plan='api',
                    amount=0,
                    granted_at=now,
                    created_at=now,
                    updated_at=now,
                )
                db.add(grant)
            grant.amount = (grant.amount or 0) + amount
            grant.updated_at = now
            db.add(_entry(user_id, amount, 'grant', usage_event_id=usage_event_id, grant_id=grant.id))
            await db.commit()

    async def forfeit(self, user_id: str, provider: str, external_account_id: str) -> float:
        """Forfeit what is left of this account's credits from the owner's balance; returns how many."""
        now = int(time.time())
        async with get_async_db_context() as db:
            grant = (
                await db.execute(
                    select(CreditGrant).where(
                        CreditGrant.provider == provider, CreditGrant.external_account_id == external_account_id
                    )
                )
            ).scalar()
            if grant is None:
                return 0.0
            # What this member got from the grant and has not forfeited yet, as far as their
            # balance still holds it (credits already spent stay spent).
            outstanding = (
                await db.execute(
                    select(func.sum(CreditEntry.delta)).where(
                        CreditEntry.grant_id == grant.id,
                        CreditEntry.user_id == user_id,
                        CreditEntry.reason.in_(('grant', 'forfeit')),
                    )
                )
            ).scalar() or 0.0
            balance = (
                await db.execute(select(func.sum(CreditEntry.delta)).where(CreditEntry.user_id == user_id))
            ).scalar() or 0.0
            amount = round(max(0.0, min(outstanding, balance)), 6)
            if amount > 0:
                db.add(_entry(user_id, -amount, 'forfeit', grant_id=grant.id))
            grant.forfeited_at, grant.updated_at = now, now
            await db.commit()
            return amount

    async def spend(self, user_id: str, credits: float, usage_event_id=None) -> float:
        """Charge credits for pool use past the share, never below zero; returns what was charged."""
        async with get_async_db_context() as db:
            balance = (
                await db.execute(select(func.sum(CreditEntry.delta)).where(CreditEntry.user_id == user_id))
            ).scalar() or 0.0
            charged = round(max(0.0, min(credits, balance)), 6)
            if charged > 0:
                db.add(_entry(user_id, -charged, 'spend', usage_event_id=usage_event_id))
                await db.commit()
            return charged

    async def adjust(self, user_id: str, delta: float) -> None:
        async with get_async_db_context() as db:
            db.add(_entry(user_id, delta, 'admin'))
            await db.commit()

    async def balance(self, user_id: str) -> float:
        async with get_async_db_context() as db:
            value = (
                await db.execute(select(func.sum(CreditEntry.delta)).where(CreditEntry.user_id == user_id))
            ).scalar()
        return round(float(value or 0.0), 6)

    async def entries(self, user_id: str, limit: int = 50) -> list[dict]:
        query = select(CreditEntry).where(CreditEntry.user_id == user_id).order_by(CreditEntry.ts.desc()).limit(limit)
        async with get_async_db_context() as db:
            rows = (await db.execute(query)).scalars().all()
        return [{'delta': row.delta, 'reason': row.reason, 'ts': row.ts} for row in rows]

    async def grants(self, user_id: str) -> list[dict]:
        query = select(CreditGrant).where(CreditGrant.user_id == user_id).order_by(CreditGrant.granted_at)
        async with get_async_db_context() as db:
            rows = (await db.execute(query)).scalars().all()
        return [
            {
                'provider': row.provider,
                'plan': row.plan,
                'amount': row.amount,
                'granted_at': row.granted_at,
                'forfeited_at': row.forfeited_at,
            }
            for row in rows
        ]


Credits = CreditsTable()
