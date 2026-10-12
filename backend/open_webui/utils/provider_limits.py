"""Usage limits providers report in response headers, per connection, for the context indicator.

Every completion response from an OpenAI-compatible connection is checked for rate-limit
headers. What a provider reports is kept per connection (one connection is one shared account
or key), so every model served by it shows the same limits. Recognized headers:

- Codex / ChatGPT: ``x-codex-{primary,secondary}-used-percent``, ``-window-minutes`` and
  ``-reset-at`` (or the older ``-reset-after-seconds``). The primary window is the 5-hour limit
  and the secondary one the weekly limit.
- OpenAI-style: ``x-ratelimit-{limit,remaining,reset}-{requests,tokens}``.
- Plain ``x-ratelimit-{limit,remaining,reset}``, as OpenRouter sends.

Some limits come from elsewhere: Z.ai's coding-plan quota endpoint, and an OpenRouter key's
``/key`` endpoint (its credit limit, and this month's spend against the owner's budget). Windows
from another source are kept when headers update a snapshot, and the other way round.

Snapshots live in Redis when it is configured, so every worker sees them, and in memory
otherwise. Providers that send none of these headers simply have no limits to show.

Mologate: the 5-hour and weekly windows of subscription accounts are also split between the owner
and the rest of the team. Each rise in a window's used percent is shared out by the number of
requests the owner and the team sent since the previous reading. A rise with no requests from
Mologate is the owner's use outside it, and so is whatever was used before Mologate first read the
window. This is an estimate until the usage ledger (docs/limits-and-accounts.md §8) exists.
"""

import asyncio
import logging
import hashlib
import math
import re
import time
from datetime import UTC, datetime, timedelta
from collections.abc import Mapping
from typing import Any

from open_webui.utils.json_codec import JSONCodec

log = logging.getLogger(__name__)

REDIS_KEY_PREFIX = 'mologate:provider_limits:'
SPLIT_KEY_PREFIX = 'mologate:provider_split:'
# A snapshot older than this says little about the account now.
SNAPSHOT_TTL_SECONDS = 8 * 24 * 60 * 60
# The windows an account's owner and the team share (5-hour and weekly).
SPLIT_WINDOWS = ('primary', 'secondary')
# Readings come from several sources and can lag a little; only a bigger drop, or a reset time
# that moved on, means the window started over.
RESET_DROP = 5
RESET_MOVE_SECONDS = 300
# The owner's monthly budget is a target, not a provider limit: a key past it still serves.
SOFT_WINDOWS = ('budget',)

# Mologate: the low-limit warning. Recent readings of the long windows (5-hour, weekly, an
# OpenRouter key's credit limit) tell how fast they are being used, and so roughly how many
# minutes of work are left.
HISTORY_KEY_PREFIX = 'mologate:provider_history:'
TRACKED_WINDOWS = ('primary', 'secondary', 'credits')
HISTORY_SECONDS = 2 * 60 * 60
HISTORY_STEP_SECONDS = 60
HISTORY_POINTS = 300
RATE_SPAN_SECONDS = 30 * 60
# No reading for this long means no request: the account sat idle.
IDLE_GAP_SECONDS = 10 * 60
# Two readings a few seconds apart say little about a pace.
MIN_RATE_MINUTES = 5
# Warn when this much of a window is left across the model's accounts, or this little time at the recent pace.
LOW_PERCENT_LEFT = 20
LOW_MINUTES_LEFT = 60
CRITICAL_PERCENT_LEFT = 10
CRITICAL_MINUTES_LEFT = 15

_snapshots: dict[str, dict] = {}
_splits: dict[str, dict] = {}
_histories: dict[str, dict] = {}
_split_lock = asyncio.Lock()
_history_lock = asyncio.Lock()

_DURATION_PART = re.compile(r'(\d+(?:\.\d+)?)(ms|h|m|s)')


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_duration_seconds(value: str | None) -> float | None:
    """OpenAI-style reset durations such as "1s", "6m0s" or "20ms"."""
    if not value:
        return None
    if (number := _number(value)) is not None:
        return number
    parts = _DURATION_PART.findall(value.strip())
    if not parts:
        return None
    scale = {'ms': 0.001, 's': 1, 'm': 60, 'h': 3600}
    return sum(float(amount) * scale[unit] for amount, unit in parts)


