"""Mologate: monitors, tasks that wait until a condition in the chat's container becomes true.

A monitor checks on a schedule: a command (true when it exits 0, or when its output matches a
pattern) or a URL fetched from inside the container (true when it answers with the expected status,
and its body matches the pattern if there is one). Checks only ever run in the chat's sandbox, so URL
probes go through its firewall and network switch, and patterns are matched there with ``grep -E``;
the server evaluates nothing and no model is called.

Each monitor is an internal child chat with ``meta.type == 'monitor'``. ``chat.timer_at`` holds the
next check while it runs, and a lease while a check is in flight; the scheduler claims due monitors
(utils/automations.py), so they survive restarts. A monitor never has ``meta.status == 'pending'``,
so the timer scheduler never claims one.

The ``monitor`` tool waits for the result and hands the monitor to the background after
``auto_background_seconds``. When a monitor ends, either the waiting tool call returns its result or,
when nobody waits any more, the result is posted to the chat. Every write is a compare-and-set on
``meta.rev``, so exactly one of the two happens.
"""

import asyncio
import copy
import logging
import shlex
import time
from urllib.parse import urlsplit
from uuid import uuid4

import aiohttp
from open_webui.internal.db import get_async_db
from open_webui.models.chats import Chat, ChatForm, Chats
from open_webui.models.users import Users
from open_webui.utils import containers, task_limits
from open_webui.utils.agent_tasks import HANDLE_PREFIXES, emit_task_changed, new_handle
from open_webui.utils.auth import VERIFIED_USER_ROLES
from open_webui.utils.json_codec import JSONCodec
from sqlalchemy import func, select, update

log = logging.getLogger(__name__)

OUTPUT_LIMIT = 4000
HISTORY_LIMIT = 20
MAX_ERRORS = 5
LEASE_MARGIN = 30
MAX_INTERVAL = task_limits.LIMITS['monitor_interval_seconds'].high
MAX_DESCRIPTION = 300
MAX_COMMAND = 4000
MAX_URL = 2048
MAX_PATTERN = 500
# The waiting tool call renews ``waiter_until`` this often; one that stops renewing is gone.
WAITER_RENEW = 10
WAITER_GRACE = 30
POLL_SECONDS = 5
# Containers pause after two idle minutes, which would freeze what the monitor waits for, so checks
# further apart than this ping the container in between.
KEEP_AWAKE = 60
# timeout(1) itself failed, or the shell could not find or run the command.
UNRUNNABLE_EXIT_CODES = {125, 126, 127}
_MARKER = '__MOLOGATE_CHECK_'

_waiters: dict[str, asyncio.Event] = {}
_start_locks: dict[str, asyncio.Lock] = {}


# -- conditions and checks -------------------------------------------------------------------------


def _has_control_chars(text: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in text)


def parse_condition(command: str, url: str, expect_status, pattern: str) -> dict:
    """The condition as stored in ``meta.condition``; raises ValueError with what to fix."""
    command = (command or '').strip()
    url = (url or '').strip()
    pattern = pattern or ''
    if bool(command) == bool(url):
        raise ValueError('give exactly one of command or url.')
    if pattern and (len(pattern) > MAX_PATTERN or _has_control_chars(pattern)):
        raise ValueError(f'pattern must be one line of at most {MAX_PATTERN} characters.')
    if command:
        if len(command) > MAX_COMMAND or '\x00' in command:
            raise ValueError(f'command must be at most {MAX_COMMAND} characters.')
        condition = {'kind': 'command', 'command': command}
    else:
        if len(url) > MAX_URL or _has_control_chars(url) or ' ' in url:
            raise ValueError(f'url must be one address of at most {MAX_URL} characters, without spaces.')
        try:
            parts = urlsplit(url)
            valid = parts.scheme in ('http', 'https') and bool(parts.hostname)
        except ValueError:
            valid = False
        if not valid:
            raise ValueError('url must start with http:// or https:// and name a host.')
        try:
            status = int(expect_status)
        except (TypeError, ValueError):
            status = 0
        if not 100 <= status <= 599:
            raise ValueError('expect_status must be an HTTP status from 100 to 599.')
        condition = {'kind': 'http', 'url': url, 'expect_status': status}
    if pattern:
        condition['pattern'] = pattern
    return condition


