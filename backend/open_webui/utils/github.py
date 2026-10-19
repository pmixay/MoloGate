"""GitHub device login lives entirely in the chat sandbox, never on the backend host.

Open Terminal 0.14: POST /execute?wait=1 with {command,cwd}, then
GET /execute/{id}/status?wait=1. Only the controller's allowlisted JSON is returned.
"""

import asyncio
import json
import re
import shlex

import aiohttp
from fastapi import HTTPException

from open_webui.utils import containers

# This small controller is executed by the sandbox's Python. Its durable state, raw gh
# output and credentials never leave the sandbox. flock also serializes backend workers.
CONTROLLER = r'''
import fcntl, json, os, pathlib, re, shutil, signal, subprocess, sys, time, uuid
root = pathlib.Path('/home/user/.local/state/mologate-github')
root.mkdir(mode=0o700, parents=True, exist_ok=True)
os.chmod(root, 0o700)
os.umask(0o077)
lock = open(root / 'lock', 'a')
fcntl.flock(lock, fcntl.LOCK_EX)
op, network = sys.argv[1], sys.argv[2] == '1'
env = dict(os.environ)
for key in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN', 'GH_DEBUG'):
    env.pop(key, None)
env.update(GH_CONFIG_DIR='/home/user/.config/gh', GH_PROMPT_DISABLED='1', GH_HOST='github.com', BROWSER='true', GH_BROWSER='true', GH_NO_UPDATE_NOTIFIER='1')
def read(name, default=''):
    try: return (root / name).read_text()[:65536]
    except OSError: return default
def run(args, timeout=10):
    try:
        return subprocess.run(['/usr/bin/gh', *args], input='\n', text=True, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired): return None
def save(name, value):
    (root / name).write_text(value)
def state():
    try: return json.loads(read('state', '{}'))
    except ValueError: return {}
def alive(s):
    try:
        # Check identity before signalling: a stopped container may have reused this PID.
        return s['nonce'].encode() in pathlib.Path('/proc/%d/cmdline' % s['pid']).read_bytes()
    except (OSError, KeyError, TypeError): return False
def clear():
    s = state()
    if alive(s):
        try: os.killpg(s['pid'], signal.SIGKILL)
        except ProcessLookupError: pass
    for name in ('state', 'output', 'done'):
        (root / name).unlink(missing_ok=True)
def login():
    p = run(['api', 'user', '--hostname', 'github.com', '--jq', '.login'])
    value = p.stdout.strip() if p and p.returncode == 0 else ''
    return value if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}', value) else None
worker = r"""
import os, pathlib, subprocess, sys
root = pathlib.Path(sys.argv[1])
result = 1
try:
    with open(root / 'output', 'w') as out:
        p = subprocess.run(['/usr/bin/gh', 'auth', 'login', '--web', '--hostname', 'github.com',
            '--git-protocol', 'https'], input='\n', text=True, stdout=out, stderr=out,
            timeout=900)
        result = p.returncode
        if result == 0:
            result = subprocess.run(['/usr/bin/gh', 'auth', 'setup-git', '--hostname', 'github.com'],
                stdout=out, stderr=out, timeout=15).returncode
except Exception:
    pass
(root / 'done').write_text(str(result))
"""
result = {'connected': False, 'pending': False}
if not os.path.isfile('/usr/bin/gh'):
    result['error'] = 'missing_cli'
elif op == 'disconnect':
    clear()
    p = run(['auth', 'logout', '--hostname', 'github.com'])
    (root / 'login').unlink(missing_ok=True)
    # logout exits nonzero when already logged out. Never print its output.
    if p is None or (p.returncode != 0 and pathlib.Path('/home/user/.config/gh/hosts.yml').exists()):
        result['error'] = 'logout_failed'
else:
    s = state()
    if s and (time.time() - s.get('started', 0) >= 900 or not alive(s)):
        clear()
        s = {}
    # An offline menu can show the last verified login, but cannot start auth.
    user = login() if network else read('login').strip()
    if user:
        if network:
            p = run(['auth', 'setup-git', '--hostname', 'github.com'])
            if not p or p.returncode != 0:
                result['error'] = 'setup_failed'
            else:
                save('login', user)
                result = {'connected': True, 'login': user, 'pending': False}
        else:
            result = {'connected': True, 'login': user, 'pending': False}
        if result.get('connected'): clear()
    elif op == 'connect' and not network:
        result['error'] = 'network_off'
    else:
        (root / 'login').unlink(missing_ok=True)
        if op == 'connect' and not s:
            clear()
            nonce = 'mologate-github-' + uuid.uuid4().hex
            p = subprocess.Popen([sys.executable, '-c', worker, str(root), nonce],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, env=env)
            s = {'pid': p.pid, 'nonce': nonce, 'started': time.time()}
            save('state', json.dumps(s))
            # Give gh a short chance to print the device code; subsequent polls handle slow networks.
            for _ in range(10):
                if 'code:' in read('output') or (root / 'done').exists(): break
                time.sleep(0.1)
        if s and alive(s) and not (root / 'done').exists():
            result['pending'] = True
            match = re.search(r'one-time code:\s*([A-Z0-9]{4}-[A-Z0-9]{4})\b', read('output'))
            if match:
                result.update(code=match.group(1), verification_uri='https://github.com/login/device')
        elif s:
            clear()
print(json.dumps(result))
'''


