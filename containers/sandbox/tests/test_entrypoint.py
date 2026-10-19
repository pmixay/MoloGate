"""Entrypoint smoke tests; stdlib only, without Docker or a running Open Terminal."""

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ENTRYPOINT = Path(__file__).resolve().parents[1] / 'mologate-sandbox.sh'
PROBE = """
import json, os, subprocess, sys, tempfile
with tempfile.NamedTemporaryFile() as f:
    child = subprocess.check_output([sys.executable, '-c',
        'import tempfile; print(tempfile.gettempdir())'], text=True).strip()
    print(json.dumps({'env': {k: os.environ[k] for k in ('TMPDIR', 'TMP', 'TEMP')},
        'file': f.name, 'child_tmp': child, 'log_dir': os.getenv('OPEN_TERMINAL_LOG_DIR')}))
"""


class EntrypointTests(unittest.TestCase):
    def probe(self, overrides=None):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / 'home'
            script = ENTRYPOINT.read_text()
            self.assertIn('/home/user/.local/state/open-terminal/tmp', script)
            # Relocate fixed container paths; execute the actual setup in /bin/sh without
            # deleting the live terminal socket or launching a second server.
            script = script.replace('/home/user', str(home)).replace('/run/mologate', root + '/run')
            script = script.split('exec /opt/open-terminal/bin/uvicorn', 1)[0]
            script += 'exec ' + shlex.quote(sys.executable) + ' -c ' + shlex.quote(PROBE)
            env = {k: v for k, v in os.environ.items() if k not in ('TMPDIR', 'TMP', 'TEMP')}
            env['OPEN_TERMINAL_API_KEY'] = 'smoke'
            env['OPEN_TERMINAL_LOG_DIR'] = str(Path(root) / 'custom-logs')
            overrides = {k: str(Path(root) / v) for k, v in (overrides or {}).items()}
            env.update(overrides)
            result = subprocess.run(['/bin/sh', '-c', script], env=env, text=True, capture_output=True, check=True)
            data = json.loads(result.stdout)
            expected = overrides.get(
                'TMPDIR', overrides.get('TMP', overrides.get('TEMP', str(home / '.local/state/open-terminal/tmp')))
            )
            self.assertEqual(data['env']['TMPDIR'], expected)
            self.assertEqual(data['env']['TMP'], overrides.get('TMP', expected))
            self.assertEqual(data['env']['TEMP'], overrides.get('TEMP', expected))
            self.assertEqual(str(Path(data['file']).parent), expected)
            self.assertEqual(data['child_tmp'], expected)
            self.assertEqual(data['log_dir'], env.get('OPEN_TERMINAL_LOG_DIR'))

    def test_default_disk_backed_temps_and_child_inheritance(self):
        self.probe()

    def test_preserve_overrides_and_create_directories(self):
        self.probe({'TMPDIR': 'custom primary', 'TMP': 'custom-tmp', 'TEMP': 'custom-temp'})

    def test_legacy_temp_overrides(self):
        for name in ('TMP', 'TEMP'):
            with self.subTest(name=name):
                self.probe({name: 'legacy-temp'})


if __name__ == '__main__':
    unittest.main()
