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


if __name__ == '__main__':
    unittest.main()
