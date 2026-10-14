"""Monitors: validation, the checks built for the container, scheduling, and who reports the result.

The check scripts run in a local shell (and curl against a local server) exactly as they would in a
chat container; the database is a throwaway SQLite file, and the container, the socket and the
result delivery are mocked.
"""

import asyncio
import contextlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from open_webui.internal import db as internal_db
from open_webui.models.chats import Chat
from open_webui.utils import monitors, task_limits
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool

LIMITS = {name: limit.default for name, limit in task_limits.LIMITS.items()}
NS = 1_000_000_000


def run_script(script: str) -> dict:
    """Run a check script like Open Terminal's run_command does, and answer like it."""
    env = {key: value for key, value in os.environ.items() if 'proxy' not in key.lower()}
    done = subprocess.run(['/bin/sh', '-c', script], capture_output=True, text=True, timeout=60, env=env)
    output = (done.stdout + done.stderr).replace('\n', '\r\n')
    return {'id': 'p1', 'status': 'done', 'exit_code': done.returncode, 'output': [{'type': 'output', 'data': output}]}


def check(condition: dict, check_timeout: int = 10) -> dict:
    nonce = 'n0nce'
    return monitors.evaluate(condition, run_script(monitors.build_check(condition, check_timeout, nonce)), nonce)


class ConditionTest(unittest.TestCase):
    def test_exactly_one_of_command_or_url(self):
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            monitors.parse_condition('true', 'http://x', 200, '')
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            monitors.parse_condition('  ', '', 200, '')
        self.assertEqual(monitors.parse_condition(' true ', '', 200, ''), {'kind': 'command', 'command': 'true'})

    def test_url_must_be_http_or_https_with_a_host(self):
        for url in ('ftp://example.com', 'file:///etc/passwd', 'http://', 'javascript:alert(1)', 'http://[::1'):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, 'http'):
                monitors.parse_condition('', url, 200, '')
        with self.assertRaisesRegex(ValueError, 'without spaces'):
            monitors.parse_condition('', 'http://exa mple.com', 200, '')
        self.assertEqual(
            monitors.parse_condition('', 'https://example.com/health', 204, 'ok'),
            {'kind': 'http', 'url': 'https://example.com/health', 'expect_status': 204, 'pattern': 'ok'},
        )

    def test_lengths_and_control_characters(self):
        with self.assertRaisesRegex(ValueError, 'command'):
            monitors.parse_condition('x' * (monitors.MAX_COMMAND + 1), '', 200, '')
        with self.assertRaisesRegex(ValueError, 'url'):
            monitors.parse_condition('', 'http://a.b/' + 'x' * monitors.MAX_URL, 200, '')
        with self.assertRaisesRegex(ValueError, 'pattern'):
            monitors.parse_condition('true', '', 200, 'x' * (monitors.MAX_PATTERN + 1))
        with self.assertRaisesRegex(ValueError, 'pattern'):
            monitors.parse_condition('true', '', 200, 'a\nb')
        # A command may span lines; only NUL is refused.
        self.assertEqual(monitors.parse_condition('cd /tmp\ntest -f x', '', 200, '')['command'], 'cd /tmp\ntest -f x')

    def test_expect_status_is_an_http_status(self):
        for status in (99, 600, 'abc', None):
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, 'expect_status'):
                monitors.parse_condition('', 'http://a.b', status, '')


