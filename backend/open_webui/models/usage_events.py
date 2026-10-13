"""Mologate: the usage ledger, one row per provider request (docs/limits-and-accounts.md, section 6).

Rows hold token counts and, when known, the cost in USD: what the provider reported (OpenRouter),
or an estimate from published prices (utils/model_prices.py). Prompts and replies are never stored.
The unit and credit columns are filled once calibration and credits ship (phases 2 and 5).
"""

import time
import uuid

from sqlalchemy import BigInteger, Column, Float, Index, Integer, String, Text, func, select

from open_webui.internal.db import Base, get_async_db_context


class UsageEvent(Base):
    __tablename__ = 'usage_event'

    id = Column(Text, primary_key=True)
    ts = Column(BigInteger, nullable=False)
    user_id = Column(Text, nullable=False)
    api_key_id = Column(Text, nullable=True)
    account_id = Column(Text, nullable=True)
    provider = Column(String(32), nullable=False)
    model_id = Column(Text, nullable=False)
    # chat | subagent | task | api_key | scheduled | workflow | outside
    source = Column(String(16), nullable=False)
    # reserve | pool | credit | worker | owner_outside
    tier = Column(String(16), nullable=True)
    input_tokens = Column(Integer, nullable=False, default=0)
    output_tokens = Column(Integer, nullable=False, default=0)
    cached_tokens = Column(Integer, nullable=False, default=0)
    reasoning_tokens = Column(Integer, nullable=False, default=0)
    cost = Column(Float, nullable=True)
    # provider | estimate
    cost_source = Column(String(16), nullable=True)
    est_units = Column(Float, nullable=True)
    units = Column(Float, nullable=True)
    credits_charged = Column(Float, nullable=False, default=0)
    # ok | error | limited
    status = Column(String(16), nullable=False)
    chat_id = Column(Text, nullable=True)
    request_id = Column(Text, nullable=True)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)
    __table_args__ = (
        Index('ix_usage_event_user_ts', 'user_id', 'ts'),
        Index('ix_usage_event_account_ts', 'account_id', 'ts'),
    )


class UsageEventsTable:
    async def insert(self, **values) -> str:
        now = int(time.time())
        row = UsageEvent(id=str(uuid.uuid4()), ts=values.pop('ts', now), created_at=now, updated_at=now, **values)
        async with get_async_db_context() as db:
            db.add(row)
            await db.commit()
        return row.id

    async def by_member(self, account_id: str, since: int) -> list[dict]:
        """Each member's requests, tokens and cost on an account since a time."""
        query = (
            select(
                UsageEvent.user_id,
                func.count(UsageEvent.id),
                func.sum(UsageEvent.input_tokens),
                func.sum(UsageEvent.output_tokens),
                func.sum(UsageEvent.cost),
                func.count(UsageEvent.cost),
            )
            .where(UsageEvent.account_id == account_id, UsageEvent.ts >= since, UsageEvent.status == 'ok')
            .group_by(UsageEvent.user_id)
        )
        async with get_async_db_context() as db:
            rows = (await db.execute(query)).all()
        return [
            {
                'user_id': user_id,
                'requests': requests,
                'input_tokens': int(input_tokens or 0),
                'output_tokens': int(output_tokens or 0),
                'cost': round(cost, 4) if priced else None,
                # Requests whose cost is unknown (no price published for the model).
                'unpriced': requests - priced,
            }
            for user_id, requests, input_tokens, output_tokens, cost, priced in rows
        ]


UsageEvents = UsageEventsTable()
