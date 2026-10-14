"""Scheduled tasks (schedule_task) and the limits automations share with them."""

import asyncio
import json
import time
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.pool import StaticPool

from open_webui import tasks as task_registry
from open_webui.internal.db import _create_async_engine
from open_webui.models import automations as automation_models
from open_webui.models.automations import Automation, AutomationRunModel, Automations
from open_webui.models.chats import Chat
from open_webui.routers import automations as automation_routes
from open_webui.utils import agent_tasks, automations, task_limits, timers

NS = 1_000_000_000
HOUR = 3600
LIMITS = {name: limit.default for name, limit in task_limits.LIMITS.items()}


def limit_of(name):
    return LIMITS[name]


def repeating(due_at: int, **changes) -> dict:
    meta = {
        'internal': True,
        'type': 'timer',
        'parent_chat_id': 'main',
        'task_id': 'sched_1a2b3c4d',
        'title': 'Check the build',
        'status': 'running',
        'timer_at': due_at,
        'timer_claim_id': 'claim',
        'schedule': {'every': HOUR, 'until': None, 'max_runs': None},
        'runs': 1,
        'failures': 0,
        'skipped': 0,
        'run_history': [],
        'run': {'model_id': 'gpt'},
        'timer_task_message_id': 'prompt',
    }
    meta.update(changes)
    return meta


def run(status: str, error: str | None = None, message_id: str | None = 'answer') -> dict:
    return timers._run_entry(int(time.time()), status, message_id, error)