def _parse_reset_at(value: str | None, now: float) -> int | None:
    """An absolute reset time in epoch seconds or milliseconds."""
    number = _number(value)
    if number is None:
        return None
    if number > 1e12:  # milliseconds
        number /= 1000
    return int(number) if number > now - 60 else None


def _codex_window(headers: Mapping[str, str], name: str, now: float) -> dict | None:
    used = _number(headers.get(f'x-codex-{name}-used-percent'))
    if used is None:
        return None

    resets_at = _parse_reset_at(headers.get(f'x-codex-{name}-reset-at'), now)
    if resets_at is None:
        reset_after = _number(headers.get(f'x-codex-{name}-reset-after-seconds'))
        resets_at = int(now + reset_after) if reset_after is not None else None

    window_minutes = _number(headers.get(f'x-codex-{name}-window-minutes'))
    return {
        'id': name,
        'used_percent': max(0.0, min(100.0, used)),
        'window_minutes': int(window_minutes) if window_minutes else None,
        'resets_at': resets_at,
    }


def _count_window(headers: Mapping[str, str], window_id: str, suffix: str, now: float) -> dict | None:
    limit = _number(headers.get(f'x-ratelimit-limit{suffix}'))
    remaining = _number(headers.get(f'x-ratelimit-remaining{suffix}'))
    if not limit or remaining is None:
        return None

    reset = headers.get(f'x-ratelimit-reset{suffix}')
    resets_at = _parse_reset_at(reset, now)
    if resets_at is None and (seconds := _parse_duration_seconds(reset)) is not None:
        resets_at = int(now + seconds)

    return {
        'id': window_id,
        'used_percent': max(0.0, min(100.0, (1 - remaining / limit) * 100)),
        'limit': int(limit),
        'remaining': int(remaining),
        'resets_at': resets_at,
    }


