"""Chat containers: node scoring, placement, moving between nodes and what the agent is told.

Pure logic, with the database and the nodes mocked; no Docker needed.
"""

import unittest
from unittest.mock import AsyncMock, patch

from open_webui.utils import containers


def status(active=0, max_active=4, memory_free=0.8, load=0.1, cpus=2, accepting=True, spare=0):
    return {
        'active': active,
        'max_active': max_active,
        'memory_total': 1000,
        'memory_available': int(1000 * memory_free),
        'load1': load,
        'cpus': cpus,
        'accepting': accepting,
        'containers': {'spare': spare},
        'limits': {'cpus': 1.0, 'memory': 1024**3, 'disk': 10 * 1024**3, 'bandwidth': '100mibps'},
    }


NODES = [
    {'id': 'a', 'name': 'A', 'url': 'http://a:8090', 'key': 'ka', 'enabled': True},
    {'id': 'b', 'name': 'B', 'url': 'http://b:8090', 'key': 'kb', 'enabled': True},
]


class ScoringTests(unittest.TestCase):
    def test_idle_node_scores_low_and_full_node_high(self):
        self.assertLess(containers.node_score(status()), 0.15)
        self.assertGreater(containers.node_score(status(active=4, memory_free=0.1, load=2)), 0.9)

    def test_pick_node_prefers_least_busy_online_accepting(self):
        statuses = {'a': status(active=3), 'b': status(active=1)}
        self.assertEqual(containers.pick_node(NODES, statuses)['id'], 'b')
        statuses = {'a': status(active=3), 'b': None}
        self.assertEqual(containers.pick_node(NODES, statuses)['id'], 'a')
        statuses = {'a': status(accepting=False), 'b': None}
        self.assertIsNone(containers.pick_node(NODES, statuses))
        self.assertIsNone(containers.pick_node(NODES, {'a': status(), 'b': status()}, exclude={'a', 'b'}))

    def test_pick_node_prefers_a_warm_spare(self):
        statuses = {'a': status(active=1, spare=1), 'b': status(active=0)}
        self.assertEqual(containers.pick_node(NODES, statuses)['id'], 'b')
        self.assertEqual(containers.pick_node(NODES, statuses, prefer_spare=True)['id'], 'a')

    def test_should_transfer_only_when_clearly_busier(self):
        busy, quiet = status(active=4, memory_free=0.3, load=1.5), status(active=0)
        self.assertTrue(containers.should_transfer(busy, quiet))
        self.assertFalse(containers.should_transfer(quiet, busy))
        self.assertFalse(containers.should_transfer(status(active=2), status(active=1)))
        self.assertTrue(containers.should_transfer(status(accepting=False), quiet))
        self.assertFalse(containers.should_transfer(None, quiet))


class ConfigTests(unittest.TestCase):
    def test_parse_nodes(self):
        nodes = containers.parse_nodes(
            '[{"id": "vps 1", "url": "http://n:8090/", "key": "k"}, {"url": "http://m"}, {"id": "vps 1", "url": "x"}, {}]'
        )
        self.assertEqual([node['id'] for node in nodes], ['vps-1', 'node-2'])
        self.assertEqual(nodes[0]['url'], 'http://n:8090')
        self.assertTrue(nodes[0]['enabled'])

    def test_normalize_connections_keeps_builtin_canonical(self):
        other = {'id': 'term', 'url': 'http://t', 'server_type': 'terminal'}
        tampered = {'id': 'mologate', 'url': 'http://evil', 'server_type': 'terminal', 'name': 'Box', 'enabled': False}
        result = containers.normalize_connections([other, tampered], enabled=True)
        builtin = next(c for c in result if c['id'] == 'mologate')
        self.assertEqual(builtin['url'], '')
        self.assertEqual(builtin['server_type'], 'mologate')
        self.assertEqual(builtin['name'], 'Box')
        self.assertFalse(builtin['enabled'])
        self.assertEqual(containers.normalize_connections([other], enabled=True)[0]['id'], 'mologate')
        self.assertEqual(containers.normalize_connections([other, tampered], enabled=False), [other])


class PromptTests(unittest.TestCase):
    def test_container_prompt_lists_files_network_and_limits(self):
        row = {
            'network': False,
            'files': {
                'f1': {'id': 'f1', 'path': '/home/user/uploads/a.csv', 'size': 2048, 'at': 1},
                'f2': {'id': 'f2', 'path': '/home/user/uploads/b.pdf', 'size': 3 * 1024**2, 'at': 2},
            },
            'note': 'Fresh container.',
        }
        prompt = containers.container_prompt(row, status(), ['f2'])
        self.assertIn('Network: off', prompt)
        self.assertIn('/home/user/uploads/a.csv (2 KB)', prompt)
        self.assertIn('/home/user/uploads/b.pdf (3 MB) (attached to this message)', prompt)
        self.assertIn('1 CPU, 1 GB memory, 10 GB disk, 100 MiB/s network', prompt)
        self.assertIn('Fresh container.', prompt)
        self.assertIn('Network: on', containers.container_prompt({'network': True}, None, []))

    def test_unique_upload_paths(self):
        taken = {'/home/user/uploads/a.txt', '/home/user/uploads/a (2).txt'}
        self.assertEqual(containers._unique_path('a.txt', taken), '/home/user/uploads/a (3).txt')
        self.assertEqual(containers._safe_name('../../etc/passwd'), 'passwd')
        self.assertEqual(containers._safe_name('C:\\Users\\me\\x.txt'), 'x.txt')


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_route_refuses_someone_elses_chat(self):
        containers._owners.clear()
        with patch.object(containers, 'owns_context', AsyncMock(return_value=False)):
            with self.assertRaises(containers.NodeUnavailable):
                await containers.route('chat:theirs', 'me')

    async def test_route_reaches_the_node_holding_the_chat(self):
        row = {'context': 'chat:c1', 'node_id': 'b', 'network': True}
        with (
            patch.object(containers, 'owns_context', AsyncMock(return_value=True)),
            patch.object(containers, 'placement', AsyncMock(return_value=row)),
            patch.object(containers, 'get_nodes', AsyncMock(return_value=NODES)),
            patch.object(containers, 'node_status', AsyncMock(return_value=status())),
        ):
            url, headers = await containers.route('chat:c1', 'me')
        self.assertEqual(url, 'http://b:8090/v1/sandboxes/chat:c1/t')
        self.assertEqual(headers, {'Authorization': 'Bearer kb'})


class PrepareTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        containers._status_cache.clear()

    async def test_stopped_container_moves_off_a_busy_node(self):
        row = {'context': 'chat:c1', 'node_id': 'a', 'network': True, 'files': {}, 'note': None}
        statuses = {'a': status(active=4, memory_free=0.2, load=2), 'b': status(active=0)}
        calls = []

        async def node_request(node, method, path, **kwargs):
            calls.append((node['id'], method, path))
            if method == 'GET':
                return {'state': 'exited', 'disk_used': 1000}
            return {'state': 'running'}

        with (
            patch.object(containers, 'placement', AsyncMock(return_value=row)),
            patch.object(containers, 'get_nodes', AsyncMock(return_value=NODES)),
            patch.object(containers, 'node_statuses', AsyncMock(return_value=statuses)),
            patch.object(containers, 'node_request', side_effect=node_request),
            patch.object(containers, 'transfer', AsyncMock()) as transfer,
            patch.object(containers.ChatContainers, 'touch', AsyncMock()),
        ):
            node, _ = await containers.prepare('chat:c1')
        transfer.assert_awaited_once()
        self.assertEqual(node['id'], 'b')
        self.assertIn(('b', 'POST', '/v1/sandboxes/chat:c1/ensure'), calls)

    async def test_running_container_stays_put(self):
        row = {'context': 'chat:c1', 'node_id': 'a', 'network': True, 'files': {}, 'note': None}
        statuses = {'a': status(active=4, memory_free=0.2, load=2), 'b': status(active=0)}
        with (
            patch.object(containers, 'placement', AsyncMock(return_value=row)),
            patch.object(containers, 'get_nodes', AsyncMock(return_value=NODES)),
            patch.object(containers, 'node_statuses', AsyncMock(return_value=statuses)),
            patch.object(containers, 'node_request', AsyncMock(return_value={'state': 'running'})),
            patch.object(containers, 'transfer', AsyncMock()) as transfer,
            patch.object(containers.ChatContainers, 'touch', AsyncMock()),
        ):
            node, _ = await containers.prepare('chat:c1')
        transfer.assert_not_awaited()
        self.assertEqual(node['id'], 'a')

    async def test_offline_node_fails_over_with_a_note(self):
        row = {'context': 'chat:c1', 'node_id': 'a', 'network': True, 'files': {'f': {}}, 'note': None}
        with (
            patch.object(containers, 'placement', AsyncMock(return_value=row)),
            patch.object(containers, 'get_nodes', AsyncMock(return_value=NODES)),
            patch.object(containers, 'node_statuses', AsyncMock(return_value={'a': None, 'b': status()})),
            patch.object(containers, 'node_request', AsyncMock(return_value={'state': 'running'})),
            patch.object(containers.ChatContainers, 'update', AsyncMock()) as update,
            patch.object(containers.ChatContainers, 'touch', AsyncMock()),
        ):
            node, result = await containers.prepare('chat:c1')
        self.assertEqual(node['id'], 'b')
        self.assertIn('unreachable', result['note'])
        self.assertEqual(update.await_args.kwargs['node_id'], 'b')
        self.assertEqual(result['files'], {})

    async def test_full_node_hands_the_container_on(self):
        row = {'context': 'chat:c1', 'node_id': 'a', 'network': True, 'files': {}, 'note': None}

        async def node_request(node, method, path, **kwargs):
            if node['id'] == 'a' and path.endswith('/ensure'):
                raise containers.NodeError(503, 'node_full', 'full')
            return {'state': 'running'}

        with (
            patch.object(containers, 'placement', AsyncMock(return_value=row)),
            patch.object(containers, 'get_nodes', AsyncMock(return_value=NODES[:1] + NODES[1:])),
            patch.object(containers, 'node_statuses', AsyncMock(return_value={'a': status(), 'b': status()})),
            patch.object(containers, 'node_request', side_effect=node_request),
            patch.object(containers, 'transfer', AsyncMock()) as transfer,
            patch.object(containers.ChatContainers, 'touch', AsyncMock()),
        ):
            node, _ = await containers.prepare('chat:c1')
        transfer.assert_awaited_once()
        self.assertEqual(node['id'], 'b')


if __name__ == '__main__':
    unittest.main()
