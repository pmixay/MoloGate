"""Mologate: the usage ledger, one row per provider request (docs/limits-and-accounts.md, section 6).

Rows hold token counts and, when known, the cost in USD: what the provider reported (OpenRouter),
or an estimate from published prices (utils/model_prices.py). Prompts and replies are never stored.
Metering (utils/metering.py) adds ``outside`` rows: rises in an account's window that Mologate's own
requests do not explain, attributed to the owner. The credit columns are filled in phase 5.
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
    # Mologate: request rows leave units empty; they come from their tokens and the calibration
    # (utils/calibration.py), so a refit re-prices history. Outside rows carry their units and window.
    units = Column(Float, nullable=True)
    # 5h | weekly, for outside rows only
    window = Column(String(16), nullable=True)
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
            .where(
                UsageEvent.account_id == account_id,
                UsageEvent.ts >= since,
                UsageEvent.status == 'ok',
                UsageEvent.source != 'outside',
            )
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

    async def tokens_by_model(self, account_id: str, start: int, end: int) -> tuple[int, dict]:
        """Requests on an account with ts in (start, end], and their tokens by model. Failed requests
        count too: a stream cut off midway still used the account."""
        query = (
            select(
                UsageEvent.model_id,
                func.count(UsageEvent.id),
                func.sum(UsageEvent.input_tokens),
                func.sum(UsageEvent.output_tokens),
                func.sum(UsageEvent.cached_tokens),
                func.sum(UsageEvent.reasoning_tokens),
            )
            .where(
                UsageEvent.account_id == account_id,
                UsageEvent.ts > start,
                UsageEvent.ts <= end,
                UsageEvent.status.in_(('ok', 'error')),
                UsageEvent.source != 'outside',
            )
            .group_by(UsageEvent.model_id)
        )
        async with get_async_db_context() as db:
            rows = (await db.execute(query)).all()
        requests = 0
        tokens = {}
        for model_id, count, input_tokens, output_tokens, cached, reasoning in rows:
            requests += count
            tokens[model_id] = {
                'input_tokens': int(input_tokens or 0),
                'output_tokens': int(output_tokens or 0),
                'cached_tokens': int(cached or 0),
                'reasoning_tokens': int(reasoning or 0),
            }
        return requests, tokens

    async def member_tokens(self, user_id: str, since: int) -> list[dict]:
        """A member's requests and tokens since a time, by account and model."""
        query = select(
            UsageEvent.account_id,
            UsageEvent.provider,
            UsageEvent.model_id,
            UsageEvent.ts,
            UsageEvent.input_tokens,
            UsageEvent.output_tokens,
            UsageEvent.cached_tokens,
            UsageEvent.reasoning_tokens,
            UsageEvent.cost,
        ).where(
            UsageEvent.user_id == user_id,
            UsageEvent.ts >= since,
            UsageEvent.status.in_(('ok', 'error')),
            UsageEvent.source != 'outside',
            UsageEvent.account_id.is_not(None),
        )
        async with get_async_db_context() as db:
            rows = (await db.execute(query)).all()
        return [
            {
                'account_id': account_id,
                'provider': provider,
                'model_id': model_id,
                'ts': ts,
                'input_tokens': input_tokens or 0,
                'output_tokens': output_tokens or 0,
                'cached_tokens': cached or 0,
                'reasoning_tokens': reasoning or 0,
                'cost': cost,
            }
            for account_id, provider, model_id, ts, input_tokens, output_tokens, cached, reasoning, cost in rows
        ]

    async def outside_units(self, account_ids: list[str], since: int) -> dict:
        """Units used outside Mologate since a time, by account and window."""
        if not account_ids:
            return {}
        query = (
            select(UsageEvent.account_id, UsageEvent.window, func.sum(UsageEvent.units))
            .where(
                UsageEvent.account_id.in_(account_ids),
                UsageEvent.source == 'outside',
                UsageEvent.ts >= since,
            )
            .group_by(UsageEvent.account_id, UsageEvent.window)
        )
        async with get_async_db_context() as db:
            rows = (await db.execute(query)).all()
        result = {}
        for account_id, window, units in rows:
            result.setdefault(account_id, {})[window] = float(units or 0)
        return result


UsageEvents = UsageEventsTable()
