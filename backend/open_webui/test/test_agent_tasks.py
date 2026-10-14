"""The chat's tasks: one shape for sub-agents, monitors and schedules, handles, stop and clear.

Queries run against a private in-memory SQLite (``MemoryDB``), never the configured database.
"""

import asyncio
import json
import time
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from open_webui import tasks as task_registry
from open_webui.models.chats import Chat
from open_webui.utils import agent_tasks
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

NOW = 1_800_000_000
NS = 1_000_000_000


class MemoryDB:
    """An in-memory SQLite with the chat table, patched in where the given modules open sessions."""

    def __init__(self, *modules):
        self.modules = modules

    async def start(self):
        self.engine = create_async_engine('sqlite+aiosqlite://', poolclass=StaticPool)
        async with self.engine.begin() as connection:
            await connection.run_sync(Chat.__table__.create)
        sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        @asynccontextmanager
        async def get_async_db():
            async with sessions() as session:
                yield session

        self.patches = [patch.object(module, 'get_async_db', get_async_db) for module in self.modules]
        for active in self.patches:
            active.start()

    async def stop(self):
        for active in self.patches:
            active.stop()
        await self.engine.dispose()

    async def add(
        self, id, meta, *, user_id='owner', title='', chat=None, timer_at=None, created_at=NOW, updated_at=NOW
    ):
        async with self.engine.begin() as connection:
            await connection.execute(
                insert(Chat).values(
                    id=id,
                    user_id=user_id,
                    title=title,
                    chat=chat or {},
                    meta=meta,
                    timer_at=timer_at,
                    created_at=created_at,
                    updated_at=updated_at,
                )
            )

    async def row(self, id):
        async with self.engine.begin() as connection:
            return (await connection.execute(select(Chat.meta, Chat.timer_at, Chat.chat).where(Chat.id == id))).first()


def subagent(**meta):
    return {
        'internal': True,
        'type': 'subagent',
        'parent_chat_id': 'main',
        'parent_message_id': 'answer-1',
        'delegation_id': 'deleg_aaaaaaaa',
        'task': 'Read the logs',
        **meta,
    }


def row(meta, **fields):
    return {
        'id': 'row-1',
        'user_id': 'owner',
        'title': '',
        'meta': meta,
        'created_at': NOW - 600,
        'updated_at': NOW - 100,
        'timer_at': None,
        **fields,
    }


class SubagentNormalizerTest(unittest.TestCase):
    def normalize(self, meta, *, active=False, **fields):
        return agent_tasks.normalize(row(meta, **fields), active=active, can_manage=True, now=NOW)

    def test_a_registered_run_is_running_and_can_be_stopped(self):
        task = self.normalize(subagent(status='running', started_at=NOW - 30), active=True)

        self.assertEqual(task['status'], 'running')
        self.assertEqual(task['handle'], 'deleg_aaaaaaaa')
        self.assertEqual(task['type'], 'subagent')
        self.assertEqual(task['title'], 'Read the logs')
        self.assertEqual(task['conversation_id'], 'row-1')
        self.assertEqual(task['message_id'], 'answer-1')
        self.assertIsNone(task['finished_at'])
        self.assertIsNone(task['duration'])
        self.assertTrue(task['can_stop'])

    def test_only_the_owner_or_an_admin_can_stop(self):
        task = agent_tasks.normalize(row(subagent(status='running', started_at=NOW)), active=True, now=NOW)

        self.assertFalse(task['can_stop'])

    def test_finished_runs_keep_their_status_preview_and_duration(self):
        task = self.normalize(
            subagent(
                status='completed',
                task_id='deleg_aaaaaaaa',
                title='Read the logs carefully',
                started_at=NOW - 300,
                finished_at=NOW - 167,
                result_preview='All quiet.',
                mode='background',
                profile='explore',
            )
        )

        self.assertEqual(task['status'], 'completed')
        self.assertEqual(task['title'], 'Read the logs carefully')
        self.assertEqual(task['duration'], 133)
        self.assertEqual(task['preview'], 'All quiet.')
        self.assertEqual(task['mode'], 'background')
        self.assertEqual(task['profile'], 'explore')
        self.assertFalse(task['can_stop'])

    def test_an_error_is_text(self):
        task = self.normalize(subagent(status='failed', error={'content': 'Model unavailable'}))

        self.assertEqual(task['status'], 'failed')
        self.assertEqual(task['error'], 'Model unavailable')

    def test_cancelled_stays_cancelled(self):
        self.assertEqual(self.normalize(subagent(status='cancelled', cancelled_by='user'))['status'], 'cancelled')

    def test_legacy_rows_count_as_completed_unless_they_have_an_error(self):
        legacy = self.normalize(subagent(), title='Sub-agent: Read the logs')
        self.assertEqual(legacy['status'], 'completed')
        self.assertEqual(legacy['started_at'], NOW - 600)
        self.assertEqual(legacy['finished_at'], NOW - 100)
        self.assertEqual(legacy['mode'], 'foreground')

        self.assertEqual(self.normalize(subagent(error='boom'))['status'], 'failed')

    def test_a_legacy_title_loses_its_prefix(self):
        meta = subagent()
        meta.pop('task')
        self.assertEqual(self.normalize(meta, title='Sub-agent: Read the logs')['title'], 'Read the logs')

    def test_running_without_a_task_is_stale_after_a_minute(self):
        stale = self.normalize(subagent(status='running', started_at=NOW - 120))
        self.assertEqual(stale['status'], 'failed')
        self.assertEqual(stale['error'], 'Stopped unexpectedly')

        starting = self.normalize(subagent(status='running', started_at=NOW - 10))
        self.assertEqual(starting['status'], 'running')


