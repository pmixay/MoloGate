"""Mologate: how many chats one member can have generating at the same time.

A chat a member sends takes a slot while it generates. Past the cap, the request waits in line in
the order it came, and the chat shows its place ("2nd in line") until a slot frees up; stopping
the reply leaves the line. Sub-agents, workflow agents, background results, timers, automations
and channel replies take no slot; they are held to limits of their own. A chat with several models
answering takes one slot.

Slots live in Redis when it is configured, so every worker shares them, and in memory otherwise.
Each slot and each place in line is a lease its holder renews, so one left behind by a stopped
worker frees itself within a minute.

The cap applies when ``mologate.limits_mode`` is ``enforce``. In ``observe`` (the default until
enforcement ships) nothing waits; a request that would have waited is logged
(docs/limits-and-accounts.md, section 1).
"""

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field

from open_webui.models.config import Config

log = logging.getLogger(__name__)

DEFAULT_LIMIT = 3
HARD_CAP = 10
LEASE_SECONDS = 60
RENEW_SECONDS = 20
POLL_SECONDS = 1.0
KEY_PREFIX = 'mologate:chat_slots:'

# Atomically: drop expired slots and waiters, join the line, and take a slot when one is free for
# this place in line. Returns -1 once the slot is taken, else the 0-based place in line.
ACQUIRE = """
local slots, line, leases = KEYS[1], KEYS[2], KEYS[3]
local ticket, now, lease, limit, joined = ARGV[1], tonumber(ARGV[2]), tonumber(ARGV[3]), tonumber(ARGV[4]), ARGV[5]
redis.call('ZREMRANGEBYSCORE', slots, '-inf', now)
for _, member in ipairs(redis.call('ZRANGE', line, 0, -1)) do
  if tonumber(redis.call('HGET', leases, member) or '0') < now then
    redis.call('ZREM', line, member)
    redis.call('HDEL', leases, member)
  end
end
if redis.call('ZSCORE', slots, ticket) then
  redis.call('ZADD', slots, now + lease, ticket)
  return -1
end
redis.call('ZADD', line, 'NX', joined, ticket)
redis.call('HSET', leases, ticket, now + lease)
local active = redis.call('ZCARD', slots)
local place = redis.call('ZRANK', line, ticket)
if place < limit - active then
  redis.call('ZREM', line, ticket)
  redis.call('HDEL', leases, ticket)
  redis.call('ZADD', slots, now + lease, ticket)
  place = -1
end
for _, key in ipairs(KEYS) do
  redis.call('EXPIRE', key, lease * 2)
end
return place
"""


@dataclass
class Slot:
    user_id: str
    ticket: str


@dataclass
class _Line:
    """The in-memory slots and line of one member, when Redis is not configured."""

    slots: dict[str, float] = field(default_factory=dict)
    line: dict[str, tuple[float, float]] = field(default_factory=dict)


_memory: dict[str, _Line] = {}
_memory_lock = asyncio.Lock()
# Tasks of one chat on this worker share its slot (several models answering at once); the slot is
# renewed until the last of them ends.
_holders: dict[str, int] = {}
_renewers: dict[str, asyncio.Task] = {}


async def chat_limit() -> int:
    try:
        value = int(await Config.get('mologate.max_concurrent_chats', DEFAULT_LIMIT))
    except (TypeError, ValueError):
        value = DEFAULT_LIMIT
    return max(1, min(HARD_CAP, value))


def counts(metadata: dict) -> bool:
    """Only a chat the member is generating takes a slot."""
    message = metadata.get('user_message') if isinstance(metadata.get('user_message'), dict) else {}
    return bool(
        metadata.get('session_id')
        and metadata.get('chat_id')
        and not metadata.get('internal')
        and not metadata.get('automation_id')
        and not str(metadata.get('chat_id')).startswith('channel:')
        # A turn started by a background result, a timer or another agent's message.
        and not (message.get('meta') or {}).get('internal')
    )


def _keys(user_id: str) -> list[str]:
    # One hash tag per member keeps the three keys on one Redis Cluster slot.
    tag = '{' + user_id + '}'
    return [f'{KEY_PREFIX}{tag}:slots', f'{KEY_PREFIX}{tag}:line', f'{KEY_PREFIX}{tag}:leases']


async def try_acquire(redis, user_id: str, ticket: str, limit: int, joined: float, now: float | None = None) -> int:
    """Take a slot (-1) or keep a place in line (0-based)."""
    now = time.time() if now is None else now
    if redis is not None:
        return int(await redis.eval(ACQUIRE, 3, *_keys(user_id), ticket, now, LEASE_SECONDS, limit, joined))
    async with _memory_lock:
        state = _memory.setdefault(user_id, _Line())
        state.slots = {key: expiry for key, expiry in state.slots.items() if expiry > now}
        state.line = {key: value for key, value in state.line.items() if value[1] >= now}
        if ticket in state.slots:
            state.slots[ticket] = now + LEASE_SECONDS
            return -1
        state.line[ticket] = (state.line.get(ticket, (joined, 0))[0], now + LEASE_SECONDS)
        place = sorted(state.line, key=lambda key: (state.line[key][0], key)).index(ticket)
        if place < limit - len(state.slots):
            del state.line[ticket]
            state.slots[ticket] = now + LEASE_SECONDS
            return -1
        return place


