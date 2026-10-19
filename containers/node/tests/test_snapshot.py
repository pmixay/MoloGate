"""Workspace snapshot policy regressions (real Git repositories)."""

import json
import subprocess
import sys

from mologate_node.snapshot import SCRIPT


def run(tmp_path, mode, limit=None):
    script = SCRIPT.replace("'/home/user'", repr(str(tmp_path / 'home'))).replace(
        "'/var/lib/mologate/snapshots'", repr(str(tmp_path / 'store'))
    )
    if limit is not None:
        script = script.replace('MAX_FILES = 20000', f'MAX_FILES = {limit}')
    out = subprocess.run([sys.executable, '-c', script, mode, 's1'], capture_output=True, check=True, timeout=30)
    return json.loads(out.stdout)


def git(path, *args):
    return subprocess.run(['git', '-C', str(path), *args], check=True, capture_output=True).stdout


def repo(path):
    path.mkdir(parents=True)
    git(path, 'init')
    git(path, 'config', 'user.name', 'Test')
    git(path, 'config', 'user.email', 'test@example.com')
    (path / 'source.py').write_text('before\n')
    (path / '.gitignore').write_text('deps/\ngenerated/\n')
    git(path, 'add', '.')
    git(path, 'commit', '-m', 'initial')
    return path


def clone(tmp_path):
    source = repo(tmp_path / 'origin')
    home = tmp_path / 'home'
    home.mkdir()
    run(tmp_path, 'snapshot')
    git(home, 'clone', str(source), 'project')
    return home / 'project'


def test_clean_clone_is_import_not_thousands_of_additions(tmp_path):
    clone(tmp_path)
    result = run(tmp_path, 'changes')
    assert (result['total'], result['added'], result['removed']) == (0, 0, 0)


def test_cloned_source_edits_additions_and_deletions(tmp_path):
    project = clone(tmp_path)
    (project / 'source.py').write_text('after\n')
    (project / 'new.py').write_text('new\n')
    (project / '.gitignore').unlink()
    result = run(tmp_path, 'changes')
    files = {f['path']: f for f in result['files']}
    assert set(files) == {'project/source.py', 'project/new.py', 'project/.gitignore'}
    assert files['project/source.py']['status'] == 'modified'
    assert files['project/source.py']['removed'] == 1
    assert files['project/.gitignore']['status'] == 'deleted'


def test_existing_repo_committed_changes_still_count(tmp_path):
    project = repo(tmp_path / 'home' / 'project')
    run(tmp_path, 'snapshot')
    (project / 'source.py').write_text('committed during turn\n')
    git(project, 'add', '.')
    git(project, 'commit', '-m', 'turn')
    result = run(tmp_path, 'changes')
    assert result['total'] == 1
    assert result['files'][0]['status'] == 'modified'
    assert (result['added'], result['removed']) == (1, 1)


def test_dependencies_ignored_but_tracked_ignored_files_included(tmp_path):
    project = repo(tmp_path / 'home' / 'project')
    (project / 'deps').mkdir()
    (project / 'deps' / 'tracked.py').write_text('before\n')
    git(project, 'add', '-f', 'deps/tracked.py')
    git(project, 'commit', '-m', 'tracked ignored')
    run(tmp_path, 'snapshot')
    (project / 'deps' / 'tracked.py').write_text('after\n')
    for directory in ['deps', 'generated', 'custom-env/lib/site-packages', 'odd-env/lib']:
        target = project / directory
        target.mkdir(parents=True, exist_ok=True)
        (target / 'noise.py').write_text('noise\n')
    (project / 'odd-env' / 'pyvenv.cfg').write_text('home = python\n')
    result = run(tmp_path, 'changes')
    assert [f['path'] for f in result['files']] == ['project/deps/tracked.py']


def test_partial_current_scan_does_not_report_false_deletions(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    for name in ['b', 'c', 'd']:
        (home / name).write_text(name)
    run(tmp_path, 'snapshot')
    (home / 'a').write_text('new')
    result = run(tmp_path, 'changes', limit=2)
    assert result['truncated']
    assert [f['path'] for f in result['files']] == ['a']


def test_partial_baseline_does_not_report_unseen_unchanged_files(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    for name in ['a', 'b', 'c']:
        (home / name).write_text(name)
    run(tmp_path, 'snapshot', limit=1)
    (home / 'a').write_text('edited')
    result = run(tmp_path, 'changes')
    assert [f['path'] for f in result['files']] == ['a']
    assert result['truncated']


def test_missing_or_legacy_baseline_is_not_empty_baseline(tmp_path):
    (tmp_path / 'home').mkdir()
    (tmp_path / 'home' / 'file').write_text('unchanged')
    assert run(tmp_path, 'changes')['total'] == 0
    store = tmp_path / 'store' / 'snaps'
    store.mkdir(parents=True)
    (store / 's1.json').write_text(json.dumps({'files': {}}))
    assert run(tmp_path, 'changes')['total'] == 0


def test_nested_worktree_git_file(tmp_path):
    parent = repo(tmp_path / 'home' / 'project')
    git(parent, 'worktree', 'add', '-b', 'nested', 'nested')
    nested = parent / 'nested'
    assert (nested / '.git').is_file()
    run(tmp_path, 'snapshot')
    (nested / 'source.py').write_text('worktree edit\n')
    result = run(tmp_path, 'changes')
    assert [f['path'] for f in result['files']] == ['project/nested/source.py']


def test_new_nested_repository_changes_not_suppressed_by_parent(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    run(tmp_path, 'snapshot')
    parent = repo(home / 'project')
    child = repo(parent / 'child')
    (child / 'source.py').write_text('nested dirty\n')
    result = run(tmp_path, 'changes')
    assert [f['path'] for f in result['files']] == ['project/child/source.py']
    assert result['files'][0]['status'] == 'modified'


def test_large_artifact_does_not_hide_unrelated_additions(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    with (home / 'large.dat').open('wb') as stream:
        stream.truncate(8 * 1024 * 1024 + 1)
    (home / 'existing.txt').write_text('before')
    assert not run(tmp_path, 'snapshot')['truncated']
    (home / 'new.py').write_text('new source')
    (home / 'existing.txt').write_text('after')
    result = run(tmp_path, 'changes')
    assert {f['path'] for f in result['files']} == {'existing.txt', 'new.py'}


def test_observed_import_changes_survive_later_commit(tmp_path):
    project = clone(tmp_path)
    assert run(tmp_path, 'changes')['total'] == 0
    (project / 'source.py').write_text('edited during turn\n')
    assert run(tmp_path, 'changes')['total'] == 1
    git(project, 'config', 'user.name', 'Test')
    git(project, 'config', 'user.email', 'test@example.com')
    git(project, 'add', '.')
    git(project, 'commit', '-m', 'model edit')
    result = run(tmp_path, 'changes')
    assert result['total'] == 1
    assert result['files'][0]['path'] == 'project/source.py'
    assert (result['added'], result['removed']) == (1, 1)
