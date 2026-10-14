"""Mologate: metering tables (docs/limits-and-accounts.md, sections 6 and 8).

- ``account_window``: the latest reading of each account's 5-hour and weekly windows.
- ``account_reading``: every change in those readings, so their rises can be set against the
  usage ledger. A reading is kept only when the used percent rises or the window starts over.
- ``meter_interval``: a stretch of readings of one window, with the rise the provider reported
  and the tokens Mologate sent in it, by model. Outside-use detection and calibration work on these.
- ``usage_calibration``: the fitted units per token of each provider and window, and the
  capacity weights fitted from them.
"""

import time
import uuid

from sqlalchemy import BigInteger, Boolean, Column, Float, Index, Integer, String, Text, delete, select

from open_webui.internal.db import Base, JSONField, get_async_db_context


class AccountWindow(Base):
    __tablename__ = 'account_window'

    account_id = Column(Text, primary_key=True)
    # 5h | weekly
    window = Column(String(16), primary_key=True)
    used_percent = Column(Float, nullable=False)
    resets_at = Column(BigInteger, nullable=True)
    window_minutes = Column(Integer, nullable=True)
    # poll | headers
    source = Column(String(16), nullable=False)
    observed_at = Column(BigInteger, nullable=False)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)


class AccountReading(Base):
    __tablename__ = 'account_reading'

    id = Column(Text, primary_key=True)
    account_id = Column(Text, nullable=False)
    window = Column(String(16), nullable=False)
    used_percent = Column(Float, nullable=False)
    resets_at = Column(BigInteger, nullable=True)
    window_minutes = Column(Integer, nullable=True)
    source = Column(String(16), nullable=False)
    observed_at = Column(BigInteger, nullable=False)
    __table_args__ = (Index('ix_account_reading_account_window', 'account_id', 'window', 'observed_at'),)


class MeterInterval(Base):
    __tablename__ = 'meter_interval'

    id = Column(Text, primary_key=True)
    account_id = Column(Text, nullable=False)
    provider = Column(String(32), nullable=False)
    window = Column(String(16), nullable=False)
    start = Column(BigInteger, nullable=False)
    end = Column(BigInteger, nullable=False)
    used_start = Column(Float, nullable=False)
    used_end = Column(Float, nullable=False)
    # Percent of the account's window the provider reported used in the interval.
    rise = Column(Float, nullable=False)
    # The account's capacity weight when the interval was measured.
    weight = Column(Float, nullable=False)
    # Ledger rows in the interval, and their tokens by model (calibration features).
    requests = Column(Integer, nullable=False, default=0)
    features = Column(JSONField, nullable=True)
    # The first reading of a window Mologate ever saw: what came before it is not Mologate's.
    first = Column(Boolean, nullable=False, default=False)
    # Percent Mologate's rows explain (unknown before calibration), and the unexplained rest
    # recorded as use outside Mologate.
    expected = Column(Float, nullable=True)
    outside = Column(Float, nullable=True)
    outside_event_id = Column(Text, nullable=True)
    created_at = Column(BigInteger, nullable=False)
    __table_args__ = (
        Index('ix_meter_interval_account_window', 'account_id', 'window', 'end'),
        Index('ix_meter_interval_provider_window', 'provider', 'window', 'end'),
    )


class UsageCalibration(Base):
    __tablename__ = 'usage_calibration'

    # provider:window
    id = Column(Text, primary_key=True)
    provider = Column(String(32), nullable=False)
    window = Column(String(16), nullable=False)
    # k (units per million tokens: uncached input, cached input, output, reasoning), model scales,
    # fitted capacity weights and each account's fit.
    values = Column(JSONField, nullable=False)
    samples = Column(Integer, nullable=False)
    error = Column(Float, nullable=True)
    fitted_at = Column(BigInteger, nullable=False)


