"""Durable timers backed by internal child chats.

Mologate: these are the chat's scheduled tasks (``schedule_task``): a prompt that runs as a new turn
in the chat once, or every so often until ``until`` or ``max_runs``. The row's ``timer_at`` column
holds the next run while it waits, and a lease while a run is in flight; upstream's one-shot timers
are the same rows without a ``schedule``.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import re
import time
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from fastapi import Request
from open_webui import tasks as task_registry
from open_webui.internal.db import get_async_db
from open_webui.models.chat_messages import ChatMessages
from open_webui.models.chats import Chat, ChatForm, Chats
from open_webui.models.users import Users
from open_webui.tasks import has_active_tasks, stop_task
from open_webui.utils import task_limits
from open_webui.utils.agent_tasks import HANDLE_PREFIXES, emit_task_changed, new_handle
from open_webui.utils.auth import VERIFIED_USER_ROLES
from open_webui.utils.chat_id import is_saved_chat_id
from open_webui.utils.json_codec import JSONCodec
from open_webui.utils.misc import get_message_list
from sqlalchemy import func, select, update
from starlette.datastructures import Headers

log = logging.getLogger(__name__)

_RELATIVE_TIME = re.compile(r'^(?:\+|in\s+)?(\d+)\s*(s|sec(?:onds?)?|m|min(?:utes?)?|h|hours?|d|days?|w|weeks?)$')
_RFC3339_TIME = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$')
_INTERVAL = re.compile(r'^(?:every\s+)?(\d+)\s*(s|sec(?:onds?)?|m|min(?:utes?)?|h|hours?|d|days?|w|weeks?)$')
_INTERVAL_WORDS = {'hourly': 3600, 'daily': 86400, 'weekly': 604800}
_UNIT_SECONDS = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}
_TIME_UNITS_NS = {unit: seconds * 1_000_000_000 for unit, seconds in _UNIT_SECONDS.items()}
_timer_locks: dict[str, asyncio.Lock] = {}

NS = 1_000_000_000
# A busy chat puts the run off this long; a model low on its limit, longer.
BUSY_RETRY_SECONDS = 15
LOW_LIMIT_RETRY_SECONDS = 15 * 60
# From claim to start, then from start to the end of the run-time limit plus this margin.
CLAIM_LEASE_SECONDS = 300
LEASE_MARGIN_SECONDS = 60
# How long a turn stopped at the run-time limit gets to save its state.
STOP_GRACE_SECONDS = 10
RUN_HISTORY_SIZE = 20
_API_STATUS = {
    'pending': 'scheduled',
    'running': 'running',
    'completed': 'completed',
    'cancelled': 'cancelled',
    'error': 'failed',
    'disabled': 'failed',
}


def parse_timer_at(value: str, field: str = 'at') -> int:
    """Normalize a relative offset or timezone-aware RFC 3339 timestamp."""
    raw = value.strip()
    now = time.time_ns()
    relative = _RELATIVE_TIME.fullmatch(raw.lower())
    if relative:
        count = int(relative.group(1))
        if count <= 0:
            raise ValueError(f'{field} must be in the future.')
        return now + count * _TIME_UNITS_NS[relative.group(2)[0]]

    if not _RFC3339_TIME.fullmatch(raw):
        raise ValueError(
            f'{field} must be a relative time such as 10s or in 10 seconds, or an RFC 3339 timestamp with a timezone.'
        )
    try:
        parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError(
            f'{field} must be a relative time such as 10s or in 10 seconds, or an RFC 3339 timestamp with a timezone.'
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f'absolute {field} values must include an explicit timezone.')

    due_at = int(parsed.timestamp() * 1_000_000_000)
    if due_at <= now:
        raise ValueError(f'{field} must be in the future.')
    return due_at


def parse_every(value: str) -> int:
    """Mologate: a repeat interval such as 30m, 2h, 1d, 1w or daily, in seconds."""
    raw = value.strip().lower()
    if raw in _INTERVAL_WORDS:
        return _INTERVAL_WORDS[raw]
    match = _INTERVAL.fullmatch(raw)
    if not match or int(match.group(1)) <= 0:
        raise ValueError('every must be an interval such as 2h, 1d or 7d.')
    return int(match.group(1)) * _UNIT_SECONDS[match.group(2)[0]]


def format_span(seconds: int) -> str:
    """The shortest exact form of a span: 90s, 30m, 2h, 1d."""
    for unit, size in (('d', 86400), ('h', 3600), ('m', 60)):
        if seconds >= size and seconds % size == 0:
            return f'{seconds // size}{unit}'
    return f'{seconds}s'


def _iso(ns: int | None) -> str | None:
    if ns is None:
        return None
    return datetime.fromtimestamp(ns / NS, timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def next_occurrence(due_ns: int, every_seconds: int | None, now_ns: int) -> int | None:
    """The first run after now of a schedule whose current run was due at ``due_ns``.

    Runs missed meanwhile (a long run, a busy chat, the server down) are skipped, so runs never overlap.
    """
    if not every_seconds:
        return None
    step = every_seconds * NS
    return due_ns + max(1, (now_ns - due_ns) // step + 1) * step


def _next_run(meta: dict, now_ns: int) -> int | None:
    """When the schedule runs next, or None when it is over."""
    schedule = meta.get('schedule') or {}
    max_runs = schedule.get('max_runs')
    if max_runs and (meta.get('runs') or 0) >= max_runs:
        return None
    next_at = next_occurrence(int(meta.get('timer_at') or now_ns), schedule.get('every'), now_ns)
    if next_at is None or (schedule.get('until') and next_at > schedule['until'] * NS):
        return None
    return next_at


def _close_run(meta: dict, run: dict, now_ns: int, max_failures: int) -> tuple[int | None, dict | None]:
    """Record a finished or skipped run in ``meta`` and plan what comes next.

    Returns the row's new ``timer_at`` and, when the schedule turned itself off, the toast to show.
    A schedule cancelled meanwhile stays cancelled; only the run is recorded.
    """
    history = list(meta.get('run_history') or [])
    if history and history[-1].get('status') == 'running':
        history[-1] = run
    else:
        history.append(run)
    meta['run_history'] = history[-RUN_HISTORY_SIZE:]
    meta['last_run'] = run
    meta['run_task_ids'] = []
    if run['status'] == 'skipped':
        meta['skipped'] = (meta.get('skipped') or 0) + 1
    elif run['status'] == 'failed':
        meta['failures'] = (meta.get('failures') or 0) + 1
    elif run['status'] == 'completed':
        meta['failures'] = 0
    if meta.get('status') == 'cancelled':
        return None, None

    now = now_ns // NS
    next_at = _next_run(meta, now_ns)
    if next_at is None:
        failed = run['status'] == 'failed'
        meta.update(
            status='error' if failed else 'completed',
            error=run.get('error') if failed else None,
            finished_at=now,
            next_run_at=None,
            postponed_reason=None,
            timer_completed_at=now_ns,
        )
        return None, None
    if (meta.get('failures') or 0) >= max_failures:
        error = f'Turned off after {meta["failures"]} failed runs in a row'
        meta.update(status='disabled', error=error, finished_at=now, next_run_at=None, postponed_reason=None)
        body = f'{error}. Last error: {run.get("error")}' if run.get('error') else f'{error}.'
        return None, {'title': f'Scheduled task turned off: {meta.get("title") or "untitled"}', 'body': body}
    meta.update(
        status='pending',
        timer_at=next_at,
        next_run_at=next_at // NS,
        postponed_reason=None,
        timer_claim_id=None,
        timer_started_at=None,
    )
    return next_at, None


def _postpone(meta: dict, now_ns: int, seconds: int, reason: str) -> int | None:
    """Put the due run off by ``seconds``; None when that runs into the next occurrence or past
    the end, so the run is skipped instead."""
    retry_at = now_ns + seconds * NS
    schedule = meta.get('schedule') or {}
    every = schedule.get('every')
    if every and retry_at >= int(meta.get('timer_at') or now_ns) + every * NS:
        return None
    if schedule.get('until') and retry_at > schedule['until'] * NS:
        return None
    meta.update(
        status='pending',
        timer_claim_id=None,
        timer_started_at=None,
        next_run_at=retry_at // NS,
        postponed_reason=reason,
    )
    return retry_at


def _run_entry(started_at: int, status: str, message_id: str | None = None, error: str | None = None) -> dict:
    finished_at = None if status == 'running' else int(time.time())
    return {
        'started_at': started_at,
        'finished_at': finished_at,
        'status': status,
        'message_id': message_id,
        'error': error,
    }


def _same_state(meta: dict) -> list:
    """Where-clauses that match the row only while it is still in ``meta``'s status and claim."""
    status, claim_id = Chat.meta['status'].as_string(), Chat.meta['timer_claim_id'].as_string()
    return [
        status == meta['status'] if meta.get('status') is not None else status.is_(None),
        claim_id == meta['timer_claim_id'] if meta.get('timer_claim_id') is not None else claim_id.is_(None),
    ]


