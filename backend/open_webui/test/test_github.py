"""Standalone sandbox-controller tests: no DB, Docker, GitHub account or backend deps.

Run: python -m unittest discover -s backend/open_webui/test -p test_github.py
The controller is the actual embedded program, running against a temporary fake gh.
"""

import ast
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).parents[1] / 'utils' / 'github.py'
TREE = ast.parse(SOURCE.read_text())
CONTROLLER = next(ast.literal_eval(n.value) for n in TREE.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'CONTROLLER' for t in n.targets))
namespace = {'re': re}
exec(compile(ast.Module(body=[n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == 'sanitized'], type_ignores=[]), str(SOURCE), 'exec'), namespace)
sanitized = namespace['sanitized']

FAKE_GH = '''#!/usr/bin/env python3
import os, pathlib, sys, time
root = pathlib.Path(os.environ['FAKE_GH_ROOT'])
args = sys.argv[1:]
with open(root / 'calls', 'a') as f: f.write(' '.join(args) + '\\n')
assert not os.environ.get('GH_TOKEN')
assert not os.environ.get('GITHUB_TOKEN')
if args[:2] == ['api', 'user']:
    if (root / 'authorized').exists(): print('octocat'); sys.exit(0)
    sys.exit(1)
if args[:2] == ['auth', 'login']:
    print('! First copy your one-time code: ABCD-1234', flush=True)
    print('never-forward-token-secret', flush=True)
    if (root / 'fail').exists(): sys.exit(1)
    while not (root / 'approve').exists(): time.sleep(.02)
    (root / 'authorized').touch()
if args[:2] == ['auth', 'logout']:
    (root / 'authorized').unlink(missing_ok=True)
'''


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.home())
        self.root = Path(self.temp.name)
        gh = self.root / 'gh'
        gh.write_text(FAKE_GH.replace('#!/usr/bin/env python3', '#!' + sys.executable))
        gh.chmod(0o755)
        self.env = {**os.environ, 'PATH': str(self.root) + ':' + os.environ['PATH'],
                    'FAKE_GH_ROOT': str(self.root), 'GH_TOKEN': 'host-token', 'GITHUB_TOKEN': 'host-token'}
        self.program = CONTROLLER.replace('/home/user/.local/state/mologate-github', str(self.root / 'state')).replace('/usr/bin/gh', str(gh)).replace('/home/user/.config/gh', str(self.root / 'config')).replace("['" + str(gh) + "',", "[sys.executable, '" + str(gh) + "',")

    def call(self, operation, network=True):
        p = subprocess.run([sys.executable, '-c', self.program, operation, '1' if network else '0'],
                           env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn('never-forward', p.stdout)
        return json.loads(p.stdout)

    def tearDown(self):
        self.call('disconnect')
        self.temp.cleanup()

    def test_connect_reuses_code_then_poll_sets_up_git(self):
        a = self.call('connect')
        self.assertEqual(a, {'connected': False, 'pending': True, 'code': 'ABCD-1234', 'verification_uri': 'https://github.com/login/device'})
        self.assertEqual(self.call('connect'), a)
        self.assertEqual(self.call('status'), a)
        calls = (self.root / 'calls').read_text()
        self.assertEqual(calls.count('auth login'), 1)
        (self.root / 'approve').touch()
        import time
        for _ in range(100):
            if (self.root / 'authorized').exists(): break
            time.sleep(.02)
        self.assertEqual(self.call('poll'), {'connected': True, 'pending': False, 'login': 'octocat'})
        self.assertIn('auth setup-git --hostname github.com', (self.root / 'calls').read_text())
        self.assertEqual(self.call('status', False)['login'], 'octocat')
        self.assertFalse((self.root / 'state' / 'output').exists())
        self.assertEqual(self.call('disconnect'), {'connected': False, 'pending': False})
        self.assertFalse((self.root / 'authorized').exists())

    def test_offline_does_not_start_login(self):
        self.assertEqual(self.call('connect', False)['error'], 'network_off')
        self.assertFalse((self.root / 'calls').exists())

    def test_expired_flow_cleans_up_and_can_restart(self):
        self.call('connect')
        path = self.root / 'state' / 'state'
        state = json.loads(path.read_text()); state['started'] = 0
        path.write_text(json.dumps(state))
        self.assertEqual(self.call('poll'), {'connected': False, 'pending': False})
        self.assertFalse(path.exists())
        self.assertTrue(self.call('connect')['pending'])

    def test_disconnect_kills_pending_and_is_idempotent(self):
        self.call('connect')
        self.call('disconnect')
        self.assertEqual(self.call('poll'), {'connected': False, 'pending': False})
        self.assertEqual(self.call('disconnect'), {'connected': False, 'pending': False})

    def test_failed_device_login_stops_pending(self):
        (self.root / 'fail').touch()
        self.call('connect')
        import time
        time.sleep(.1)
        self.assertEqual(self.call('poll'), {'connected': False, 'pending': False})


class ContractTests(unittest.TestCase):
    def test_output_is_allowlisted(self):
        self.assertEqual(sanitized({'connected': True, 'login': 'octocat', 'token': 'secret', 'logs': 'secret'}),
                         {'connected': True, 'pending': False, 'login': 'octocat'})
        self.assertEqual(sanitized({'connected': True, 'login': 'secret\nTOKEN', 'pending': False}),
                         {'connected': False, 'pending': False})
        self.assertEqual(sanitized({'pending': True, 'code': 'ABCD-1234', 'verification_uri': 'https://evil.test'}),
                         {'connected': False, 'pending': True, 'code': 'ABCD-1234', 'verification_uri': 'https://github.com/login/device'})
        self.assertNotIn('code', sanitized({'pending': False, 'code': 'ABCD-1234'}))

    def test_routes_share_ownership_and_enabled_gate(self):
        tree = ast.parse((SOURCE.parents[1] / 'routers' / 'containers.py').read_text())
        functions = {n.name: n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)}
        gate = ast.unparse(functions['_github'])
        self.assertLess(gate.index('await _require_enabled()'), gate.index('await github('))
        self.assertLess(gate.index('await _chat_context('), gate.index('await github('))
        for name in ('get_chat_github', 'connect_chat_github', 'poll_chat_github', 'disconnect_chat_github'):
            self.assertIn('await _github(', ast.unparse(functions[name]))
            self.assertIn('Depends(get_verified_user)', ast.unparse(functions[name]))


if __name__ == '__main__':
    unittest.main()