def describe_condition(condition: dict) -> str:
    pattern = condition.get('pattern')
    if condition.get('kind') == 'http':
        text = f'{condition.get("url")} answers HTTP {condition.get("expect_status", 200)}'
        return f'{text} with a body matching /{pattern}/' if pattern else text
    command = condition.get('command')
    return f'the output of `{command}` matches /{pattern}/' if pattern else f'`{command}` exits with code 0'


def _int(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def resolve_settings(limits: dict, interval_seconds, timeout_minutes, max_checks) -> dict:
    """What the agent asked for, within the admin's limits; 0 asks for the default."""
    floor = limits['monitor_interval_seconds']
    lifetime = limits['monitor_lifetime_seconds']
    timeout = _int(timeout_minutes) * 60
    checks = _int(max_checks)
    return {
        'interval': min(max(floor, _int(interval_seconds)), MAX_INTERVAL),
        'lifetime': min(timeout, lifetime) if timeout else lifetime,
        'max_checks': min(checks, limits['monitor_max_checks']) if checks else limits['monitor_max_checks'],
        'check_timeout': limits['monitor_check_timeout_seconds'],
    }


def build_check(condition: dict, check_timeout: int, nonce: str) -> str:
    """The shell script one check runs in the container.

    It prints the check's output, capped, then a marker line with the exit code, the HTTP status and
    grep's exit code for the pattern. Everything the agent gave is shell-quoted.
    """
    if condition.get('kind') == 'http':
        run = (
            'http=$(curl -sS -L --proto =http,https --proto-redir =http,https '
            f'--max-time {check_timeout} -o "$d/out" -w "%{{http_code}}" -- {shlex.quote(condition["url"])} '
            '</dev/null 2>"$d/err"); code=$?'
        )
        show = f'head -c {OUTPUT_LIMIT} "$d/out"; [ -s "$d/err" ] && {{ echo; head -c 500 "$d/err"; }}'
    else:
        run = (
            f'http=-; timeout -k 5 {check_timeout} sh -c {shlex.quote(condition["command"])} '
            '</dev/null >"$d/out" 2>&1; code=$?'
        )
        show = f'tail -c {OUTPUT_LIMIT} "$d/out"'
    pattern = condition.get('pattern')
    match = f'grep -Eq -- {shlex.quote(pattern)} "$d/out" 2>/dev/null; match=$?' if pattern else 'match=-'
    return '; '.join(
        [
            'd=$(mktemp -d) || exit 1',
            ': >"$d/out"',
            run,
            match,
            show,
            f'printf "\\n{_MARKER}{nonce}__ %s %s %s\\n" "$code" "$http" "$match"',
            'rm -rf "$d"',
        ]
    )


def _check_error(error: str, output: str = '') -> dict:
    return {'met': False, 'exit_code': None, 'http_status': None, 'output': output, 'error': error}


def evaluate(condition: dict, response, nonce: str) -> dict:
    """Turn Open Terminal's run_command answer into {met, exit_code, http_status, output, error}.

    ``error`` is set when the check could not run at all; those count towards ``MAX_ERRORS``.
    """
    if not isinstance(response, dict) or response.get('error'):
        detail = response.get('error') if isinstance(response, dict) else response
        return _check_error(f'The container did not run the check: {detail}'[:500])
    text = ''.join(
        str(entry.get('data') or '') for entry in response.get('output') or [] if isinstance(entry, dict)
    ).replace('\r\n', '\n')
    before, found, after = text.rpartition(f'{_MARKER}{nonce}__')
    output = (before if found else text).strip()
    output = output[:OUTPUT_LIMIT] if condition.get('kind') == 'http' else output[-OUTPUT_LIMIT:]
    if response.get('status') == 'running':
        return _check_error('The check did not finish in time.', output)
    if not found:
        return _check_error('The check ended without a result.', output)

    code, http, match = (after.split() + ['-', '-', '-'])[:3]
    exit_code = int(code) if code.lstrip('-').isdigit() else None
    result = {'met': False, 'exit_code': exit_code, 'http_status': None, 'output': output, 'error': None}
    pattern = condition.get('pattern')
    if pattern and match == '2':
        result['error'] = 'The pattern is not a valid extended regular expression (grep -E).'
    elif condition.get('kind') == 'http':
        status = int(http) if http.isdigit() and int(http) > 0 else None
        result['http_status'] = status
        result['met'] = status == condition.get('expect_status', 200) and (match == '0' if pattern else True)
    elif exit_code in UNRUNNABLE_EXIT_CODES:
        result['error'] = f'The command could not run (exit code {exit_code}).'
    else:
        result['met'] = match == '0' if pattern else exit_code == 0
    return result


async def _run_in_container(context: str, user_id: str, monitor_id: str, command: str, wait: int):
    from open_webui.utils.tools import execute_tool_server

    if not context.startswith('chat:'):
        raise containers.NodeUnavailable('Monitors run only in a chat container.')
    if not await containers.containers_enabled():
        raise containers.NodeUnavailable('Chat containers are turned off.')
    base_url, node_headers = await containers.route(context, user_id)
    response, _ = await execute_tool_server(
        url=base_url,
        # Its own session, so the checks stay out of the agent's process list and working directory.
        headers={
            'Content-Type': 'application/json',
            'X-User-Id': user_id,
            'X-Session-Id': f'monitor:{monitor_id}',
            **node_headers,
        },
        cookies={},
        name='run_command',
        params={'command': command, 'wait': wait, 'tail': 20},
        server_data=await containers.cached_tool_server_data(),
    )
    return response


async def _run_check(monitor_id: str, meta: dict, user_id: str) -> dict:
    condition = meta.get('condition') or {}
    check_timeout = int(meta.get('check_timeout') or 30)
    nonce = uuid4().hex[:12]
    try:
        response = await asyncio.wait_for(
            _run_in_container(
                meta.get('context') or '',
                user_id,
                monitor_id,
                build_check(condition, check_timeout, nonce),
                wait=check_timeout + 10,
            ),
            # Ends before the lease, so a slow check is never run twice.
            check_timeout + LEASE_MARGIN - 5,
        )
    except asyncio.TimeoutError:
        return _check_error('The container did not answer in time.')
    except Exception as e:
        return _check_error(f'The container is unavailable: {e}'[:500])
    return evaluate(condition, response, nonce)


async def _keep_awake(context: str, user_id: str) -> None:
    try:
        base_url, headers = await containers.route(context, user_id)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15, connect=5)) as session:
            async with session.get(f'{base_url}/health', headers=headers, allow_redirects=False) as response:
                await response.read()
    except Exception as e:
        log.debug('Could not keep the container of %s awake: %s', context, e)