async def _change_timer(timer_id: str, change) -> dict | None:
    """Apply ``change(meta, timer_at)`` to the timer row and return the saved meta.

    ``change`` edits ``meta`` in place and returns the other columns to set (``{}`` for none), or
    None to leave the row alone. The write only lands while nobody else changed the row's status
    meanwhile (a stop from the Tasks panel); otherwise ``change`` runs again on the new state.
    """
    for _ in range(3):
        async with get_async_db() as db:
            stmt = select(Chat.meta, Chat.timer_at).where(Chat.id == timer_id)
            if db.bind.dialect.name == 'postgresql':
                stmt = stmt.with_for_update()
            row = (await db.execute(stmt)).first()
            if row is None:
                return None
            before = dict(row[0] or {})
            meta = copy.deepcopy(before)
            columns = change(meta, row[1])
            if columns is None:
                return None
            result = await db.execute(
                update(Chat)
                .where(Chat.id == timer_id, *_same_state(before))
                .values(meta=meta, updated_at=int(time.time()), **columns)
            )
            await db.commit()
            if result.rowcount:
                return meta
    log.warning(f'Timer {timer_id} kept changing; giving up on this update')
    return None


async def _emit(user_id: str, timer_id: str, meta: dict, notify: dict | None = None) -> None:
    await emit_task_changed(
        user_id,
        meta.get('parent_chat_id') or '',
        timer_id,
        task_type='scheduled',
        status=_API_STATUS.get(meta.get('status'), 'failed'),
        handle=meta.get('task_id') or timer_id,
        notify=notify,
    )


