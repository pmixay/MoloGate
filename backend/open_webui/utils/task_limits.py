"""Mologate: limits for the chat's tasks: sub-agents, background work, monitors and schedules.

Every limit has a default and a hard range. Values are clamped to that range when they are read
and when an admin saves them, so a typo or an old stored value can't lift a cap; admins aren't
exempt. Defaults come from ``MOLOGATE_TASKS_<NAME>`` when it is set, and admins change them under
Settings → Sub-agents → Tasks (stored as ``tasks.limits.<name>``).
"""

import os
from dataclasses import dataclass

from open_webui.models.config import Config


@dataclass(frozen=True)
class Limit:
    default: int
    low: int
    high: int
    # 0 is allowed as "off" even though it is below ``low``.
    zero_allowed: bool = False


LIMITS: dict[str, Limit] = {
    # A sub-agent or monitor still running after this long moves to the background (0 = never).
    'auto_background_seconds': Limit(300, 30, 3600, zero_allowed=True),
    'subagents_per_chat': Limit(4, 1, 10),
    'background_tasks_per_user': Limit(5, 1, 15),
    'background_run_seconds': Limit(7200, 300, 28800),
    'monitors_per_user': Limit(5, 1, 20),
    # The default and the shortest time between two checks of a monitor.
    'monitor_interval_seconds': Limit(60, 15, 3600),
    'monitor_check_timeout_seconds': Limit(30, 5, 30),
    'monitor_lifetime_seconds': Limit(21600, 60, 86400),
    'monitor_max_checks': Limit(500, 1, 2000),
    # Scheduled tasks and automations together.
    'schedules_per_user': Limit(10, 1, 25),
    # The shortest time between two runs of a repeating schedule.
    'schedule_interval_seconds': Limit(3600, 900, 2592000),
    'schedule_ahead_seconds': Limit(2592000, 60, 7776000),
    'schedule_run_seconds': Limit(1800, 60, 7200),
    'schedule_lifetime_seconds': Limit(7776000, 3600, 31536000),
    'schedule_max_failures': Limit(3, 1, 10),
    # A scheduled run waits while the model has less than this much of its limit left (0 = off).
    'schedule_low_limit_percent': Limit(10, 1, 50, zero_allowed=True),
}


def config_key(name: str) -> str:
    return f'tasks.limits.{name}'


def clamp(name: str, value) -> int:
    limit = LIMITS[name]
    try:
        number = int(value)
    except (TypeError, ValueError):
        return limit.default
    if number == 0 and limit.zero_allowed:
        return 0
    return max(limit.low, min(limit.high, number))


def _env_default(name: str) -> int:
    value = os.environ.get(f'MOLOGATE_TASKS_{name.upper()}', '').strip()
    return clamp(name, value) if value else LIMITS[name].default


DEFAULT_CONFIG: dict[str, int] = {config_key(name): _env_default(name) for name in LIMITS}


async def get(name: str) -> int:
    return clamp(name, await Config.get(config_key(name), DEFAULT_CONFIG[config_key(name)]))


async def get_all() -> dict[str, int]:
    stored = await Config.get_many(*(config_key(name) for name in LIMITS))
    return {name: clamp(name, stored.get(config_key(name), DEFAULT_CONFIG[config_key(name)])) for name in LIMITS}


async def set_many(values: dict) -> dict[str, int]:
    """Store the given limits, clamped; unknown names are ignored. Returns every limit."""
    updates = {config_key(name): clamp(name, value) for name, value in values.items() if name in LIMITS}
    if updates:
        await Config.upsert(updates)
    return await get_all()


def describe(values: dict[str, int]) -> list[dict]:
    return [
        {
            'name': name,
            'value': values.get(name, limit.default),
            'default': limit.default,
            'min': limit.low,
            'max': limit.high,
            'zero_allowed': limit.zero_allowed,
        }
        for name, limit in LIMITS.items()
    ]