class MonitorNormalizerTest(unittest.TestCase):
    def test_a_running_monitor(self):
        meta = {
            'internal': True,
            'type': 'monitor',
            'parent_chat_id': 'main',
            'task_id': 'mon_1a2b3c4d',
            'description': 'Wait for the build',
            'status': 'running',
            'mode': 'background',
            'condition': {'kind': 'command', 'command': 'test -f /home/user/done'},
            'interval': 60,
            'checks': 3,
            'max_checks': 500,
            'ends_at': NOW + 3600,
            'started_at': NOW - 180,
        }
        task = agent_tasks.normalize(row(meta), can_manage=True, now=NOW)

        self.assertEqual(task['status'], 'running')
        self.assertEqual(task['handle'], 'mon_1a2b3c4d')
        self.assertEqual(task['title'], 'Wait for the build')
        self.assertEqual(
            task['monitor'],
            {
                'kind': 'command',
                'target': 'test -f /home/user/done',
                'interval': 60,
                'checks': 3,
                'max_checks': 500,
                'ends_at': NOW + 3600,
            },
        )
        self.assertTrue(task['can_stop'])
        self.assertIsNone(task['conversation_id'])

    def test_finished_monitors(self):
        base = {
            'internal': True,
            'type': 'monitor',
            'task_id': 'mon_x',
            'condition': {'kind': 'http', 'url': 'https://a'},
        }

        met = agent_tasks.normalize(row({**base, 'status': 'completed', 'finished_at': NOW, 'result': 'It is up.'}))
        self.assertEqual(met['status'], 'completed')
        self.assertEqual(met['preview'], 'It is up.')
        self.assertEqual(met['monitor']['target'], 'https://a')
        self.assertEqual(met['finished_at'], NOW)

        self.assertEqual(agent_tasks.normalize(row({**base, 'status': 'failed'}))['status'], 'failed')
        self.assertEqual(agent_tasks.normalize(row({**base, 'status': 'cancelled'}))['status'], 'cancelled')