async def count_active_schedules(user_id: str) -> int:
    """Mologate: the member's waiting or running scheduled tasks plus their active automations;
    ``schedules_per_user`` caps both together."""
    from open_webui.models.automations import Automations

    async with get_async_db() as db:
        result = await db.execute(
            select(func.count())
            .select_from(Chat)
            .where(Chat.user_id == user_id)
            .where(Chat.timer_at.isnot(None))
            .where(Chat.meta['type'].as_string() == 'timer')
            .where(Chat.meta['status'].as_string().in_(('pending', 'running')))
        )
        timers = result.scalar() or 0
    return timers + await Automations.count_active_by_user(user_id)


async def create_schedule(
    *,
    prompt: str,
    at: str = '',
    every: str = '',
    max_runs: int | None = None,
    until: str = '',
    name: str = '',
    cancel_on: list[Literal['chat.read', 'chat.user_message']] | None = None,
    request: Request,
    user_data: dict,
    metadata: dict,
    parent_chat_id: str,
    parent_message_id: str | None,
) -> str:
    """Mologate: the ``schedule_task`` tool. One-off when ``every`` is empty."""
    prompt = prompt.strip()
    if not prompt:
        return 'Error: prompt must not be empty.'
    if not is_saved_chat_id(parent_chat_id) or not user_data.get('id'):
        return 'Error: scheduled tasks need a saved chat.'

    limits = await task_limits.get_all()
    now = time.time_ns()
    try:
        every_seconds = parse_every(every) if (every or '').strip() else None
        if every_seconds is not None and every_seconds < limits['schedule_interval_seconds']:
            raise ValueError(f'every must be at least {format_span(limits["schedule_interval_seconds"])}.')
        if (at or '').strip():
            due_at = parse_timer_at(at)
        elif every_seconds:
            due_at = now + every_seconds * NS
        else:
            raise ValueError('at is required for a one-off task; set every for a repeating one.')
        if due_at > now + limits['schedule_ahead_seconds'] * NS:
            raise ValueError(f'at must be within {format_span(limits["schedule_ahead_seconds"])} from now.')
        if max_runs is not None and (int(max_runs) < 1 or (not every_seconds and int(max_runs) > 1)):
            raise ValueError('max_runs must be at least 1, and more than 1 only with every.')
        lifetime_end = now + limits['schedule_lifetime_seconds'] * NS
        ends_at = parse_timer_at(until, 'until') if (until or '').strip() else (lifetime_end if every_seconds else None)
        if ends_at is not None:
            ends_at = min(ends_at, lifetime_end)
            if ends_at < due_at:
                raise ValueError('until must be after the first run.')
    except ValueError as exc:
        return f'Error: {exc}'

    selected_events = cancel_on or []
    allowed_events = {'chat.read', 'chat.user_message'}
    if any(event not in allowed_events for event in selected_events):
        return 'Error: cancel_on accepts only chat.read and chat.user_message.'
    selected_events = list(dict.fromkeys(selected_events))

    model_id = metadata.get('model_id') or (metadata.get('model') or {}).get('id')
    if not model_id:
        return 'Error: model context is required.'
    if metadata.get('direct'):
        return 'Error: scheduled tasks are unavailable for direct connections.'

    user_id = user_data['id']
    active = await count_active_schedules(user_id)
    if active >= limits['schedules_per_user']:
        return (
            f'Error: you already have the most scheduled tasks and automations allowed at once '
            f'({limits["schedules_per_user"]}). Cancel one (cancel_task) or turn off an automation first.'
        )

    title = (name or '').strip()[:200] or prompt.splitlines()[0][:60]
    handle = new_handle(HANDLE_PREFIXES['scheduled'])
    schedule = {
        'every': every_seconds,
        'until': ends_at // NS if ends_at is not None else None,
        'max_runs': int(max_runs) if max_runs is not None else (None if every_seconds else 1),
    }
    chat_id = str(uuid4())
    user_message_id = str(uuid4())
    run = {
        'model_id': model_id,
        'session_id': metadata.get('session_id'),
        'tool_ids': copy.deepcopy(metadata.get('tool_ids') or []),
        'skill_ids': copy.deepcopy(metadata.get('skill_ids') or []),
        'system_prompt': metadata.get('system_prompt'),
        'filter_ids': copy.deepcopy(metadata.get('filter_ids') or []),
        'terminal_id': metadata.get('terminal_id'),
        'features': copy.deepcopy(metadata.get('features') or {}),
        'files': copy.deepcopy(metadata.get('files') or []),
        'variables': copy.deepcopy(metadata.get('variables') or {}),
    }

    meta = {
        'internal': True,
        'type': 'timer',
        'parent_chat_id': parent_chat_id,
        'parent_message_id': parent_message_id,
        'timer_at': due_at,
        'status': 'pending',
        'timer_model_id': model_id,
        'timer_task_message_id': user_message_id,
        'cancel_on': selected_events,
        'run': run,
        'task_id': handle,
        'title': title,
        'schedule': schedule,
        'runs': 0,
        'failures': 0,
        'skipped': 0,
        'next_run_at': due_at // NS,
        'postponed_reason': None,
        'last_run': None,
        'run_history': [],
        'run_task_ids': [],
        'started_at': None,
        'finished_at': None,
        'error': None,
        'result_preview': None,
    }
    chat = await Chats.insert_new_chat(
        chat_id,
        user_id,
        ChatForm(
            chat={
                'id': chat_id,
                'title': f'Scheduled: {title[:60]}',
                'models': [model_id],
                'history': {
                    'currentId': user_message_id,
                    'messages': {
                        user_message_id: {
                            'id': user_message_id,
                            'parentId': None,
                            'childrenIds': [],
                            'role': 'user',
                            'content': prompt,
                            'timestamp': int(time.time()),
                            'models': [model_id],
                        },
                    },
                },
                'messages': [{'role': 'user', 'content': prompt}],
            }
        ),
        internal_meta=meta,
        timer_at=due_at,
    )
    if not chat:
        return 'Error: failed to schedule the task.'
    await _emit(user_id, chat_id, meta)

    return JSONCodec.dumps(
        {
            'status': 'scheduled',
            'task_id': handle,
            'name': title,
            'next_run_at': _iso(due_at),
            'every': format_span(every_seconds) if every_seconds else None,
            'max_runs': schedule['max_runs'],
            'ends_at': _iso(ends_at),
            'cancel_on': selected_events,
            'note': 'Each run is a new turn in this chat; cancel_task stops it.',
        },
        ensure_ascii=False,
    )


