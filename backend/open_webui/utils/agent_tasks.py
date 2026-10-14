"""Mologate: the chat's tasks, as the Tasks panel and the agent's task tools see them.

A task is work an agent started that runs apart from its reply: a sub-agent (foreground or in the
background), a monitor waiting for a condition, or a scheduled prompt. Each one is an internal
child chat of the main chat (``chat.meta.internal`` with ``parent_chat_id``), with ``meta.type``
``'subagent'``, ``'monitor'`` or ``'timer'``; this module normalizes them into one shape.

Not to be confused with ``open_webui/tasks.py`` (the running asyncio tasks) or the chat's
checklist (``create_tasks``/``update_task``).
"""

import asyncio
import copy
import logging
import time
import uuid
from datetime import datetime, timezone

from open_webui.internal.db import get_async_db
from open_webui.models.chats import Chat, Chats
from open_webui.tasks import get_response_streams_by_chat_id, has_active_tasks, stop_item_tasks, stop_task
from open_webui.utils.agent_messages import _message_text
from open_webui.utils.misc import get_output_text
from open_webui.utils.task_limits import LIMITS
from sqlalchemy import or_, select, update

log = logging.getLogger(__name__)

# The ``type`` a task has in the API, and the ``meta.type`` of its row.
TASK_TYPES = {'subagent': 'subagent', 'monitor': 'monitor', 'scheduled': 'timer'}
# Short handles the agent uses: deleg_… (sub-agents' delegation id), mon_…, sched_….
HANDLE_PREFIXES = {'subagent': 'deleg', 'monitor': 'mon', 'scheduled': 'sched'}

ACTIVE = {'running', 'scheduled'}
TERMINAL = {'completed', 'failed', 'cancelled'}
STATUSES = ACTIVE | TERMINAL

ROW_TYPES = {row_type: task_type for task_type, row_type in TASK_TYPES.items()}
# A row's own ``meta.status`` values that mean its work is over, whatever its type.
FINISHED = {'completed', 'failed', 'cancelled', 'error', 'disabled'}
# Statuses of timer rows (utils/timers.py) as the API shows them.
SCHEDULED_STATUSES = {
    'pending': 'scheduled',
    'running': 'running',
    'completed': 'completed',
    'cancelled': 'cancelled',
    'error': 'failed',
    'failed': 'failed',
    'disabled': 'failed',
}
# A sub-agent row says running but has no task left: after this long it stopped without saying so
# (a restart); before, its run may still be starting.
STALE_SECONDS = 60
MAX_LISTED = 100
# The finished tasks task_status lists next to the unfinished ones.
MAX_FINISHED_LISTED = 20
MAX_ACTIVITY = 20
MAX_WAIT_SECONDS = 300
PREVIEW_CHARS = 300
TITLE_CHARS = 200

_KEEP = object()


def new_handle(prefix: str) -> str:
    return f'{prefix}_{uuid.uuid4().hex[:8]}'


async def emit_task_changed(
    user_id: str,
    chat_id: str,
    task_id: str,
    *,
    task_type: str,
    status: str,
    handle: str | None = None,
    notify: dict | None = None,
) -> None:
    """Tell the chat owner's open tabs a task changed; the Tasks panel refetches on it.

    ``notify`` ({title, body}) also shows a toast, e.g. when a schedule turns itself off.
    """
    from open_webui.socket.main import sio

    try:
        await sio.emit(
            'events',
            {
                'chat_id': chat_id,
                'message_id': None,
                'data': {
                    'type': 'chat:task',
                    'data': {
                        'task_id': task_id,
                        'handle': handle,
                        'task_type': task_type,
                        'status': status,
                        'notify': notify,
                    },
                },
            },
            room=f'user:{user_id}',
        )
    except Exception as e:
        log.debug(f'Could not send the task update for {task_id}: {e}')