# -- rows ------------------------------------------------------------------------------------------


async def _get_row(monitor_id: str) -> tuple[dict, str] | None:
    async with get_async_db() as db:
        row = (await db.execute(select(Chat.meta, Chat.user_id).where(Chat.id == monitor_id))).first()
        return (dict(row[0] or {}), row[1]) if row else None


async def _update(monitor_id: str, change) -> dict | None:
    """Apply ``change(meta)`` and save it, unless the row changed since it was read; then try again.

    ``change`` edits meta in place and returns the columns to set with it ({} for none), or None to
    leave the row alone. Returns the saved meta, or None when nothing was saved.
    """
    for _ in range(5):
        async with get_async_db() as db:
            row = (await db.execute(select(Chat.meta).where(Chat.id == monitor_id))).first()
            if row is None:
                return None
            meta = dict(row[0] or {})
            rev, status = meta.get('rev', 0), meta.get('status')
            columns = change(meta)
            if columns is None:
                return None
            meta['rev'] = rev + 1
            # The status too: a stop from the Tasks panel changes it without counting a revision.
            result = await db.execute(
                update(Chat)
                .where(
                    Chat.id == monitor_id,
                    Chat.meta['rev'].as_integer() == rev,
                    Chat.meta['status'].as_string() == status,
                )
                .values(meta=meta, updated_at=int(time.time()), **columns)
                .execution_options(synchronize_session=False)
            )
            await db.commit()
            if result.rowcount:
                return meta
    log.warning('Monitor %s kept changing under an update; gave up on it', monitor_id)
    return None