async def claim_due_timers(now_ns: int, limit: int = 10) -> list[tuple[str, str]]:
    """Claim due timers by moving them from pending to running.

    Mologate: a claimed run holds a lease in ``timer_at``; runs whose lease ran out (the server
    stopped mid-run) are recorded as failed and rescheduled here. Monitors share ``timer_at`` and
    the 'running' status, so these queries only look at timer rows.
    """
    await _recover_stale_runs(now_ns)
    async with get_async_db() as db:
        stmt = (
            select(Chat)
            .where(Chat.timer_at <= now_ns)
            .where(Chat.meta['type'].as_string() == 'timer')
            .where(Chat.meta['status'].as_string() == 'pending')
            .order_by(Chat.timer_at)
            .limit(limit)
        )
        if db.bind.dialect.name == 'postgresql':
            stmt = stmt.with_for_update(skip_locked=True)

        result = await db.execute(stmt)

        claimed = []
        for row in result.scalars().all():
            claim_id = str(uuid4())
            row.meta = {
                **(row.meta or {}),
                'status': 'running',
                'timer_started_at': now_ns,
                'timer_claim_id': claim_id,
            }
            row.timer_at = now_ns + CLAIM_LEASE_SECONDS * NS
            row.updated_at = int(time.time())
            claimed.append((row.id, claim_id))
        await db.commit()
        return claimed


