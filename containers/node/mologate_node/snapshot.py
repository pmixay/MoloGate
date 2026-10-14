"""Which files a response changed, with lines added and removed (docs/container-nodes.md).

``SCRIPT`` runs inside the chat's container with its own Python. When a response starts, it records
every file under /home/user (size, modification time, hash) and keeps a compressed copy of each
small text file; when the response is done, it compares the workspace with that record and gives
each changed text file's unified diff and lines added and removed, from git's diff (difflib where git
is missing). Diffs are capped per file and per response. Records are incremental: unchanged files
aren't read again. Dependency, cache and version-control folders are skipped.
"""

SCRIPT = r'''
import hashlib, json, os, subprocess, sys, tempfile, time, zlib

ROOT = '/home/user'
STORE = '/var/lib/mologate/snapshots'
SKIP_DIRS = {
    '.git', '.hg', '.svn', 'node_modules', '.cache', '.npm', '.local', '.config', '__pycache__',
    '.venv', 'venv', '.mypy_cache', '.pytest_cache', '.ruff_cache', '.tox', '.next', '.nuxt',
    '.gradle', '.cargo', '.rustup', '.m2', '.ipython', '.jupyter', '.vscode-server',
}
MAX_FILES = 20000
MAX_TEXT = 1024 * 1024
KEEP = 20
MAX_LISTED = 300
# Diffs kept with a response: at most this much per file, and in all.
DIFF_FILE = 32 * 1024
DIFF_TOTAL = 160 * 1024


def walk():
    files, truncated = {}, False
    for top, dirs, names in os.walk(ROOT):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not os.path.islink(os.path.join(top, d)))
        for name in names:
            path = os.path.join(top, name)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if not os.path.isfile(path) or os.path.islink(path):
                continue
            if len(files) >= MAX_FILES:
                return files, True
            files[os.path.relpath(path, ROOT)] = st
    return files, truncated


def read_entry(rel, st, previous):
    old = previous.get(rel)
    if old and old[0] == st.st_size and old[1] == st.st_mtime_ns:
        return old
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
    files, truncated = walk()
    manifest = {}
    for rel, st in files.items():
        entry = read_entry(rel, st, previous)
        if entry is not None:
            manifest[rel] = entry
    return manifest, truncated


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
    previous = (load('last.json') or {}).get('files', {})
    manifest, truncated = scan(previous)
    for rel, entry in manifest.items():
        store_object(rel, entry)
    record = {'at': time.time(), 'files': manifest, 'truncated': truncated}
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


def changes(snap_id):
    base = load(f'snaps/{snap_id}.json')
    if base is None:
        return {'error': 'unknown snapshot'}
    before = base['files']
    after, truncated = scan(before)
    result = []
    budget = DIFF_TOTAL
    for rel in sorted(set(before) | set(after)):
        old, new = before.get(rel), after.get(rel)
        if old and new and old[2] == new[2]:
            continue
        status = 'added' if not old else 'deleted' if not new else 'modified'
        item = {'path': rel, 'status': status, 'added': None, 'removed': None}
        result.append(item)
        # Text on both sides that is small enough to keep (a deleted or new file has one side).
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
    save('last.json', {'at': time.time(), 'files': after, 'truncated': truncated})
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