class MeteringTable:
    async def latest_reading(self, account_id: str, window: str):
        async with get_async_db_context() as db:
            result = await db.execute(
                select(AccountReading)
                .where(AccountReading.account_id == account_id, AccountReading.window == window)
                .order_by(AccountReading.observed_at.desc())
                .limit(1)
            )
            return result.scalars().first()

    async def add_reading(self, **values) -> None:
        async with get_async_db_context() as db:
            db.add(AccountReading(id=str(uuid.uuid4()), **values))
            await db.commit()

    async def readings(self, account_id: str, window: str, since: int) -> list:
        """Readings from the last one at or before `since` onwards, oldest first."""
        async with get_async_db_context() as db:
            base = await db.execute(
                select(AccountReading.observed_at)
                .where(
                    AccountReading.account_id == account_id,
                    AccountReading.window == window,
                    AccountReading.observed_at <= since,
                )
                .order_by(AccountReading.observed_at.desc())
                .limit(1)
            )
            start = base.scalar()
            query = select(AccountReading).where(
                AccountReading.account_id == account_id, AccountReading.window == window
            )
            if start is not None:
                query = query.where(AccountReading.observed_at >= start)
            result = await db.execute(query.order_by(AccountReading.observed_at, AccountReading.id))
            return list(result.scalars().all())

    async def reading_keys(self) -> list[tuple[str, str]]:
        async with get_async_db_context() as db:
            result = await db.execute(select(AccountReading.account_id, AccountReading.window).distinct())
            return [tuple(row) for row in result.all()]

    async def save_window(self, account_id: str, window: str, **values) -> None:
        now = int(time.time())
        async with get_async_db_context() as db:
            row = await db.get(AccountWindow, (account_id, window))
            if row is None:
                db.add(AccountWindow(account_id=account_id, window=window, created_at=now, updated_at=now, **values))
            else:
                for key, value in values.items():
                    setattr(row, key, value)
                row.updated_at = now
            await db.commit()

    async def windows(self, account_ids: list[str] | None = None) -> list:
        async with get_async_db_context() as db:
            query = select(AccountWindow)
            if account_ids is not None:
                query = query.where(AccountWindow.account_id.in_(account_ids))
            return list((await db.execute(query)).scalars().all())

    async def last_interval(self, account_id: str, window: str):
        async with get_async_db_context() as db:
            result = await db.execute(
                select(MeterInterval)
                .where(MeterInterval.account_id == account_id, MeterInterval.window == window)
                .order_by(MeterInterval.end.desc())
                .limit(1)
            )
            return result.scalars().first()

    async def add_interval(self, **values) -> str:
        identifier = str(uuid.uuid4())
        async with get_async_db_context() as db:
            db.add(MeterInterval(id=identifier, created_at=int(time.time()), **values))
            await db.commit()
        return identifier

    async def intervals(self, since: int, provider: str | None = None, window: str | None = None) -> list:
        async with get_async_db_context() as db:
            query = select(MeterInterval).where(MeterInterval.end >= since)
            if provider is not None:
                query = query.where(MeterInterval.provider == provider)
            if window is not None:
                query = query.where(MeterInterval.window == window)
            result = await db.execute(query.order_by(MeterInterval.end))
            return list(result.scalars().all())

    async def save_calibration(self, provider: str, window: str, values: dict, samples: int, error) -> None:
        identifier = f'{provider}:{window}'
        async with get_async_db_context() as db:
            row = await db.get(UsageCalibration, identifier)
            if row is None:
                row = UsageCalibration(id=identifier, provider=provider, window=window)
                db.add(row)
            row.values = values
            row.samples = samples
            row.error = error
            row.fitted_at = int(time.time())
            await db.commit()

    async def calibrations(self) -> list:
        async with get_async_db_context() as db:
            return list((await db.execute(select(UsageCalibration))).scalars().all())

    async def prune(self, before: int) -> None:
        async with get_async_db_context() as db:
            await db.execute(delete(AccountReading).where(AccountReading.observed_at < before))
            await db.execute(delete(MeterInterval).where(MeterInterval.end < before))
            await db.commit()

    async def forget_account(self, account_id: str) -> None:
        async with get_async_db_context() as db:
            for table in (AccountWindow, AccountReading, MeterInterval):
                await db.execute(delete(table).where(table.account_id == account_id))
            await db.commit()


Metering = MeteringTable()
