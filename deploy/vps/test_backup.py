"""Exercise backup failures and retention without accessing the host's Docker."""

import fcntl
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import unittest


FAKE_DOCKER = """#!/usr/bin/python3
import io
import os
from pathlib import Path
import sys
import tarfile

args = sys.argv[1:]
failure = os.environ.get('BACKUP_TEST_FAILURE')
if args[:4] == ['compose', 'exec', '-T', 'postgres']:
    if args[4] == 'sh':
        sys.stdout.buffer.write(b'PGDMP fixture')
        sys.exit(1 if failure == 'dump' else 0)
    if args[4:] == ['pg_restore', '--list']:
        assert sys.stdin.buffer.read() == b'PGDMP fixture'
        sys.exit(1 if failure == 'validation' else 0)
elif args == ['compose', 'ps', '-q', 'mologate']:
    print('fixture-container')
elif args[:1] == ['inspect']:
    print('fixture-volume')
elif args[:1] == ['run']:
    if failure == 'archive':
        sys.exit(1)
    output = next(arg[:-5] for arg in args if arg.endswith(':/out'))
    with tarfile.open(Path(output) / 'data.tgz', 'w:gz') as archive:
        info = tarfile.TarInfo('uploads/example.txt')
        data = b'uploaded file fixture'
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
else:
    raise AssertionError(args)
"""


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='mologate-backup-test-')
        self.addCleanup(self.tmp.cleanup)
        self.stack = Path(self.tmp.name) / 'stack with spaces'
        self.stack.mkdir()
        shutil.copyfile(Path(__file__).with_name('backup.sh'), self.stack / 'backup.sh')
        for name in ['compose.yaml', 'Caddyfile']:
            (self.stack / name).write_text('fixture\n')
        (self.stack / '.env').write_text('WEBUI_SECRET_KEY=recovery-fixture\n')
        (self.stack / 'caddy-sites').mkdir()
        self.old = self.stack / 'backups' / '2020-01-01T00-00-00Z'
        self.old.mkdir(parents=True)
        (self.old / 'prior-backup').write_text('keep unless a new backup succeeds')
        old_time = time.time() - 9 * 86400
        os.utime(self.old, (old_time, old_time))
        bin_dir = Path(self.tmp.name) / 'bin'
        bin_dir.mkdir()
        docker = bin_dir / 'docker'
        docker.write_text(FAKE_DOCKER)
        docker.chmod(0o755)
        self.env = dict(os.environ, PATH=f'{bin_dir}:{os.environ["PATH"]}')

    def run_backup(self, failure=None):
        env = dict(self.env)
        if failure:
            env['BACKUP_TEST_FAILURE'] = failure
        return subprocess.run(
            ['bash', str(self.stack / 'backup.sh')],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def assert_previous_backup_preserved(self, result):
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list((self.stack / 'backups').iterdir()), [self.old])
        self.assertTrue((self.old / 'prior-backup').exists())
        self.assertNotIn('backup complete', result.stdout)

    def test_success_publishes_private_recoverable_set_and_prunes_old_backup(self):
        result = self.run_backup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.old.exists())
        sets = list((self.stack / 'backups').iterdir())
        self.assertEqual(len(sets), 1)
        saved = sets[0]
        self.assertFalse(saved.name.startswith('.partial'))
        self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o700)
        self.assertEqual(
            {p.name for p in saved.iterdir()},
            {'db.dump', 'data.tgz', 'config.tgz', 'SHA256SUMS'},
        )
        for line in (saved / 'SHA256SUMS').read_text().splitlines():
            checksum, name = line.split()
            self.assertEqual(hashlib.sha256((saved / name).read_bytes()).hexdigest(), checksum)
        for p in saved.iterdir():
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        with tarfile.open(saved / 'config.tgz') as archive:
            self.assertEqual(
                archive.extractfile('.env').read(), b'WEBUI_SECRET_KEY=recovery-fixture\n'
            )
        with tarfile.open(saved / 'data.tgz') as archive:
            self.assertEqual(archive.extractfile('uploads/example.txt').read(), b'uploaded file fixture')
        self.assertIn('backup complete', result.stdout)

    def test_partial_database_dump_preserves_previous_backup(self):
        self.assert_previous_backup_preserved(self.run_backup('dump'))

    def test_invalid_database_dump_preserves_previous_backup(self):
        self.assert_previous_backup_preserved(self.run_backup('validation'))

    def test_failed_data_archive_preserves_previous_backup(self):
        self.assert_previous_backup_preserved(self.run_backup('archive'))

    def test_missing_recovery_config_preserves_previous_backup(self):
        (self.stack / '.env').unlink()
        self.assert_previous_backup_preserved(self.run_backup())

    def test_concurrent_backup_is_rejected(self):
        with (self.stack / '.backup.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_backup()
        self.assert_previous_backup_preserved(result)
        self.assertIn('Another Mologate backup is running', result.stderr)


if __name__ == '__main__':
    unittest.main()