def describe_seconds(seconds: int) -> str:
    """How a limit reads in a message: 45 s, 5 min, 2 h."""
    seconds = int(seconds)
    if seconds >= 3600 and seconds % 3600 == 0:
        return f'{seconds // 3600} h'
    if seconds >= 60:
        return f'{round(seconds / 60)} min'
    return f'{seconds} s'


def preview(text) -> str | None:
    """A task's result on one line, at most ``PREVIEW_CHARS`` long."""
    text = ' '.join(str(text or '').split())
    if len(text) > PREVIEW_CHARS:
        text = f'{text[: PREVIEW_CHARS - 1].rstrip()}…'
    return text or None


def error_text(value) -> str | None:
    """An error as text: rows and messages keep it as a string or as {content}."""
    if isinstance(value, dict):
        value = value.get('content') or value.get('message') or value.get('detail')
    return str(value) if value else None


def _seconds(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _ns_seconds(value) -> int | None:
    """Legacy timer rows keep their times in nanoseconds."""
    value = _seconds(value)
    return value // 1_000_000_000 if value else None


def _iso(seconds) -> str | None:
    seconds = _seconds(seconds)
    if seconds is None:
        return None
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace('+00:00', 'Z')


def _title(meta: dict, fallback: str, prefix: str) -> str:
    title = meta.get('title') or meta.get('task') or meta.get('description') or (fallback or '').removeprefix(prefix)
    title = ' '.join(str(title).split())
    return title[:TITLE_CHARS]


####################
# Rows
####################


_COLUMNS = (Chat.id, Chat.user_id, Chat.title, Chat.meta, Chat.created_at, Chat.updated_at, Chat.timer_at)


def _scope(root_chat_id: str, user_id: str) -> tuple:
    return (
        Chat.user_id == user_id,
        Chat.meta['internal'].as_boolean().is_(True),
        Chat.meta['parent_chat_id'].as_string() == root_chat_id,
        Chat.meta['type'].as_string().in_(list(ROW_TYPES)),
    )


def _row(result) -> dict:
    return {
        'id': result.id,
        'user_id': result.user_id,
        'title': result.title,
        'meta': result.meta or {},
        'created_at': result.created_at,
        'updated_at': result.updated_at,
        'timer_at': result.timer_at,
    }


async def task_rows(root_chat_id: str, user_id: str, *, include_cleared: bool = False) -> list[dict]:
    """The task rows of a main chat, without their messages."""
    async with get_async_db() as db:
        stmt = select(*_COLUMNS).where(*_scope(root_chat_id, user_id))
        if not include_cleared:
            stmt = stmt.where(Chat.meta['task_cleared'].as_boolean().is_not(True))
        return [_row(result) for result in (await db.execute(stmt)).all()]


async def resolve_handle(root_chat_id: str, user_id: str, handle: str) -> dict | None:
    """The task row a handle (deleg_…, mon_…, sched_…) or row id names, among the main chat's tasks."""
    handle = (handle or '').strip()
    if not handle:
        return None
    async with get_async_db() as db:
        stmt = (
            select(*_COLUMNS)
            .where(*_scope(root_chat_id, user_id))
            .where(
                or_(
                    Chat.id == handle,
                    Chat.meta['task_id'].as_string() == handle,
                    Chat.meta['delegation_id'].as_string() == handle,
                )
            )
        )
        result = (await db.execute(stmt)).first()
        return _row(result) if result else None


async def root_chat_id(chat_id: str) -> str:
    """The main chat an agent works for: the parent of a sub-agent's chat, else the chat itself."""
    async with get_async_db() as db:
        result = (await db.execute(select(Chat.meta).where(Chat.id == chat_id))).first()
    meta = (result[0] if result else None) or {}
    if meta.get('type') == 'subagent' and meta.get('parent_chat_id'):
        return meta['parent_chat_id']
    return chat_id


async def get_task_meta(chat_id: str) -> dict | None:
    async with get_async_db() as db:
        result = (await db.execute(select(Chat.meta).where(Chat.id == chat_id))).first()
        return dict(result[0] or {}) if result else None


async def update_task_meta(chat_id: str, change, *, timer_at=_KEEP):
    """Apply ``change(meta)`` to a task row's meta under a row lock and save it; returns what
    ``change`` returns, or None when the row is gone. A change that returns False saves nothing.

    When the change leaves the task finished, the row's ``timer_at`` is cleared too, so the scheduler
    leaves it alone; pass ``timer_at`` to set the column yourself.
    """
    async with get_async_db() as db:
        stmt = select(Chat.meta).where(Chat.id == chat_id)
        if db.bind.dialect.name == 'postgresql':
            stmt = stmt.with_for_update()
        result = (await db.execute(stmt)).first()
        if result is None:
            return None
        meta = copy.deepcopy(result[0] or {})
        outcome = change(meta)
        if outcome is False:
            return False
        values = {'meta': meta}
        if timer_at is not _KEEP:
            values['timer_at'] = timer_at
        elif meta.get('status') in FINISHED:
            values['timer_at'] = None
        await db.execute(update(Chat).where(Chat.id == chat_id).values(**values))
        await db.commit()
        return outcome


####################
# Normalizers
####################


def _subagent_fields(row: dict, meta: dict, active: bool, now: float) -> dict:
    raw = meta.get('status')
    error = error_text(meta.get('error'))
    started_at = _seconds(meta.get('started_at')) or row['created_at']
    if active:
        status = 'running'
    elif raw in ('completed', 'failed', 'cancelled'):
        status = raw
    elif raw == 'error':
        status = 'failed'
    elif raw == 'running':
        if now - started_at > STALE_SECONDS:
            status, error = 'failed', error or 'Stopped unexpectedly'
        else:
            status = 'running'
    else:
        # Rows from before sub-agents kept a status.
        status = 'failed' if error else 'completed'
    return {
        'status': status,
        'mode': meta.get('mode') or 'foreground',
        'started_at': started_at,
        'finished_at': None if status == 'running' else _seconds(meta.get('finished_at')) or row['updated_at'],
        'error': error,
        'preview': meta.get('result_preview'),
        'conversation_id': row['id'],
        'profile': meta.get('profile') or 'general-purpose',
    }


def _monitor_fields(row: dict, meta: dict) -> dict:
    raw = meta.get('status')
    status = raw if raw in STATUSES else 'failed' if raw == 'error' else 'running'
    condition = meta.get('condition') or {}
    return {
        'status': status,
        'mode': meta.get('mode') or 'foreground',
        'started_at': _seconds(meta.get('started_at')) or row['created_at'],
        'finished_at': (_seconds(meta.get('finished_at')) or row['updated_at']) if status in TERMINAL else None,
        'error': error_text(meta.get('error')),
        'preview': meta.get('result_preview') or preview(meta.get('result')),
        'monitor': {
            'kind': condition.get('kind') or ('http' if condition.get('url') else 'command'),
            'target': condition.get('command') or condition.get('url') or '',
            'interval': _seconds(meta.get('interval')),
            'checks': _seconds(meta.get('checks')) or 0,
            'max_checks': _seconds(meta.get('max_checks')),
            'ends_at': _seconds(meta.get('ends_at')),
        },
    }


def _scheduled_fields(row: dict, meta: dict) -> dict:
    raw = meta.get('status')
    status = SCHEDULED_STATUSES.get(raw, 'scheduled')
    last_run = meta.get('last_run') or {}
    schedule = meta.get('schedule')
    error = error_text(meta.get('error') or meta.get('timer_error'))
    if raw == 'disabled':
        failures = _seconds(meta.get('failures')) or LIMITS['schedule_max_failures'].default
        error = f'Turned off after {failures} failed runs in a row'
    next_run_at = None
    if status in ACTIVE:
        next_run_at = _seconds(meta.get('next_run_at'))
        if next_run_at is None and status == 'scheduled':
            # Legacy one-shot timers keep their due time in nanoseconds.
            next_run_at = _ns_seconds(meta.get('timer_at') or row.get('timer_at'))
    finished_at = None
    if status in TERMINAL:
        finished_at = (
            _seconds(meta.get('finished_at'))
            or _seconds(last_run.get('finished_at'))
            or _ns_seconds(meta.get('timer_completed_at') or meta.get('timer_cancelled_at'))
            or row['updated_at']
        )
    if schedule is None:
        # A legacy one-shot timer: it ran once when it completed.
        schedule = {'every': None, 'until': None, 'max_runs': 1}
        runs = 1 if raw == 'completed' else 0
    else:
        runs = _seconds(meta.get('runs')) or 0
    return {
        'status': status,
        'mode': None,
        'started_at': (
            _seconds(meta.get('started_at'))
            or _seconds(last_run.get('started_at'))
            or _ns_seconds(meta.get('timer_started_at'))
        ),
        'finished_at': finished_at,
        'error': error,
        'preview': meta.get('result_preview'),
        'schedule': {
            'next_run_at': next_run_at,
            'every': _seconds(schedule.get('every')),
            'runs': runs,
            'max_runs': _seconds(schedule.get('max_runs')),
            'ends_at': _seconds(schedule.get('until')),
            'failures': _seconds(meta.get('failures')) or 0,
        },
    }


def normalize(row: dict, *, active: bool = False, can_manage: bool = False, now: float | None = None) -> dict:
    """A task row in the shape the API and the tools share (``active``: a sub-agent's run is registered)."""
    meta = row.get('meta') or {}
    task_type = ROW_TYPES[meta.get('type')]
    now = time.time() if now is None else now
    if task_type == 'subagent':
        fields = _subagent_fields(row, meta, active, now)
        title = _title(meta, row.get('title'), 'Sub-agent: ')
    elif task_type == 'monitor':
        fields = _monitor_fields(row, meta)
        title = _title(meta, row.get('title'), 'Monitor: ')
    else:
        fields = _scheduled_fields(row, meta)
        title = _title({'title': meta.get('title')}, row.get('title'), 'Timer: ')
    started_at, finished_at = fields['started_at'], fields['finished_at']
    return {
        'id': row['id'],
        'handle': meta.get('task_id') or meta.get('delegation_id') or row['id'],
        'type': task_type,
        'title': title,
        'status': fields['status'],
        'mode': fields['mode'],
        'created_at': row['created_at'],
        'started_at': started_at,
        'finished_at': finished_at,
        'duration': max(0, finished_at - started_at) if started_at and finished_at else None,
        'preview': fields['preview'],
        'error': fields['error'],
        'conversation_id': fields.get('conversation_id'),
        'message_id': meta.get('parent_message_id'),
        'profile': fields.get('profile'),
        'schedule': fields.get('schedule'),
        'monitor': fields.get('monitor'),
        'can_stop': fields['status'] in ACTIVE and can_manage,
    }


async def to_task(redis, row: dict, *, can_manage: bool = False) -> dict:
    meta = row.get('meta') or {}
    active = False
    # A finished sub-agent that is messaged again is marked running first (utils/agent_messages.py).
    if meta.get('type') == 'subagent' and meta.get('status') not in FINISHED:
        active = await has_active_tasks(redis, row['id'])
    return normalize(row, active=active, can_manage=can_manage)


def _ordered(tasks: list[dict]) -> list[dict]:
    """Active tasks first, the newest started first; then finished ones, the newest finished first."""
    active = [task for task in tasks if task['status'] in ACTIVE]
    finished = [task for task in tasks if task['status'] not in ACTIVE]
    active.sort(key=lambda task: task['started_at'] or task['created_at'] or 0, reverse=True)
    finished.sort(key=lambda task: task['finished_at'] or task['created_at'] or 0, reverse=True)
    return active + finished


async def list_tasks(redis, root_chat_id: str, user_id: str, *, can_manage: bool = False) -> dict:
    tasks = [await to_task(redis, row, can_manage=can_manage) for row in await task_rows(root_chat_id, user_id)]
    return {
        'tasks': _ordered(tasks)[:MAX_LISTED],
        'running': sum(1 for task in tasks if task['status'] == 'running'),
    }


####################
# Details
####################


def _by_time(messages: dict) -> list[dict]:
    return sorted(
        (message for message in messages.values() if isinstance(message, dict)),
        key=lambda message: message.get('timestamp') or 0,
    )


async def _history(chat_id: str) -> dict:
    chat = await Chats.get_chat_by_id(chat_id)
    return (((chat.chat if chat else None) or {}).get('history') or {}).get('messages') or {}


async def _subagent_detail(redis, row: dict, running: bool) -> dict:
    messages = _by_time(await _history(row['id']))
    streams = {}
    if running:
        streams = {
            stream.get('message_id'): stream for stream in await get_response_streams_by_chat_id(redis, row['id'])
        }
    prompt = next((m for m in messages if m.get('role') == 'user' and not m.get('parentId')), None)
    answers = [message for message in messages if message.get('role') == 'assistant']
    activity = []
    for answer in answers:
        stream = streams.get(answer.get('id'))
        for item in (stream.get('output') if stream else answer.get('output')) or []:
            if isinstance(item, dict) and item.get('type') == 'function_call':
                activity.append({'name': item.get('name') or '', 'status': item.get('status') or 'completed'})
    output, live = None, False
    if answers:
        stream = streams.get(answers[-1].get('id'))
        if stream:
            output, live = stream.get('content') or get_output_text(stream.get('output')), True
        else:
            output = _message_text(answers[-1])
    return {
        'prompt': _message_text(prompt) or None,
        'output': output or None,
        'live': live,
        'activity': activity[-MAX_ACTIVITY:],
    }


def _monitor_detail(meta: dict) -> dict:
    last_check = meta.get('last_check') or {}
    return {
        'prompt': meta.get('description') or None,
        'output': meta.get('result') or last_check.get('output') or None,
        'checks': [
            {
                'at': check.get('at'),
                'met': bool(check.get('met')),
                'exit_code': check.get('exit_code'),
                'http_status': check.get('http_status'),
            }
            for check in meta.get('history') or []
            if isinstance(check, dict)
        ],
    }


def _run_answer(messages: dict, timer_id: str, message_id: str | None) -> dict | None:
    """The answer a scheduled run got in the main chat: by the run's message id, else its latest run."""
    message = messages.get(message_id) if message_id else None
    if message is None:
        prompts = [
            m
            for m in messages.values()
            if isinstance(m, dict)
            and m.get('role') == 'user'
            and (m.get('meta') or {}).get('type') == 'timer'
            and timer_id in ((m.get('meta') or {}).get('timer_id'), *((m.get('meta') or {}).get('timer_ids') or []))
        ]
        message = max(prompts, key=lambda m: m.get('timestamp') or 0) if prompts else None
    if message and message.get('role') == 'user':
        answers = [messages[child] for child in message.get('childrenIds') or [] if child in messages]
        message = max(answers, key=lambda m: m.get('timestamp') or 0) if answers else None
    return message


async def _scheduled_detail(row: dict, root_chat_id: str) -> dict:
    meta = row['meta']
    prompt_message = None
    if meta.get('timer_task_message_id'):
        prompt_message = await Chats.get_message_by_id_and_message_id(row['id'], meta['timer_task_message_id'])
    last_run = meta.get('last_run') or {}
    answer = _run_answer(await _history(root_chat_id), row['id'], last_run.get('message_id'))
    runs = [
        {
            'started_at': _seconds(run.get('started_at')),
            'finished_at': _seconds(run.get('finished_at')),
            'status': run.get('status') or 'completed',
            'message_id': run.get('message_id'),
            'error': error_text(run.get('error')),
        }
        for run in meta.get('run_history') or []
        if isinstance(run, dict)
    ]
    if not runs and meta.get('schedule') is None and answer:
        # A legacy one-shot timer: its one run is the turn it started.
        runs = [
            {
                'started_at': _ns_seconds(meta.get('timer_completed_at') or meta.get('timer_started_at')),
                'finished_at': None,
                'status': 'failed' if answer.get('error') else 'completed',
                'message_id': answer.get('id'),
                'error': error_text(answer.get('error')),
            }
        ]
    return {
        'prompt': _message_text(prompt_message) or None,
        'output': _message_text(answer) or None,
        'runs': runs,
    }


async def get_task(redis, root_chat_id: str, user_id: str, task_id: str, *, can_manage: bool = False) -> dict | None:
    """One task with what its detail view shows: prompt, output (live while a sub-agent runs),
    activity, runs and checks. None when it isn't one of the main chat's tasks."""
    row = await resolve_handle(root_chat_id, user_id, task_id)
    if not row:
        return None
    task = await to_task(redis, row, can_manage=can_manage)
    detail = {**task, 'prompt': None, 'output': None, 'live': False, 'activity': [], 'runs': [], 'checks': []}
    if task['type'] == 'subagent':
        detail.update(await _subagent_detail(redis, row, task['status'] == 'running'))
    elif task['type'] == 'monitor':
        detail.update(_monitor_detail(row['meta']))
    else:
        detail.update(await _scheduled_detail(row, root_chat_id))
    return detail


####################
# Stop and clear
####################


async def stop_task_row(
    redis, root_chat_id: str, user_id: str, task_id: str, *, cancelled_by: str, can_manage: bool = True
) -> tuple[dict | None, bool]:
    """Stop a task: mark it cancelled, then stop its run. Returns (the task, whether it was stopped);
    a finished task comes back unchanged. Nobody's result is posted, so the main agent isn't woken."""
    row = await resolve_handle(root_chat_id, user_id, task_id)
    if not row:
        return None, False
    task = await to_task(redis, row, can_manage=can_manage)
    if task['status'] not in ACTIVE:
        return task, False

    run_task_ids = []

    def cancel(meta: dict):
        if meta.get('status') in FINISHED:
            return False
        meta.update(status='cancelled', cancelled_by=cancelled_by, finished_at=int(time.time()))
        run_task_ids.extend(meta.get('run_task_ids') or [])
        return True

    if not await update_task_meta(row['id'], cancel):
        row = await resolve_handle(root_chat_id, user_id, task_id) or row
        return await to_task(redis, row, can_manage=can_manage), False

    await stop_item_tasks(redis, row['id'])
    # A scheduled run is a turn of the main chat. The agent cancelling its schedule is most likely
    # that very turn, so its run finishes and only the next ones are called off.
    if cancelled_by != 'agent':
        for run_task_id in run_task_ids:
            await stop_task(redis, run_task_id)

    row = await resolve_handle(root_chat_id, user_id, task_id) or row
    task = normalize(row, can_manage=can_manage)
    await emit_task_changed(
        row['user_id'], root_chat_id, row['id'], task_type=task['type'], status=task['status'], handle=task['handle']
    )
    return task, True


async def clear_tasks(redis, root_chat_id: str, user_id: str) -> int:
    """Hide the finished tasks from the panel. Rows stay: the chat still links sub-agent conversations."""

    def clear(meta: dict):
        meta['task_cleared'] = True
        return True

    cleared = 0
    for row in await task_rows(root_chat_id, user_id):
        if (await to_task(redis, row))['status'] in TERMINAL and await update_task_meta(row['id'], clear):
            cleared += 1
    if cleared:
        await emit_task_changed(user_id, root_chat_id, None, task_type=None, status='cleared')
    return cleared


####################
# Agent tools
####################


def _agent_summary(task: dict, now: float) -> dict:
    summary = {
        'task_id': task['handle'],
        'type': task['type'],
        'title': task['title'],
        'status': task['status'],
        'mode': task['mode'],
        'started_at': _iso(task['started_at']),
    }
    if task['status'] == 'running' and task['started_at']:
        summary['elapsed_seconds'] = max(0, int(now - task['started_at']))
    elif task['duration'] is not None:
        summary['duration_seconds'] = task['duration']
    summary['preview'] = task['preview']
    if task['schedule'] and task['schedule']['next_run_at']:
        summary['next_run_at'] = _iso(task['schedule']['next_run_at'])
    return summary


def _agent_detail(detail: dict, *, max_output: int, can_cancel: bool, now: float) -> dict:
    # A sub-agent's answer (so far, while it runs), a monitor's result or last check output, or the
    # answer to a schedule's last run.
    result = detail['output'] or ''
    if len(result) > max_output:
        result = f'{result[:max_output]}\n\n[output truncated]'
    report = {
        'task_id': detail['handle'],
        'type': detail['type'],
        'title': detail['title'],
        'status': detail['status'],
        'mode': detail['mode'],
        'started_at': _iso(detail['started_at']),
        'finished_at': _iso(detail['finished_at']),
        'duration_seconds': detail['duration'],
    }
    if detail['status'] == 'running' and detail['started_at']:
        report['elapsed_seconds'] = max(0, int(now - detail['started_at']))
    report.update(
        result=result or None,
        error=detail['error'],
        conversation_id=detail['conversation_id'],
        can_cancel=can_cancel and detail['status'] in ACTIVE,
    )
    if detail['type'] == 'subagent' and detail['activity']:
        report['activity'] = detail['activity']
    if detail['schedule']:
        schedule = detail['schedule']
        report['schedule'] = {
            **schedule,
            'next_run_at': _iso(schedule['next_run_at']),
            'ends_at': _iso(schedule['ends_at']),
        }
    if detail['monitor']:
        report['monitor'] = {**detail['monitor'], 'ends_at': _iso(detail['monitor']['ends_at'])}
    return report


async def agent_task_status(
    redis, chat_id: str, user_id: str, task_id: str = '', wait_seconds: int = 0, *, max_output: int = 30_000
) -> dict:
    """What ``task_status`` returns: one task (waiting a while for it if asked), or all of them."""
    root = await root_chat_id(chat_id)
    now = time.time()
    if not (task_id or '').strip():
        listing = await list_tasks(redis, root, user_id)
        unfinished = [task for task in listing['tasks'] if task['status'] in ACTIVE]
        finished = [task for task in listing['tasks'] if task['status'] not in ACTIVE][:MAX_FINISHED_LISTED]
        return {
            'tasks': [_agent_summary(task, now) for task in unfinished + finished],
            'running': listing['running'],
        }

    row = await resolve_handle(root, user_id, task_id)
    if not row:
        return {'error': f'No task "{task_id}" in this chat. Call task_status without a task_id to list them.'}
    try:
        wait = max(0, min(int(wait_seconds or 0), MAX_WAIT_SECONDS))
    except (TypeError, ValueError):
        wait = 0
    deadline = time.monotonic() + wait
    while (await to_task(redis, row))['status'] == 'running' and time.monotonic() < deadline:
        await asyncio.sleep(1)
        row = await resolve_handle(root, user_id, task_id) or row
    detail = await get_task(redis, root, user_id, row['id'])
    if not detail:
        return {'error': f'Task "{task_id}" no longer exists.'}
    return _agent_detail(detail, max_output=max_output, can_cancel=root == chat_id, now=time.time())


async def agent_cancel_task(redis, chat_id: str, user_id: str, task_id: str) -> dict:
    """What ``cancel_task`` returns. Stopping posts no result, so it doesn't start a turn."""
    if not (task_id or '').strip():
        return {'error': 'task_id is required. Call task_status to list the tasks of this chat.'}
    root = await root_chat_id(chat_id)
    task, stopped = await stop_task_row(redis, root, user_id, task_id, cancelled_by='agent')
    if task is None:
        return {'error': f'No task "{task_id}" in this chat. Call task_status to list them.'}
    if stopped:
        return {'status': 'cancelled', 'task_id': task['handle'], 'type': task['type']}
    return {
        'status': 'already_finished',
        'task_id': task['handle'],
        'type': task['type'],
        'task_status': task['status'],
    }
