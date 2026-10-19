"""Which files a response changed, with lines added and removed (docs/container-nodes.md).

``SCRIPT`` runs inside the chat's container with its own Python. When a response starts, it records
every file under /home/user (size, modification time, hash) and keeps a compressed copy of each
small text file; when the response is done, it compares the workspace with that record and gives
each changed text file's unified diff and lines added and removed, from git's diff (difflib where git
is missing). Diffs are capped per file and per response. Records are incremental: unchanged files
aren't read again. Git repositories use tracked plus non-ignored untracked paths (including
worktrees); dependency/cache folders and virtualenv markers are excluded for untracked files.
New repositories are imported at HEAD, reporting only their dirty/untracked paths. Changes committed
before a new repository is first observed cannot be distinguished from imported content. Partial
or incompatible baselines deliberately undercount rather than invent additions/deletions.
"""

SCRIPT = r'''
import hashlib, json, os, subprocess, sys, tempfile, time, zlib

ROOT = '/home/user'
STORE = '/var/lib/mologate/snapshots'
SKIP_DIRS = {
    '.git', '.hg', '.svn', 'node_modules', '.cache', '.npm', '.local', '.config', '__pycache__',
    '.venv', 'venv', '.mypy_cache', '.pytest_cache', '.ruff_cache', '.tox', '.next', '.nuxt',
    'site-packages', 'dist-packages', '.eggs', '.nox', '.gradle', '.cargo', '.rustup', '.m2', '.ipython', '.jupyter', '.vscode-server',
}
POLICY = 2
MAX_FILES = 20000
MAX_SECONDS = 15
MAX_GIT_BYTES = 8 * 1024 * 1024
MAX_HASH = 8 * 1024 * 1024
DEADLINE = time.monotonic() + 25
MAX_TEXT = 1024 * 1024
KEEP = 20
MAX_LISTED = 300
# Diffs kept with a response: at most this much per file, and in all.
DIFF_FILE = 32 * 1024
DIFF_TOTAL = 160 * 1024


def git(repo, *args):
    # Bound both execution time and the amount of output loaded into memory.
    if time.monotonic() >= DEADLINE:
        return None
    with tempfile.TemporaryFile() as out:
        try:
            proc = subprocess.run(['git', '-C', repo, *args], stdout=out,
                                  stderr=subprocess.DEVNULL,
                                  timeout=max(0.01, min(5, DEADLINE - time.monotonic())))
            if proc.returncode:
                return None
            out.seek(0)
            data = out.read(MAX_GIT_BYTES + 1)
            return data if len(data) <= MAX_GIT_BYTES else None
        except (OSError, subprocess.TimeoutExpired):
            return None


def excluded(path):
    name = os.path.basename(path)
    return (name in SKIP_DIRS or name.endswith('.dist-info') or
            os.path.isfile(os.path.join(path, 'pyvenv.cfg')))


def walk():
    files, repos, truncated = {}, {}, False
    deadline = min(DEADLINE, time.monotonic() + MAX_SECONDS)

    def add(path):
        nonlocal truncated
        if len(files) >= MAX_FILES or time.monotonic() >= deadline:
            truncated = True
            return
        try:
            st = os.lstat(path)
            if os.path.isfile(path) and not os.path.islink(path):
                files[os.path.relpath(path, ROOT)] = st
        except FileNotFoundError:
            pass  # Deleted tracked files remain in ls-files.
        except OSError:
            truncated = True

    def visit(top):
        nonlocal truncated
        if len(files) >= MAX_FILES or time.monotonic() >= deadline:
            truncated = True
            return
        if os.path.lexists(os.path.join(top, '.git')):
            tracked = git(top, 'ls-files', '-z', '-c')
            untracked = git(top, 'ls-files', '-z', '-o', '--exclude-standard')
            if tracked is None or untracked is None:
                truncated = True
                return  # Never fall back to counting ignored dependency trees.
            relrepo = os.path.relpath(top, ROOT)
            repos[relrepo] = True
            tracked = set(tracked.split(b'\0')) - {b''}
            listing = tracked | (set(untracked.split(b'\0')) - {b''})
            for raw in sorted(listing):
                name = os.fsdecode(raw)
                path = os.path.join(top, name)
                if raw not in tracked:
                    parts = name.rstrip('/').split('/')
                    if any(excluded(os.path.join(top, *parts[:i]))
                           for i in range(1, len(parts))):
                        continue
                if os.path.isdir(path):
                    # Gitlinks and untracked embedded repositories (including worktrees).
                    if not os.path.islink(path) and not excluded(path):
                        visit(path)
                else:
                    # Tracked files are included even if ignored or inside dependency dirs.
                    add(path)
                if truncated:
                    break
            return
        try:
            entries = sorted(os.scandir(top), key=lambda e: e.name)
        except OSError:
            truncated = True
            return
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                if not excluded(entry.path):
                    visit(entry.path)
            else:
                add(entry.path)
            if truncated:
                break

    visit(ROOT)
    return files, truncated, repos


def read_entry(rel, st, previous):
    old = previous.get(rel)
    if old and old[0] == st.st_size and old[1] == st.st_mtime_ns:
        return old
    if st.st_size > MAX_HASH:
        # A known large artifact must not mark namespace traversal incomplete and hide
        # unrelated additions. Metadata-only entries have no text diff or line counts.
        digest = hashlib.sha1(f'large:{st.st_size}:{st.st_mtime_ns}'.encode()).hexdigest()
        return [st.st_size, st.st_mtime_ns, digest, False]
    digest, head = hashlib.sha1(), b''
    try:
        with open(os.path.join(ROOT, rel), 'rb') as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                if not head:
                    head = chunk[:8192]
                digest.update(chunk)
    except OSError:
        return None
    text = b'\0' not in head
    if text:
        try:
            head.decode('utf-8')
        except UnicodeDecodeError as e:
            text = e.start > len(head) - 4
    return [st.st_size, st.st_mtime_ns, digest.hexdigest(), text]


def obj_path(sha):
    return os.path.join(STORE, 'objects', sha[:2], sha[2:])


def store_object(rel, entry):
    if not entry[3] or entry[0] > MAX_TEXT:
        return
    path = obj_path(entry[2])
    if os.path.exists(path):
        return
    try:
        with open(os.path.join(ROOT, rel), 'rb') as f:
            data = f.read(MAX_TEXT + 1)
    except OSError:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(zlib.compress(data, 6))
    os.replace(tmp, path)


def load(name):
    try:
        with open(os.path.join(STORE, name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save(name, data):
    path = os.path.join(STORE, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f, separators=(',', ':'))
    os.replace(tmp, path)


def scan(previous):
    files, truncated, repos = walk()
    manifest = {}
    deadline = min(DEADLINE, time.monotonic() + MAX_SECONDS)
    for rel, st in files.items():
        if time.monotonic() >= deadline:
            truncated = True
            break
        entry = read_entry(rel, st, previous)
        if entry is not None:
            manifest[rel] = entry
        else:
            truncated = True
    return manifest, truncated, repos


def gc():
    snaps_dir = os.path.join(STORE, 'snaps')
    snaps = sorted(
        (os.path.join(snaps_dir, n) for n in os.listdir(snaps_dir) if n.endswith('.json')),
        key=os.path.getmtime,
    )
    for path in snaps[:-KEEP]:
        os.unlink(path)
    keep = set()
    for path in snaps[-KEEP:]:
        try:
            with open(path) as f:
                keep.update(entry[2] for entry in json.load(f)['files'].values())
        except (OSError, ValueError, KeyError):
            pass
    objects = os.path.join(STORE, 'objects')
    for prefix in os.listdir(objects) if os.path.isdir(objects) else []:
        for name in os.listdir(os.path.join(objects, prefix)):
            if prefix + name not in keep:
                os.unlink(os.path.join(objects, prefix, name))


def snapshot(snap_id):
    last = load('last.json') or {}
    previous = last.get('files', {}) if last.get('policy') == POLICY else {}
    manifest, truncated, repos = scan(previous)
    for rel, entry in manifest.items():
        store_object(rel, entry)
    record = {'policy': POLICY, 'at': time.time(), 'files': manifest, 'truncated': truncated, 'repos': repos}
    save(f'snaps/{snap_id}.json', record)
    save('last.json', record)
    gc()
    return {'files': len(manifest), 'truncated': truncated}


def old_text(entry):
    try:
        with open(obj_path(entry[2]), 'rb') as f:
            return zlib.decompress(f.read())
    except (OSError, zlib.error):
        return None



def diff(old, new_path, rel):
    """Unified diff from old bytes (None: the file is new) to a file (None: it was deleted), with
    lines added and removed; (None, None, None) when git calls it binary."""
    old_path = '/dev/null'
    if old is not None:
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(old)
            old_path = f.name
    try:
        proc = subprocess.run(
            ['git', 'diff', '--no-index', '--no-color', '--no-ext-diff', '-U3', '--',
             old_path, new_path or '/dev/null'],
            capture_output=True, timeout=20,
        )
        out = proc.stdout.decode('utf-8', 'replace')
        # 1: the files differ, 0: they don't; anything else is git failing.
        if proc.returncode in (0, 1):
            if 'Binary files' in out.split('@@', 1)[0]:
                return None, None, None
            hunks = out[out.index('@@'):] if '@@' in out else ''
            return finish(hunks.splitlines(), old is None, new_path is None, rel)
    except (OSError, subprocess.TimeoutExpired):
        pass
    finally:
        if old is not None:
            os.unlink(old_path)
    import difflib
    try:
        new = b''
        if new_path:
            with open(new_path, 'rb') as f:
                new = f.read()
        a = (old or b'').decode('utf-8', 'replace').splitlines()
        b = new.decode('utf-8', 'replace').splitlines()
    except OSError:
        return None, None, None
    lines = list(difflib.unified_diff(a, b, lineterm=''))[2:]
    return finish(lines, old is None, new_path is None, rel)


def finish(hunks, created, deleted, rel):
    added = sum(1 for line in hunks if line.startswith('+'))
    removed = sum(1 for line in hunks if line.startswith('-'))
    header = ['--- ' + ('/dev/null' if created else 'a/' + rel), '+++ ' + ('/dev/null' if deleted else 'b/' + rel)]
    return added, removed, '\n'.join(header + hunks) if hunks else ''


def imported_baseline(repo_rel, ref):
    """A newly discovered repository starts at HEAD, not at an empty directory.

    Read old blobs in one batch, rather than launching git once per file.
    """
    repo = os.path.join(ROOT, repo_rel)
    changed = git(repo, 'diff', '--no-ext-diff', '--name-only', '-z', ref, '--')
    untracked = git(repo, 'ls-files', '-z', '-o', '--exclude-standard')
    tree = git(repo, 'ls-tree', '-r', '-z', ref)
    if changed is None or untracked is None or tree is None:
        return None  # Unborn HEAD or failed git: conservatively import everything.
    names = set(changed.split(b'\0')) | set(untracked.split(b'\0'))
    names.discard(b'')
    blobs = {}
    for line in tree.split(b'\0'):
        if not line:
            continue
        meta, name = line.split(b'\t', 1)
        mode, kind, sha = meta.split()
        if name in names and kind == b'blob':
            blobs[name] = sha
    old = {}
    if blobs:
        # cat-file --batch accepts object IDs, so even unusual filenames are safe.
        with tempfile.TemporaryFile() as inp, tempfile.TemporaryFile() as out:
            inp.write(b''.join(sha + b'\n' for sha in blobs.values()))
            inp.seek(0)
            try:
                proc = subprocess.run(['git', '-C', repo, 'cat-file', '--batch'],
                                      stdin=inp, stdout=out, stderr=subprocess.DEVNULL,
                                      timeout=max(0.01, min(5, DEADLINE - time.monotonic())))
                if proc.returncode or out.tell() > MAX_GIT_BYTES:
                    return None
                out.seek(0)
                for name in blobs:
                    header = out.readline().split()
                    size = int(header[2])
                    data = out.read(size)
                    out.read(1)
                    digest = hashlib.sha1(data).hexdigest()
                    text = b'\0' not in data
                    entry = [size, 0, digest, text]
                    rel = os.path.normpath(os.path.join(repo_rel, os.fsdecode(name)))
                    old[rel] = entry
                    if text and size <= MAX_TEXT:
                        path = obj_path(digest)
                        os.makedirs(os.path.dirname(path), exist_ok=True)
                        with open(path, 'wb') as f:
                            f.write(zlib.compress(data))
            except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
                return None
    paths = {os.path.normpath(os.path.join(repo_rel, os.fsdecode(n))) for n in names}
    return old, paths


def changes(snap_id):
    base = load(f'snaps/{snap_id}.json')
    if base is None or base.get('policy') != POLICY:
        return {'files': [], 'total': 0, 'added': 0, 'removed': 0, 'truncated': True,
                'error': 'missing or incompatible snapshot'}
    before = dict(base['files'])
    after, truncated, repos = scan(before)
    suppressed = set()
    imports = base.setdefault('imports', {})
    for repo in sorted(set(repos) - set(base.get('repos', {}))):
        prefix = '' if repo == '.' else repo + os.sep
        # A parent repo must not suppress a nested repo's dirty paths.
        children = [child + os.sep for child in repos
                    if child != repo and child.startswith(prefix)]
        def belongs(rel):
            return rel.startswith(prefix) and not any(rel.startswith(child) for child in children)
        if repo not in imports:
            head = git(os.path.join(ROOT, repo), 'rev-parse', '--verify', 'HEAD')
            imports[repo] = head.decode().strip() if head else None
            # Pin the first-observed commit for this response: later commits must not
            # erase dirty edits already shown to the user.
            save(f'snaps/{snap_id}.json', base)
        imported = imported_baseline(repo, imports[repo]) if imports[repo] else None
        candidates = {rel for rel in after if belongs(rel) and rel not in before}
        if imported is None:
            suppressed.update(candidates)
            truncated = True
        else:
            old, changed = imported
            suppressed.update(candidates - changed)
            before.update({rel: entry for rel, entry in old.items()
                           if belongs(rel) and rel not in before})
    result = []
    budget = DIFF_TOTAL
    deadline = min(DEADLINE, time.monotonic() + MAX_SECONDS)
    for rel in sorted(set(before) | set(after)):
        old, new = before.get(rel), after.get(rel)
        if rel in suppressed:
            continue
        # A partial baseline cannot establish that an unseen file was newly created.
        if not old and base.get('truncated'):
            continue
        # Exclusion, a read failure or an incomplete scan is not a deletion.
        if old and not new and os.path.lexists(os.path.join(ROOT, rel)):
            continue
        if old and new and old[2] == new[2]:
            continue
        status = 'added' if not old else 'deleted' if not new else 'modified'
        item = {'path': rel, 'status': status, 'added': None, 'removed': None}
        result.append(item)
        # Text on both sides that is small enough to keep (a deleted or new file has one side).
        if time.monotonic() >= deadline:
            truncated = True
            continue
        old_data = (old_text(old) if old[3] else None) if old else b''
        new_ok = not new or (new[3] and new[0] <= MAX_TEXT)
        if old_data is None or not new_ok:
            continue
        added, removed, text = diff(old_data if old else None, os.path.join(ROOT, rel) if new else None, rel)
        item['added'], item['removed'] = added, removed
        if text is None or len(result) > MAX_LISTED:
            continue
        limit = min(DIFF_FILE, budget)
        if len(text) > limit:
            text = text[: text.rfind('\n', 0, limit) + 1 if limit > 0 else 0].rstrip('\n')
            item['diff_truncated'] = True
        if text:
            item['diff'] = text
            budget -= len(text)
    save('last.json', {'policy': POLICY, 'at': time.time(), 'files': after, 'truncated': truncated, 'repos': repos})
    return {
        'files': result[:MAX_LISTED],
        'total': len(result),
        'added': sum(item['added'] or 0 for item in result),
        'removed': sum(item['removed'] or 0 for item in result),
        'truncated': truncated or base.get('truncated', False) or len(result) > MAX_LISTED,
    }


mode, snap_id = sys.argv[1], sys.argv[2]
print(json.dumps(snapshot(snap_id) if mode == 'snapshot' else changes(snap_id)))
'''