class ScheduledNormalizerTest(unittest.TestCase):
    def legacy_timer(self, **meta):
        return {
            'internal': True,
            'type': 'timer',
            'parent_chat_id': 'main',
            'parent_message_id': 'answer-1',
            'timer_at': (NOW + 600) * NS,
            'status': 'pending',
            'timer_task_message_id': 'prompt',
            **meta,
        }

    def test_a_legacy_timer_waits_with_its_row_id_as_handle(self):
        task = agent_tasks.normalize(row(self.legacy_timer(), title='Timer: check the deploy'), can_manage=True)

        self.assertEqual(task['type'], 'scheduled')
        self.assertEqual(task['status'], 'scheduled')
        self.assertEqual(task['handle'], 'row-1')
        self.assertEqual(task['title'], 'check the deploy')
        self.assertIsNone(task['mode'])
        self.assertEqual(
            task['schedule'],
            {'next_run_at': NOW + 600, 'every': None, 'runs': 0, 'max_runs': 1, 'ends_at': None, 'failures': 0},
        )
        self.assertTrue(task['can_stop'])

    def test_legacy_timer_statuses(self):
        completed = agent_tasks.normalize(
            row(self.legacy_timer(status='completed', timer_started_at=NOW * NS, timer_completed_at=(NOW + 1) * NS))
        )
        self.assertEqual(completed['status'], 'completed')
        self.assertEqual(completed['schedule']['runs'], 1)
        self.assertIsNone(completed['schedule']['next_run_at'])
        self.assertEqual(completed['started_at'], NOW)
        self.assertEqual(completed['finished_at'], NOW + 1)

        error = agent_tasks.normalize(
            row(self.legacy_timer(status='error', timer_error='parent chat no longer exists'))
        )
        self.assertEqual(error['status'], 'failed')
        self.assertEqual(error['error'], 'parent chat no longer exists')

        self.assertEqual(agent_tasks.normalize(row(self.legacy_timer(status='running')))['status'], 'running')
        self.assertEqual(agent_tasks.normalize(row(self.legacy_timer(status='cancelled')))['status'], 'cancelled')

    def test_a_repeating_schedule(self):
        meta = self.legacy_timer(
            task_id='sched_1a2b3c4d',
            title='Daily report',
            schedule={'every': 3600, 'until': NOW + 86400, 'max_runs': None},
            runs=3,
            failures=1,
            next_run_at=NOW + 1200,
            last_run={'started_at': NOW - 2400, 'finished_at': NOW - 2300, 'status': 'completed'},
        )
        task = agent_tasks.normalize(row(meta, title='Timer: Daily report'))

        self.assertEqual(task['handle'], 'sched_1a2b3c4d')
        self.assertEqual(task['title'], 'Daily report')
        self.assertEqual(task['started_at'], NOW - 2400)
        self.assertEqual(
            task['schedule'],
            {
                'next_run_at': NOW + 1200,
                'every': 3600,
                'runs': 3,
                'max_runs': None,
                'ends_at': NOW + 86400,
                'failures': 1,
            },
        )

    def test_a_disabled_schedule_failed(self):
        task = agent_tasks.normalize(
            row(self.legacy_timer(status='disabled', schedule={'every': 3600}, failures=3, error='HTTP 500'))
        )

        self.assertEqual(task['status'], 'failed')
        self.assertEqual(task['error'], 'Turned off after 3 failed runs in a row')
        self.assertIsNone(task['schedule']['next_run_at'])


class OrderTest(unittest.TestCase):
    def test_active_first_newest_started_then_newest_finished(self):
        tasks = [
            {'id': 'old-done', 'status': 'completed', 'started_at': 1, 'finished_at': 10, 'created_at': 1},
            {'id': 'new-done', 'status': 'failed', 'started_at': 2, 'finished_at': 20, 'created_at': 2},
            {'id': 'old-run', 'status': 'running', 'started_at': 5, 'finished_at': None, 'created_at': 5},
            {'id': 'new-run', 'status': 'running', 'started_at': 30, 'finished_at': None, 'created_at': 30},
            {'id': 'waiting', 'status': 'scheduled', 'started_at': None, 'finished_at': None, 'created_at': 15},
        ]

        self.assertEqual(
            [task['id'] for task in agent_tasks._ordered(tasks)],
            ['new-run', 'waiting', 'old-run', 'new-done', 'old-done'],
        )

    def test_small_helpers(self):
        self.assertEqual(agent_tasks.describe_seconds(300), '5 min')
        self.assertEqual(agent_tasks.describe_seconds(7200), '2 h')
        self.assertEqual(agent_tasks.describe_seconds(45), '45 s')
        self.assertEqual(len(agent_tasks.preview('word ' * 200)), agent_tasks.PREVIEW_CHARS)
        self.assertEqual(agent_tasks.preview('a\n\n b'), 'a b')
        self.assertIsNone(agent_tasks.preview(''))


class DatabaseCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = MemoryDB(agent_tasks)
        await self.db.start()
        self.emit = AsyncMock()
        self.emit_patch = patch.object(agent_tasks, 'emit_task_changed', self.emit)
        self.emit_patch.start()

    async def asyncTearDown(self):
        self.emit_patch.stop()
        for task in list(task_registry.tasks.values()):
            task.cancel()
        task_registry.tasks.clear()
        task_registry.item_tasks.clear()
        task_registry.response_streams.clear()
        await self.db.stop()

    async def add_tasks(self):
        # Running sub-agents count as stale a minute after they started, so these use the real time.
        now = int(time.time())
        await self.db.add('main', {})
        await self.db.add('other-main', {})
        await self.db.add('sub-1', subagent(task_id='deleg_aaaaaaaa', status='completed', finished_at=NOW))
        await self.db.add(
            'sub-2',
            subagent(delegation_id='deleg_bbbbbbbb', task_id='deleg_bbbbbbbb', status='running', started_at=now),
            created_at=now,
        )
        await self.db.add(
            'mon-1',
            {
                'internal': True,
                'type': 'monitor',
                'parent_chat_id': 'main',
                'task_id': 'mon_cccccccc',
                'status': 'running',
                'condition': {'kind': 'command', 'command': 'true'},
            },
            timer_at=(now + 60) * NS,
            created_at=now - 100,
        )
        await self.db.add(
            'timer-1',
            {'internal': True, 'type': 'timer', 'parent_chat_id': 'main', 'status': 'pending', 'timer_at': NOW * NS},
            timer_at=NOW * NS,
            created_at=now - 200,
        )
        await self.db.add('note-1', {'internal': True, 'type': 'note', 'parent_chat_id': 'main'})
        await self.db.add('elsewhere', subagent(parent_chat_id='other-main', task_id='deleg_dddddddd'))
        await self.db.add('someone-else', subagent(task_id='deleg_eeeeeeee'), user_id='intruder')


class HandleTest(DatabaseCase):
    async def test_handles_resolve_within_the_main_chat_only(self):
        await self.add_tasks()

        self.assertEqual((await agent_tasks.resolve_handle('main', 'owner', 'deleg_aaaaaaaa'))['id'], 'sub-1')
        self.assertEqual((await agent_tasks.resolve_handle('main', 'owner', 'mon_cccccccc'))['id'], 'mon-1')
        self.assertEqual((await agent_tasks.resolve_handle('main', 'owner', 'timer-1'))['id'], 'timer-1')
        self.assertEqual((await agent_tasks.resolve_handle('main', 'owner', ' sub-2 '))['id'], 'sub-2')
        # Another chat's task, another member's, a row that isn't a task, and nothing at all.
        self.assertIsNone(await agent_tasks.resolve_handle('main', 'owner', 'deleg_dddddddd'))
        self.assertIsNone(await agent_tasks.resolve_handle('main', 'owner', 'deleg_eeeeeeee'))
        self.assertIsNone(await agent_tasks.resolve_handle('main', 'owner', 'note-1'))
        self.assertIsNone(await agent_tasks.resolve_handle('main', 'owner', ''))

    async def test_a_sub_agent_works_for_its_main_chat(self):
        await self.add_tasks()

        self.assertEqual(await agent_tasks.root_chat_id('sub-2'), 'main')
        self.assertEqual(await agent_tasks.root_chat_id('main'), 'main')

    async def test_listing_covers_every_type_active_first(self):
        await self.add_tasks()

        listing = await agent_tasks.list_tasks(None, 'main', 'owner', can_manage=True)

        self.assertEqual(
            [(task['id'], task['status']) for task in listing['tasks']],
            [('sub-2', 'running'), ('mon-1', 'running'), ('timer-1', 'scheduled'), ('sub-1', 'completed')],
        )
        self.assertEqual(listing['running'], 2)