class SettingsTest(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(
            monitors.resolve_settings(LIMITS, 0, 0, 0),
            {'interval': 60, 'lifetime': 21600, 'max_checks': 500, 'check_timeout': 30},
        )

    def test_requests_are_clamped_to_the_limits(self):
        settings = monitors.resolve_settings(LIMITS, 5, 100_000, 5000)
        self.assertEqual((settings['interval'], settings['lifetime'], settings['max_checks']), (60, 21600, 500))
        settings = monitors.resolve_settings(LIMITS, 90_000, 10, 3)
        self.assertEqual((settings['interval'], settings['lifetime'], settings['max_checks']), (3600, 600, 3))
        settings = monitors.resolve_settings({**LIMITS, 'monitor_interval_seconds': 15}, 20, 'soon', -4)
        self.assertEqual((settings['interval'], settings['lifetime'], settings['max_checks']), (20, 21600, 500))


class CheckScriptTest(unittest.TestCase):
    def test_command_exit_codes(self):
        self.assertTrue(check({'kind': 'command', 'command': 'true'})['met'])
        result = check({'kind': 'command', 'command': 'echo not yet; exit 3'})
        self.assertEqual((result['met'], result['exit_code'], result['error']), (False, 3, None))
        self.assertEqual(result['output'], 'not yet')
        self.assertIsNone(result['http_status'])

    def test_a_command_that_cannot_run_is_an_error(self):
        result = check({'kind': 'command', 'command': 'no-such-command-for-monitors'})
        self.assertFalse(result['met'])
        self.assertEqual(result['exit_code'], 127)
        self.assertIn('could not run', result['error'])

    def test_a_slow_command_is_cut_off_and_not_met(self):
        started = time.monotonic()
        result = check({'kind': 'command', 'command': 'sleep 20'}, check_timeout=1)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual((result['met'], result['exit_code'], result['error']), (False, 124, None))

    def test_the_pattern_is_matched_in_the_shell(self):
        condition = {'kind': 'command', 'command': 'echo "build: SUCCESS"; exit 1', 'pattern': 'SUCC?ESS$'}
        self.assertTrue(check(condition)['met'])
        self.assertFalse(check({**condition, 'pattern': '^FAILED'})['met'])

    def test_an_invalid_pattern_is_an_error(self):
        result = check({'kind': 'command', 'command': 'echo x', 'pattern': '('})
        self.assertFalse(result['met'])
        self.assertIn('regular expression', result['error'])

    def test_the_agents_text_stays_literal(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        marker = os.path.join(directory, 'pwned')
        result = check(
            {
                'kind': 'command',
                'command': 'printf "%s\\n" "it\'s $((1+1))"',
                'pattern': f"x'; touch {marker}; echo '",
            }
        )
        self.assertEqual(result['output'], "it's 2")
        self.assertFalse(result['met'])
        self.assertFalse(os.path.exists(marker))

    def test_output_is_capped_to_its_end(self):
        result = check({'kind': 'command', 'command': 'head -c 10000 /dev/zero | tr "\\000" a; echo; echo end'})
        self.assertLessEqual(len(result['output']), monitors.OUTPUT_LIMIT)
        self.assertGreater(len(result['output']), monitors.OUTPUT_LIMIT - 10)
        self.assertTrue(result['output'].endswith('a\nend'))


class _Handler(BaseHTTPRequestHandler):
    paths: list[str] = []

    def do_GET(self):
        type(self).paths.append(self.path)
        status, body = (200, b'ready') if self.path.startswith('/ok') else (503, b'starting')
        self.send_response(status)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@unittest.skipUnless(shutil.which('curl'), 'needs curl')
class UrlProbeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.server.server_address[1]}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def probe(self, path: str, expect_status: int = 200, pattern: str = '') -> dict:
        condition = monitors.parse_condition('', f'{self.base}{path}', expect_status, pattern)
        return check(condition, check_timeout=5)

    def test_status_and_pattern(self):
        result = self.probe('/ok')
        self.assertEqual((result['met'], result['http_status'], result['exit_code']), (True, 200, 0))
        self.assertEqual(result['output'], 'ready')
        self.assertTrue(self.probe('/ok', pattern='^rea')['met'])
        self.assertFalse(self.probe('/ok', pattern='^starting')['met'])
        down = self.probe('/down')
        self.assertEqual((down['met'], down['http_status'], down['error']), (False, 503, None))
        self.assertTrue(self.probe('/down', expect_status=503)['met'])

    def test_a_server_that_is_not_up_yet_is_not_met(self):
        with socket.socket() as unused:
            unused.bind(('127.0.0.1', 0))
            port = unused.getsockname()[1]
        condition = monitors.parse_condition('', f'http://127.0.0.1:{port}/', 200, '')
        result = check(condition, check_timeout=5)
        self.assertEqual((result['met'], result['http_status'], result['error']), (False, None, None))
        self.assertEqual(result['exit_code'], 7)
        self.assertIn('curl: (7)', result['output'])

    def test_the_url_is_quoted(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        marker = os.path.join(directory, 'pwned')
        _Handler.paths.clear()
        self.assertTrue(self.probe(f'/ok?a=1&b=$(touch%20{marker})')['met'])
        self.assertFalse(os.path.exists(marker))
        self.assertEqual(_Handler.paths, [f'/ok?a=1&b=$(touch%20{marker})'])


class EvaluateTest(unittest.TestCase):
    COMMAND = {'kind': 'command', 'command': 'true'}

    def test_errors_from_the_container(self):
        self.assertIn(
            'HTTP error 502', monitors.evaluate(self.COMMAND, {'error': 'HTTP error 502: down'}, 'n')['error']
        )
        self.assertIn('did not run', monitors.evaluate(self.COMMAND, 'oops', 'n')['error'])
        unfinished = {'status': 'running', 'output': [{'type': 'output', 'data': 'partial'}]}
        self.assertEqual(monitors.evaluate(self.COMMAND, unfinished, 'n')['error'], 'The check did not finish in time.')
        no_marker = {'status': 'done', 'output': [{'type': 'output', 'data': 'sh: mktemp: failed'}]}
        result = monitors.evaluate(self.COMMAND, no_marker, 'n')
        self.assertEqual(
            (result['error'], result['output']), ('The check ended without a result.', 'sh: mktemp: failed')
        )

    def test_only_the_last_marker_with_this_nonce_counts(self):
        data = 'x\r\n__MOLOGATE_CHECK_n__ 0 - -\r\nreal\r\n__MOLOGATE_CHECK_n__ 1 - -\r\n'
        result = monitors.evaluate(self.COMMAND, {'status': 'done', 'output': [{'type': 'output', 'data': data}]}, 'n')
        self.assertEqual((result['met'], result['exit_code']), (False, 1))
        self.assertEqual(result['output'], 'x\n__MOLOGATE_CHECK_n__ 0 - -\nreal')

    def test_http_status_parsing(self):
        condition = {'kind': 'http', 'url': 'http://a.b', 'expect_status': 200}

        def answer(code, http):
            data = f'\r\n__MOLOGATE_CHECK_n__ {code} {http} -\r\n'
            return monitors.evaluate(condition, {'status': 'done', 'output': [{'type': 'output', 'data': data}]}, 'n')

        self.assertEqual(answer(0, '200')['http_status'], 200)
        self.assertTrue(answer(0, '200')['met'])
        self.assertIsNone(answer(7, '000')['http_status'])
        self.assertFalse(answer(0, '301')['met'])


class DatabaseCase(unittest.IsolatedAsyncioTestCase):
    """A real SQLite database in a temporary file; everything outside it is mocked."""

    async def asyncSetUp(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        self.path = os.path.join(directory, 'monitors.db')
        engine = internal_db._create_async_engine(f'sqlite+aiosqlite:///{self.path}', poolclass=NullPool)
        async with engine.begin() as connection:
            await connection.run_sync(Chat.__table__.create)
        self.addAsyncCleanup(engine.dispose)
        sessions = async_sessionmaker(bind=engine, class_=AsyncSession, autoflush=False, expire_on_commit=False)

        self.limits = dict(LIMITS)
        self.emit = AsyncMock()
        self.post = AsyncMock(return_value='message-1')
        self.checks: list[dict] = []
        self.run_check = AsyncMock(side_effect=lambda *args: self.checks.pop(0))
        self.keep_awake = AsyncMock()
        self.user = SimpleNamespace(role='user')
        for patcher in (
            patch.object(internal_db, 'AsyncSessionLocal', sessions),
            patch.object(task_limits, 'get_all', AsyncMock(side_effect=lambda: dict(self.limits))),
            patch.object(monitors.containers, 'context_for', AsyncMock(return_value='chat:parent')),
            patch.object(monitors, 'emit_task_changed', self.emit),
            patch.object(monitors, '_run_check', self.run_check),
            patch.object(monitors, '_keep_awake', self.keep_awake),
            patch.object(monitors.Users, 'get_user_by_id', AsyncMock(side_effect=lambda user_id: self.user)),
            patch.object(monitors, 'POLL_SECONDS', 0.05),
            # A writes post_task_result in utils/subagents.py; the monitor only hands its result over.
            patch('open_webui.utils.subagents.post_task_result', self.post, create=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def start(self, background: bool = True, **fields) -> str:
        arguments = {
            'description': 'the build to finish',
            'command': 'test -f /home/user/done',
            'url': '',
            'expect_status': 200,
            'pattern': '',
            'interval_seconds': 0,
            'timeout_minutes': 0,
            'max_checks': 0,
            **fields,
        }
        return await monitors.start_monitor(
            **arguments,
            background=background,
            user_data={'id': 'u1'},
            metadata={'chat_id': 'parent', 'model_id': 'model', 'terminal_id': 'mologate', 'session_id': 's'},
            parent_chat_id='parent',
            parent_message_id='answer-1',
        )

    @contextlib.contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def rows(self) -> list[tuple[str, dict, int | None]]:
        with self.connect() as connection:
            return [
                (row[0], json.loads(row[1]), row[2])
                for row in connection.execute('SELECT id, meta, timer_at FROM chat')
            ]

    def row(self) -> tuple[str, dict, int | None]:
        rows = [row for row in self.rows() if row[1].get('type') == 'monitor']
        self.assertEqual(len(rows), 1)
        return rows[0]

    def meta_of(self, monitor_id: str) -> dict:
        return next(meta for row_id, meta, _ in self.rows() if row_id == monitor_id)

    def write(self, monitor_id: str, timer_at=..., **changes) -> None:
        """Another writer (another worker, or the Tasks panel) changes the row right now."""
        with self.connect() as connection:
            meta = json.loads(connection.execute('SELECT meta FROM chat WHERE id = ?', (monitor_id,)).fetchone()[0])
            meta.update(changes)
            if 'rev' not in changes:
                meta['rev'] = meta.get('rev', 0) + 1
            connection.execute('UPDATE chat SET meta = ? WHERE id = ?', (json.dumps(meta), monitor_id))
            if timer_at is not ...:
                connection.execute('UPDATE chat SET timer_at = ? WHERE id = ?', (timer_at, monitor_id))

    def due(self, monitor_id: str, **changes) -> None:
        """Skip the wait: the next check is due now."""
        self.write(monitor_id, timer_at=1, next_check_at=None, **changes)

    async def run_due(self, at: float | None = None) -> list[str]:
        """What the scheduler does: claim what is due, and run each check."""
        claimed = await monitors.claim_due_monitors(int((at or time.time()) * NS))
        for monitor_id, claim_id in claimed:
            await monitors.run_monitor_check(SimpleNamespace(), monitor_id, claim_id)
        return [monitor_id for monitor_id, _ in claimed]

    def result(self, met=False, error=None, output='') -> dict:
        return {'met': met, 'exit_code': 0 if met else 1, 'http_status': None, 'output': output, 'error': error}

    async def until(self, condition, timeout: float = 5) -> None:
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                self.fail('timed out')
            await asyncio.sleep(0.02)


class StartTest(DatabaseCase):
    async def test_bad_input_is_refused_before_anything_is_stored(self):
        self.assertEqual(await self.start(description='  '), 'Error: description must say what you are waiting for.')
        self.assertIn('exactly one', await self.start(url='http://a.b'))
        self.assertIn('at most 300', await self.start(description='x' * 301))
        self.assertIn('http://', await self.start(command='', url='ftp://a.b'))
        self.assertEqual(self.rows(), [])

    async def test_it_needs_the_chats_container(self):
        result = await monitors.start_monitor(
            description='x',
            command='true',
            url='',
            expect_status=200,
            pattern='',
            interval_seconds=0,
            timeout_minutes=0,
            max_checks=0,
            background=True,
            user_data={'id': 'u1'},
            metadata={'chat_id': 'parent', 'model_id': 'model', 'terminal_id': 'other-terminal'},
            parent_chat_id='parent',
            parent_message_id=None,
        )
        self.assertIn('container', result)
        self.assertEqual(self.rows(), [])

    async def test_a_running_row_due_at_once_and_never_pending(self):
        reply = json.loads(await self.start(interval_seconds=5, timeout_minutes=10, max_checks=9999))
        self.assertEqual(reply['status'], 'started')
        self.assertRegex(reply['task_id'], r'^mon_[0-9a-f]{8}$')
        self.assertEqual((reply['interval_seconds'], reply['max_checks']), (60, 500))

        monitor_id, meta, timer_at = self.row()
        self.assertEqual((meta['type'], meta['status'], meta['mode']), ('monitor', 'running', 'background'))
        self.assertTrue(meta['internal'])
        self.assertEqual((meta['parent_chat_id'], meta['parent_message_id']), ('parent', 'answer-1'))
        self.assertEqual(meta['task_id'], reply['task_id'])
        self.assertEqual(meta['condition'], {'kind': 'command', 'command': 'test -f /home/user/done'})
        self.assertEqual(meta['ends_at'] - meta['started_at'], 600)
        self.assertEqual((meta['context'], meta['run']['model_id']), ('chat:parent', 'model'))
        self.assertLessEqual(timer_at, time.time_ns())
        self.emit.assert_awaited_with(
            'u1', 'parent', monitor_id, task_type='monitor', status='running', handle=meta['task_id']
        )

        from open_webui.utils.timers import claim_due_timers

        self.assertEqual(await claim_due_timers(time.time_ns()), [])

    async def test_each_member_runs_a_limited_number(self):
        self.limits['monitors_per_user'] = 2
        await self.start()
        await self.start()
        self.assertIn('2 monitors are already running', await self.start())
        monitor_id = self.rows()[0][0]
        self.write(monitor_id, timer_at=None, status='completed')
        self.assertEqual(json.loads(await self.start())['status'], 'started')


class SchedulingTest(DatabaseCase):
    async def test_a_claim_takes_a_lease(self):
        await self.start()
        monitor_id = self.row()[0]
        now = time.time()
        claimed = await monitors.claim_due_monitors(int(now * NS))
        self.assertEqual([item[0] for item in claimed], [monitor_id])
        _, meta, timer_at = self.row()
        self.assertEqual(meta['claim_id'], claimed[0][1])
        self.assertEqual(timer_at, int(now * NS) + (30 + monitors.LEASE_MARGIN) * NS)

        self.assertEqual(await monitors.claim_due_monitors(int(now * NS)), [])
        # A worker that died mid-check: once the lease runs out, the check is claimed again.
        again = await monitors.claim_due_monitors(int((now + 61) * NS))
        self.assertEqual([item[0] for item in again], [monitor_id])
        self.assertNotEqual(again[0][1], claimed[0][1])

    async def test_only_running_monitors_are_claimed(self):
        await self.start()
        monitor_id = self.row()[0]
        with self.connect() as connection:
            connection.execute(
                'INSERT INTO chat (id, user_id, title, chat, meta, timer_at) VALUES (?, ?, ?, ?, ?, ?)',
                ('timer-1', 'u1', 'Timer', '{}', json.dumps({'type': 'timer', 'status': 'running'}), 1),
            )
        self.write(monitor_id, status='cancelled')
        self.assertEqual(await monitors.claim_due_monitors(time.time_ns()), [])

    async def test_a_check_schedules_the_next(self):
        await self.start()
        monitor_id = self.row()[0]
        now = time.time()
        claimed = await monitors.claim_due_monitors(int(now * NS))
        self.checks = [self.result(output='not there yet')]
        await monitors.run_monitor_check(SimpleNamespace(), monitor_id, claimed[0][1])

        _, meta, timer_at = self.row()
        self.assertEqual((meta['status'], meta['checks'], meta['claim_id']), ('running', 1, None))
        self.assertEqual(meta['last_check']['output'], 'not there yet')
        self.assertEqual(len(meta['history']), 1)
        self.assertAlmostEqual(timer_at / NS, now + 60, delta=2)
        self.assertEqual(self.emit.await_args.kwargs['status'], 'running')

        # A stale claim (the lease ran out and another worker took it) changes nothing.
        await monitors.run_monitor_check(SimpleNamespace(), monitor_id, claimed[0][1])
        self.assertEqual(self.row()[1]['checks'], 1)
        self.run_check.assert_awaited_once()

    async def test_checks_far_apart_keep_the_container_awake(self):
        await self.start(interval_seconds=300)
        monitor_id = self.row()[0]
        now = time.time()
        self.checks = [self.result()]
        await self.run_due(now)
        _, meta, timer_at = self.row()
        self.assertAlmostEqual(meta['next_check_at'], now + 300, delta=2)
        self.assertAlmostEqual(timer_at / NS, now + monitors.KEEP_AWAKE, delta=2)

        with patch.object(monitors.time, 'time', return_value=now + 61):
            self.assertEqual(await self.run_due(now + 61), [monitor_id])
        self.keep_awake.assert_awaited_once_with('chat:parent', 'u1')
        _, meta, timer_at = self.row()
        self.assertEqual((meta['checks'], meta['claim_id']), (1, None))
        self.assertAlmostEqual(timer_at / NS, now + 61 + monitors.KEEP_AWAKE, delta=2)
        self.run_check.assert_awaited_once()


class FinishTest(DatabaseCase):
    async def test_true_in_the_background_is_posted_once(self):
        await self.start()
        monitor_id, meta, _ = self.row()
        self.checks = [self.result(met=True, output='done')]
        await self.run_due()

        _, meta, timer_at = self.row()
        self.assertEqual((meta['status'], meta['outcome'], meta['delivery']), ('completed', 'met', 'message'))
        self.assertIsNone(timer_at)
        self.assertTrue(meta['result_preview'].startswith('True after 1 check'))
        self.post.assert_awaited_once()
        kwargs = self.post.await_args.kwargs
        self.assertEqual(
            kwargs['meta'],
            {
                'type': 'task',
                'task_type': 'monitor',
                'task_id': monitor_id,
                'handle': meta['task_id'],
                'status': 'completed',
            },
        )
        self.assertEqual((kwargs['parent_chat_id'], kwargs['user_id']), ('parent', 'u1'))
        self.assertEqual((kwargs['parent_message_id'], kwargs['model_id']), ('answer-1', 'model'))
        self.assertTrue(kwargs['content'].startswith(f'[MONITOR COMPLETE - {meta["task_id"]}]'))
        self.assertIn('--- LAST CHECK OUTPUT ---\ndone', kwargs['content'])
        self.assertEqual(self.emit.await_args.kwargs['status'], 'completed')

        self.assertEqual(await self.run_due(time.time() + 3600), [])
        self.post.assert_awaited_once()

    async def test_five_errors_in_a_row_fail_it(self):
        await self.start()
        monitor_id = self.row()[0]
        error = self.result(error='The container is unavailable: down')
        self.checks = [error] * 4 + [self.result()] + [error] * 5
        for _ in range(9):
            self.due(monitor_id)
            await self.run_due()
            self.assertEqual(self.row()[1]['status'], 'running')
        self.due(monitor_id)
        await self.run_due()

        _, meta, _ = self.row()
        self.assertEqual((meta['status'], meta['outcome'], meta['checks']), ('failed', 'failed', 10))
        self.assertIn('5 checks in a row could not run', meta['error'])
        self.assertTrue(self.post.await_args.kwargs['content'].startswith('[MONITOR ENDED - '))

    async def test_it_gives_up_when_out_of_checks(self):
        await self.start(max_checks=2)
        monitor_id = self.row()[0]
        self.checks = [self.result(), self.result()]
        await self.run_due()
        self.due(monitor_id)
        await self.run_due()
        meta = self.meta_of(monitor_id)
        self.assertEqual(
            (meta['status'], meta['outcome'], meta['error']), ('failed', 'timed_out', 'Gave up after 2 checks.')
        )
        self.assertTrue(self.post.await_args.kwargs['content'].startswith('[MONITOR ENDED - '))

    async def test_it_gives_up_when_out_of_time(self):
        await self.start(timeout_minutes=1, interval_seconds=120)
        monitor_id, meta, _ = self.row()
        # The last check runs when the time is up, not an interval later.
        self.checks = [self.result()]
        await self.run_due()
        self.assertEqual(self.meta_of(monitor_id)['next_check_at'], meta['ends_at'])

        self.due(monitor_id, ends_at=int(time.time()) - 1)
        self.checks = [self.result()]
        await self.run_due()
        meta = self.meta_of(monitor_id)
        self.assertEqual((meta['status'], meta['outcome']), ('failed', 'timed_out'))
        self.assertTrue(meta['error'].startswith('Timed out after'))

    async def test_a_demoted_owner_stops_it(self):
        await self.start()
        self.user = SimpleNamespace(role='pending')
        await self.run_due()
        meta = self.row()[1]
        self.assertEqual((meta['status'], meta['error']), ('failed', 'Its owner may no longer run tasks.'))
        self.run_check.assert_not_awaited()


class HandoffTest(DatabaseCase):
    async def test_the_waiting_call_gets_the_result_and_nothing_is_posted(self):
        task = asyncio.create_task(self.start(background=False))
        await self.until(lambda: self.rows())
        self.assertEqual(self.row()[1]['mode'], 'foreground')
        self.checks = [self.result(met=True, output='there')]
        await self.run_due()

        reply = json.loads(await asyncio.wait_for(task, 5))
        self.assertEqual((reply['status'], reply['checks'], reply['output']), ('met', 1, 'there'))
        self.assertRegex(reply['task_id'], r'^mon_')
        self.assertEqual(self.row()[1]['delivery'], 'reply')
        self.post.assert_not_awaited()

    async def test_moving_to_the_background_hands_the_result_to_a_message(self):
        self.limits['auto_background_seconds'] = 0.5
        task = asyncio.create_task(self.start(background=False))
        await self.until(lambda: self.rows())
        self.checks = [self.result()]
        await self.run_due()

        reply = json.loads(await asyncio.wait_for(task, 5))
        self.assertEqual((reply['status'], reply['checks']), ('moved_to_background', 1))
        meta = self.row()[1]
        self.assertEqual((meta['mode'], meta['waiter_until'], meta['status']), ('background', None, 'running'))
        self.assertIsNotNone(meta['moved_to_background_at'])

        self.checks = [self.result(met=True)]
        self.due(self.row()[0])
        await self.run_due()
        self.post.assert_awaited_once()
        self.assertEqual(self.row()[1]['delivery'], 'message')

    async def test_a_monitor_that_ended_is_not_moved(self):
        await self.start()
        monitor_id = self.row()[0]
        self.write(monitor_id, mode='foreground', waiter_until=int(time.time()) + 60)
        self.checks = [self.result(met=True)]
        await self.run_due()
        self.assertIsNone(await monitors._update(monitor_id, monitors._move_to_background))
        self.assertEqual(self.row()[1]['mode'], 'foreground')
        self.post.assert_not_awaited()

    async def test_a_move_while_the_result_is_written_wins_and_the_result_is_posted(self):
        """The move lands between the check's read and its write: the write retries, then posts."""
        await self.start()
        monitor_id = self.row()[0]
        self.write(monitor_id, mode='foreground', waiter_until=int(time.time()) + 60)
        claimed = await monitors.claim_due_monitors(time.time_ns())
        record = monitors._record
        calls = []

        def record_racing_a_move(meta, *args):
            calls.append(meta.get('mode'))
            if len(calls) == 1:
                self.write(monitor_id, mode='background', waiter_until=None)
            return record(meta, *args)

        self.checks = [self.result(met=True)]
        with patch.object(monitors, '_record', record_racing_a_move):
            await monitors.run_monitor_check(SimpleNamespace(), monitor_id, claimed[0][1])

        self.assertEqual(calls, ['foreground', 'background'])
        self.assertEqual(self.row()[1]['delivery'], 'message')
        self.post.assert_awaited_once()

    async def test_a_waiter_that_is_gone_means_the_result_is_posted(self):
        await self.start()
        monitor_id = self.row()[0]
        self.write(monitor_id, mode='foreground', waiter_until=int(time.time()) - 1)
        self.checks = [self.result(met=True)]
        await self.run_due()
        self.assertEqual(self.row()[1]['delivery'], 'message')
        self.post.assert_awaited_once()

    async def test_stopping_the_reply_cancels_its_monitor(self):
        task = asyncio.create_task(self.start(background=False))
        await self.until(lambda: self.rows())
        await asyncio.sleep(0.1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        _, meta, timer_at = self.row()
        self.assertEqual((meta['status'], meta['outcome']), ('cancelled', 'cancelled'))
        self.assertIsNone(timer_at)
        self.assertEqual(self.emit.await_args.kwargs['status'], 'cancelled')
        self.assertEqual(await self.run_due(), [])
        self.post.assert_not_awaited()

    async def test_a_stop_that_comes_after_the_move_leaves_it_running(self):
        await self.start()
        monitor_id = self.row()[0]
        self.assertIsNone(await monitors._update(monitor_id, monitors._stop_with_reply))
        self.assertEqual(self.row()[1]['status'], 'running')

    async def test_a_stop_from_the_panel_ends_the_wait_without_a_message(self):
        task = asyncio.create_task(self.start(background=False))
        await self.until(lambda: self.rows())
        monitor_id = self.row()[0]
        self.write(monitor_id, timer_at=None, status='cancelled', cancelled_by='user', rev=self.row()[1]['rev'])

        reply = json.loads(await asyncio.wait_for(task, 5))
        self.assertEqual(reply['status'], 'cancelled')
        self.post.assert_not_awaited()


class ContainerCheckTest(unittest.IsolatedAsyncioTestCase):
    """_run_check sends the script to the chat container's run_command, and reads its answer."""

    async def asyncSetUp(self):
        self.execute = AsyncMock(side_effect=self.fake_execute)
        for patcher in (
            patch.object(monitors.containers, 'containers_enabled', AsyncMock(return_value=True)),
            patch.object(
                monitors.containers,
                'route',
                AsyncMock(return_value=('http://node:8090/v1/sandboxes/chat:parent/t', {'Authorization': 'Bearer k'})),
            ),
            patch.object(monitors.containers, 'cached_tool_server_data', AsyncMock(return_value={'openapi': {}})),
            patch('open_webui.utils.tools.execute_tool_server', self.execute),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def fake_execute(self, **kwargs):
        return run_script(kwargs['params']['command']), {}

    def meta(self, command: str, pattern: str = '') -> dict:
        condition = monitors.parse_condition(command, '', 200, pattern)
        return {'condition': condition, 'check_timeout': 5, 'context': 'chat:parent'}

    async def test_run_command_in_the_chats_container(self):
        result = await monitors._run_check('m1', self.meta('echo hello', pattern='hel+o'), 'u1')
        self.assertEqual((result['met'], result['output']), (True, 'hello'))

        kwargs = self.execute.await_args.kwargs
        self.assertEqual(
            (kwargs['name'], kwargs['url']), ('run_command', 'http://node:8090/v1/sandboxes/chat:parent/t')
        )
        self.assertEqual(set(kwargs['params']), {'command', 'wait', 'tail'})
        self.assertEqual(kwargs['params']['wait'], 15)
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer k')
        self.assertEqual((kwargs['headers']['X-User-Id'], kwargs['headers']['X-Session-Id']), ('u1', 'monitor:m1'))
        self.assertRegex(kwargs['params']['command'], r"timeout -k 5 5 sh -c 'echo hello'")
        monitors.containers.route.assert_awaited_once_with('chat:parent', 'u1')

    async def test_an_unreachable_container_is_a_check_error(self):
        monitors.containers.route.side_effect = monitors.containers.NodeUnavailable('No container node answered.')
        result = await monitors._run_check('m1', self.meta('true'), 'u1')
        self.assertFalse(result['met'])
        self.assertIn('No container node answered', result['error'])

        monitors.containers.containers_enabled.return_value = False
        result = await monitors._run_check('m1', self.meta('true'), 'u1')
        self.assertIn('turned off', result['error'])

    async def test_never_outside_a_chat_container(self):
        result = await monitors._run_check('m1', {**self.meta('true'), 'context': 'automation:a1'}, 'u1')
        self.assertIn('only in a chat container', result['error'])
        self.execute.assert_not_awaited()


class GatingTest(unittest.IsolatedAsyncioTestCase):
    async def tools(self, *, internal=False, chat_id='c1', terminal_id='mologate', enabled=True) -> dict:
        from open_webui.utils import tools

        request = SimpleNamespace(state=SimpleNamespace(internal=internal))
        extra_params = {
            '__user__': {'id': 'u1', 'role': 'admin'},
            '__metadata__': {'chat_id': chat_id, 'terminal_id': terminal_id},
            '__chat_id__': chat_id,
        }
        with (
            patch.object(tools.Config, 'get_many', AsyncMock(return_value={'subagents.enable': True})),
            patch.object(tools.Config, 'get', AsyncMock(return_value=None)),
            patch.object(tools.containers, 'containers_enabled', AsyncMock(return_value=enabled)),
        ):
            return await tools.get_builtin_tools(request, extra_params)

    async def test_only_the_main_agent_of_a_saved_chat_with_its_container(self):
        self.assertIn('monitor', await self.tools())
        self.assertNotIn('monitor', await self.tools(internal=True))
        self.assertNotIn('monitor', await self.tools(chat_id='local:abc'))
        self.assertNotIn('monitor', await self.tools(terminal_id='my-terminal'))
        self.assertNotIn('monitor', await self.tools(enabled=False))


if __name__ == '__main__':
    unittest.main()