async def count_active(user_id: str) -> int:
    async with get_async_db() as db:
        result = await db.execute(
            select(func.count(Chat.id)).where(
                Chat.user_id == user_id,
                Chat.timer_at.isnot(None),
                Chat.meta['type'].as_string() == 'monitor',
                Chat.meta['status'].as_string() == 'running',
            )
        )
        return result.scalar() or 0


async def claim_due_monitors(now_ns: int, limit: int = 10) -> list[tuple[str, str]]:
    """Claim the monitors whose next check is due, with a lease on ``timer_at`` while it runs."""
    async with get_async_db() as db:
        stmt = (
            select(Chat.id, Chat.meta)
            .where(Chat.timer_at <= now_ns)
            .where(Chat.meta['type'].as_string() == 'monitor')
            .where(Chat.meta['status'].as_string() == 'running')
            .order_by(Chat.timer_at)
            .limit(limit)
        )
        if db.bind.dialect.name == 'postgresql':
            stmt = stmt.with_for_update(skip_locked=True)

        claimed = []
        for monitor_id, meta in (await db.execute(stmt)).all():
            meta = dict(meta or {})
            rev = meta.get('rev', 0)
            claim_id = str(uuid4())
            lease = now_ns + (int(meta.get('check_timeout') or 30) + LEASE_MARGIN) * 1_000_000_000
            result = await db.execute(
                update(Chat)
                .where(
                    Chat.id == monitor_id,
                    Chat.meta['rev'].as_integer() == rev,
                    Chat.meta['status'].as_string() == 'running',
                )
                .values(meta={**meta, 'claim_id': claim_id, 'rev': rev + 1}, timer_at=lease)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount:
                claimed.append((monitor_id, claim_id))
        await db.commit()
        return claimed


# -- results ---------------------------------------------------------------------------------------


def _duration(seconds) -> str:
    seconds = max(0, int(seconds or 0))
    if seconds < 60:
        return f'{seconds}s'
    if seconds < 3600:
        return f'{seconds // 60}m {seconds % 60}s'
    return f'{seconds // 3600}h {seconds % 3600 // 60}m'


def _elapsed(meta: dict) -> int:
    return max(0, int((meta.get('finished_at') or time.time()) - (meta.get('started_at') or time.time())))


def _summary(meta: dict) -> str:
    checks = meta.get('checks') or 0
    counted = f'{checks} check{"" if checks == 1 else "s"}'
    elapsed = _duration(_elapsed(meta))
    description = meta.get('description') or ''
    outcome = meta.get('outcome')
    if outcome == 'met':
        return f'True after {counted} and {elapsed}: {description}'
    if outcome == 'timed_out':
        return f'Still not true after {counted} and {elapsed}, so it gave up: {description}'
    if outcome == 'cancelled':
        return f'Stopped with the reply after {counted} and {elapsed}.'
    return f'Stopped after {counted} and {elapsed}: {meta.get("error")}'


def _finish(meta: dict, status: str, outcome: str, error: str | None, now: float) -> None:
    meta.update(
        status=status,
        outcome=outcome,
        error=error,
        finished_at=int(now),
        claim_id=None,
        next_check_at=None,
    )
    meta['result'] = _summary(meta)
    meta['result_preview'] = meta['result'][:300]


def _record(meta: dict, claim_id: str, check: dict, now: float) -> dict | None:
    """Count one check, then schedule the next or finish the monitor (a change for ``_update``)."""
    if meta.get('status') != 'running' or meta.get('claim_id') != claim_id:
        return None
    checks = int(meta.get('checks') or 0) + 1
    errors = int(meta.get('consecutive_errors') or 0) + 1 if check.get('error') else 0
    entry = {
        'at': int(now),
        'met': bool(check.get('met')),
        'exit_code': check.get('exit_code'),
        'http_status': check.get('http_status'),
    }
    meta.update(
        checks=checks,
        consecutive_errors=errors,
        claim_id=None,
        last_check={
            **entry,
            'output': (check.get('output') or '')[:OUTPUT_LIMIT],
            **({'error': check['error']} if check.get('error') else {}),
        },
        history=[*(meta.get('history') or []), entry][-HISTORY_LIMIT:],
    )
    ends_at = meta.get('ends_at') or now
    if check.get('met'):
        _finish(meta, 'completed', 'met', None, now)
    elif check.get('fatal') or errors >= MAX_ERRORS:
        reason = (
            check.get('error') if check.get('fatal') else f'{errors} checks in a row could not run. {check["error"]}'
        )
        _finish(meta, 'failed', 'failed', reason, now)
    elif checks >= int(meta.get('max_checks') or 1):
        _finish(meta, 'failed', 'timed_out', f'Gave up after {checks} checks.', now)
    elif now >= ends_at:
        _finish(meta, 'failed', 'timed_out', f'Timed out after {_duration(now - meta.get("started_at", now))}.', now)
    else:
        next_check = min(now + int(meta.get('interval') or 60), ends_at)
        meta['next_check_at'] = int(next_check)
        return {'timer_at': int(min(next_check, now + KEEP_AWAKE) * 1_000_000_000)}
    # Nobody waits for the answer any more: it goes to the chat as a message instead.
    waiter_gone = (meta.get('waiter_until') or 0) < now
    meta['delivery'] = 'message' if meta.get('mode') == 'background' or waiter_gone else 'reply'
    return {'timer_at': None}


def _result_message(meta: dict) -> str:
    handle = meta.get('task_id')
    met = meta.get('outcome') == 'met'
    lines = [
        f'[MONITOR COMPLETE - {handle}]' if met else f'[MONITOR ENDED - {handle}]',
        'The condition a monitor you started was waiting for is true.'
        if met
        else 'A monitor you started stopped before its condition became true.',
        '',
        f'Waiting for: {meta.get("description")}',
        f'Condition: {describe_condition(meta.get("condition") or {})}',
        f'Status: {meta.get("outcome")}   Checks: {meta.get("checks") or 0}   Elapsed: {_duration(_elapsed(meta))}',
    ]
    if meta.get('error'):
        lines.append(f'Reason: {meta["error"]}')
    lines.extend(['--- LAST CHECK OUTPUT ---', (meta.get('last_check') or {}).get('output') or '(no output)'])
    return '\n'.join(lines)


async def _deliver(app, monitor_id: str, user_id: str, meta: dict) -> None:
    from open_webui.utils.subagents import post_task_result

    run = meta.get('run') or {}
    try:
        await post_task_result(
            app,
            parent_chat_id=meta.get('parent_chat_id') or '',
            user_id=user_id,
            content=_result_message(meta),
            meta={
                'type': 'task',
                'task_type': 'monitor',
                'task_id': monitor_id,
                'handle': meta.get('task_id'),
                'status': meta.get('status'),
            },
            run=run,
            parent_message_id=meta.get('parent_message_id'),
            model_id=run.get('model_id'),
        )
    except Exception:
        log.exception('Could not post the result of monitor %s', monitor_id)


async def _emit(user_id: str, monitor_id: str, meta: dict) -> None:
    await emit_task_changed(
        user_id,
        meta.get('parent_chat_id') or '',
        monitor_id,
        task_type='monitor',
        status=meta.get('status') or 'running',
        handle=meta.get('task_id'),
    )


async def run_monitor_check(app, monitor_id: str, claim_id: str) -> None:
    """Run one claimed check, record it, and finish the monitor when it's true or out of time."""
    try:
        row = await _get_row(monitor_id)
        if row is None:
            return
        meta, user_id = row
        if meta.get('status') != 'running' or meta.get('claim_id') != claim_id:
            return

        next_check_at = meta.get('next_check_at')
        if next_check_at and time.time() < next_check_at - 1:
            await _keep_awake(meta.get('context') or '', user_id)

            def wait_more(meta: dict) -> dict | None:
                if meta.get('status') != 'running' or meta.get('claim_id') != claim_id:
                    return None
                meta['claim_id'] = None
                wake = min(meta.get('next_check_at') or 0, time.time() + KEEP_AWAKE)
                return {'timer_at': int(wake * 1_000_000_000)}

            await _update(monitor_id, wait_more)
            return

        # Re-gate the owner: a demoted owner's monitors stop.
        user = await Users.get_user_by_id(user_id)
        if user is None or user.role not in VERIFIED_USER_ROLES:
            check = {**_check_error('Its owner may no longer run tasks.'), 'fatal': True}
        else:
            check = await _run_check(monitor_id, meta, user_id)

        saved = await _update(monitor_id, lambda meta: _record(meta, claim_id, check, time.time()))
        if saved is None:
            return
        await _emit(user_id, monitor_id, saved)
        if saved.get('status') == 'running':
            return
        event = _waiters.get(monitor_id)
        if event is not None:
            event.set()
        if saved.get('delivery') == 'message':
            await _deliver(app, monitor_id, user_id, saved)
    except Exception:
        log.exception('Monitor %s check failed', monitor_id)


# -- the tool --------------------------------------------------------------------------------------


def _reply(meta: dict) -> str:
    """What the waiting tool call returns for a finished monitor."""
    outcome = meta.get('outcome')
    # Stopped from the Tasks panel or with cancel_task: no outcome of its own.
    if not outcome:
        outcome = 'cancelled' if meta.get('status') == 'cancelled' else 'failed'
    reply = {
        'status': outcome,
        'task_id': meta.get('task_id'),
        'checks': meta.get('checks') or 0,
        'elapsed_seconds': _elapsed(meta),
        'output': (meta.get('last_check') or {}).get('output') or '',
    }
    if meta.get('error'):
        reply['error'] = meta['error']
    return JSONCodec.dumps(reply, ensure_ascii=False)


def _move_to_background(meta: dict) -> dict | None:
    if meta.get('status') != 'running':
        return None
    meta.update(mode='background', moved_to_background_at=int(time.time()), waiter_until=None)
    return {}


def _renew_waiter(meta: dict) -> dict | None:
    if meta.get('status') != 'running' or meta.get('mode') != 'foreground':
        return None
    meta['waiter_until'] = int(time.time()) + WAITER_GRACE
    return {}


def _stop_with_reply(meta: dict) -> dict | None:
    # One that just moved to the background keeps going.
    if meta.get('status') != 'running' or meta.get('mode') != 'foreground':
        return None
    _finish(meta, 'cancelled', 'cancelled', None, time.time())
    return {'timer_at': None}


async def _wait(monitor_id: str, user_id: str, meta: dict, auto_background: float) -> str:
    """Wait in the foreground until the monitor ends, or hand it to the background."""
    event = _waiters.setdefault(monitor_id, asyncio.Event())
    started = time.monotonic()
    renew_at = started + WAITER_RENEW
    try:
        await _emit(user_id, monitor_id, meta)
        while True:
            now = time.monotonic()
            wake = min(now + POLL_SECONDS, renew_at)
            if auto_background:
                wake = min(wake, started + auto_background)
            try:
                await asyncio.wait_for(event.wait(), max(0, wake - now))
            except asyncio.TimeoutError:
                pass

            row = await _get_row(monitor_id)
            if row is None:
                return 'Error: the monitor no longer exists.'
            meta = row[0]
            if meta.get('status') != 'running':
                return _reply(meta)

            now = time.monotonic()
            if auto_background and now - started >= auto_background:
                moved = await _update(monitor_id, _move_to_background)
                if moved is None:
                    # It ended meanwhile; the next round returns its result.
                    continue
                await _emit(user_id, monitor_id, moved)
                return JSONCodec.dumps(
                    {
                        'status': 'moved_to_background',
                        'task_id': moved.get('task_id'),
                        'checks': moved.get('checks') or 0,
                        'elapsed_seconds': _elapsed(moved),
                        'note': f'Not true yet after {_duration(_elapsed(moved))}, so it moved to the background. '
                        "You'll get a message here once when it's true or when it gives up. "
                        'task_status shows its checks; cancel_task stops it.',
                    },
                    ensure_ascii=False,
                )
            if now >= renew_at:
                await _update(monitor_id, _renew_waiter)
                renew_at = now + WAITER_RENEW
    except asyncio.CancelledError:
        # Stopping the reply stops its foreground monitor.
        stopped = await asyncio.shield(_update(monitor_id, _stop_with_reply))
        if stopped is not None:
            await asyncio.shield(_emit(user_id, monitor_id, stopped))
        raise
    finally:
        _waiters.pop(monitor_id, None)


async def start_monitor(
    *,
    description: str,
    command: str,
    url: str,
    expect_status,
    pattern: str,
    interval_seconds,
    timeout_minutes,
    max_checks,
    background: bool,
    user_data: dict,
    metadata: dict,
    parent_chat_id: str,
    parent_message_id: str | None,
) -> str:
    description = ' '.join(str(description or '').split())
    if not description:
        return 'Error: description must say what you are waiting for.'
    if len(description) > MAX_DESCRIPTION:
        return f'Error: description must be at most {MAX_DESCRIPTION} characters.'
    try:
        condition = parse_condition(command, url, expect_status, pattern)
    except ValueError as exc:
        return f'Error: {exc}'

    user_id = user_data.get('id')
    if not parent_chat_id or not user_id:
        return 'Error: chat and user context are required.'
    model_id = metadata.get('model_id') or (metadata.get('model') or {}).get('id')
    if not model_id:
        return 'Error: model context is required.'
    if metadata.get('direct'):
        return 'Error: monitors are unavailable for direct connections.'
    context = await containers.context_for(metadata)
    if metadata.get('terminal_id') != containers.CONNECTION_ID or not (context or '').startswith('chat:'):
        return "Error: monitors need this chat's own container as its terminal."
    background = background is True or str(background).strip().lower() in ('true', '1', 'yes')

    limits = await task_limits.get_all()
    settings = resolve_settings(limits, interval_seconds, timeout_minutes, max_checks)
    now = time.time()
    monitor_id = str(uuid4())
    handle = new_handle(HANDLE_PREFIXES['monitor'])
    meta = {
        'internal': True,
        'type': 'monitor',
        'parent_chat_id': parent_chat_id,
        'parent_message_id': parent_message_id,
        'task_id': handle,
        'title': description[:200],
        'description': description,
        'status': 'running',
        'mode': 'background' if background else 'foreground',
        'started_at': int(now),
        'finished_at': None,
        'error': None,
        'result': None,
        'result_preview': None,
        'task_cleared': False,
        'cancelled_by': None,
        'condition': condition,
        'interval': settings['interval'],
        'check_timeout': settings['check_timeout'],
        'max_checks': settings['max_checks'],
        'ends_at': int(now + settings['lifetime']),
        'checks': 0,
        'consecutive_errors': 0,
        'last_check': None,
        'history': [],
        'run': {
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
        },
        'context': context,
        'waiter_until': None if background else int(now) + WAITER_GRACE,
        'claim_id': None,
        'rev': 0,
    }

    async with _start_locks.setdefault(user_id, asyncio.Lock()):
        if await count_active(user_id) >= limits['monitors_per_user']:
            return (
                f'Error: {limits["monitors_per_user"]} monitors are already running for you. '
                'Wait for one to end, or stop one with cancel_task.'
            )
        # The first check runs at once.
        chat = await Chats.insert_new_chat(
            monitor_id,
            user_id,
            ChatForm(
                chat={
                    'id': monitor_id,
                    'title': f'Monitor: {description[:60]}',
                    'models': [model_id],
                    'history': {'currentId': None, 'messages': {}},
                    'messages': [],
                }
            ),
            internal_meta=meta,
            timer_at=time.time_ns(),
        )
    if not chat:
        return 'Error: failed to start the monitor.'
    if not background:
        return await _wait(monitor_id, user_id, meta, limits['auto_background_seconds'])

    await _emit(user_id, monitor_id, meta)
    return JSONCodec.dumps(
        {
            'status': 'started',
            'task_id': handle,
            'description': description,
            'condition': describe_condition(condition),
            'interval_seconds': settings['interval'],
            'max_checks': settings['max_checks'],
            'ends_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(meta['ends_at'])),
            'note': "You'll get a message here once when it's true or when it gives up. "
            'task_status shows its checks; cancel_task stops it.',
        },
        ensure_ascii=False,
    )
