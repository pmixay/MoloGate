"""Change sources: the session source by default, a repository source only for linked chats."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from open_webui.utils import change_sources

LINK = {'provider': 'github', 'name': 'pmixay/MoloGate', 'branch': 'feature/x', 'base': 'main'}
RECORD = {'files': [{'path': 'a.py', 'status': 'modified', 'added': 1, 'removed': 0}], 'total': 1}


class RepositoryTests(unittest.TestCase):
    def test_valid_links(self):
        self.assertEqual(change_sources.repository({'repository': LINK}), LINK)
        self.assertIsNone(change_sources.repository(None))
        self.assertIsNone(change_sources.repository({}))
        self.assertIsNone(change_sources.repository({'repository': {**LINK, 'provider': 'gitlab'}}))
        self.assertIsNone(change_sources.repository({'repository': {**LINK, 'name': 'not a repo'}}))
        self.assertIsNone(change_sources.repository({'repository': {**LINK, 'branch': 'x; rm -rf /'}}))
        self.assertIsNone(change_sources.repository({'repository': 'pmixay/MoloGate'}))


class CollectTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_by_default(self):
        with patch.object(change_sources.containers, 'collect_changes', AsyncMock(return_value=RECORD)):
            record = await change_sources.collect({'chat_id': 'c1'})
        self.assertEqual(record, {**RECORD, 'source': 'session'})
        with patch.object(change_sources.containers, 'collect_changes', AsyncMock(return_value=None)):
            self.assertIsNone(await change_sources.collect({'chat_id': 'c1'}))

    async def test_git_source_takes_linked_chats(self):
        git = AsyncMock(return_value={**RECORD, 'workspace': {'base': 'main', 'total': 3}})
        chats = {'linked': SimpleNamespace(meta={'repository': LINK}), 'plain': SimpleNamespace(meta={})}
        with (
            patch.dict(change_sources.SOURCES, {'git': git}),
            patch.object(change_sources.Chats, 'get_chat_by_id', AsyncMock(side_effect=lambda id: chats[id])),
            patch.object(change_sources.containers, 'collect_changes', AsyncMock(return_value=RECORD)),
        ):
            linked = await change_sources.collect({'chat_id': 'linked'})
            plain = await change_sources.collect({'chat_id': 'plain'})
        self.assertEqual((linked['source'], linked['workspace']['total']), ('git', 3))
        git.assert_awaited_once_with({'chat_id': 'linked'}, LINK)
        self.assertEqual(plain['source'], 'session')

    async def test_a_failing_source_records_nothing(self):
        with patch.object(change_sources.containers, 'collect_changes', AsyncMock(side_effect=RuntimeError('x'))):
            self.assertIsNone(await change_sources.collect({'chat_id': 'c1'}))


class LiveChangesTests(unittest.IsolatedAsyncioTestCase):
    async def test_publishes_and_saves_once_for_same_record(self):
        metadata = {'chat_id': 'chat', 'message_id': 'message', 'container_snapshot': {'id': 'message'}}
        emit = AsyncMock()
        with (
            patch.object(change_sources, 'collect', AsyncMock(return_value=RECORD)),
            patch.object(change_sources.Chats, 'upsert_message_to_chat_by_id_and_message_id', AsyncMock()) as save,
        ):
            await change_sources.publish_live_changes(metadata, emit, force=True)
            await change_sources.publish_live_changes(metadata, emit, force=True)
        emit.assert_awaited_once_with({'type': 'chat:message:changes', 'data': {'changes': RECORD}})
        save.assert_awaited_once()

    async def test_reverting_changes_publishes_empty_record(self):
        metadata = {'container_snapshot': {'id': 'message'}}
        emit = AsyncMock()
        empty = {'files': [], 'total': 0, 'added': 0, 'removed': 0, 'source': 'session'}
        with patch.object(change_sources, 'collect', AsyncMock(side_effect=[RECORD, empty])):
            await change_sources.publish_live_changes(metadata, emit, force=True)
            await change_sources.publish_live_changes(metadata, emit, force=True)
        self.assertEqual(emit.await_args_list[-1].args[0]['data']['changes']['total'], 0)

    async def test_throttles_background_polling_and_ignores_missing_snapshot(self):
        emit = AsyncMock()
        with patch.object(change_sources, 'collect', AsyncMock(return_value=RECORD)) as collect:
            await change_sources.publish_live_changes({}, emit)
            metadata = {'container_snapshot': {'id': 'm'}}
            await change_sources.publish_live_changes(metadata, emit)
            await change_sources.publish_live_changes(metadata, emit)
        self.assertEqual(collect.await_count, 1)

    async def test_failed_tracking_does_not_break_tool_or_clear_verified_result(self):
        emit = AsyncMock()
        with patch.object(change_sources, 'collect', AsyncMock(side_effect=RuntimeError('offline'))):
            await change_sources.publish_live_changes({'container_snapshot': {'id': 'm'}}, emit, force=True)
        emit.assert_not_awaited()


class LiveFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_failed_emit_of_identical_record(self):
        metadata = {'container_snapshot': {'id': 'm'}}
        emit = AsyncMock(side_effect=[RuntimeError('socket'), None])
        with patch.object(change_sources, 'collect', AsyncMock(return_value=RECORD)):
            await change_sources.publish_live_changes(metadata, emit, force=True)
            await change_sources.publish_live_changes(metadata, emit, force=True)
        self.assertEqual(emit.await_count, 2)

    async def test_retries_failed_persistence_without_duplicate_emit(self):
        metadata = {'chat_id': 'c', 'message_id': 'm', 'container_snapshot': {'id': 'm'}}
        emit = AsyncMock()
        with (
            patch.object(change_sources, 'collect', AsyncMock(return_value=RECORD)),
            patch.object(
                change_sources.Chats,
                'upsert_message_to_chat_by_id_and_message_id',
                AsyncMock(side_effect=[None, object()]),
            ) as save,
        ):
            await change_sources.publish_live_changes(metadata, emit, force=True)
            await change_sources.publish_live_changes(metadata, emit, force=True)
            await change_sources.publish_live_changes(metadata, emit, force=True)
        self.assertEqual(save.await_count, 2)
        emit.assert_awaited_once()

    async def test_live_collection_uses_short_deadline(self):
        emit = AsyncMock()

        async def timeout(coro, *, timeout):
            self.assertEqual(timeout, 5)
            coro.close()
            raise TimeoutError()

        with patch.object(change_sources.asyncio, 'wait_for', side_effect=timeout):
            await change_sources.publish_live_changes({'container_snapshot': {'id': 'm'}}, emit, force=True)
        emit.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