class UpdateMetaTest(DatabaseCase):
    async def test_finishing_clears_the_schedule_column(self):
        await self.db.add('mon-1', {'type': 'monitor', 'status': 'running'}, timer_at=NOW * NS)

        result = await agent_tasks.update_task_meta('mon-1', lambda meta: meta.update(status='completed') or 'done')

        self.assertEqual(result, 'done')
        meta, timer_at, _ = await self.db.row('mon-1')
        self.assertEqual(meta['status'], 'completed')
        self.assertIsNone(timer_at)

    async def test_a_change_returning_false_saves_nothing(self):
        await self.db.add('mon-1', {'type': 'monitor', 'status': 'running'}, timer_at=NOW * NS)

        def change(meta):
            meta['status'] = 'cancelled'
            return False

        self.assertIs(await agent_tasks.update_task_meta('mon-1', change), False)
        meta, timer_at, _ = await self.db.row('mon-1')
        self.assertEqual(meta['status'], 'running')
        self.assertEqual(timer_at, NOW * NS)

    async def test_an_explicit_timer_at_and_a_missing_row(self):
        await self.db.add('timer-1', {'type': 'timer', 'status': 'running'})

        await agent_tasks.update_task_meta('timer-1', lambda meta: meta.update(status='pending'), timer_at=NOW * NS)

        self.assertEqual((await self.db.row('timer-1'))[1], NOW * NS)
        self.assertIsNone(await agent_tasks.update_task_meta('gone', lambda meta: True))


class StopTest(DatabaseCase):
    async def test_stopping_a_sub_agent_cancels_its_run_and_wakes_nobody(self):
        await self.add_tasks()
        run = asyncio.Event()
        _, child = await task_registry.create_task(None, run.wait(), id='sub-2')

        task, stopped = await agent_tasks.stop_task_row(None, 'main', 'owner', 'deleg_bbbbbbbb', cancelled_by='user')

        self.assertTrue(stopped)
        self.assertTrue(child.cancelled())
        self.assertEqual(task['status'], 'cancelled')
        self.assertFalse(task['can_stop'] and task['status'] in agent_tasks.ACTIVE)
        meta = (await self.db.row('sub-2'))[0]
        self.assertEqual((meta['status'], meta['cancelled_by']), ('cancelled', 'user'))
        self.assertIsInstance(meta['finished_at'], int)
        self.emit.assert_awaited_once_with(
            'owner', 'main', 'sub-2', task_type='subagent', status='cancelled', handle='deleg_bbbbbbbb'
        )

    async def test_stopping_is_idempotent(self):
        await self.add_tasks()

        task, stopped = await agent_tasks.stop_task_row(None, 'main', 'owner', 'deleg_aaaaaaaa', cancelled_by='user')

        self.assertFalse(stopped)
        self.assertEqual(task['status'], 'completed')
        self.emit.assert_not_awaited()
        self.assertEqual(
            await agent_tasks.stop_task_row(None, 'main', 'owner', 'nope', cancelled_by='user'), (None, False)
        )

    async def test_a_monitor_stops_being_scheduled(self):
        await self.add_tasks()

        task, stopped = await agent_tasks.stop_task_row(None, 'main', 'owner', 'mon_cccccccc', cancelled_by='user')

        self.assertTrue(stopped)
        self.assertEqual(task['status'], 'cancelled')
        meta, timer_at, _ = await self.db.row('mon-1')
        self.assertIsNone(timer_at)

    async def test_the_user_stops_a_scheduled_run_but_the_agent_only_its_next_runs(self):
        await self.db.add('main', {})
        for id in ('sched-user', 'sched-agent'):
            await self.db.add(
                id,
                {
                    'internal': True,
                    'type': 'timer',
                    'parent_chat_id': 'main',
                    'task_id': f'sched_{id}',
                    'status': 'running',
                    'run_task_ids': [f'turn-{id}'],
                },
                timer_at=NOW * NS,
            )
        stop_task = AsyncMock()

        with patch.object(agent_tasks, 'stop_task', stop_task):
            await agent_tasks.stop_task_row(None, 'main', 'owner', 'sched_sched-user', cancelled_by='user')
            await agent_tasks.stop_task_row(None, 'main', 'owner', 'sched_sched-agent', cancelled_by='agent')

        stop_task.assert_awaited_once_with(None, 'turn-sched-user')
        for id in ('sched-user', 'sched-agent'):
            meta, timer_at, _ = await self.db.row(id)
            self.assertEqual(meta['status'], 'cancelled')
            self.assertIsNone(timer_at)

    async def test_the_agent_cancels_with_its_own_tool(self):
        await self.add_tasks()

        cancelled = await agent_tasks.agent_cancel_task(None, 'main', 'owner', 'mon_cccccccc')
        again = await agent_tasks.agent_cancel_task(None, 'main', 'owner', 'mon_cccccccc')
        missing = await agent_tasks.agent_cancel_task(None, 'main', 'owner', 'mon_nope')

        self.assertEqual(cancelled, {'status': 'cancelled', 'task_id': 'mon_cccccccc', 'type': 'monitor'})
        self.assertEqual(again['status'], 'already_finished')
        self.assertEqual(again['task_status'], 'cancelled')
        self.assertIn('error', missing)
        self.assertEqual((await self.db.row('mon-1'))[0]['cancelled_by'], 'agent')