async def active_count(redis, user_id: str, now: float | None = None) -> int:
    now = time.time() if now is None else now
    if redis is not None:
        return int(await redis.zcount(_keys(user_id)[0], now, '+inf'))
    state = _memory.get(user_id)
    return sum(1 for expiry in state.slots.values() if expiry > now) if state else 0


async def leave(redis, user_id: str, ticket: str) -> None:
    """Free the slot, or the place in line."""
    if redis is not None:
        slots, line, leases = _keys(user_id)
        pipe = redis.pipeline(transaction=False)
        pipe.zrem(slots, ticket)
        pipe.zrem(line, ticket)
        pipe.hdel(leases, ticket)
        await pipe.execute()
        return
    async with _memory_lock:
        state = _memory.get(user_id)
        if state:
            state.slots.pop(ticket, None)
            state.line.pop(ticket, None)


async def _renew(redis, user_id: str, ticket: str, limit: int) -> None:
    while True:
        await asyncio.sleep(RENEW_SECONDS)
        try:
            await try_acquire(redis, user_id, ticket, limit, time.time())
        except Exception as e:
            log.debug('Could not renew a chat slot: %s', e)


async def acquire(redis, user_id: str, chat_id: str, on_wait=None) -> Slot | None:
    """Wait for a slot for this chat. `on_wait(place, active, limit)` is awaited when the place in
    line changes (place 0 is next) and with place None once the slot is taken after waiting."""
    mode = await Config.get('mologate.limits_mode', 'observe')
    if mode == 'off':
        return None
    ticket = f'{user_id}:{chat_id}'
    limit = await chat_limit()
    if _holders.get(ticket):
        _holders[ticket] += 1
        return Slot(user_id, ticket)
    try:
        if mode == 'enforce':
            await _wait_in_line(redis, user_id, ticket, limit, on_wait)
        else:
            if (active := await active_count(redis, user_id)) >= limit:
                log.info('Observe: user %s would wait for a chat slot (%d of %d generating).', user_id, active, limit)
            # Counted, so the next request can tell whether it would have waited; never refused.
            await try_acquire(redis, user_id, ticket, HARD_CAP * 100, time.time())
    except asyncio.CancelledError:
        raise
    except Exception as e:
        # The cap protects no one when its storage is down; never block a chat on it.
        log.warning('Chat slots unavailable; the chat goes ahead: %s', e)
        return None
    _holders[ticket] = _holders.get(ticket, 0) + 1
    if ticket not in _renewers:
        _renewers[ticket] = asyncio.create_task(_renew(redis, user_id, ticket, limit))
    return Slot(user_id, ticket)


async def _wait_in_line(redis, user_id: str, ticket: str, limit: int, on_wait) -> None:
    joined = time.time()
    last_place = None
    try:
        while (place := await try_acquire(redis, user_id, ticket, limit, joined)) >= 0:
            if place != last_place and on_wait is not None:
                last_place = place
                with contextlib.suppress(Exception):
                    await on_wait(place, await active_count(redis, user_id), limit)
            await asyncio.sleep(POLL_SECONDS)
    except asyncio.CancelledError:
        # Stopped while waiting: leave the line.
        with contextlib.suppress(Exception):
            await asyncio.shield(leave(redis, user_id, ticket))
        raise
    if last_place is not None and on_wait is not None:
        with contextlib.suppress(Exception):
            await on_wait(None, 0, limit)


async def release(redis, slot: Slot | None) -> None:
    if slot is None:
        return
    remaining = _holders.get(slot.ticket, 1) - 1
    if remaining > 0:
        _holders[slot.ticket] = remaining
        return
    _holders.pop(slot.ticket, None)
    if renewer := _renewers.pop(slot.ticket, None):
        renewer.cancel()
    with contextlib.suppress(Exception):
        await leave(redis, slot.user_id, slot.ticket)


def queue_notice(metadata: dict):
    """Show the waiting chat its place in line, as a status line under its reply."""

    async def on_wait(place: int | None, active: int, limit: int) -> None:
        from open_webui.socket.main import get_event_emitter

        emitter = await get_event_emitter(metadata)
        if not emitter:
            return
        if place is None:
            data = {'action': 'queue', 'description': '', 'done': True, 'hidden': True}
        else:
            ahead = 'This one is next.' if place == 0 else f'{place} ahead of this one.'
            data = {
                'action': 'queue',
                'place': place,
                'active': active,
                'limit': limit,
                'description': f'Waiting: {active} of your chats are already generating. {ahead}',
                'done': False,
            }
        await emitter({'type': 'status', 'data': data})

    return on_wait
