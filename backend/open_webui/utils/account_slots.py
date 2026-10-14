"""Mologate: how many requests one provider account serves at the same time (phase 4).

A shared account should look like one person at work, not a team: past the cap, the gate sends a
request to another account in the member's tier, or waits for a slot (utils/allowance_gate.py,
docs/limits-and-accounts.md section 7.4). A slot is held until the answer, or its stream, ends.

Slots live in Redis when it is configured, so every worker shares them, and in memory otherwise.
Each slot is a lease its holder renews, so one left behind by a stopped worker frees itself within
a minute. Like the chat cap (utils/chat_slots.py), requests wait only when ``mologate.limits_mode``
is ``enforce``; ``observe`` takes slots without a cap and logs a request that would have waited.
"""

import asyncio
import contextlib
import logging
import time
import uuid

from open_webui.models.config import Config

log = logging.getLogger(__name__)

DEFAULT_LIMIT = 4
HARD_CAP = 20
LEASE_SECONDS = 60
RENEW_SECONDS = 20
# A slot whose holder never released it (a stream nobody read) stops being renewed after this.
MAX_HOLD_SECONDS = 3600
POLL_SECONDS = 0.5
# How long a request waits for a busy account before it is refused as busy.
WAIT_SECONDS = 120
KEY_PREFIX = 'mologate:account_slots:'

# Atomically: drop expired slots, then take one if there is room. Returns 1 when taken.
TAKE = """
local key, ticket, now, lease, limit = KEYS[1], ARGV[1], tonumber(ARGV[2]), tonumber(ARGV[3]), tonumber(ARGV[4])
redis.call('ZREMRANGEBYSCORE', key, '-inf', now)
if redis.call('ZSCORE', key, ticket) or redis.call('ZCARD', key) < limit then
  redis.call('ZADD', key, now + lease, ticket)
  redis.call('EXPIRE', key, lease * 2)
  return 1
end
return 0
"""

_memory: dict[str, dict[str, float]] = {}
_memory_lock = asyncio.Lock()


async def account_limit() -> int:
    try:
        value = int(await Config.get('mologate.max_account_requests', DEFAULT_LIMIT))
    except (TypeError, ValueError):
        value = DEFAULT_LIMIT
    return max(1, min(HARD_CAP, value))


def _key(account_id: str) -> str:
    return KEY_PREFIX + account_id


async def take(redis, account_id: str, ticket: str, limit: int, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    if redis is not None:
        return bool(await redis.eval(TAKE, 1, _key(account_id), ticket, now, LEASE_SECONDS, limit))
    async with _memory_lock:
        slots = {key: expiry for key, expiry in _memory.get(account_id, {}).items() if expiry > now}
        _memory[account_id] = slots
        if ticket in slots or len(slots) < limit:
            slots[ticket] = now + LEASE_SECONDS
            return True
        return False


async def active(redis, account_id: str, now: float | None = None) -> int:
    now = time.time() if now is None else now
    if redis is not None:
        return int(await redis.zcount(_key(account_id), now, '+inf'))
    return sum(1 for expiry in _memory.get(account_id, {}).values() if expiry > now)


async def leave(redis, account_id: str, ticket: str) -> None:
    if redis is not None:
        await redis.zrem(_key(account_id), ticket)
        return
    async with _memory_lock:
        _memory.get(account_id, {}).pop(ticket, None)


class Slot:
    """One request's hold on an account, renewed until released. Releasing twice is harmless."""

    def __init__(self, redis, account_id: str, ticket: str):
        self.redis = redis
        self.account_id = account_id
        self.ticket = ticket
        self.released = False
        # Set once the answer is a stream: the stream's end frees the slot, not the request's.
        self.streaming = False
        try:
            self._renewer = asyncio.create_task(self._renew())
        except RuntimeError:
            self._renewer = None

    async def _renew(self) -> None:
        for _ in range(MAX_HOLD_SECONDS // RENEW_SECONDS):
            await asyncio.sleep(RENEW_SECONDS)
            try:
                if self.redis is not None:
                    await self.redis.zadd(_key(self.account_id), {self.ticket: time.time() + LEASE_SECONDS})
                else:
                    async with _memory_lock:
                        _memory.setdefault(self.account_id, {})[self.ticket] = time.time() + LEASE_SECONDS
            except Exception as e:
                log.debug('Could not renew an account slot: %s', e)

    def release(self) -> None:
        """Free the slot. Called from stream ends and finally blocks, so it never raises or waits."""
        if self.released:
            return
        self.released = True
        if self._renewer is not None:
            self._renewer.cancel()
        try:
            task = asyncio.create_task(self._leave())
        except RuntimeError:
            return
        _leaving.add(task)
        task.add_done_callback(_leaving.discard)

    async def _leave(self) -> None:
        with contextlib.suppress(Exception):
            await leave(self.redis, self.account_id, self.ticket)


_leaving: set = set()


async def flush() -> None:
    """Wait for pending releases (tests)."""
    if _leaving:
        await asyncio.gather(*list(_leaving), return_exceptions=True)


class Busy(Exception):
    """Every account the request may use stayed at its cap for the whole wait."""


async def acquire(redis, account_ids: list[str], enforce: bool, wait: float | None = None) -> tuple[str, Slot] | None:
    """A slot on the first of these accounts that has one, waiting while all are full.

    In observe, the first account is taken whatever its count, and a request that would have
    waited is logged. Returns None when slots cannot be counted (storage down): the request goes
    ahead on the first account, since the cap protects no one then. Raises Busy after `wait`.
    """
    if not account_ids:
        return None
    ticket = uuid.uuid4().hex
    try:
        limit = await account_limit()
        if not enforce:
            if await active(redis, account_ids[0]) >= limit:
                log.info('Observe: a request would wait for account %s (%d at once).', account_ids[0], limit)
            await take(redis, account_ids[0], ticket, HARD_CAP * 100)
            return account_ids[0], Slot(redis, account_ids[0], ticket)
        deadline = time.monotonic() + (WAIT_SECONDS if wait is None else wait)
        while True:
            for account_id in account_ids:
                if await take(redis, account_id, ticket, limit):
                    return account_id, Slot(redis, account_id, ticket)
            if time.monotonic() >= deadline:
                raise Busy()
            await asyncio.sleep(POLL_SECONDS)
    except (Busy, asyncio.CancelledError):
        raise
    except Exception as e:
        log.warning('Account slots unavailable; the request goes ahead: %s', e)
        return None