class ParseTest(unittest.TestCase):
    def test_every(self):
        self.assertEqual(timers.parse_every('2h'), 2 * HOUR)
        self.assertEqual(timers.parse_every('every 1d'), 24 * HOUR)
        self.assertEqual(timers.parse_every('90 minutes'), 5400)
        self.assertEqual(timers.parse_every('1w'), 7 * 24 * HOUR)
        self.assertEqual(timers.parse_every('Daily'), 24 * HOUR)
        for value in ('soon', '0h', '1 month', ''):
            with self.assertRaises(ValueError, msg=value):
                timers.parse_every(value)

    def test_span(self):
        self.assertEqual(timers.format_span(HOUR), '1h')
        self.assertEqual(timers.format_span(5400), '90m')
        self.assertEqual(timers.format_span(7 * 24 * HOUR), '7d')
        self.assertEqual(timers.format_span(45), '45s')

    def test_until_errors_name_until(self):
        with self.assertRaisesRegex(ValueError, '^until must be in the future'):
            timers.parse_timer_at('2001-01-01T00:00:00Z', 'until')
        self.assertGreater(timers.parse_timer_at('2w'), time.time_ns() + 13 * 24 * HOUR * NS)

    def test_next_occurrence_skips_missed_runs(self):
        due = 1_000 * NS
        step = HOUR * NS
        self.assertEqual(timers.next_occurrence(due, HOUR, due + step // 2), due + step)
        self.assertEqual(timers.next_occurrence(due, HOUR, due + 2 * step + step // 3), due + 3 * step)
        # Exactly on an occurrence: that one is now, so the next is the one after.
        self.assertEqual(timers.next_occurrence(due, HOUR, due + 2 * step), due + 3 * step)
        self.assertEqual(timers.next_occurrence(due, HOUR, due - step), due + step)
        self.assertIsNone(timers.next_occurrence(due, None, due))


class CloseRunTest(unittest.TestCase):
    def test_a_finished_run_reschedules_to_the_first_occurrence_after_now(self):
        now = time.time_ns()
        meta = repeating(now - 5 * HOUR * NS - HOUR * NS // 2)

        timer_at, notify = timers._close_run(meta, run('completed'), now, 3)

        self.assertEqual(timer_at, meta['timer_at'])
        self.assertGreater(timer_at, now)
        self.assertLessEqual(timer_at - now, HOUR * NS)
        self.assertEqual(meta['status'], 'pending')
        self.assertEqual(meta['next_run_at'], timer_at // NS)
        self.assertIsNone(notify)

    def test_failures_in_a_row_turn_the_schedule_off_with_a_toast(self):
        now = time.time_ns()
        meta = repeating(now)
        for _ in range(2):
            timer_at, notify = timers._close_run(meta, run('failed', 'boom'), now, 3)
            self.assertEqual(meta['status'], 'pending')
            self.assertIsNotNone(timer_at)
            meta['status'] = 'running'

        timer_at, notify = timers._close_run(meta, run('failed', 'boom'), now, 3)

        self.assertIsNone(timer_at)
        self.assertEqual(meta['status'], 'disabled')
        self.assertEqual(meta['error'], 'Turned off after 3 failed runs in a row')
        self.assertEqual(notify['title'], 'Scheduled task turned off: Check the build')
        self.assertIn('boom', notify['body'])

    def test_a_good_run_resets_the_failures(self):
        now = time.time_ns()
        meta = repeating(now, failures=2)
        timers._close_run(meta, run('completed'), now, 3)
        self.assertEqual(meta['failures'], 0)

    def test_skipped_and_stopped_runs_do_not_count_as_failures(self):
        now = time.time_ns()
        meta = repeating(now, failures=2)
        timers._close_run(meta, run('skipped', 'The chat was busy', None), now, 3)
        meta['status'] = 'running'
        timers._close_run(meta, run('stopped'), now, 3)
        self.assertEqual(meta['failures'], 2)
        self.assertEqual(meta['skipped'], 1)
        self.assertEqual(meta['status'], 'pending')

    def test_max_runs_and_until_end_the_schedule(self):
        now = time.time_ns()
        meta = repeating(now, runs=3, schedule={'every': HOUR, 'until': None, 'max_runs': 3})
        self.assertEqual(timers._close_run(meta, run('completed'), now, 3), (None, None))
        self.assertEqual(meta['status'], 'completed')

        meta = repeating(now, schedule={'every': HOUR, 'until': now // NS + 60, 'max_runs': None})
        timers._close_run(meta, run('completed'), now, 3)
        self.assertEqual(meta['status'], 'completed')

    def test_a_failed_one_off_fails(self):
        now = time.time_ns()
        meta = repeating(now, schedule={'every': None, 'until': None, 'max_runs': 1})
        timers._close_run(meta, run('failed', 'boom'), now, 3)
        self.assertEqual((meta['status'], meta['error']), ('error', 'boom'))

    def test_legacy_timers_complete_after_their_run(self):
        now = time.time_ns()
        meta = {'type': 'timer', 'status': 'running', 'timer_at': now}
        self.assertEqual(timers._close_run(meta, run('completed'), now, 3), (None, None))
        self.assertEqual(meta['status'], 'completed')

    def test_a_cancelled_schedule_only_records_the_run(self):
        now = time.time_ns()
        meta = repeating(now, status='cancelled', run_history=[run('running')])
        self.assertEqual(timers._close_run(meta, run('stopped'), now, 3), (None, None))
        self.assertEqual(meta['status'], 'cancelled')
        self.assertEqual([entry['status'] for entry in meta['run_history']], ['stopped'])
        self.assertEqual(meta['last_run']['status'], 'stopped')

    def test_history_keeps_the_last_twenty(self):
        now = time.time_ns()
        meta = repeating(now, run_history=[run('completed') for _ in range(20)])
        timers._close_run(meta, run('failed', 'boom'), now, 3)
        self.assertEqual(len(meta['run_history']), 20)
        self.assertEqual(meta['run_history'][-1]['status'], 'failed')

    def test_postpone_until_it_would_run_into_the_next_occurrence(self):
        now = time.time_ns()
        meta = repeating(now)
        retry_at = timers._postpone(meta, now, 15, 'busy')
        self.assertEqual(retry_at, now + 15 * NS)
        self.assertEqual((meta['status'], meta['postponed_reason']), ('pending', 'busy'))
        self.assertEqual(meta['timer_at'], now, 'the occurrence itself stays put')

        late = repeating(now - HOUR * NS + 10 * NS)
        self.assertIsNone(timers._postpone(late, now, 15, 'busy'))
        one_off = repeating(now - 10 * HOUR * NS, schedule={'every': None, 'until': None, 'max_runs': 1})
        self.assertIsNotNone(timers._postpone(one_off, now, 15, 'busy'))


class DatabaseCase(unittest.IsolatedAsyncioTestCase):
    """An in-memory database for the timer queries; the app's own database is never touched."""

    async def asyncSetUp(self):
        self.engine = _create_async_engine(
            'sqlite+aiosqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool
        )
        async with self.engine.begin() as connection:
            await connection.run_sync(Chat.__table__.create)
            await connection.run_sync(Automation.__table__.create)
        sessions = async_sessionmaker(bind=self.engine, class_=AsyncSession, expire_on_commit=False)

        @asynccontextmanager
        async def database(db=None):
            async with sessions() as session:
                yield session

        for target in (
            patch.object(timers, 'get_async_db', database),
            patch.object(automation_models, 'get_async_db_context', database),
            patch.object(timers, '_emit', AsyncMock()),
            patch.object(task_limits, 'get', AsyncMock(side_effect=limit_of)),
            patch.object(task_limits, 'get_all', AsyncMock(return_value=dict(LIMITS))),
        ):
            target.start()
            self.addCleanup(target.stop)
        self.sessions = sessions

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def add_row(self, row_id: str, meta: dict, timer_at: int | None, user_id: str = 'u1', chat=None):
        async with self.sessions() as session:
            session.add(
                Chat(
                    id=row_id,
                    user_id=user_id,
                    title=row_id,
                    chat=chat or {},
                    meta=meta,
                    timer_at=timer_at,
                    created_at=0,
                    updated_at=0,
                )
            )
            await session.commit()

    async def row(self, row_id: str) -> Chat:
        async with self.sessions() as session:
            return (await session.execute(select(Chat).where(Chat.id == row_id))).scalar_one()


class CountTest(DatabaseCase):
    async def test_waiting_and_running_schedules_count_with_active_automations(self):
        now = time.time_ns()
        await self.add_row('pending', repeating(now, status='pending'), now)
        await self.add_row('running', repeating(now), now + HOUR * NS)
        await self.add_row('done', repeating(now, status='completed'), None)
        await self.add_row('monitor', {'type': 'monitor', 'status': 'running'}, now)
        await self.add_row('other', repeating(now, status='pending'), now, user_id='u2')
        async with self.sessions() as session:
            for automation_id, active, next_run_at in (('a1', True, now), ('a2', True, None), ('a3', False, now)):
                session.add(
                    Automation(
                        id=automation_id,
                        user_id='u1',
                        name=automation_id,
                        data={},
                        is_active=active,
                        next_run_at=next_run_at,
                        created_at=0,
                        updated_at=0,
                    )
                )
            await session.commit()

        self.assertEqual(await Automations.count_active_by_user('u1'), 1)
        self.assertEqual(await timers.count_active_schedules('u1'), 3)

    async def test_deactivate_turns_an_automation_off_once(self):
        async with self.sessions() as session:
            session.add(
                Automation(
                    id='a1', user_id='u1', name='a1', data={}, is_active=True, next_run_at=1, created_at=0, updated_at=0
                )
            )
            await session.commit()

        turned_off = await Automations.deactivate('a1')

        self.assertFalse(turned_off.is_active)
        self.assertIsNone(turned_off.next_run_at)
        self.assertIsNone(await Automations.deactivate('a1'))


class ClaimTest(DatabaseCase):
    async def test_due_timers_are_claimed_with_a_lease(self):
        now = time.time_ns()
        await self.add_row('due', repeating(now, status='pending', timer_claim_id=None), now - NS)
        await self.add_row('later', repeating(now, status='pending'), now + HOUR * NS)

        claimed = await timers.claim_due_timers(now)

        self.assertEqual([timer_id for timer_id, _ in claimed], ['due'])
        row = await self.row('due')
        self.assertEqual(row.meta['status'], 'running')
        self.assertEqual(row.meta['timer_claim_id'], claimed[0][1])
        self.assertEqual(row.timer_at, now + timers.CLAIM_LEASE_SECONDS * NS)

    async def test_stale_running_timers_are_recovered_but_monitors_are_left_alone(self):
        now = time.time_ns()
        due = now - 2 * HOUR * NS
        stale = repeating(due, last_run=run('running'), run_history=[run('running')])
        await self.add_row('stale', stale, now - NS)
        monitor = {'internal': True, 'type': 'monitor', 'status': 'running', 'checks': 3}
        await self.add_row('monitor', monitor, now - NS)

        claimed = await timers.claim_due_timers(now)

        self.assertEqual(claimed, [])
        row = await self.row('stale')
        self.assertEqual(row.meta['status'], 'pending')
        self.assertEqual(row.meta['last_run']['status'], 'failed')
        self.assertEqual(row.meta['last_run']['error'], 'Stopped unexpectedly')
        self.assertEqual(row.meta['last_run']['message_id'], 'answer')
        self.assertEqual(row.meta['failures'], 1)
        self.assertEqual(row.timer_at, due + 3 * HOUR * NS)
        self.assertEqual(len(row.meta['run_history']), 1)
        untouched = await self.row('monitor')
        self.assertEqual((untouched.meta, untouched.timer_at), (monitor, now - NS))
        timers._emit.assert_awaited_once()

    async def test_cancel_on_events_cancel_waiting_schedules(self):
        now = time.time_ns()
        await self.add_row('watch', repeating(now, status='pending', cancel_on=['chat.read']), now + NS)
        await self.add_row('keep', repeating(now, status='pending', cancel_on=['chat.user_message']), now + NS)

        await timers.cancel_timers_for_chat('main', 'chat.read', 'u1')

        cancelled = await self.row('watch')
        self.assertEqual((cancelled.meta['status'], cancelled.timer_at), ('cancelled', None))
        self.assertEqual((await self.row('keep')).meta['status'], 'pending')
        timers._emit.assert_awaited_once()


class PutOffTest(DatabaseCase):
    async def test_a_busy_chat_postpones_and_then_skips_the_run(self):
        now = time.time_ns()
        await self.add_row('soon', repeating(now), now + HOUR * NS)
        await timers._put_off('soon', 'u1', timers.BUSY_RETRY_SECONDS, 'busy', 'The chat was busy')

        row = await self.row('soon')
        self.assertEqual(row.meta['status'], 'pending')
        self.assertEqual(row.meta['postponed_reason'], 'busy')
        self.assertAlmostEqual(row.timer_at / NS, now / NS + 15, delta=2)

        due = now - HOUR * NS + 5 * NS
        await self.add_row('late', repeating(due), now + HOUR * NS)
        await timers._put_off('late', 'u1', timers.BUSY_RETRY_SECONDS, 'busy', 'The chat was busy')

        row = await self.row('late')
        self.assertEqual(row.meta['status'], 'pending')
        self.assertEqual(row.meta['last_run']['status'], 'skipped')
        self.assertEqual(row.meta['last_run']['error'], 'The chat was busy')
        self.assertEqual(row.meta['skipped'], 1)
        self.assertEqual(row.meta['failures'], 0)
        self.assertEqual(row.timer_at, due + HOUR * NS)

    async def test_a_cancelled_row_is_not_postponed(self):
        now = time.time_ns()
        await self.add_row('stopped', repeating(now, status='cancelled'), None)
        await timers._put_off('stopped', 'u1', 15, 'busy', 'The chat was busy')
        row = await self.row('stopped')
        self.assertEqual((row.meta['status'], row.timer_at), ('cancelled', None))


class StartRunTest(DatabaseCase):
    def app(self):
        return SimpleNamespace(state=SimpleNamespace(redis=None, MODELS={}, CHAT_COMPLETION_HANDLER=AsyncMock()))

    def start_patches(self, meta: dict, *, busy: bool = False, left: float | None = None):
        timer = SimpleNamespace(id='sched', user_id='u1', meta=meta)
        for target in (
            patch.object(timers.Chats, 'get_chat_by_id', AsyncMock(return_value=timer)),
            patch.object(timers.Chats, 'get_chat_by_id_and_user_id', AsyncMock(return_value=SimpleNamespace())),
            patch.object(timers.Chats, 'get_message_by_id_and_message_id', AsyncMock(return_value={'content': 'go'})),
            patch.object(timers.Users, 'get_user_by_id', AsyncMock(return_value=SimpleNamespace(role='user'))),
            patch.object(timers, 'has_active_tasks', AsyncMock(return_value=busy)),
            patch.object(timers, '_limit_left_percent', AsyncMock(return_value=left)),
        ):
            target.start()
            self.addCleanup(target.stop)

    async def test_a_run_starts_a_turn_and_is_recorded_when_it_ends(self):
        now = time.time_ns()
        meta = repeating(now)
        await self.add_row('sched', meta, now + NS)
        answer = {'id': 'a0', 'role': 'assistant', 'done': True, 'timestamp': 1, 'childrenIds': [], 'content': 'hi'}
        await self.add_row('main', {}, None, chat={'history': {'currentId': 'a0', 'messages': {'a0': answer}}})
        self.start_patches(meta)
        app = self.app()
        seen = {}

        async def start_turn(request, form_data, user):
            seen['form'] = form_data
            seen['row'] = await self.row('sched')
            return {'status': True, 'task_ids': ['t1'], 'chat_id': 'main'}

        async def watching(redis, task_ids, run_seconds):
            seen['run_task_ids'] = (await self.row('sched')).meta['run_task_ids']
            return 'done'

        app.state.CHAT_COMPLETION_HANDLER.side_effect = start_turn
        with (
            patch.object(timers.ChatMessages, 'upsert_message', AsyncMock()),
            patch('open_webui.socket.main.sio.emit', AsyncMock()),
            patch.object(timers, 'watch_run', AsyncMock(side_effect=watching)) as watch,
        ):
            await timers.execute_due_timer(app, 'sched', 'claim')

        form, during = seen['form'], seen['row']
        self.assertEqual(
            form['user_message']['meta'],
            {
                'internal': True,
                'type': 'timer',
                'timer_id': 'sched',
                'task_id': 'sched_1a2b3c4d',
                'run_number': 2,
                'title': 'Check the build',
            },
        )
        self.assertEqual(form['user_message']['parentId'], 'a0')
        self.assertEqual((during.meta['status'], during.meta['runs']), ('running', 2))
        self.assertEqual(during.meta['last_run']['status'], 'running')
        self.assertEqual(during.meta['last_run']['message_id'], form['id'])
        lease = now / NS + LIMITS['schedule_run_seconds'] + timers.LEASE_MARGIN_SECONDS
        self.assertAlmostEqual(during.timer_at / NS, lease, delta=5)
        watch.assert_awaited_once_with(None, ['t1'], LIMITS['schedule_run_seconds'])
        self.assertEqual(seen['run_task_ids'], ['t1'])

        row = await self.row('sched')
        self.assertEqual(row.meta['status'], 'pending')
        self.assertEqual(row.timer_at, now + HOUR * NS)
        self.assertEqual([entry['status'] for entry in row.meta['run_history']], ['completed'])
        self.assertEqual(row.meta['run_task_ids'], [])
        parent = await self.row('main')
        self.assertEqual(parent.chat['history']['currentId'], form['id'])

    async def test_a_stop_while_the_turn_starts_still_stops_it(self):
        now = time.time_ns()
        meta = repeating(now)
        await self.add_row('sched', meta, now + NS)
        await self.add_row('main', {}, None, chat={'history': {'messages': {}}})
        self.start_patches(meta)
        app = self.app()

        async def stopped_meanwhile(request, form_data, user):
            # What the Tasks panel's Stop does, before the run's task ids are saved.
            async with self.sessions() as session:
                row = await session.get(Chat, 'sched')
                row.meta = {**row.meta, 'status': 'cancelled', 'cancelled_by': 'user'}
                row.timer_at = None
                await session.commit()
            return {'status': True, 'task_ids': ['t1']}

        app.state.CHAT_COMPLETION_HANDLER.side_effect = stopped_meanwhile
        with (
            patch.object(timers.ChatMessages, 'upsert_message', AsyncMock()),
            patch('open_webui.socket.main.sio.emit', AsyncMock()),
            patch.object(timers, 'stop_task', AsyncMock()) as stop_task,
            patch.object(timers, 'watch_run', AsyncMock(return_value='cancelled')),
        ):
            await timers.execute_due_timer(app, 'sched', 'claim')

        stop_task.assert_awaited_once_with(None, 't1')
        row = await self.row('sched')
        self.assertEqual((row.meta['status'], row.timer_at), ('cancelled', None))
        self.assertEqual(row.meta['last_run']['status'], 'stopped')

    async def test_a_busy_chat_puts_the_run_off(self):
        now = time.time_ns()
        meta = repeating(now)
        await self.add_row('sched', meta, now + NS)
        self.start_patches(meta, busy=True)
        app = self.app()

        await timers.execute_due_timer(app, 'sched', 'claim')

        app.state.CHAT_COMPLETION_HANDLER.assert_not_awaited()
        row = await self.row('sched')
        self.assertEqual((row.meta['status'], row.meta['postponed_reason']), ('pending', 'busy'))

    async def test_a_model_low_on_its_limit_puts_the_run_off_without_a_failure(self):
        now = time.time_ns()
        meta = repeating(now, failures=2)
        await self.add_row('sched', meta, now + NS)
        self.start_patches(meta, left=4.0)
        app = self.app()

        await timers.execute_due_timer(app, 'sched', 'claim')

        app.state.CHAT_COMPLETION_HANDLER.assert_not_awaited()
        row = await self.row('sched')
        self.assertEqual((row.meta['status'], row.meta['postponed_reason']), ('pending', 'low_limit'))
        self.assertAlmostEqual(row.timer_at / NS, now / NS + timers.LOW_LIMIT_RETRY_SECONDS, delta=2)
        self.assertEqual(row.meta['failures'], 2)

    async def test_low_limit_check_is_off_at_zero(self):
        now = time.time_ns()
        meta = repeating(now)
        await self.add_row('sched', meta, now + NS)
        self.start_patches(meta, busy=True, left=1.0)
        task_limits.get_all.return_value = {**LIMITS, 'schedule_low_limit_percent': 0}

        await timers.execute_due_timer(self.app(), 'sched', 'claim')

        timers._limit_left_percent.assert_not_awaited()
        self.assertEqual((await self.row('sched')).meta['postponed_reason'], 'busy')


class FinishRunTest(DatabaseCase):
    async def finish(self, outcome: str, answer: dict, meta: dict):
        await self.add_row('sched', meta, time.time_ns() + HOUR * NS)
        with (
            patch.object(timers, 'watch_run', AsyncMock(return_value=outcome)),
            patch.object(timers.Chats, 'get_message_by_id_and_message_id', AsyncMock(return_value=answer)),
        ):
            await timers._finish_run(
                SimpleNamespace(state=SimpleNamespace(redis=None)),
                'sched',
                user_id='u1',
                parent_chat_id='main',
                message_id='answer',
                started_at=int(time.time()),
                run_seconds=1800,
                task_ids=['t1'],
                error=None,
            )
        return await self.row('sched')

    async def test_a_run_stopped_at_the_limit_is_a_failure(self):
        meta = repeating(time.time_ns(), last_run=run('running'), run_history=[run('running')])
        row = await self.finish('timed_out', {'content': 'half'}, meta)
        self.assertEqual(row.meta['last_run']['status'], 'failed')
        self.assertEqual(row.meta['last_run']['error'], 'Stopped at the 30m run-time limit')
        self.assertEqual((row.meta['failures'], row.meta['status']), (1, 'pending'))
        self.assertEqual(row.meta['run_task_ids'], [])

    async def test_an_answer_with_an_error_is_a_failure_and_the_third_turns_it_off(self):
        meta = repeating(time.time_ns(), failures=2, last_run=run('running'), run_history=[run('running')])
        row = await self.finish('done', {'content': '', 'error': {'content': 'quota exceeded'}}, meta)
        self.assertEqual(row.meta['status'], 'disabled')
        self.assertIsNone(row.timer_at)
        self.assertEqual(row.meta['last_run']['error'], 'quota exceeded')
        notify = timers._emit.await_args.args[3]
        self.assertIn('Turned off after 3 failed runs in a row', notify['body'])

    async def test_a_good_run_keeps_its_answer_preview(self):
        meta = repeating(time.time_ns(), last_run=run('running'), run_history=[run('running')])
        row = await self.finish('done', {'content': 'All green.'}, meta)
        self.assertEqual(row.meta['last_run']['status'], 'completed')
        self.assertEqual(row.meta['result_preview'], 'All green.')

    async def test_a_stop_keeps_the_schedule_cancelled(self):
        meta = repeating(time.time_ns(), status='cancelled', last_run=run('running'), run_history=[run('running')])
        row = await self.finish('cancelled', {'content': ''}, meta)
        self.assertEqual(row.meta['status'], 'cancelled')
        self.assertEqual(row.meta['last_run']['status'], 'stopped')

    async def test_a_run_already_recovered_is_not_recorded_twice(self):
        meta = repeating(time.time_ns(), status='pending', last_run=run('failed', 'Stopped unexpectedly'))
        row = await self.finish('done', {'content': 'late'}, meta)
        self.assertEqual(row.meta['last_run']['error'], 'Stopped unexpectedly')
        timers._emit.assert_not_awaited()


class CreateScheduleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.insert = AsyncMock(return_value=SimpleNamespace(id='row'))
        for target in (
            patch.object(task_limits, 'get_all', AsyncMock(return_value=dict(LIMITS))),
            patch.object(timers, 'count_active_schedules', AsyncMock(return_value=0)),
            patch.object(timers.Chats, 'insert_new_chat', self.insert),
            patch.object(timers, '_emit', AsyncMock()),
        ):
            target.start()
            self.addCleanup(target.stop)

    async def create(self, chat_id: str = 'main', **arguments) -> str:
        return await timers.create_schedule(
            prompt=arguments.pop('prompt', 'Check the build'),
            request=None,
            user_data={'id': 'u1', 'name': 'U', 'email': 'u@example.com', 'role': 'user'},
            metadata={'model_id': 'gpt'},
            parent_chat_id=chat_id,
            parent_message_id='m1',
            **arguments,
        )

    async def test_a_repeating_task(self):
        now = time.time()
        result = json.loads(await self.create(every='2h', max_runs=5, name='Build watch'))

        self.assertEqual(result['status'], 'scheduled')
        self.assertTrue(result['task_id'].startswith('sched_'))
        self.assertEqual((result['every'], result['max_runs'], result['name']), ('2h', 5, 'Build watch'))
        meta = self.insert.await_args.kwargs['internal_meta']
        self.assertEqual(meta['schedule']['every'], 2 * HOUR)
        self.assertAlmostEqual(meta['next_run_at'], now + 2 * HOUR, delta=5)
        self.assertAlmostEqual(meta['schedule']['until'], now + LIMITS['schedule_lifetime_seconds'], delta=5)
        self.assertEqual(self.insert.await_args.kwargs['timer_at'], meta['timer_at'])
        self.assertEqual((meta['status'], meta['task_id'], meta['runs']), ('pending', result['task_id'], 0))
        timers._emit.assert_awaited_once()

    async def test_a_one_off(self):
        result = json.loads(await self.create(at='10m'))
        self.assertEqual((result['every'], result['max_runs'], result['ends_at']), (None, 1, None))

    async def test_bounds(self):
        cases = {
            'every must be at least 1h': {'every': '30m'},
            'at must be within 30d': {'at': '40d'},
            'until must be after the first run': {'at': '3h', 'every': '1h', 'until': '2h'},
            'at is required for a one-off': {},
            'max_runs must be at least 1': {'at': '1h', 'max_runs': 3},
            'until must be in the future': {'every': '1h', 'until': '2001-01-01T00:00:00Z'},
        }
        for message, arguments in cases.items():
            self.assertIn(message, await self.create(**arguments), arguments)
        self.insert.assert_not_awaited()

    async def test_until_is_clamped_to_the_lifetime(self):
        now = time.time()
        await self.create(every='1d', until='200d')
        until = self.insert.await_args.kwargs['internal_meta']['schedule']['until']
        self.assertAlmostEqual(until, now + LIMITS['schedule_lifetime_seconds'], delta=5)

    async def test_the_admin_minimum_is_the_floor(self):
        task_limits.get_all.return_value = {**LIMITS, 'schedule_interval_seconds': 900}
        self.assertEqual(json.loads(await self.create(every='15m'))['every'], '15m')

    async def test_only_saved_chats(self):
        self.assertIn('saved chat', await self.create(chat_id='local:abc', at='1h'))
        self.assertIn('saved chat', await self.create(chat_id='channel:abc', at='1h'))

    async def test_the_shared_per_member_cap(self):
        timers.count_active_schedules.return_value = LIMITS['schedules_per_user']
        result = await self.create(every='1h')
        self.assertEqual(
            result.split(' Cancel')[0],
            'Error: you already have the most scheduled tasks and automations allowed at once (10).',
        )
        self.insert.assert_not_awaited()


class WatchRunTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_run_time_limit_stops_the_turn(self):
        task_id, task = await task_registry.create_task(None, asyncio.sleep(60), id='main')

        async def stop(redis, stopped_id):
            task_registry.tasks[stopped_id].cancel()

        with patch.object(timers, 'stop_task', AsyncMock(side_effect=stop)) as stop_task:
            self.assertEqual(await timers.watch_run(None, [task_id], 0.05), 'timed_out')

        stop_task.assert_awaited_once_with(None, task_id)
        self.assertTrue(task.cancelled())

    async def test_a_finished_or_stopped_turn(self):
        task_id, _ = await task_registry.create_task(None, asyncio.sleep(0), id='main')
        self.assertEqual(await timers.watch_run(None, [task_id], 5), 'done')

        task_id, task = await task_registry.create_task(None, asyncio.sleep(60), id='main')
        asyncio.get_running_loop().call_later(0.01, task.cancel)
        self.assertEqual(await timers.watch_run(None, [task_id], 5), 'cancelled')
        self.assertEqual(await timers.watch_run(None, ['gone'], 5), 'done')


class LowLimitOutlookTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_least_window_left_across_the_model_accounts(self):
        app = SimpleNamespace(
            state=SimpleNamespace(redis=None, MODELS={'mine': {'info': {'base_model_id': 'account:openai/gpt'}}})
        )
        account = SimpleNamespace(id='acc', capacity_weight=1)
        with (
            patch('open_webui.utils.provider_accounts.select_accounts', AsyncMock(return_value=[account])),
            patch(
                'open_webui.utils.provider_limits.get_provider_limits',
                AsyncMock(return_value={'windows': [{'id': 'primary', 'used_percent': 95}]}),
            ),
            patch('open_webui.utils.provider_limits.account_rates', AsyncMock(return_value={})) as rates,
        ):
            left = await timers._limit_left_percent(app, 'mine', SimpleNamespace(id='u1'))

        self.assertAlmostEqual(left, 5.0)
        rates.assert_awaited_once_with(None, 'acc')
        self.assertIsNone(await timers._limit_left_percent(app, 'ollama-model', SimpleNamespace(id='u1')))


class AutomationLimitsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        for target in (
            patch.object(task_limits, 'get', AsyncMock(side_effect=limit_of)),
            patch.object(automation_routes, 'count_active_schedules', AsyncMock(return_value=0)),
            patch.object(automation_routes, 'next_n_runs_ns', AsyncMock(return_value=[0, HOUR * NS, 2 * HOUR * NS])),
            patch.object(automation_routes.Config, 'get', AsyncMock(return_value=None)),
        ):
            target.start()
            self.addCleanup(target.stop)
        self.admin = SimpleNamespace(id='admin', role='admin')

    async def test_the_interval_floor_applies_to_admins(self):
        automation_routes.next_n_runs_ns.return_value = [0, HOUR * NS, HOUR * NS + 1800 * NS]
        with self.assertRaises(HTTPException) as caught:
            await automation_routes.check_automation_limits(None, self.admin, 'RRULE:FREQ=MINUTELY', None)
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn('3600 seconds', caught.exception.detail)

    async def test_the_shared_count_applies_to_admins_when_one_is_turned_on(self):
        automation_routes.count_active_schedules.return_value = LIMITS['schedules_per_user']
        with self.assertRaises(HTTPException) as caught:
            await automation_routes.check_automation_limits(None, self.admin, 'RRULE:FREQ=DAILY', None, is_create=True)
        self.assertEqual(caught.exception.status_code, 403)

        # Saving one that is already on, or creating one paused, doesn't take another slot.
        await automation_routes.check_automation_limits(None, self.admin, 'RRULE:FREQ=DAILY', None)
        await automation_routes.check_automation_limits(
            None, self.admin, 'RRULE:FREQ=DAILY', None, is_create=True, activating=False
        )

    async def test_an_hourly_schedule_is_fine(self):
        await automation_routes.check_automation_limits(None, self.admin, 'RRULE:FREQ=HOURLY', None, is_create=True)
        automation_routes.count_active_schedules.assert_awaited_once_with('admin')


def automation(**changes):
    values = {
        'id': 'auto',
        'user_id': 'u1',
        'name': 'Digest',
        'data': {'prompt': 'Summarize', 'model_id': 'gpt', 'rrule': 'RRULE:FREQ=DAILY'},
        'is_active': True,
        'created_at': 0,
        'updated_at': 0,
    }
    return automation_models.AutomationModel(**{**values, **changes})


def automation_run(status: str, chat_id: str | None = 'c0', run_id: str = 'r0') -> AutomationRunModel:
    return AutomationRunModel(id=run_id, automation_id='auto', chat_id=chat_id, status=status, created_at=0)


class ExecuteAutomationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = SimpleNamespace(state=SimpleNamespace(redis=None, MODELS={}, CHAT_COMPLETION_HANDLER=AsyncMock()))
        self.record = AsyncMock(return_value='run1')
        for target in (
            patch.object(automations, '_record_run', self.record),
            patch.object(automations, 'publish_event', AsyncMock()),
            patch.object(task_limits, 'get', AsyncMock(side_effect=limit_of)),
        ):
            target.start()
            self.addCleanup(target.stop)

    async def test_a_run_due_while_the_previous_is_going_is_skipped(self):
        with (
            patch.object(automations.AutomationRuns, 'get_latest', AsyncMock(return_value=automation_run('running'))),
            patch('open_webui.tasks.has_active_tasks', AsyncMock(return_value=True)),
            patch.object(automations.Users, 'get_user_by_id', AsyncMock()) as get_user,
        ):
            await automations.execute_automation(self.app, automation())

        self.record.assert_awaited_once_with('auto', 'skipped', chat_id='c0', error='The previous run was still going')
        get_user.assert_not_awaited()

    async def test_the_run_time_limit_stops_a_run_and_fails_it(self):
        self.app.state.CHAT_COMPLETION_HANDLER.return_value = {'status': True, 'task_ids': ['t1']}
        with (
            patch.object(automations.AutomationRuns, 'get_latest', AsyncMock(return_value=None)),
            patch.object(
                automations.Users,
                'get_user_by_id',
                AsyncMock(return_value=SimpleNamespace(id='u1', role='admin', name='U')),
            ),
            patch.object(automations, 'prompt_template', AsyncMock(return_value='Summarize')),
            patch.object(automations.Config, 'get', AsyncMock(return_value='1h')),
            patch.object(automations, '_resolve_model_defaults', AsyncMock(return_value={})),
            patch.object(automations.Chats, 'insert_new_chat', AsyncMock(return_value=SimpleNamespace(id='c1'))),
            patch.object(automations.Chats, 'get_message_by_id_and_message_id', AsyncMock(return_value={})),
            patch('open_webui.socket.main.sio.emit', AsyncMock()),
            patch.object(timers, 'watch_run', AsyncMock(return_value='timed_out')) as watch,
        ):
            await automations.execute_automation(self.app, automation())

        watch.assert_awaited_once_with(None, ['t1'], LIMITS['schedule_run_seconds'])
        self.assertEqual(self.record.await_args_list[0].args, ('auto', 'running'))
        self.record.assert_awaited_with(
            'auto',
            'error',
            chat_id='c1',
            error='Stopped at the 30m run-time limit',
            run_id='run1',
            app=self.app,
        )


class TurnOffAutomationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = SimpleNamespace(state=SimpleNamespace(redis=None))
        for target in (
            patch.object(automations, 'publish_event', AsyncMock()),
            patch.object(agent_tasks, 'emit_task_changed', AsyncMock()),
            patch.object(task_limits, 'get', AsyncMock(side_effect=limit_of)),
        ):
            target.start()
            self.addCleanup(target.stop)

    async def test_failures_in_a_row_turn_it_off_and_tell_the_owner(self):
        runs = [automation_run(status) for status in ('error', 'skipped', 'error', 'error', 'success')]
        with (
            patch.object(automations.AutomationRuns, 'get_by_automation', AsyncMock(return_value=runs)),
            patch.object(automations.Automations, 'deactivate', AsyncMock(return_value=automation(is_active=False))),
        ):
            await automations._turn_off_after_failures(self.app, 'auto')

        automations.publish_event.assert_awaited_once()
        self.assertEqual(automations.publish_event.await_args.args[1], automations.EVENTS.AUTOMATION_DISABLED)
        notify = agent_tasks.emit_task_changed.await_args.kwargs['notify']
        self.assertEqual(notify['title'], 'Automation turned off: Digest')
        self.assertIn('3 failed runs in a row', notify['body'])

    async def test_a_success_in_between_keeps_it_on(self):
        runs = [automation_run(status) for status in ('error', 'error', 'success', 'error')]
        with (
            patch.object(automations.AutomationRuns, 'get_by_automation', AsyncMock(return_value=runs)),
            patch.object(automations.Automations, 'deactivate', AsyncMock()) as deactivate,
        ):
            await automations._turn_off_after_failures(self.app, 'auto')
        deactivate.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