class ClearTest(DatabaseCase):
    async def test_clear_hides_finished_tasks_only(self):
        await self.add_tasks()
        await self.db.add('sub-3', subagent(task_id='deleg_ffffffff', status='cancelled'))

        cleared = await agent_tasks.clear_tasks(None, 'main', 'owner')

        self.assertEqual(cleared, 2)
        self.assertTrue((await self.db.row('sub-1'))[0]['task_cleared'])
        listing = await agent_tasks.list_tasks(None, 'main', 'owner')
        self.assertEqual({task['id'] for task in listing['tasks']}, {'sub-2', 'mon-1', 'timer-1'})
        # A cleared task can still be looked up by its handle.
        self.assertEqual((await agent_tasks.resolve_handle('main', 'owner', 'deleg_aaaaaaaa'))['id'], 'sub-1')
        self.emit.assert_awaited_once()
        self.assertEqual(await agent_tasks.clear_tasks(None, 'main', 'owner'), 0)


class DetailTest(DatabaseCase):
    async def test_a_running_sub_agent_shows_its_live_output(self):
        await self.add_tasks()
        history = {
            'messages': {
                'q': {'id': 'q', 'role': 'user', 'parentId': None, 'content': 'Read the logs', 'timestamp': 1},
                'a': {'id': 'a', 'role': 'assistant', 'parentId': 'q', 'content': '', 'done': False, 'timestamp': 2},
            }
        }
        stream_id = 'stream-1'
        await task_registry.create_task(None, asyncio.Event().wait(), id='sub-2', task_id=stream_id)
        await task_registry.save_response_stream(
            None,
            stream_id,
            'sub-2',
            'a',
            'Reading app.log',
            [{'type': 'function_call', 'name': 'run_command', 'status': 'in_progress'}],
        )

        with patch.object(
            agent_tasks.Chats, 'get_chat_by_id', AsyncMock(return_value=SimpleNamespace(chat={'history': history}))
        ):
            detail = await agent_tasks.get_task(None, 'main', 'owner', 'sub-2', can_manage=True)

        self.assertEqual(detail['status'], 'running')
        self.assertEqual(detail['prompt'], 'Read the logs')
        self.assertEqual(detail['output'], 'Reading app.log')
        self.assertTrue(detail['live'])
        self.assertEqual(detail['activity'], [{'name': 'run_command', 'status': 'in_progress'}])
        self.assertEqual((detail['runs'], detail['checks']), ([], []))

    async def test_a_finished_sub_agent_shows_its_answer(self):
        await self.add_tasks()
        history = {
            'messages': {
                'q': {'id': 'q', 'role': 'user', 'parentId': None, 'content': 'Read the logs', 'timestamp': 1},
                'a': {
                    'id': 'a',
                    'role': 'assistant',
                    'parentId': 'q',
                    'content': 'All quiet.',
                    'output': [{'type': 'function_call', 'name': 'view_file', 'status': 'completed'}],
                    'timestamp': 2,
                },
            }
        }

        with patch.object(
            agent_tasks.Chats, 'get_chat_by_id', AsyncMock(return_value=SimpleNamespace(chat={'history': history}))
        ):
            detail = await agent_tasks.get_task(None, 'main', 'owner', 'deleg_aaaaaaaa')

        self.assertEqual((detail['output'], detail['live']), ('All quiet.', False))
        self.assertEqual(detail['activity'], [{'name': 'view_file', 'status': 'completed'}])

    async def test_a_scheduled_task_shows_its_prompt_and_last_answer(self):
        await self.add_tasks()
        main_history = {
            'messages': {
                'p': {
                    'id': 'p',
                    'role': 'user',
                    'meta': {'internal': True, 'type': 'timer', 'timer_id': 'timer-1'},
                    'childrenIds': ['r'],
                    'timestamp': 5,
                },
                'r': {'id': 'r', 'role': 'assistant', 'parentId': 'p', 'content': 'Deploy is green.', 'timestamp': 6},
            }
        }
        await self.db.add(
            'timer-2',
            {
                'internal': True,
                'type': 'timer',
                'parent_chat_id': 'main',
                'status': 'completed',
                'timer_task_message_id': 'prompt',
            },
        )

        with (
            patch.object(
                agent_tasks.Chats,
                'get_chat_by_id',
                AsyncMock(return_value=SimpleNamespace(chat={'history': main_history})),
            ),
            patch.object(
                agent_tasks.Chats,
                'get_message_by_id_and_message_id',
                AsyncMock(return_value={'content': 'Check the deploy'}),
            ),
        ):
            missing = await agent_tasks.get_task(None, 'main', 'owner', 'timer-2')
            main_history['messages']['p']['meta']['timer_id'] = 'timer-2'
            detail = await agent_tasks.get_task(None, 'main', 'owner', 'timer-2')

        self.assertIsNone(missing['output'])
        self.assertEqual(detail['prompt'], 'Check the deploy')
        self.assertEqual(detail['output'], 'Deploy is green.')
        self.assertEqual([run['message_id'] for run in detail['runs']], ['r'])

    async def test_a_monitor_shows_its_checks(self):
        await self.db.add(
            'mon-1',
            {
                'internal': True,
                'type': 'monitor',
                'parent_chat_id': 'main',
                'task_id': 'mon_x',
                'status': 'running',
                'description': 'Wait for the file',
                'last_check': {'at': NOW, 'met': False, 'exit_code': 1, 'output': 'No such file'},
                'history': [{'at': NOW, 'met': False, 'exit_code': 1, 'http_status': None}],
            },
        )

        detail = await agent_tasks.get_task(None, 'main', 'owner', 'mon_x')

        self.assertEqual(detail['prompt'], 'Wait for the file')
        self.assertEqual(detail['output'], 'No such file')
        self.assertEqual(detail['checks'], [{'at': NOW, 'met': False, 'exit_code': 1, 'http_status': None}])
        self.assertIsNone(await agent_tasks.get_task(None, 'other-main', 'owner', 'mon_x'))


