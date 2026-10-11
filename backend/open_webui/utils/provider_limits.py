"""Usage limits providers report in response headers, per connection, for the context indicator.

Every completion response from an OpenAI-compatible connection is checked for rate-limit
headers. What a provider reports is kept per connection (one connection is one shared account
or key), so every model served by it shows the same limits. Recognized headers:

- Codex / ChatGPT: ``x-codex-{primary,secondary}-used-percent``, ``-window-minutes`` and
  ``-reset-at`` (or the older ``-reset-after-seconds``). The primary window is the 5-hour limit
  and the secondary one the weekly limit.
- OpenAI-style: ``x-ratelimit-{limit,remaining,reset}-{requests,tokens}``.
- Plain ``x-ratelimit-{limit,remaining,reset}``, as OpenRouter sends.

Snapshots live in Redis when it is configured, so every worker sees them, and in memory
otherwise. Providers that send none of these headers simply have no limits to show.
"""

import logging
import re
import time
from collections.abc import Mapping
from typing import Any

from open_webui.utils.json_codec import JSONCodec

log = logging.getLogger(__name__)

REDIS_KEY_PREFIX = 'mologate:provider_limits:'
# A snapshot older than this says little about the account now.
SNAPSHOT_TTL_SECONDS = 8 * 24 * 60 * 60

_snapshots: dict[str, dict] = {}

_DURATION_PART = re.compile(r'(\d+(?:\.\d+)?)(ms|h|m|s)')


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # drop NaN


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

    seen = set()
    result = []
    for window in windows:
        if window and window['id'] not in seen:
            seen.add(window['id'])
            result.append(window)
    return result


def connection_key(url_idx: Any) -> str:
    return f'openai:{url_idx}'


async def record_provider_limits(redis, url_idx: Any, headers: Mapping[str, str]) -> None:
    """Keep what a response's headers say about the connection's limits. Never raises."""
    try:
        windows = parse_rate_limit_headers(headers)
        if not windows:
            return

        key = connection_key(url_idx)
        snapshot = {'windows': windows, 'updated_at': int(time.time())}
        _snapshots[key] = snapshot
        if redis is not None:
            await redis.set(f'{REDIS_KEY_PREFIX}{key}', JSONCodec.dumps(snapshot), ex=SNAPSHOT_TTL_SECONDS)
    except Exception as e:
        log.debug('Could not record provider limits: %s', e)


async def get_provider_limits(redis, url_idx: Any) -> dict | None:
    key = connection_key(url_idx)
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