def sanitized(value: dict) -> dict:
    """Treat even sandbox output as untrusted: never forward arbitrary fields/logs."""
    result = {'connected': value.get('connected') is True, 'pending': value.get('pending') is True}
    login = value.get('login')
    if result['connected'] and isinstance(login, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}', login):
        result['login'] = login
    else:
        result['connected'] = False
    if result['connected']:
        result['pending'] = False
    code = value.get('code')
    if result['pending'] and isinstance(code, str) and re.fullmatch(r'[A-Z0-9]{4}-[A-Z0-9]{4}', code):
        result.update(code=code, verification_uri='https://github.com/login/device')
    return result


async def github(context: str, user_id: str, operation: str) -> dict:
    """Execute the fixed controller using the existing per-context terminal proxy."""
    try:
        row = await containers.placement(context, user_id=user_id, create=operation == 'connect')
        if row is None:
            return {'connected': False, 'pending': False}
        network = row.get('network', True)
        if operation == 'connect' and not network:
            raise HTTPException(409, 'Enable internet access for this container before connecting GitHub.')
        # _chat_context has already checked ownership (and permits admins). Do not check
        # it a second time in route(), which would incorrectly reject admin access.
        base, headers = await containers.route(context)
        command = 'python3 -c ' + shlex.quote(CONTROLLER) + ' ' + shlex.quote(operation) + (' 1' if network else ' 0')
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45, connect=5)) as session:
            async def request(method, path, **kwargs):
                async with session.request(method, base + path, headers=headers, allow_redirects=False, **kwargs) as response:
                    if response.status != 200:
                        raise HTTPException(502, 'The container could not complete the GitHub request.')
                    return await response.json()

            result = await request('POST', '/execute', params={'wait': 1}, json={'command': command, 'cwd': '/home/user'})
            for _ in range(25):
                if result.get('status') != 'running':
                    break
                process_id = result.get('id', '')
                if not re.fullmatch(r'[A-Za-z0-9-]{1,100}', process_id):
                    raise ValueError('Invalid process id')
                result = await request('GET', f'/execute/{process_id}/status', params={'wait': 1})
            if result.get('status') == 'running':
                raise HTTPException(504, 'The GitHub request timed out. Try again shortly.')
            text = ''.join(entry.get('data', '') for entry in result.get('output', []) if entry.get('type') == 'output')
            value = json.loads(text)
            if not isinstance(value, dict):
                raise ValueError('Invalid result')
            errors = {
                'missing_cli': 'Rebuild the sandbox image with GitHub CLI support before connecting.',
                'network_off': 'Enable internet access for this container before connecting GitHub.',
                'setup_failed': 'GitHub Git authentication could not be configured. Try connecting again.',
                'logout_failed': 'GitHub could not be disconnected. Try again.',
            }
            if value.get('error') in errors:
                raise HTTPException(409 if value['error'] in ('missing_cli', 'network_off') else 502, errors[value['error']])
            return sanitized(value)
    except containers.NodeUnavailable:
        raise HTTPException(503, 'The chat container is unavailable right now.') from None
    except (containers.NodeError, aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError, KeyError):
        raise HTTPException(502, 'The container could not complete the GitHub request. Try again.') from None