class AgentStatusTest(DatabaseCase):
    async def test_the_list_for_the_agent(self):
        await self.add_tasks()

        report = await agent_tasks.agent_task_status(None, 'main', 'owner')

        self.assertEqual(report['running'], 2)
        by_id = {task['task_id']: task for task in report['tasks']}
        self.assertEqual(set(by_id), {'deleg_bbbbbbbb', 'mon_cccccccc', 'timer-1', 'deleg_aaaaaaaa'})
        self.assertIn('elapsed_seconds', by_id['deleg_bbbbbbbb'])
        self.assertTrue(by_id['timer-1']['next_run_at'].endswith('Z'))
        json.dumps(report)

    async def test_waiting_for_a_task_ends_when_it_finishes(self):
        await self.add_tasks()

        async def finish_soon():
            await asyncio.sleep(0.2)
            await agent_tasks.update_task_meta('mon-1', lambda meta: meta.update(status='completed', result='Met.'))

        finisher = asyncio.create_task(finish_soon())
        started = time.monotonic()
        report = await agent_tasks.agent_task_status(None, 'main', 'owner', 'mon_cccccccc', wait_seconds=30)
        await finisher

        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['result'], 'Met.')
        self.assertFalse(report['can_cancel'])

    async def test_a_sub_agent_sees_the_main_chats_tasks_but_cannot_cancel(self):
        await self.add_tasks()

        report = await agent_tasks.agent_task_status(None, 'sub-2', 'owner', 'mon_cccccccc')
        from_main = await agent_tasks.agent_task_status(None, 'main', 'owner', 'mon_cccccccc')

        self.assertEqual(report['status'], 'running')
        self.assertFalse(report['can_cancel'])
        self.assertTrue(from_main['can_cancel'])
        self.assertIn('error', await agent_tasks.agent_task_status(None, 'main', 'owner', 'deleg_dddddddd'))


