"""Provider credentials belong to accounts, independently of connection ordering."""

import secrets
import string
import time
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import BigInteger, Column, Float, Index, String, Text, UniqueConstraint, delete, func, select

from open_webui.internal.db import Base, JSONField, get_async_db_context
from open_webui.models.oauth_sessions import OAuthSessions

Provider = Literal['codex', 'anthropic_api', 'openai_api', 'openrouter', 'zai']
# Mologate: every account serves the whole team; its owner can only pause it.
Sharing = Literal['shared', 'paused']


class ProviderAccount(Base):
    __tablename__ = 'provider_account'

    id = Column(Text, primary_key=True)
    # Mologate: no owner means a team account, which serves everyone and admins manage.
    owner_user_id = Column(Text, nullable=True)
    provider = Column(String(32), nullable=False)
    plan = Column(Text, nullable=False)
    external_account_id = Column(Text, nullable=False)
    label = Column(Text, nullable=False)
    secret = Column(Text, nullable=False)
    sharing = Column(String(16), nullable=False, default='shared')
    capacity_weight = Column(Float, nullable=False, default=1)
    budget = Column(JSONField, nullable=True)
    status = Column(String(32), nullable=False, default='ok')
    last_error = Column(Text, nullable=True)
    models = Column(JSONField, nullable=False, default=list)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)
    __table_args__ = (
        UniqueConstraint('provider', 'external_account_id', name='uq_provider_account_external'),
        Index('ix_provider_account_owner', 'owner_user_id'),
    )


class ProviderLogin(Base):
    __tablename__ = 'provider_login'

    id = Column(Text, primary_key=True)
    owner_user_id = Column(Text, nullable=False)
    secret = Column(Text, nullable=False)
    label = Column(Text, nullable=False)
    sharing = Column(String(16), nullable=False)
    status = Column(String(16), nullable=False, default='pending')
    account_id = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    expires_at = Column(BigInteger, nullable=False)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)
    __table_args__ = (Index('ix_provider_login_owner', 'owner_user_id'),)


class LimitResetEvent(Base):
    """One automatic redemption of a provider reset coupon. Kept after the account is removed."""

    __tablename__ = 'limit_reset_event'

    id = Column(Text, primary_key=True)
    account_id = Column(Text, nullable=False)
    provider = Column(String(32), nullable=False)
    triggered_by = Column(Text, nullable=True)
    credit_id = Column(Text, nullable=True)
    idempotency_key = Column(Text, nullable=False)
    outcome = Column(String(32), nullable=False)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)
    __table_args__ = (Index('ix_limit_reset_event_provider', 'provider', 'created_at'),)


class AccountView(BaseModel):
    """An explicit allowlist: encrypted secrets and fingerprints never leave the server."""

    model_config = ConfigDict(from_attributes=True)
    id: str
    owner_user_id: str | None
    provider: Provider
    plan: str
    label: str
    sharing: Sharing
    capacity_weight: float
    budget: dict | None
    status: str
    last_error: str | None
    models: list[dict] = Field(default_factory=list)
    created_at: int
    updated_at: int


NAME_ALPHABET = string.digits + string.ascii_letters


def generate_label() -> str:
    """Accounts are named by Mologate, never by members: 'Account #' and 6 random characters."""
    return 'Account #' + ''.join(secrets.choice(NAME_ALPHABET) for _ in range(6))


def seal_secret(data: dict) -> str:
    return OAuthSessions._encrypt_token(data)


def open_secret(value: str) -> dict:
    return OAuthSessions._decrypt_token(value)


class ProviderAccountsTable:
    # Mologate: counts changes to accounts in this process, so the allowance gate's cached pool
    # (utils/allowance_gate.py) is rebuilt as soon as an account is added, changed or removed.
    version = 0

    async def get(self, account_id: str):
        async with get_async_db_context() as db:
            return await db.get(ProviderAccount, account_id)

    async def list(self):
        async with get_async_db_context() as db:
            result = await db.execute(select(ProviderAccount).order_by(ProviderAccount.created_at, ProviderAccount.id))
            return list(result.scalars().all())

    async def unique_label(self, preferred: str | None = None, include_logins: bool = False) -> str:
        """A generated name no account (and, when asked, no pending sign-in) uses yet."""
        async with get_async_db_context() as db:
            taken = set((await db.execute(select(ProviderAccount.label))).scalars())
            if include_logins:
                taken |= set(
                    (await db.execute(select(ProviderLogin.label).where(ProviderLogin.status == 'pending'))).scalars()
                )
        if preferred and preferred not in taken:
            return preferred
        for _ in range(100):
            label = generate_label()
            if label not in taken:
                return label
        raise RuntimeError('No free account name.')

    async def insert(self, *, secret: dict, label: str | None = None, **values):
        now = int(time.time())
        account = ProviderAccount(
            id=str(uuid.uuid4()),
            label=await self.unique_label(label),
            secret=seal_secret(secret),
            created_at=now,
            updated_at=now,
            **values,
        )
        async with get_async_db_context() as db:
            db.add(account)
            await db.commit()
            await db.refresh(account)
        ProviderAccountsTable.version += 1
        return account

    async def update(self, account_id: str, values: dict):
        async with get_async_db_context() as db:
            account = await db.get(ProviderAccount, account_id)
            if account is None:
                return None
            for key, value in values.items():
                setattr(account, key, value)
            account.updated_at = int(time.time())
            await db.commit()
            await db.refresh(account)
        ProviderAccountsTable.version += 1
        return account

    async def delete(self, account_id: str):
        async with get_async_db_context() as db:
            await db.execute(delete(ProviderAccount).where(ProviderAccount.id == account_id))
            await db.commit()
        ProviderAccountsTable.version += 1


ProviderAccounts = ProviderAccountsTable()

# A pending or failed attempt may still have spent the coupon, so it blocks a retry too.
RESET_BLOCKING_OUTCOMES = ('pending', 'reset', 'error')


class LimitResetEventsTable:
    async def start(self, account, triggered_by: str | None, credit_id: str | None):
        now = int(time.time())
        event = LimitResetEvent(
            id=str(uuid.uuid4()),
            account_id=account.id,
            provider=account.provider,
            triggered_by=triggered_by,
            credit_id=credit_id,
            idempotency_key=str(uuid.uuid4()),
            outcome='pending',
            created_at=now,
            updated_at=now,
        )
        async with get_async_db_context() as db:
            db.add(event)
            await db.commit()
            await db.refresh(event)
            return event

    async def finish(self, event_id: str, outcome: str):
        async with get_async_db_context() as db:
            event = await db.get(LimitResetEvent, event_id)
            if event is not None:
                event.outcome = outcome
                event.updated_at = int(time.time())
                await db.commit()

    async def recent(self, provider: str, seconds: int) -> bool:
        async with get_async_db_context() as db:
            result = await db.execute(
                select(LimitResetEvent.id)
                .where(
                    LimitResetEvent.provider == provider,
                    LimitResetEvent.outcome.in_(RESET_BLOCKING_OUTCOMES),
                    LimitResetEvent.created_at > int(time.time()) - seconds,
                )
                .limit(1)
            )
            return result.first() is not None

    async def last_resets(self) -> dict[str, int]:
        """The latest successful automatic reset per account."""
        async with get_async_db_context() as db:
            result = await db.execute(
                select(LimitResetEvent.account_id, func.max(LimitResetEvent.created_at))
                .where(LimitResetEvent.outcome == 'reset')
                .group_by(LimitResetEvent.account_id)
            )
            return {account_id: created_at for account_id, created_at in result.all()}


LimitResetEvents = LimitResetEventsTable()