async def _recover_stale_runs(now_ns: int, limit: int = 10) -> None:
    async with get_async_db() as db:
        stmt = (
            select(Chat)
            .where(Chat.timer_at <= now_ns)
            .where(Chat.meta['type'].as_string() == 'timer')
            .where(Chat.meta['status'].as_string() == 'running')
            .order_by(Chat.timer_at)
            .limit(limit)
        )
        if db.bind.dialect.name == 'postgresql':
            stmt = stmt.with_for_update(skip_locked=True)
        rows = (await db.execute(stmt)).scalars().all()
        if not rows:
            return

        max_failures = await task_limits.get('schedule_max_failures')
        changed = []
        for row in rows:
            meta = copy.deepcopy(row.meta or {})
            last = meta.get('last_run') or {}
            if last.get('status') == 'running':
                run = _run_entry(last.get('started_at'), 'failed', last.get('message_id'), 'Stopped unexpectedly')
            else:
                # Claimed, but the turn never started.
                started_at = (meta.get('timer_started_at') or now_ns) // NS
                run = _run_entry(started_at, 'failed', error='Stopped unexpectedly')
            row.timer_at, notify = _close_run(meta, run, now_ns, max_failures)
            row.meta = meta
            row.updated_at = int(time.time())
            changed.append((row.user_id, row.id, meta, notify))
        await db.commit()

    for user_id, timer_id, meta, notify in changed:
        log.warning(f'Timer {timer_id}: its run stopped unexpectedly')
        await _emit(user_id, timer_id, meta, notify)


async def cancel_timers_for_chat(
    parent_chat_id: str, event: Literal['chat.read', 'chat.user_message'], user_id: str
) -> None:
    """Owner-scoped: without the user filter, reading a chat cancels every user's timers on it."""
    cancelled = []
    async with get_async_db() as db:
        result = await db.execute(
            select(Chat)
            .where(Chat.timer_at.isnot(None))
            .where(Chat.user_id == user_id)
            .where(Chat.meta['parent_chat_id'].as_string() == parent_chat_id)
            .where(Chat.meta['status'].as_string() == 'pending')
        )
        now_ns = int(time.time_ns())
        for row in result.scalars().all():
            meta = row.meta or {}
            if event not in (meta.get('cancel_on') or []):
                continue
            row.meta = {
                **meta,
                'status': 'cancelled',
                'timer_cancelled_at': now_ns,
                'timer_cancelled_by': event,
                'finished_at': now_ns // NS,
                'next_run_at': None,
            }
            row.timer_at = None
            row.updated_at = int(time.time())
            cancelled.append((row.id, row.meta))
        await db.commit()

    for timer_id, meta in cancelled:
        await _emit(user_id, timer_id, meta)


async def watch_run(redis, task_ids: list[str], run_seconds: int) -> str:
    """Mologate: wait for a turn's tasks to end, stopping them at ``run_seconds``.

    Returns 'done', 'cancelled' (someone stopped the turn) or 'timed_out'. The turn was started on
    this worker, so its tasks are in this worker's registry.
    """
    running = [task for task_id in task_ids if (task := task_registry.tasks.get(task_id)) is not None]
    if not running:
        return 'done'
    done, pending = await asyncio.wait(running, timeout=run_seconds)
    if pending:
        for task_id in task_ids:
            await stop_task(redis, task_id)
        await asyncio.wait(pending, timeout=STOP_GRACE_SECONDS)
        return 'timed_out'
    return 'cancelled' if any(task.cancelled() for task in done) else 'done'