class RouterTest(DatabaseCase):
    """The REST endpoints: paths, who may do what, and the response shapes."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        from fastapi import FastAPI
        from open_webui.internal.db import get_async_session
        from open_webui.routers import chats as routes

        await self.add_tasks()
        self.user = SimpleNamespace(id='owner', role='user')
        app = FastAPI()
        app.state.redis = None
        app.include_router(routes.router, prefix='/api/v1/chats')
        app.dependency_overrides[routes.get_verified_user] = lambda: self.user
        app.dependency_overrides[get_async_session] = lambda: None

        async def get_chat_by_id_for_user(id, user, db=None):
            return SimpleNamespace(id=id, user_id='owner') if id in ('main', 'other-main') else None

        self.access_patch = patch.object(routes.Chats, 'get_chat_by_id_for_user', get_chat_by_id_for_user)
        self.access_patch.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')

    async def asyncTearDown(self):
        await self.client.aclose()
        self.access_patch.stop()
        await super().asyncTearDown()

    async def test_the_list_and_one_task(self):
        listing = (await self.client.get('/api/v1/chats/main/tasks')).json()
        self.assertEqual(listing['running'], 2)
        self.assertEqual(len(listing['tasks']), 4)
        self.assertTrue(all(task['can_stop'] for task in listing['tasks'] if task['status'] == 'running'))

        with patch.object(agent_tasks.Chats, 'get_chat_by_id', AsyncMock(return_value=None)):
            detail = await self.client.get('/api/v1/chats/main/tasks/sub-1')
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()['handle'], 'deleg_aaaaaaaa')
        self.assertEqual((await self.client.get('/api/v1/chats/other-main/tasks/sub-1')).status_code, 404)
        self.assertEqual((await self.client.get('/api/v1/chats/unknown/tasks')).status_code, 401)

    async def test_stop_and_clear(self):
        stopped = await self.client.post('/api/v1/chats/main/tasks/mon-1/stop')
        self.assertEqual(stopped.status_code, 200)
        self.assertEqual(stopped.json()['task']['status'], 'cancelled')
        again = await self.client.post('/api/v1/chats/main/tasks/mon-1/stop')
        self.assertEqual(again.json()['task']['status'], 'cancelled')
        self.assertEqual((await self.client.post('/api/v1/chats/main/tasks/nope/stop')).status_code, 404)

        self.assertEqual((await self.client.post('/api/v1/chats/main/tasks/clear')).json(), {'cleared': 2})

    async def test_readers_who_dont_own_the_chat_cannot_stop_or_clear(self):
        self.user = SimpleNamespace(id='reader', role='user')

        listing = (await self.client.get('/api/v1/chats/main/tasks')).json()
        self.assertFalse(any(task['can_stop'] for task in listing['tasks']))
        self.assertEqual((await self.client.post('/api/v1/chats/main/tasks/mon-1/stop')).status_code, 403)
        self.assertEqual((await self.client.post('/api/v1/chats/main/tasks/clear')).status_code, 403)

        self.user = SimpleNamespace(id='admin', role='admin')
        self.assertEqual((await self.client.post('/api/v1/chats/main/tasks/mon-1/stop')).status_code, 200)


if __name__ == '__main__':
    unittest.main()
