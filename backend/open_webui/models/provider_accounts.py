"""Provider credentials belong to accounts, independently of connection ordering."""

import time
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import BigInteger, Boolean, Column, Float, Index, String, Text, UniqueConstraint, delete, select

from open_webui.internal.db import Base, JSONField, get_async_db_context
from open_webui.models.oauth_sessions import OAuthSessions

Provider = Literal['codex', 'anthropic_api', 'openai_api', 'openrouter', 'zai']
Sharing = Literal['shared', 'private', 'paused']


class ProviderAccount(Base):
    __tablename__ = 'provider_account'

    id = Column(Text, primary_key=True)
    owner_user_id = Column(Text, nullable=False)
    provider = Column(String(32), nullable=False)
    plan = Column(Text, nullable=False)
    external_account_id = Column(Text, nullable=False)
    label = Column(Text, nullable=False)
    secret = Column(Text, nullable=False)
    sharing = Column(String(16), nullable=False, default='private')
    capacity_weight = Column(Float, nullable=False, default=1)
    budget = Column(JSONField, nullable=True)
    allow_team_resets = Column(Boolean, nullable=False, default=True)
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


class AccountView(BaseModel):
    """An explicit allowlist: encrypted secrets and fingerprints never leave the server."""

    model_config = ConfigDict(from_attributes=True)
    id: str
    owner_user_id: str
    provider: Provider
    plan: str
    label: str
    sharing: Sharing
    capacity_weight: float
    budget: dict | None
    allow_team_resets: bool
    status: str
    last_error: str | None
    models: list[dict] = Field(default_factory=list)
    created_at: int
    updated_at: int


def seal_secret(data: dict) -> str:
    return OAuthSessions._encrypt_token(data)


def open_secret(value: str) -> dict:
    return OAuthSessions._decrypt_token(value)


class ProviderAccountsTable:
    async def get(self, account_id: str):
        async with get_async_db_context() as db:
            return await db.get(ProviderAccount, account_id)

    async def list(self):
        async with get_async_db_context() as db:
            result = await db.execute(select(ProviderAccount).order_by(ProviderAccount.created_at, ProviderAccount.id))
            return list(result.scalars().all())

    async def insert(self, *, secret: dict, **values):
        now = int(time.time())
        account = ProviderAccount(
            id=str(uuid.uuid4()), secret=seal_secret(secret), created_at=now, updated_at=now, **values
        )
        async with get_async_db_context() as db:
            db.add(account)
            await db.commit()
            await db.refresh(account)
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
            return account

    async def delete(self, account_id: str):
        async with get_async_db_context() as db:
            await db.execute(delete(ProviderAccount).where(ProviderAccount.id == account_id))
            await db.commit()


ProviderAccounts = ProviderAccountsTable()