async def _limit_left_percent(app, model_id: str, user) -> float | None:
    """The least of any usage window left (percent) across the provider accounts that serve the
    model, as the model's limits outlook reports it (routers/models.py); None when not known."""
    model = (getattr(app.state, 'MODELS', None) or {}).get(model_id) or {}
    base_model_id = (model.get('info') or {}).get('base_model_id') or model_id
    if not base_model_id.startswith('account:'):
        return None

    from open_webui.utils.provider_accounts import select_accounts
    from open_webui.utils.provider_limits import account_rates, get_provider_limits, limit_outlook

    redis = app.state.redis
    try:
        pool = []
        for account in await select_accounts(base_model_id, user):
            windows = ((await get_provider_limits(redis, account.id)) or {}).get('windows', [])
            pool.append(
                {'weight': account.capacity_weight, 'windows': windows, 'rates': await account_rates(redis, account.id)}
            )
        outlook = limit_outlook(pool)
    except Exception as e:
        log.debug(f'No limits outlook for {model_id}: {e}')
        return None
    return min((100 - window['used_percent'] for window in outlook), default=None)


async def _put_off(timer_id: str, user_id: str, seconds: int, reason: str, text: str) -> None:
    """Put a claimed run off, or skip it when that would run into the next one."""
    max_failures = await task_limits.get('schedule_max_failures')
    now_ns = time.time_ns()
    outcome = {}

    def change(meta, timer_at):
        if meta.get('status') != 'running':
            return None
        outcome['emit'] = meta.get('postponed_reason') != reason
        retry_at = _postpone(meta, now_ns, seconds, reason)
        if retry_at is not None:
            return {'timer_at': retry_at}
        outcome['emit'] = True
        next_at, outcome['notify'] = _close_run(
            meta, _run_entry(now_ns // NS, 'skipped', error=text), now_ns, max_failures
        )
        return {'timer_at': next_at}

    meta = await _change_timer(timer_id, change)
    if meta and outcome.get('emit'):
        await _emit(user_id, timer_id, meta, outcome.get('notify'))


async def _fail(timer_id: str, user_id: str, error: str) -> None:
    """The schedule can't run at all any more (its chat, owner or prompt is gone)."""

    def change(meta, timer_at):
        if meta.get('status') == 'cancelled':
            return None
        meta.update(status='error', timer_error=error, error=error, finished_at=int(time.time()), next_run_at=None)
        meta['run_task_ids'] = []
        return {'timer_at': None}

    meta = await _change_timer(timer_id, change)
    if meta:
        await _emit(user_id, timer_id, meta)


async def execute_due_timer(app, timer_id: str, claim_id: str | None = None) -> None:
    lock = _timer_locks.setdefault(timer_id, asyncio.Lock())
    async with lock:
        started = await _start_run(app, timer_id, claim_id)
        if started:
            await _finish_run(app, timer_id, **started)


async def _start_run(app, timer_id: str, claim_id: str | None) -> dict | None:
    """Start the claimed run's turn in the parent chat, unless it has to wait or be skipped."""
    from open_webui.socket.main import sio
    from open_webui.utils.subagents import _parent_locks

    timer = await Chats.get_chat_by_id(timer_id)
    if not timer:
        return None
    meta = timer.meta or {}
    if meta.get('type') != 'timer' or meta.get('status') != 'running':
        return None
    if claim_id is not None and meta.get('timer_claim_id') != claim_id:
        return None

    parent_chat_id = meta.get('parent_chat_id') or ''
    parent = await Chats.get_chat_by_id_and_user_id(parent_chat_id, timer.user_id)
    if not parent:
        await _fail(timer_id, timer.user_id, 'parent chat no longer exists')
        return None

    prompt_message_id = meta.get('timer_task_message_id')
    prompt_message = await Chats.get_message_by_id_and_message_id(timer_id, prompt_message_id)
    if not prompt_message:
        await _fail(timer_id, timer.user_id, 'timer task message is missing')
        return None

    user = await Users.get_user_by_id(timer.user_id)
    if not user:
        await _fail(timer_id, timer.user_id, 'timer user no longer exists')
        return None

    # Re-gate the rehydrated owner: a demoted owner must not run.
    if user.role not in VERIFIED_USER_ROLES:
        await _fail(timer_id, timer.user_id, 'owner no longer permitted to run timers')
        return None

    run = meta.get('run') or {}
    model_id = run.get('model_id') or meta.get('timer_model_id')
    if not model_id:
        await _fail(timer_id, timer.user_id, 'model context is missing')
        return None

    prompt = prompt_message.get('content') or ''
    if isinstance(prompt, list):
        prompt = ''.join(
            str(part.get('text', '')) for part in prompt if isinstance(part, dict) and part.get('type') == 'text'
        )

    limits = await task_limits.get_all()
    # Mologate: don't spend the last of the model's limit on a scheduled run; that isn't a failure.
    low_limit = limits['schedule_low_limit_percent']
    if low_limit:
        left = await _limit_left_percent(app, model_id, user)
        if left is not None and left < low_limit:
            await _put_off(
                timer_id,
                timer.user_id,
                LOW_LIMIT_RETRY_SECONDS,
                'low_limit',
                f'The model had less than {low_limit}% of its limit left',
            )
            return None

    user_message_id = str(uuid4())
    assistant_message_id = str(uuid4())
    user_message = None
    assistant_message = None
    message_list = []
    started_at = int(time.time())
    run_seconds = limits['schedule_run_seconds']
    run_number = (meta.get('runs') or 0) + 1
    parent_lock = _parent_locks.setdefault(parent_chat_id, asyncio.Lock())
    async with parent_lock:
        busy = await has_active_tasks(app.state.redis, parent_chat_id)
        if not busy:
            async with get_async_db() as db:
                stmt = select(Chat).where(Chat.id == parent_chat_id, Chat.user_id == timer.user_id)
                if db.bind.dialect.name == 'postgresql':
                    stmt = stmt.with_for_update()
                result = await db.execute(stmt)
                parent = result.scalar_one_or_none()
                if not parent:
                    await db.rollback()
                    await _fail(timer_id, timer.user_id, 'parent chat no longer exists')
                    return None

                parent_chat = copy.deepcopy(parent.chat or {})
                history = parent_chat.setdefault('history', {})
                messages = history.setdefault('messages', {})
                done_assistants = [
                    message
                    for message in messages.values()
                    if message.get('role') == 'assistant' and message.get('done') is not False
                ]
                parent_id = (
                    max(done_assistants, key=lambda message: message.get('timestamp', 0)).get('id')
                    if done_assistants
                    else meta.get('parent_message_id')
                )
                message_list = get_message_list(messages, parent_id)

                user_message = {
                    'id': user_message_id,
                    'parentId': parent_id,
                    'childrenIds': [assistant_message_id],
                    'role': 'user',
                    'content': prompt,
                    'model': model_id,
                    'meta': {
                        'internal': True,
                        'type': 'timer',
                        'timer_id': timer_id,
                        'task_id': meta.get('task_id'),
                        'run_number': run_number,
                        'title': meta.get('title'),
                    },
                    'timestamp': int(time.time()),
                }
                assistant_message = {
                    'id': assistant_message_id,
                    'parentId': user_message_id,
                    'childrenIds': [],
                    'role': 'assistant',
                    'content': '',
                    'done': False,
                    'model': model_id,
                    'timestamp': int(time.time()),
                }
                messages[user_message_id] = user_message
                messages[assistant_message_id] = assistant_message
                if parent_id and parent_id in messages:
                    children = messages[parent_id].setdefault('childrenIds', [])
                    if user_message_id not in children:
                        children.append(user_message_id)

                parent.chat = parent_chat
                history['currentId'] = assistant_message_id
                parent.current_message_id = assistant_message_id
                parent.updated_at = int(time.time())

                # Mologate: the run holds the row (status running, a lease in timer_at) until it ends.
                run_meta = copy.deepcopy(meta)
                entry = _run_entry(started_at, 'running', assistant_message_id)
                run_meta.update(
                    runs=run_number,
                    started_at=run_meta.get('started_at') or started_at,
                    last_run=entry,
                    run_history=[*(run_meta.get('run_history') or []), entry][-RUN_HISTORY_SIZE:],
                    run_task_ids=[],
                    postponed_reason=None,
                )
                upcoming = _next_run(run_meta, time.time_ns())
                run_meta['next_run_at'] = upcoming // NS if upcoming else None
                claimed = await db.execute(
                    update(Chat)
                    .where(Chat.id == timer_id, *_same_state(meta))
                    .values(
                        meta=run_meta,
                        timer_at=time.time_ns() + (run_seconds + LEASE_MARGIN_SECONDS) * NS,
                        updated_at=int(time.time()),
                    )
                )
                if not claimed.rowcount:
                    # Cancelled while it was being started.
                    await db.rollback()
                    return None
                await db.commit()
            await ChatMessages.upsert_message(user_message_id, parent_chat_id, timer.user_id, user_message)
            await ChatMessages.upsert_message(assistant_message_id, parent_chat_id, timer.user_id, assistant_message)
    if busy:
        await _put_off(timer_id, timer.user_id, BUSY_RETRY_SECONDS, 'busy', 'The chat was busy')
        return None

    await sio.emit(
        'events',
        {
            'chat_id': parent_chat_id,
            'message_id': assistant_message_id,
            'data': {'type': 'chat:reload'},
        },
        room=f'user:{timer.user_id}',
    )
    await _emit(timer.user_id, timer_id, run_meta)
    form_data = {
        'model': model_id,
        'messages': [
            *([{'role': 'system', 'content': run.get('system_prompt')}] if run.get('system_prompt') else []),
            *message_list,
            {'role': 'user', 'content': prompt},
        ],
        'stream': True,
        'chat_id': parent_chat_id,
        'id': assistant_message_id,
        'parent_id': user_message.get('parentId'),
        'user_message': user_message,
        'session_id': run.get('session_id') or f'timer:{parent_chat_id}',
        'background_tasks': {},
        'tool_ids': run.get('tool_ids') or [],
        'skill_ids': run.get('skill_ids') or [],
        'filter_ids': run.get('filter_ids') or [],
        'features': run.get('features') or {},
        'files': run.get('files') or [],
        'variables': run.get('variables') or {},
    }
    if run.get('terminal_id'):
        form_data['terminal_id'] = run['terminal_id']
    request = Request(
        {
            'type': 'http',
            'asgi': {'version': '3.0', 'spec_version': '2.0'},
            'method': 'POST',
            'path': '/api/v1/timers/internal',
            'query_string': b'',
            'headers': Headers({}).raw,
            'client': ('127.0.0.1', 0),
            'server': ('127.0.0.1', 80),
            'scheme': 'http',
            'app': app,
        }
    )
    request.state.token = None
    request.state.enable_api_keys = False
    started = {
        'user_id': timer.user_id,
        'parent_chat_id': parent_chat_id,
        'message_id': assistant_message_id,
        'started_at': started_at,
        'run_seconds': run_seconds,
        'task_ids': [],
        'error': None,
    }
    try:
        result = await app.state.CHAT_COMPLETION_HANDLER(request, form_data, user=user)
    except Exception as exc:
        log.exception(f'Timer {timer_id} completion failed')
        return {**started, 'error': str(exc)[:500]}

    task_ids = [task_id for task_id in ((result or {}).get('task_ids') or []) if task_id]

    # Mologate: a stop finds the run's tasks here (and stops them with the row).
    def keep_task_ids(meta, timer_at):
        if meta.get('status') != 'running':
            return None
        meta['run_task_ids'] = task_ids
        return {}

    if task_ids and not await _change_timer(timer_id, keep_task_ids):
        # Stopped before its tasks were known: stop them here.
        for task_id in task_ids:
            await stop_task(app.state.redis, task_id)
    return {**started, 'task_ids': task_ids}


async def _finish_run(
    app,
    timer_id: str,
    *,
    user_id: str,
    parent_chat_id: str,
    message_id: str,
    started_at: int,
    run_seconds: int,
    task_ids: list[str],
    error: str | None,
) -> None:
    """Wait for the run's turn to end (stopping it at the run-time limit), then record it."""
    outcome = await watch_run(app.state.redis, task_ids, run_seconds) if task_ids else 'done'
    answer = await Chats.get_message_by_id_and_message_id(parent_chat_id, message_id) or {}
    if not error and outcome == 'timed_out':
        error = f'Stopped at the {format_span(run_seconds)} run-time limit'
    if not error and answer.get('error'):
        answer_error = answer['error']
        error = str(answer_error.get('content') if isinstance(answer_error, dict) else answer_error)[:500]
    status = 'failed' if error else 'stopped' if outcome == 'cancelled' else 'completed'
    run = _run_entry(started_at, status, message_id, error)
    content = answer.get('content')
    preview = content.strip()[:300] if isinstance(content, str) and content.strip() else None
    max_failures = await task_limits.get('schedule_max_failures')
    now_ns = time.time_ns()
    toast = {}

    def change(meta, timer_at):
        last = meta.get('last_run') or {}
        # Already recorded (the lease ran out first) or not this run.
        if last.get('status') != 'running' or last.get('message_id') != message_id:
            return None
        if preview:
            meta['result_preview'] = preview
        next_at, toast['notify'] = _close_run(meta, run, now_ns, max_failures)
        return {} if meta.get('status') == 'cancelled' else {'timer_at': next_at}

    meta = await _change_timer(timer_id, change)
    if meta:
        await _emit(user_id, timer_id, meta, toast.get('notify'))
