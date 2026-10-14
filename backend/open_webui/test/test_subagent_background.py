"""Sub-agents as tasks: moving to the background, stopping, the task limits, and results coming back.

The model run is a stand-in coroutine; chat rows live in a dict or a private in-memory SQLite.
"""

import asyncio
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from open_webui import tasks as task_registry
from open_webui.socket import main as socket_main
from open_webui.test.test_agent_tasks import MemoryDB
from open_webui.utils import agent_messages, agent_tasks, subagents, task_limits

USER = {
    'id': 'owner',
    'email': 'owner@example.com',
    'name': 'Owner',
    'role': 'user',
    'last_active_at': 0,
    'updated_at': 0,
    'created_at': 0,
}
CONFIG = {
    'subagents.background_enabled': True,
    'subagents.max_concurrent': 20,
    'subagents.max_async': 20,
    'subagents.max_iterations': 30,
    'subagents.max_output': 30_000,
    'subagents.system_prompt': '',
    'subagents.profiles': None,
}
LIMITS = {
    'auto_background_seconds': 0,
    'subagents_per_chat': 4,
    'background_tasks_per_user': 5,
    'background_run_seconds': 7200,
}


async def eventually(check, timeout=3.0):
    """Wait until ``check()`` is true, for things that finish in other tasks."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError('timed out waiting')
        await asyncio.sleep(0.02)


def reset_module_state():
    subagents._background_lock = asyncio.Lock()
    subagents._foreground_semaphore = None
    subagents._background_active.clear()
    subagents._chat_running.clear()
    subagents._user_background.clear()
    subagents._deliveries.clear()
    subagents._parent_locks.clear()


class DelegateCase(unittest.IsolatedAsyncioTestCase):
    """delegate() with the model run, chat rows and messages faked."""

    async def asyncSetUp(self):
        reset_module_state()
        self.rows = {}
        self.messages = {}
        self.limits = dict(LIMITS)
        self.run_seconds = 0.05
        self.started = asyncio.Event()
        self.release = None
        self.run_cancelled = []

        async def insert_new_chat(chat_id, user_id, form, internal_meta=None, **_):
            self.rows[chat_id] = copy.deepcopy(internal_meta)
            for message_id, message in form.chat['history']['messages'].items():
                self.messages[(chat_id, message_id)] = copy.deepcopy(message)
            return True

        async def upsert_message(chat_id, message_id, message, **_):
            self.messages.setdefault((chat_id, message_id), {}).update(message)

        async def get_message(chat_id, message_id):
            return copy.deepcopy(self.messages.get((chat_id, message_id)))

        async def update_task_meta(chat_id, change, **_):
            if chat_id not in self.rows:
                return None
            draft = copy.deepcopy(self.rows[chat_id])
            outcome = change(draft)
            if outcome is not False:
                self.rows[chat_id] = draft
            return outcome

        async def get_task_meta(chat_id):
            return copy.deepcopy(self.rows.get(chat_id))

        async def handler(request, form_data, user):
            self.started.set()
            try:
                if self.release:
                    await self.release.wait()
                else:
                    await asyncio.sleep(self.run_seconds)
            except asyncio.CancelledError:
                self.run_cancelled.append(form_data['chat_id'])
                raise
            self.messages[(form_data['chat_id'], form_data['id'])].update(content='Done.', done=True)

        self.post_task_result = AsyncMock(return_value='result-message')
        self.emit = AsyncMock()
        self.patches = [
            patch.object(subagents.Config, 'get_many', AsyncMock(return_value=CONFIG)),
            patch.object(task_limits, 'get_all', AsyncMock(side_effect=lambda: dict(self.limits))),
            patch.object(subagents.Chats, 'insert_new_chat', insert_new_chat),
            patch.object(subagents.Chats, 'upsert_message_to_chat_by_id_and_message_id', upsert_message),
            patch.object(subagents.Chats, 'get_message_by_id_and_message_id', get_message),
            patch.object(agent_tasks, 'update_task_meta', update_task_meta),
            patch.object(agent_tasks, 'get_task_meta', get_task_meta),
            patch.object(agent_tasks, 'emit_task_changed', self.emit),
            patch.object(subagents, 'post_task_result', self.post_task_result),
            patch.object(agent_messages, 'schedule_leftover_messages', MagicMock()),
        ]
        for active in self.patches:
            active.start()
        self.request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(redis=None, MODELS={}, CHAT_COMPLETION_HANDLER=handler))
        )

    async def asyncTearDown(self):
        for task in list(task_registry.tasks.values()):
            task.cancel()
        for delivery in list(subagents._deliveries):
            delivery.cancel()
        await asyncio.sleep(0)
        task_registry.tasks.clear()
        task_registry.item_tasks.clear()
        task_registry.response_streams.clear()
        for active in self.patches:
            active.stop()

    def delegate(self, task='Read the logs', *, background=False, chat='main'):
        return subagents.delegate(
            task,
            '',
            background,
            request=self.request,
            user_data=USER,
            metadata={'model_id': 'model', 'session_id': 'session', 'features': {}},
            parent_chat_id=chat,
            parent_message_id='answer-1',
        )

    async def wait_started(self):
        await asyncio.wait_for(self.started.wait(), 5)

    def answer(self, chat_id):
        return next(m for (c, _), m in self.messages.items() if c == chat_id and m['role'] == 'assistant')

    def only_row(self):
        (chat_id, meta), *_ = self.rows.items()
        return chat_id, meta

    def assert_no_slots_held(self):
        self.assertEqual(subagents._chat_running, {})
        self.assertEqual(subagents._user_background, {})
        self.assertEqual(subagents._background_active, set())


class ForegroundTest(DelegateCase):
    async def test_a_quick_sub_agent_answers_in_place(self):
        result = await self.delegate()

        chat_id, meta = self.only_row()
        self.assertTrue(
            result.startswith(f'[Sub-agent {meta["delegation_id"]} (general-purpose) finished · chat {chat_id}]')
        )
        self.assertTrue(result.endswith('Done.'))
        self.assertEqual(meta['task_id'], meta['delegation_id'])
        self.assertEqual(meta['title'], 'Read the logs')
        self.assertEqual(meta['status'], 'completed')
        self.assertEqual(meta['result_preview'], 'Done.')
        self.assertIsInstance(meta['started_at'], int)
        self.assertIsInstance(meta['finished_at'], int)
        self.post_task_result.assert_not_awaited()
        self.assertEqual([call.kwargs['status'] for call in self.emit.await_args_list], ['running', 'completed'])
        self.assert_no_slots_held()

    async def test_its_live_output_is_kept_under_its_task(self):
        self.release = asyncio.Event()
        delegation = asyncio.create_task(self.delegate())
        await self.wait_started()

        chat_id, _ = self.only_row()
        (stream_id,) = task_registry.item_tasks[chat_id]
        # The run's request names the same id, so the middleware saves the live output under it.
        self.assertIn(stream_id, task_registry.tasks)
        self.release.set()
        await delegation

    async def test_a_long_sub_agent_moves_to_the_background(self):
        self.limits['auto_background_seconds'] = 0.1
        self.run_seconds = 0.4

        moved = json.loads(await self.delegate())

        chat_id, meta = self.only_row()
        self.assertEqual(moved['status'], 'moved_to_background')
        self.assertEqual(moved['task_id'], meta['delegation_id'])
        self.assertEqual(moved['subagent_chat_id'], chat_id)
        self.assertEqual(moved['mode'], 'background')
        self.assertIn('cancel_task stops it', moved['note'])
        self.assertEqual(meta['mode'], 'background')
        self.assertIsInstance(meta['moved_to_background_at'], int)
        self.assertEqual(subagents._user_background, {'owner': {meta['delegation_id']}})

        await eventually(lambda: self.post_task_result.await_count == 1)
        kwargs = self.post_task_result.await_args.kwargs
        self.assertEqual(kwargs['parent_chat_id'], 'main')
        self.assertEqual(
            kwargs['meta'], {'type': 'subagent', 'delegation_id': meta['delegation_id'], 'subagent_chat_id': chat_id}
        )
        self.assertIn(f'[ASYNC SUBAGENT COMPLETE - {meta["delegation_id"]}]', kwargs['content'])
        self.assertIn('Done.', kwargs['content'])
        await eventually(lambda: not subagents._user_background)
        self.assert_no_slots_held()

    async def test_without_background_room_it_keeps_waiting(self):
        self.limits.update(auto_background_seconds=0.1, background_tasks_per_user=1)
        subagents._user_background['owner'] = {'deleg_busy'}
        self.run_seconds = 0.4

        result = await self.delegate()

        self.assertTrue(result.endswith('Done.'))
        self.post_task_result.assert_not_awaited()
        self.assertEqual(self.only_row()[1]['mode'], 'foreground')

    async def test_stopping_the_reply_stops_the_sub_agent(self):
        self.release = asyncio.Event()
        delegation = asyncio.create_task(self.delegate())
        await self.wait_started()

        delegation.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await delegation

        chat_id, _ = self.only_row()
        await eventually(lambda: self.rows[chat_id]['status'] == 'cancelled')
        self.assertEqual(self.run_cancelled, [chat_id])
        self.assertIsNone(self.rows[chat_id].get('cancelled_by'))
        self.assertEqual(self.answer(chat_id)['error'], {'content': 'Sub-agent cancelled.'})
        await eventually(lambda: not subagents._chat_running)
        self.post_task_result.assert_not_awaited()

    async def test_the_user_stopping_it_from_the_panel(self):
        self.release = asyncio.Event()
        delegation = asyncio.create_task(self.delegate())
        await self.wait_started()
        chat_id, _ = self.only_row()

        self.rows[chat_id].update(status='cancelled', cancelled_by='user')
        await task_registry.stop_item_tasks(None, chat_id)

        self.assertEqual(await delegation, 'Error: the user stopped this sub-agent.')
        self.assertEqual(self.rows[chat_id]['status'], 'cancelled')
        self.post_task_result.assert_not_awaited()
        self.assert_no_slots_held()

    async def test_the_run_time_limit(self):
        self.limits['background_run_seconds'] = 0.1
        self.release = asyncio.Event()

        result = await self.delegate()

        chat_id, meta = self.only_row()
        self.assertIn('run-time limit', result)
        self.assertEqual(meta['status'], 'failed')
        self.assertIn('run-time limit', meta['error'])
        self.assertEqual(self.run_cancelled, [chat_id])


class BackgroundTest(DelegateCase):
    async def test_a_background_sub_agent_reports_back(self):
        dispatched = json.loads(await self.delegate(background=True))

        chat_id, meta = self.only_row()
        self.assertEqual(dispatched['status'], 'dispatched')
        self.assertEqual(dispatched['task_id'], meta['delegation_id'])
        self.assertEqual(meta['mode'], 'background')
        await eventually(lambda: self.post_task_result.await_count == 1)
        self.assertIn('Status: completed', self.post_task_result.await_args.kwargs['content'])
        await eventually(lambda: not subagents._background_active)
        self.assert_no_slots_held()

    async def test_stopped_from_the_panel_it_posts_nothing(self):
        self.release = asyncio.Event()
        await self.delegate(background=True)
        await self.wait_started()
        chat_id, _ = self.only_row()

        self.rows[chat_id].update(status='cancelled', cancelled_by='user')
        await task_registry.stop_item_tasks(None, chat_id)
        await eventually(lambda: not subagents._deliveries)

        self.post_task_result.assert_not_awaited()
        self.assertEqual(self.rows[chat_id]['status'], 'cancelled')

    async def test_cancelled_otherwise_it_reports_the_interruption(self):
        self.release = asyncio.Event()
        await self.delegate(background=True)
        await self.wait_started()
        chat_id, _ = self.only_row()

        await task_registry.stop_item_tasks(None, chat_id)

        await eventually(lambda: self.post_task_result.await_count == 1)
        self.assertIn('interrupted', self.post_task_result.await_args.kwargs['content'])
        self.assertEqual(self.rows[chat_id]['status'], 'cancelled')


class LimitTest(DelegateCase):
    async def test_over_the_chat_limit_foreground_waits_and_background_is_refused(self):
        self.limits['subagents_per_chat'] = 1
        self.release = asyncio.Event()
        first = asyncio.create_task(self.delegate('first'))
        await self.wait_started()

        second = asyncio.create_task(self.delegate('second'))
        await asyncio.sleep(0.3)
        refused = await self.delegate('third', background=True)

        self.assertFalse(second.done())
        self.assertEqual(len(self.rows), 1)
        self.assertTrue(refused.startswith('Error: 1 sub-agents are already running in this chat'))
        # Another chat has room of its own.
        other = asyncio.create_task(self.delegate('elsewhere', chat='other-main'))
        await eventually(lambda: len(self.rows) == 2)

        self.release.set()
        self.assertTrue((await first).endswith('Done.'))
        self.assertTrue((await second).endswith('Done.'))
        self.assertTrue((await other).endswith('Done.'))
        self.assert_no_slots_held()

    async def test_a_member_has_a_few_background_tasks_at_once(self):
        self.limits['background_tasks_per_user'] = 1
        self.release = asyncio.Event()
        await self.delegate('first', background=True, chat='chat-a')

        refused = await self.delegate('second', background=True, chat='chat-b')

        self.assertTrue(refused.startswith('Error: 1 of your background tasks are already running'))
        self.release.set()
        await eventually(lambda: not subagents._user_background)


class MainChatCase(unittest.IsolatedAsyncioTestCase):
    """A main chat in a private in-memory SQLite, with the turn it starts faked."""

    async def asyncSetUp(self):
        reset_module_state()
        self.db = MemoryDB(subagents)
        await self.db.start()
        self.handler = AsyncMock(return_value={'status': True})
        self.request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(redis=None, MODELS={}, CHAT_COMPLETION_HANDLER=self.handler))
        )
        self.sio_emit = AsyncMock()
        self.patches = [
            patch.object(subagents.Users, 'get_user_by_id', AsyncMock(return_value=SimpleNamespace(id='owner'))),
            patch.object(subagents.ChatMessages, 'upsert_message', AsyncMock()),
            patch.object(subagents.ChatMessages, 'delete_message_ids_by_chat_id', AsyncMock()),
            patch.object(socket_main.sio, 'emit', self.sio_emit),
        ]
        for active in self.patches:
            active.start()

    async def asyncTearDown(self):
        for active in self.patches:
            active.stop()
        await self.db.stop()

    async def add_main_chat(self, messages: dict, current_id: str):
        await self.db.add('main', {}, chat={'history': {'currentId': current_id, 'messages': messages}})

    @staticmethod
    def answer(id, parent, timestamp, children=()):
        return {
            'id': id,
            'role': 'assistant',
            'parentId': parent,
            'childrenIds': list(children),
            'content': f'answer {id}',
            'done': True,
            'timestamp': timestamp,
        }

    @staticmethod
    def result(id, parent, meta, timestamp=10):
        return {
            'id': id,
            'role': 'user',
            'parentId': parent,
            'childrenIds': [],
            'content': f'result {id}',
            'model': 'model',
            'meta': {'internal': True, **meta},
            'timestamp': timestamp,
        }


class PendingMessagesTest(MainChatCase):
    """Results waiting in the main chat get one turn, after whatever turn ran when they came."""

    async def process(self):
        return await subagents.process_pending_internal_messages(self.request, 'main', 'owner', {'model_id': 'model'})

    async def test_task_results_that_came_together_share_a_turn(self):
        monitor = {'type': 'task', 'task_type': 'monitor', 'task_id': 'row-1', 'handle': 'mon_1', 'status': 'completed'}
        failed = {**monitor, 'task_id': 'row-2', 'handle': 'mon_2', 'status': 'failed', 'delivery': 'pending'}
        await self.add_main_chat(
            {
                'a1': self.answer('a1', None, 1, children=['t1', 't2']),
                't1': self.result('t1', 'a1', {**monitor, 'delivery': 'pending'}),
                't2': self.result('t2', 'a1', failed),
            },
            'a1',
        )

        await self.process()

        form_data = self.handler.await_args.args[1]
        self.assertEqual(
            form_data['user_message']['meta'],
            {'internal': True, 'type': 'task', 'task_ids': ['row-1', 'row-2'], 'handles': ['mon_1', 'mon_2']},
        )
        self.assertEqual(form_data['messages'][-1]['content'], 'result t1\n\nresult t2')
        self.assertEqual(form_data['parent_id'], 'a1')
        history = (await self.db.row('main'))[2]['history']
        self.assertNotIn('t1', history['messages'])
        self.assertNotIn('t2', history['messages'])

    async def test_one_task_result_keeps_its_status(self):
        monitor = {'type': 'task', 'task_type': 'monitor', 'task_id': 'row-1', 'handle': 'mon_1', 'status': 'completed'}
        await self.add_main_chat(
            {'a1': self.answer('a1', None, 1, children=['t1']), 't1': self.result('t1', 'a1', monitor)}, 'a1'
        )

        await self.process()

        user_message = self.handler.await_args.args[1]['user_message']
        self.assertEqual(user_message['id'], 't1')
        self.assertEqual(user_message['meta'], {'internal': True, **monitor})
        # Answered now, so it doesn't start another turn.
        self.handler.reset_mock()
        self.assertIsNone(await self.process())
        self.handler.assert_not_awaited()

    async def test_a_result_that_waited_continues_after_the_turn_that_ran(self):
        # The result came while the turn of a2 ran, so it hangs under a1.
        await self.add_main_chat(
            {
                'a1': self.answer('a1', None, 1, children=['u2', 's1']),
                'u2': {
                    'id': 'u2',
                    'role': 'user',
                    'parentId': 'a1',
                    'childrenIds': ['a2'],
                    'content': 'go',
                    'timestamp': 2,
                },
                'a2': self.answer('a2', 'u2', 3),
                's1': self.result('s1', 'a1', {'type': 'subagent', 'delegation_id': 'deleg_1', 'status': 'pending'}),
            },
            'a2',
        )

        await self.process()

        form_data = self.handler.await_args.args[1]
        self.assertEqual(form_data['parent_id'], 'a2')
        self.assertEqual(form_data['user_message']['parentId'], 'a2')
        self.assertEqual(
            form_data['user_message']['meta'], {'internal': True, 'type': 'subagent', 'delegation_id': 'deleg_1'}
        )
        messages = (await self.db.row('main'))[2]['history']['messages']
        self.assertEqual(messages['a1']['childrenIds'], ['u2'])


class PostTaskResultTest(MainChatCase):
    async def test_a_busy_chat_keeps_the_result_waiting(self):
        await self.add_main_chat({'a1': self.answer('a1', None, 1)}, 'a1')
        process = AsyncMock()
        meta = {'type': 'task', 'task_type': 'monitor', 'task_id': 'row-1', 'handle': 'mon_1', 'status': 'completed'}

        with (
            patch.object(subagents, 'has_active_tasks', AsyncMock(return_value=True)),
            patch.object(subagents, 'process_pending_internal_messages', process),
        ):
            message_id = await subagents.post_task_result(
                self.request.app,
                parent_chat_id='main',
                user_id='owner',
                content='[MONITOR COMPLETE]',
                meta=meta,
                run={'model_id': 'model'},
            )

        message = (await self.db.row('main'))[2]['history']['messages'][message_id]
        self.assertEqual(message['parentId'], 'a1')
        self.assertEqual(message['meta'], {'internal': True, **meta, 'delivery': 'pending'})
        self.assertEqual(self.sio_emit.await_args.args[1]['data'], {'type': 'chat:reload'})
        process.assert_not_awaited()

    async def test_an_idle_chat_starts_a_turn_and_a_missing_one_gets_nothing(self):
        await self.add_main_chat({'a1': self.answer('a1', None, 1)}, 'a1')

        message_id = await subagents.post_task_result(
            self.request.app,
            parent_chat_id='main',
            user_id='owner',
            content='[MONITOR COMPLETE]',
            meta={'type': 'task', 'task_type': 'monitor', 'task_id': 'row-1', 'handle': 'mon_1', 'status': 'completed'},
            run={'model_id': 'model'},
        )

        self.assertEqual(self.handler.await_args.args[1]['user_message']['id'], message_id)
        self.assertIsNone(
            await subagents.post_task_result(
                self.request.app, parent_chat_id='gone', user_id='owner', content='x', meta={'type': 'task'}, run={}
            )
        )


if __name__ == '__main__':
    unittest.main()