def parse_rate_limit_headers(headers: Mapping[str, str], now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    headers = {str(key).lower(): value for key, value in headers.items()}

    windows = [_codex_window(headers, name, now) for name in ('primary', 'secondary')]
    windows += [
        _count_window(headers, 'requests', '-requests', now),
        _count_window(headers, 'tokens', '-tokens', now),
        _count_window(headers, 'requests', '', now),
    ]
    # Mologate: Anthropic uses its own prefix and ISO-8601 reset timestamps.
    for name in ('requests', 'tokens', 'input-tokens', 'output-tokens'):
        prefix = f'anthropic-ratelimit-{name}'
        limit = _number(headers.get(prefix + '-limit'))
        remaining = _number(headers.get(prefix + '-remaining'))
        if limit and limit > 0 and remaining is not None:
            try:
                resets_at = int(
                    datetime.fromisoformat(headers.get(prefix + '-reset', '').replace('Z', '+00:00')).timestamp()
                )
            except (ValueError, OverflowError):
                resets_at = None
            windows.append(
                {
                    'id': name,
                    'used_percent': max(0.0, min(100.0, (1 - remaining / limit) * 100)),
                    'limit': int(limit),
                    'remaining': int(remaining),
                    'resets_at': resets_at,
                }
            )

    seen = set()
    result = []
    for window in windows:
        if window and window['id'] not in seen:
            seen.add(window['id'])
            result.append(window)
    return result


def legacy_account_key(url: str, key: str) -> str:
    """Mologate: legacy connections also keep their identity when reordered."""
    return 'legacy:' + hashlib.sha256(f'{url.rstrip("/")}\0{key}'.encode()).hexdigest()


def window_exhausted(window: dict, now: float) -> bool:
    if window.get('id') in SOFT_WINDOWS:
        return False
    resets_at = _number(window.get('resets_at'))
    return (_number(window.get('used_percent')) or 0) >= 100 and (resets_at is None or resets_at > now)


def snapshot_exhausted(snapshot: dict | None, now: float | None = None) -> bool:
    """Mologate: only a reported empty window counts; unknown quota is never treated as empty."""
    if not snapshot:
        return False
    now = time.time() if now is None else now
    return any(window_exhausted(window, now) for window in snapshot.get('windows', []))


def parse_zai_quota(result: dict) -> list[dict]:
    """Only map observed coding-plan windows. Unknown/empty quota is not zero usage."""
    # Z.ai wraps answers as {code, msg, data, success}; some only carry code 200.
    if result.get('success') is False or (result.get('success') is not True and result.get('code') != 200):
        return []
    windows = []
    for window in (result.get('data') or {}).get('limits', []):
        if window.get('type') not in ('CREDIT_LIMIT', 'TOKENS_LIMIT'):
            continue
        used = _number(window.get('percentage'))
        # Live coding-plan responses identify the 5-hour window as (3,5), weekly as (6,1).
        minutes = {(3, 5): 300, (6, 1): 10080}.get((window.get('unit'), window.get('number')))
        if used is None or minutes is None:
            continue
        windows.append(
            {
                'id': 'primary' if minutes == 300 else 'secondary',
                'used_percent': max(0, min(100, used)),
                'window_minutes': minutes,
                'resets_at': _parse_reset_at(str(window.get('nextResetTime', '')), time.time()),
            }
        )
    return windows


def period_start(period: str, now: float) -> int:
    """When the UTC day, week (from Monday) or month that contains `now` began."""
    today = datetime.fromtimestamp(now, UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    if period == 'weekly':
        today -= timedelta(days=today.weekday())
    elif period == 'monthly':
        today = today.replace(day=1)
    return int(today.timestamp())


def period_end(period: str | None, now: float) -> int | None:
    """When a UTC day, week (Monday to Sunday) or month that contains `now` ends."""
    today = datetime.fromtimestamp(now, UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    if period == 'daily':
        return int((today + timedelta(days=1)).timestamp())
    if period == 'weekly':
        return int((today + timedelta(days=7 - today.weekday())).timestamp())
    if period == 'monthly':
        month = today.replace(day=1)
        following = (
            month.replace(year=month.year + 1, month=1) if month.month == 12 else month.replace(month=month.month + 1)
        )
        return int(following.timestamp())
    return None


def parse_openrouter_key(info: dict, budget: dict | None, now: float | None = None) -> list[dict]:
    """An OpenRouter key's credit limit, and this month's spend against the owner's budget, in USD.

    OpenRouter's ``/key`` reports ``limit`` (null without one), ``limit_remaining``, ``limit_reset``
    (daily, weekly, monthly or null; at midnight UTC) and ``usage_monthly`` for the UTC month.
    """
    now = time.time() if now is None else now
    windows = []
    limit = _number(info.get('limit'))
    remaining = _number(info.get('limit_remaining'))
    if limit and limit > 0 and remaining is not None:
        windows.append(
            {
                'id': 'credits',
                'used_percent': max(0.0, min(100.0, (1 - remaining / limit) * 100)),
                'limit': round(limit, 2),
                'remaining': round(max(0.0, remaining), 2),
                'unit': 'usd',
                'resets_at': period_end(info.get('limit_reset'), now),
                'source': 'key',
            }
        )
    spent = _number(info.get('usage_monthly'))
    amount = _number((budget or {}).get('amount'))
    if spent is not None and amount and amount > 0:
        windows.append(budget_window(amount, spent, now, 'key'))
    return windows


def budget_window(amount: float, spent: float, now: float, source: str) -> dict:
    """This month's spend against the owner's monthly budget (a target the provider never enforces)."""
    spent = max(0.0, spent)
    return {
        'id': 'budget',
        'used_percent': max(0.0, min(100.0, spent / amount * 100)),
        'limit': round(amount, 2),
        'remaining': round(max(0.0, amount - spent), 2),
        'spent': round(spent, 4),
        'unit': 'usd',
        'resets_at': period_end('monthly', now),
        'source': source,
    }


async def record_provider_limits(redis, account_id: str, headers: Mapping[str, str]) -> None:
    """Keep what a response's headers say about the connection's limits. Never raises."""
    try:
        windows = parse_rate_limit_headers(headers)
        if not windows:
            return

        # Headers carry no reset coupons; the count the account service last reported is kept.
        await record_provider_snapshot(redis, account_id, windows)
    except Exception as e:
        log.debug('Could not record provider limits: %s', e)


async def record_provider_snapshot(
    redis, account_id: str, windows: list[dict], reset_credits: float | None = None, source: str | None = None
) -> None:
    """Keep a reading. It replaces the windows its source reported before; other sources' stay."""
    previous = await get_provider_limits(redis, account_id) or {}
    ids = {window.get('id') for window in windows}
    kept = [
        window
        for window in previous.get('windows', [])
        if window.get('source') != source and window.get('id') not in ids
    ]
    snapshot = {'windows': windows + kept, 'updated_at': int(time.time())}
    if reset_credits is None:
        reset_credits = previous.get('reset_credits')
    if reset_credits is not None:
        snapshot['reset_credits'] = int(reset_credits)
    _snapshots[account_id] = snapshot
    if redis is not None:
        try:
            await redis.set(f'{REDIS_KEY_PREFIX}{account_id}', JSONCodec.dumps(snapshot), ex=SNAPSHOT_TTL_SECONDS)
        except Exception:
            log.debug('Provider limit storage unavailable; using memory.')
    if any(window.get('id') in TRACKED_WINDOWS for window in windows):
        try:
            async with _history_lock:
                history = await _load_state(redis, HISTORY_KEY_PREFIX, account_id, _histories)
                track_reading(history, windows, time.time())
                await _save_state(redis, HISTORY_KEY_PREFIX, account_id, _histories, history, HISTORY_SECONDS)
        except Exception as e:
            log.debug('Could not keep the limit history: %s', e)
    if not any(window.get('id') in SPLIT_WINDOWS for window in windows):
        return
    try:
        async with _split_lock:
            state = await _load_split(redis, account_id)
            split_reading(state, windows)
            await _save_split(redis, account_id, state)
    except Exception as e:
        log.debug('Could not split provider usage: %s', e)


async def _load_state(redis, prefix: str, account_id: str, memory: dict) -> dict:
    state = None
    if redis is not None:
        try:
            raw = await redis.get(f'{prefix}{account_id}')
            state = JSONCodec.loads(raw) if raw else None
        except Exception as e:
            log.debug('Could not read %s: %s', prefix, e)
    return state or memory.get(account_id) or {}


async def _save_state(redis, prefix: str, account_id: str, memory: dict, state: dict, ttl: int) -> None:
    memory[account_id] = state
    if redis is not None:
        try:
            await redis.set(f'{prefix}{account_id}', JSONCodec.dumps(state), ex=ttl)
        except Exception:
            log.debug('%s storage unavailable; using memory.', prefix)


def track_reading(history: dict, windows: list[dict], now: float) -> None:
    """Keep recent readings of an account's long windows, to tell how fast they are being used."""
    for window in windows:
        window_id = window.get('id')
        used = _number(window.get('used_percent'))
        if window_id not in TRACKED_WINDOWS or used is None:
            continue
        resets_at = _number(window.get('resets_at'))
        points = list(history.get(window_id) or [])
        if points:
            last_used, last_reset = points[-1][1], points[-1][2]
            started_over = used < last_used - RESET_DROP or (
                resets_at is not None and last_reset is not None and resets_at > last_reset + RESET_MOVE_SECONDS
            )
            if started_over:
                points = []
            else:
                # A slightly lower reading is a stale one.
                used = max(used, last_used)
                # Readings come with every request. Within a step only the latest one after the
                # step's first is kept, so where each run of use started stays known.
                if len(points) > 1 and now - points[-2][0] < HISTORY_STEP_SECONDS:
                    points.pop()
        points.append([round(now, 1), used, resets_at])
        history[window_id] = [point for point in points if point[0] >= now - HISTORY_SECONDS][-HISTORY_POINTS:]


def burn_rate(points: list, now: float, span: float = RATE_SPAN_SECONDS) -> float | None:
    """Percent of the window used per minute recently, or None when that is unknown.

    Readings come with requests, so a long gap between two of them is idle time. The pace is
    measured over the latest run of use within the last `span`, from the reading just before the
    span when use ran on across it: an idle hour before a busy session never dilutes its pace.
    No reading inside the span means nothing was used recently (rate 0).
    """
    start = now - span
    recent = [point for point in points if point[0] > start]
    if not recent:
        return 0.0 if points else None
    sequence = [point for point in points if point[0] <= start][-1:] + recent
    first = len(sequence) - 1
    while first > 0 and sequence[first][0] - sequence[first - 1][0] <= IDLE_GAP_SECONDS:
        first -= 1
    if first < len(sequence) - 1:
        base = sequence[first]
    elif first > 0:
        # A run of one reading tells no pace by itself: measure from the reading before it.
        base = sequence[first - 1]
    else:
        return None
    minutes = max(MIN_RATE_MINUTES, (now - base[0]) / 60)
    return max(0.0, sequence[-1][1] - base[1]) / minutes


async def account_rates(redis, account_id: str, now: float | None = None) -> dict[str, float | None]:
    """Each long window's recent use per minute (see burn_rate)."""
    now = time.time() if now is None else now
    history = await _load_state(redis, HISTORY_KEY_PREFIX, account_id, _histories)
    return {window_id: burn_rate(points, now) for window_id, points in history.items()}


def limit_outlook(accounts: list[dict], now: float | None = None) -> list[dict]:
    """How much of each long window is left across every account that can serve a model, how many
    minutes of work that is at the recent pace, and whether it is running low.

    `accounts` holds each account's `weight` (capacity weight), `windows` and `rates`. Accounts take
    over from each other when one runs out, so their remaining shares add up, weighted by size.
    """
    now = time.time() if now is None else now
    return [pool for window_id in TRACKED_WINDOWS if (pool := _pooled_window(accounts, window_id, now))]


def _pooled_window(accounts: list[dict], window_id: str, now: float) -> dict | None:
    capacity = remaining = rate = 0.0
    known_rate = False
    resets = []
    minutes = None
    for entry in accounts:
        window = next((item for item in entry.get('windows') or [] if item.get('id') == window_id), None)
        used = _number((window or {}).get('used_percent'))
        if used is None:
            continue
        weight = _number(entry.get('weight')) or 1.0
        capacity += weight * 100
        remaining += weight * (0.0 if window_exhausted(window, now) else max(0.0, 100 - used))
        if (resets_at := _number(window.get('resets_at'))) and resets_at > now:
            resets.append(resets_at)
        minutes = minutes or window.get('window_minutes')
        if (account_rate := (entry.get('rates') or {}).get(window_id)) is not None:
            known_rate = True
            rate += weight * account_rate
    if not capacity:
        return None
    minutes_left = remaining / rate if rate > 0 else None
    resets_at = min(resets) if resets else None
    # Work left that outlasts the first refill does not run out at this pace.
    lasts = minutes_left is None or (resets_at is not None and minutes_left >= (resets_at - now) / 60)
    left_percent = remaining / capacity * 100
    return {
        'id': window_id,
        'window_minutes': minutes,
        'used_percent': round(100 - left_percent, 2),
        'minutes_left': round(minutes_left) if minutes_left is not None else None,
        'rate_known': known_rate,
        'lasts_until_reset': bool(lasts and minutes_left is not None),
        'resets_at': int(resets_at) if resets_at else None,
        'level': _outlook_level(left_percent, None if lasts else minutes_left),
    }


def _outlook_level(left_percent: float, runs_out_in: float | None) -> str:
    """`runs_out_in`: minutes until the window is used up at the recent pace, unless it refills first."""
    if left_percent <= 0:
        return 'empty'
    if left_percent <= CRITICAL_PERCENT_LEFT or (runs_out_in is not None and runs_out_in <= CRITICAL_MINUTES_LEFT):
        return 'critical'
    if left_percent <= LOW_PERCENT_LEFT or (runs_out_in is not None and runs_out_in <= LOW_MINUTES_LEFT):
        return 'low'
    return 'ok'


async def _load_split(redis, account_id: str) -> dict:
    state = None
    if redis is not None:
        try:
            raw = await redis.get(f'{SPLIT_KEY_PREFIX}{account_id}')
            state = JSONCodec.loads(raw) if raw else None
        except Exception as e:
            log.debug('Could not read the usage split: %s', e)
    state = state or _splits.get(account_id) or {}
    state.setdefault('windows', {})
    state.setdefault('pending', {})
    return state


async def _save_split(redis, account_id: str, state: dict) -> None:
    _splits[account_id] = state
    if redis is not None:
        try:
            await redis.set(f'{SPLIT_KEY_PREFIX}{account_id}', JSONCodec.dumps(state), ex=SNAPSHOT_TTL_SECONDS)
        except Exception:
            log.debug('Usage split storage unavailable; using memory.')


def split_reading(state: dict, windows: list[dict]) -> None:
    """Share each window's rise since the last reading between the owner and the team."""
    for window in windows:
        window_id = window.get('id')
        used = _number(window.get('used_percent'))
        if window_id not in SPLIT_WINDOWS or used is None:
            continue
        resets_at = _number(window.get('resets_at'))
        previous = state['windows'].get(window_id)
        pending = state['pending'].get(window_id) or {'owner': 0, 'team': 0}
        if previous is None:
            # Use before Mologate first read this window is the owner's (decision 4).
            state['windows'][window_id] = {'used': used, 'resets_at': resets_at, 'team': 0.0}
            state['pending'].pop(window_id, None)
            continue
        started_over = used < previous['used'] - RESET_DROP or (
            resets_at is not None
            and previous.get('resets_at') is not None
            and resets_at > previous['resets_at'] + RESET_MOVE_SECONDS
        )
        baseline, team = (0.0, 0.0) if started_over else (previous['used'], previous['team'])
        rise = used - baseline
        if rise > 0:
            requests = pending['owner'] + pending['team']
            if requests:
                team += rise * pending['team'] / requests
            # A rise with no requests from Mologate is the owner's use outside it.
            state['pending'].pop(window_id, None)
        state['windows'][window_id] = {
            # A slightly lower reading is a stale one; it never undoes use already counted.
            'used': max(used, baseline),
            'resets_at': resets_at,
            'team': min(team, max(used, baseline)),
        }


async def count_account_request(redis, account_id: str, by_owner: bool) -> None:
    """Note who sent a request, so the next rise in the account's windows can be shared out."""
    try:
        async with _split_lock:
            state = await _load_split(redis, account_id)
            for window_id in SPLIT_WINDOWS:
                pending = state['pending'].setdefault(window_id, {'owner': 0, 'team': 0})
                pending['owner' if by_owner else 'team'] += 1
            await _save_split(redis, account_id, state)
    except Exception as e:
        log.debug('Could not count the account request: %s', e)


async def with_team_use(redis, account_id: str, snapshot: dict | None) -> dict | None:
    """The snapshot with each shared window's team use (`team_percent`), in account percent."""
    if not snapshot:
        return snapshot
    state = await _load_split(redis, account_id)
    windows = []
    for window in snapshot.get('windows', []):
        split = state['windows'].get(window.get('id'))
        if window.get('id') in SPLIT_WINDOWS and split is not None:
            window = {**window, 'team_percent': round(min(split['team'], window.get('used_percent') or 0), 2)}
        windows.append(window)
    return {**snapshot, 'windows': windows}


async def get_provider_limits(redis, account_id: str) -> dict | None:
    key = account_id
    snapshot = None
    if redis is not None:
        try:
            raw = await redis.get(f'{REDIS_KEY_PREFIX}{key}')
            snapshot = JSONCodec.loads(raw) if raw else None
        except Exception as e:
            log.debug('Could not read provider limits: %s', e)
    snapshot = snapshot or _snapshots.get(key)

    if not snapshot or snapshot.get('updated_at', 0) < time.time() - SNAPSHOT_TTL_SECONDS:
        return None
    return snapshot
