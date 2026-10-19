"""Mologate: each chat's own container, on one of the team's container nodes (docs/container-nodes.md).

A container node (``containers/node``) runs the containers of one machine. This module decides
which node holds each chat's container, balances new containers across nodes by load, moves a
stopped container to a quieter node when its own is busy, copies the user's attachments into it and
tells the agent about it. The agent and the file browser reach the container through the built-in
``mologate`` terminal connection, which routes every request to the right node.
"""

import asyncio
import logging
import os
import re
import time
from urllib.parse import quote

import aiohttp
from sqlalchemy import select

from open_webui.internal.db import get_async_db_context
from open_webui.models.chat_containers import ChatContainers
from open_webui.models.config import Config
from open_webui.utils.chat_id import is_saved_chat_id
from open_webui.utils.json_codec import JSONCodec

log = logging.getLogger(__name__)

CONNECTION_ID = 'mologate'
SERVER_TYPE = 'mologate'
WORKSPACE = '/home/user'
UPLOADS_DIR = f'{WORKSPACE}/uploads'
STOPPED_STATES = {'created', 'exited', 'dead'}

STATUS_TTL = 10
OFFLINE_TTL = 30
# A stopped container moves when its node is at least this busy and this much busier than the
# quietest other node (scores run from 0, idle, to about 1, full).
TRANSFER_BUSY = 0.6
TRANSFER_MARGIN = 0.25
TRANSFER_MAX_BYTES = 2 * 1024**3
WARMUP_INTERVAL = 15
CLEANUP_INTERVAL = 1800
# Nodes delete idle containers after 7 days; rows outlive them by a day, then go too.
ROW_TTL = 8 * 86400

CONNECTION = {
    'id': CONNECTION_ID,
    'name': 'Chat container',
    'server_type': SERVER_TYPE,
    'url': '',
    'path': '/openapi.json',
    'auth_type': 'none',
    'key': '',
    'config': {
        'contexts': {'chat': {'context_id': 'chat_id'}, 'automation': {'context_id': 'automation_id'}},
        'access_grants': [{'principal_type': 'user', 'principal_id': '*', 'permission': 'read'}],
        'chat_uploads': 'default',
    },
}


class NodeUnavailable(Exception):
    """No container node can take this chat right now."""


class NodeError(Exception):
    def __init__(self, status: int, code: str, detail: str):
        super().__init__(detail)
        self.status = status
        self.code = code


# -- nodes -------------------------------------------------------------------------------------


def parse_nodes(value) -> list[dict]:
    """Normalize a node list from the environment or the admin API."""
    if isinstance(value, str):
        value = JSONCodec.loads(value) if value.strip() else []
    nodes = []
    seen = set()
    for index, item in enumerate(value or []):
        if not isinstance(item, dict) or not item.get('url'):
            continue
        node_id = re.sub(r'[^A-Za-z0-9._-]', '-', str(item.get('id') or f'node-{index + 1}'))[:64]
        if node_id in seen:
            continue
        seen.add(node_id)
        nodes.append(
            {
                'id': node_id,
                'name': str(item.get('name') or node_id)[:100],
                'url': str(item['url']).rstrip('/'),
                'key': str(item.get('key') or ''),
                'enabled': item.get('enabled', True) is not False,
            }
        )
    return nodes


async def get_nodes(include_disabled: bool = False) -> list[dict]:
    nodes = parse_nodes(await Config.get('containers.nodes', []) or [])
    return nodes if include_disabled else [node for node in nodes if node['enabled']]


async def containers_enabled() -> bool:
    return bool(await Config.get('containers.enable', True)) and bool(await get_nodes())


def public_node(node: dict) -> dict:
    return {key: node[key] for key in ('id', 'name', 'url', 'enabled')}


_status_cache: dict[str, tuple[float, dict | None]] = {}


async def node_request(
    node: dict,
    method: str,
    path: str,
    *,
    json: dict | None = None,
    data=None,
    params: dict | None = None,
    timeout: float = 30,
) -> dict:
    url = f'{node["url"]}{path}'
    headers = {'Authorization': f'Bearer {node["key"]}'}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout, connect=5)) as session:
            async with session.request(
                method, url, headers=headers, json=json, data=data, params=params, allow_redirects=False
            ) as response:
                try:
                    body = await response.json(content_type=None)
                except (ValueError, aiohttp.ContentTypeError):
                    body = {}
                if response.status >= 400:
                    body = body if isinstance(body, dict) else {}
                    raise NodeError(
                        response.status, body.get('error', 'error'), body.get('detail') or f'HTTP {response.status}'
                    )
                return body if isinstance(body, dict) else {'items': body}
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        _status_cache[node['id']] = (time.monotonic(), None)
        raise NodeError(502, 'unreachable', f'Container node {node["name"]} is unreachable: {e or type(e).__name__}')


async def node_status(node: dict, fresh: bool = False) -> dict | None:
    """The node's load and limits, or None while it's unreachable. Cached for a few seconds."""
    cached = _status_cache.get(node['id'])
    now = time.monotonic()
    if cached and not fresh:
        age = now - cached[0]
        if (cached[1] is not None and age < STATUS_TTL) or (cached[1] is None and age < OFFLINE_TTL):
            return cached[1]
    try:
        status = await node_request(node, 'GET', '/v1/status', timeout=5)
    except NodeError:
        status = None
    _status_cache[node['id']] = (now, status)
    return status


async def node_statuses(nodes: list[dict], fresh: bool = False) -> dict[str, dict | None]:
    results = await asyncio.gather(*(node_status(node, fresh) for node in nodes))
    return {node['id']: status for node, status in zip(nodes, results)}


def node_score(status: dict) -> float:
    """How busy a node is: about 0 when idle, 1 when full. Container slots weigh most, then memory
    and CPU load, so new chats spread across nodes before any of them runs short."""
    active = status.get('active', 0) / max(1, status.get('max_active', 1))
    memory_total = max(1, status.get('memory_total', 1))
    memory = 1 - min(memory_total, status.get('memory_available', memory_total)) / memory_total
    cpu = min(2.0, status.get('load1', 0) / max(1, status.get('cpus', 1)))
    return round(0.5 * active + 0.3 * memory + 0.2 * cpu, 4)


def pick_node(
    nodes: list[dict], statuses: dict[str, dict | None], exclude=(), prefer_spare: bool = False
) -> dict | None:
    """The least busy node that is online and accepting containers.

    With ``prefer_spare``, a node holding a warmed-up spare container counts as a little less busy,
    so a new chat lands where its container is already running.
    """
    candidates = []
    for node in nodes:
        status = statuses.get(node['id'])
        if node['id'] in exclude or not status or not status.get('accepting', True):
            continue
        score = node_score(status)
        if prefer_spare and (status.get('containers') or {}).get('spare'):
            score -= 0.15
        candidates.append((score, node['id'], node))
    if not candidates:
        return None
    return min(candidates, key=lambda item: (item[0], item[1]))[2]


def should_transfer(current: dict | None, best: dict | None) -> bool:
    """Move a stopped container from ``current`` to ``best``? Only when its node is clearly busier."""
    if not current or not best:
        return False
    if not current.get('accepting', True):
        return True
    current_score, best_score = node_score(current), node_score(best)
    return current_score >= TRANSFER_BUSY and current_score - best_score >= TRANSFER_MARGIN


# -- contexts and placement ----------------------------------------------------------------------

_root_cache: dict[str, str] = {}


async def root_chat_id(chat_id: str) -> str:
    """A sub-agent works in its main chat's container, so follow sub-agent chats up to their root."""
    if chat_id in _root_cache:
        return _root_cache[chat_id]
    from open_webui.models.chats import Chat

    current = chat_id
    for _ in range(8):
        async with get_async_db_context() as db:
            meta = (await db.execute(select(Chat.meta).where(Chat.id == current))).scalar_one_or_none()
        parent = (meta or {}).get('parent_chat_id') if (meta or {}).get('type') == 'subagent' else None
        if not parent:
            break
        current = parent
    if len(_root_cache) > 5000:
        _root_cache.clear()
    _root_cache[chat_id] = current
    return current


async def context_for(metadata: dict | None) -> str | None:
    """The container context of a request: its automation, or its (root) saved chat."""
    metadata = metadata or {}
    if metadata.get('automation_id'):
        return f'automation:{metadata["automation_id"]}'
    chat_id = metadata.get('chat_id')
    if chat_id and is_saved_chat_id(chat_id):
        return f'chat:{await root_chat_id(chat_id)}'
    return None


async def context_from_terminal_id(context_id: str | None) -> str | None:
    """Map the terminal layer's ``chat:<id>`` context to the container context (sub-agents → root)."""
    if not context_id:
        return None
    if context_id.startswith('chat:'):
        chat_id = context_id[5:]
        return f'chat:{await root_chat_id(chat_id)}' if is_saved_chat_id(chat_id) else None
    if context_id.startswith('automation:'):
        return context_id
    return None


_locks: dict[str, asyncio.Lock] = {}


def _lock(context: str) -> asyncio.Lock:
    lock = _locks.get(context)
    if lock is None:
        if len(_locks) > 5000:
            for key in [key for key, value in _locks.items() if not value.locked()]:
                _locks.pop(key, None)
        lock = _locks[context] = asyncio.Lock()
    return lock


def sandbox_path(context: str) -> str:
    return f'/v1/sandboxes/{quote(context, safe=":")}'


async def placement(
    context: str,
    *,
    user_id: str | None = None,
    network: bool | None = None,
    create: bool = True,
) -> dict | None:
    """The context's row, choosing the least busy node for a new one."""
    row = await ChatContainers.get(context)
    if row is not None or not create:
        return row
    nodes = await get_nodes()
    node = pick_node(nodes, await node_statuses(nodes), prefer_spare=True)
    if node is None:
        raise NodeUnavailable('No container node is available right now.')
    chat_id = context[5:] if context.startswith('chat:') else None
    return await ChatContainers.create(
        context, node['id'], True if network is None else network, chat_id=chat_id, user_id=user_id
    )


async def transfer(context: str, source: dict, target: dict, network: bool) -> None:
    """Move a container's workspace from one node to another (it's stopped while it moves)."""
    started = time.monotonic()
    headers_source = {'Authorization': f'Bearer {source["key"]}'}
    headers_target = {'Authorization': f'Bearer {target["key"]}'}
    timeout = aiohttp.ClientTimeout(total=1800, connect=10, sock_read=300)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(f'{source["url"]}{sandbox_path(context)}/export', headers=headers_source) as exported:
            if exported.status != 200:
                raise NodeError(exported.status, 'export_failed', await exported.text())

            async def body():
                async for chunk in exported.content.iter_chunked(1024 * 1024):
                    yield chunk

            async with session.put(
                f'{target["url"]}{sandbox_path(context)}/import',
                params={'network': '1' if network else '0'},
                headers=headers_target,
                data=body(),
            ) as imported:
                if imported.status != 200:
                    raise NodeError(imported.status, 'import_failed', await imported.text())
    await ChatContainers.update(context, node_id=target['id'], moved_at=int(time.time()))
    try:
        await node_request(source, 'DELETE', sandbox_path(context))
    except NodeError as e:
        log.warning('Moved %s but could not delete it from %s: %s', context, source['id'], e)
    log.info('Moved %s from %s to %s in %.1fs', context, source['id'], target['id'], time.monotonic() - started)


async def prepare(
    context: str,
    *,
    user_id: str | None = None,
    network: bool | None = None,
    start: bool = True,
) -> tuple[dict, dict]:
    """Get the context's container ready for a turn and return (node, row).

    Places a new container on the least busy node. A stopped container whose node is much busier
    than another moves there first. If its node is gone or offline, the chat starts over in a fresh
    container elsewhere and the agent is told. A node that is full hands its stopped container on.
    """
    async with _lock(context):
        row = await placement(context, user_id=user_id, network=network)
        if network is not None and network != row['network']:
            await set_network(context, network, row=row)
            row['network'] = network
        nodes = await get_nodes()
        by_id = {node['id']: node for node in nodes}
        statuses = await node_statuses(nodes)
        node = by_id.get(row['node_id'])

        if node is None or statuses.get(node['id']) is None:
            old_name = node['name'] if node else row['node_id']
            replacement = pick_node(nodes, statuses, exclude={row['node_id']}, prefer_spare=True)
            if replacement is None:
                raise NodeUnavailable(f'Container node {old_name} is unreachable and no other node is available.')
            note = (
                f"The container node that held this chat's files ({old_name}) is unreachable, so this chat now "
                'has a fresh container; files from before are not in it.'
            )
            await ChatContainers.update(context, node_id=replacement['id'], note=note, files={})
            row.update(node_id=replacement['id'], note=note, files={})
            node = replacement
            log.warning('%s: node %s unreachable, starting over on %s', context, old_name, node['id'])
        elif len(nodes) > 1:
            info = None
            try:
                info = await node_request(node, 'GET', sandbox_path(context), timeout=5)
            except NodeError as e:
                if e.status != 404:
                    raise
            if info and info.get('state') in STOPPED_STATES:
                best = pick_node(nodes, statuses, exclude={node['id']})
                if (
                    best
                    and should_transfer(statuses[node['id']], statuses[best['id']])
                    and (info.get('disk_used') or 0) <= TRANSFER_MAX_BYTES
                ):
                    try:
                        await transfer(context, node, best, row['network'])
                        node = best
                    except NodeError as e:
                        log.warning('Could not move %s to %s: %s', context, best['id'], e)

        if start:
            try:
                await node_request(
                    node, 'POST', f'{sandbox_path(context)}/ensure', json={'network': row['network']}, timeout=90
                )
            except NodeError as e:
                if e.code != 'node_full':
                    raise
                best = pick_node(nodes, statuses, exclude={node['id']})
                if best is None:
                    raise NodeUnavailable('Every container node is full right now.') from e
                try:
                    await transfer(context, node, best, row['network'])
                except NodeError:
                    # Nothing to move yet (the container was never created): just place it there.
                    await ChatContainers.update(context, node_id=best['id'])
                node = best
                await node_request(
                    node, 'POST', f'{sandbox_path(context)}/ensure', json={'network': row['network']}, timeout=90
                )
            _status_cache.pop(node['id'], None)
        await ChatContainers.touch(context)
        row['node_id'] = node['id']
        return node, row


_owners: dict[str, str] = {}


async def owns_context(context: str, user_id: str) -> bool:
    """Whether a chat container belongs to this user. Containers are per chat, and the chat id can
    come from the browser, so only the chat's owner (whose sub-agents share it) may reach it."""
    if not context.startswith('chat:'):
        return True
    chat_id = context[5:]
    if _owners.get(chat_id) == user_id:
        return True
    from open_webui.models.chats import Chat

    async with get_async_db_context() as db:
        owner = (await db.execute(select(Chat.user_id).where(Chat.id == chat_id))).scalar_one_or_none()
    if owner is None:
        return False
    if len(_owners) > 5000:
        _owners.clear()
    _owners[chat_id] = owner
    return owner == user_id


async def route(context: str, user_id: str | None = None) -> tuple[str, dict]:
    """Base URL and headers that reach the context's Open Terminal (the node starts it on demand)."""
    if user_id is not None and not await owns_context(context, user_id):
        raise NodeUnavailable('This chat belongs to someone else.')
    row = await placement(context, user_id=user_id)
    nodes = {node['id']: node for node in await get_nodes()}
    node = nodes.get(row['node_id'])
    if node is None or await node_status(node) is None:
        node, row = await prepare(context, user_id=user_id, start=False)
    return f'{node["url"]}{sandbox_path(context)}/t', {'Authorization': f'Bearer {node["key"]}'}


async def set_network(context: str, enabled: bool, row: dict | None = None) -> dict:
    """Switch the container's internet access; applies at once if it already exists."""
    row = row or await ChatContainers.get(context)
    if row is None:
        raise NodeUnavailable('No container yet.')
    await ChatContainers.update(context, network=enabled)
    node = next((node for node in await get_nodes() if node['id'] == row['node_id']), None)
    if node is not None:
        try:
            await node_request(node, 'POST', f'{sandbox_path(context)}/network', json={'enabled': enabled})
        except NodeError as e:
            # Not created yet (404): the node applies the setting when it creates the container.
            if e.status != 404:
                raise
    return {**row, 'network': enabled}


async def describe(context: str) -> dict:
    """What the ⋯ menu shows: network, state, node and limits."""
    row = await ChatContainers.get(context)
    nodes = await get_nodes()
    if row is None:
        return {'exists': False, 'network': True, 'state': 'none', 'node': None, 'nodes': len(nodes)}
    node = next((node for node in nodes if node['id'] == row['node_id']), None)
    state, limits, info = 'none', None, {}
    if node is not None:
        status = await node_status(node)
        if status is None:
            state = 'unreachable'
        else:
            limits = status.get('limits')
            try:
                info = await node_request(node, 'GET', sandbox_path(context), timeout=5)
                state = info.get('state', 'none')
            except NodeError as e:
                state = 'none' if e.status == 404 else 'unreachable'
    return {
        'exists': True,
        'network': row['network'],
        'state': state,
        'node': {'id': node['id'], 'name': node['name']} if node else None,
        'nodes': len(nodes),
        'limits': limits,
        'disk_used': info.get('disk_used'),
        'files': [entry for entry in (row.get('files') or {}).values()],
    }


async def warm_up(context: str | None, user_id: str) -> None:
    """The user started typing: wake the chat's container, or start a spare for a new chat."""
    try:
        if context:
            row = await ChatContainers.get(context)
            if row is None:
                await prepare(context, user_id=user_id)
                return
            node = next((node for node in await get_nodes() if node['id'] == row['node_id']), None)
            if node is not None:
                await node_request(
                    node,
                    'POST',
                    f'{sandbox_path(context)}/ensure',
                    json={'network': row['network'], 'wait': False},
                    timeout=5,
                )
            return
        nodes = await get_nodes()
        node = pick_node(nodes, await node_statuses(nodes), prefer_spare=True)
        if node is not None:
            await node_request(node, 'POST', '/v1/spares', timeout=60)
    except (NodeError, NodeUnavailable) as e:
        log.info('Warm-up failed: %s', e)


async def delete_container(context: str) -> None:
    row = await ChatContainers.get(context)
    if row is None:
        return
    node = next((node for node in await get_nodes(include_disabled=True) if node['id'] == row['node_id']), None)
    if node is not None:
        try:
            await node_request(node, 'DELETE', sandbox_path(context))
        except NodeError as e:
            log.info('Could not delete %s on %s: %s', context, node['id'], e)
            if e.code == 'unreachable':
                return
    await ChatContainers.delete(context)


# -- attachments and what the agent is told --------------------------------------------------------


def _safe_name(name: str) -> str:
    name = os.path.basename((name or '').replace('\\', '/')).strip().replace('\x00', '')
    name = re.sub(r'[\r\n\t]', ' ', name)
    return (name or 'file')[:200]


def _unique_path(name: str, taken: set[str]) -> str:
    base, ext = os.path.splitext(name)
    path, counter = f'{UPLOADS_DIR}/{name}', 2
    while path in taken:
        path = f'{UPLOADS_DIR}/{base} ({counter}){ext}'
        counter += 1
    return path


async def sync_attachments(context: str, node: dict, row: dict, files: list[dict] | None, user) -> list[str]:
    """Copy the files attached to this request into the container; return the file ids copied now."""
    from open_webui.models.files import Files
    from open_webui.storage.provider import Storage

    copied = dict(row.get('files') or {})
    taken = {entry['path'] for entry in copied.values()}
    new_ids = []
    for item in files or []:
        if not isinstance(item, dict) or item.get('type', 'file') != 'file':
            continue
        file_id = str(item.get('id') or '')
        if not file_id or file_id in copied:
            continue
        file = await Files.get_file_by_id(file_id)
        if file is None or not file.path or (file.user_id != user.id and user.role != 'admin'):
            continue
        try:
            local_path = await asyncio.to_thread(Storage.get_file, file.path)
            size = os.path.getsize(local_path)
            path = _unique_path(_safe_name(file.filename), taken)
            with open(local_path, 'rb') as handle:
                await node_request(
                    node, 'PUT', f'{sandbox_path(context)}/files', params={'path': path}, data=handle, timeout=600
                )
        except (NodeError, OSError, RuntimeError) as e:
            log.warning('Could not copy file %s into %s: %s', file_id, context, e)
            continue
        taken.add(path)
        copied[file_id] = {'id': file_id, 'path': path, 'name': file.filename, 'size': size, 'at': int(time.time())}
        new_ids.append(file_id)
    if new_ids:
        await ChatContainers.update(context, files=copied)
        row['files'] = copied
    return new_ids


def _size(value: int | None) -> str:
    value = float(value or 0)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if value < 1024 or unit == 'TB':
            return f'{value:.1f}'.rstrip('0').rstrip('.') + f' {unit}'
        value /= 1024


def _rate(rate: str | None) -> str:
    match = re.fullmatch(r'(\d+(?:\.\d+)?)(ki|mi|gi|k|m|g)?(bps|bit)', (rate or '').lower())
    if not match:
        return rate or ''
    number, prefix, unit = match.groups()
    prefix = {'ki': 'Ki', 'mi': 'Mi', 'gi': 'Gi', 'k': 'K', 'm': 'M', 'g': 'G'}.get(prefix or '', '')
    return f'{number} {prefix}{"B" if unit == "bps" else "bit"}/s'


def container_prompt(row: dict, status: dict | None, new_file_ids: list[str]) -> str:
    """The system-prompt section that tells the agent about its container."""
    lines = [
        '<container>',
        'This chat has its own Linux container (Debian, Python 3.12, Node.js, git, build tools and common '
        f'command-line tools). Your terminal tools run there as root, starting in {WORKSPACE}. Its files stay '
        'between turns: it pauses when idle and is deleted after 7 days without use.',
    ]
    limits = (status or {}).get('limits') or {}
    if limits:
        parts = [f'{limits.get("cpus", 1):g} CPU', f'{_size(limits.get("memory"))} memory']
        if limits.get('disk'):
            parts.append(f'{_size(limits["disk"])} disk')
        if limits.get('bandwidth'):
            parts.append(f'{_rate(limits["bandwidth"])} network')
        lines.append('Limits: ' + ', '.join(parts) + '.')
    if row.get('network', True):
        lines.append(
            'Network: on. Commands can reach the internet, but not private addresses, the host or outgoing mail '
            '(port 25).'
        )
    else:
        lines.append(
            'Network: off. The user switched internet access off for this container, so commands that need the '
            'network (installing packages, downloads, API calls) will fail. Work offline, or ask the user to switch '
            'it on in the ⋯ menu next to the attach button.'
        )
    files = sorted((row.get('files') or {}).values(), key=lambda entry: entry.get('at', 0))
    if files:
        lines.append('Files the user attached, copied into the container:')
        for entry in files[-50:]:
            marker = ' (attached to this message)' if entry.get('id') in new_file_ids else ''
            lines.append(f'- {entry["path"]} ({_size(entry.get("size"))}){marker}')
    if row.get('note'):
        lines.append(row['note'])
    lines.append('</container>')
    return '\n'.join(lines)


async def turn_context(request, user, metadata: dict) -> str | None:
    """Ready the container for a turn: place, wake or move it, copy new attachments in, record the
    workspace for the change summary, and return what the agent should know. None when the request
    has no container context."""
    context = await context_for(metadata)
    if context is None:
        return None
    network = metadata.get('network_access')
    node, row = await prepare(context, user_id=user.id, network=network if isinstance(network, bool) else None)
    new_ids = await sync_attachments(context, node, row, metadata.get('files'), user)
    await start_change_tracking(context, node, metadata)
    prompt = container_prompt(row, await node_status(node), new_ids)
    if row.get('note'):
        await ChatContainers.update(context, note=None)
    return prompt


# -- what a response changed ----------------------------------------------------------------------


def _snapshot_id(message_id: str | None) -> str | None:
    if not message_id or not re.fullmatch(r'[A-Za-z0-9._-]{1,128}', str(message_id)):
        return None
    return str(message_id)


async def start_change_tracking(context: str, node: dict, metadata: dict) -> None:
    """Record the workspace as the response starts, so its end can list what changed."""
    snap_id = _snapshot_id(metadata.get('message_id'))
    if snap_id is None:
        return
    try:
        await node_request(node, 'POST', f'{sandbox_path(context)}/snapshots/{snap_id}', timeout=130)
        metadata['container_snapshot'] = {'context': context, 'id': snap_id}
    except NodeError as e:
        log.info('No change tracking for %s: %s', context, e)


async def collect_changes(metadata: dict) -> dict | None:
    """Files the finished response changed in the container, with lines added and removed."""
    snapshot = metadata.get('container_snapshot')
    if not snapshot:
        return None
    row = await ChatContainers.get(snapshot['context'])
    node = next((node for node in await get_nodes() if row and node['id'] == row['node_id']), None)
    if node is None:
        return None
    try:
        result = await node_request(
            node, 'GET', f'{sandbox_path(snapshot["context"])}/snapshots/{snapshot["id"]}/changes', timeout=130
        )
    except NodeError as e:
        log.info('Could not list changes for %s: %s', snapshot['context'], e)
        return None
    if result.get('error'):
        return None
    return {
        'files': result.get('files') or [],
        'total': result.get('total', 0),
        'added': result.get('added', 0),
        'removed': result.get('removed', 0),
        'truncated': bool(result.get('truncated')),
    }


def unavailable_prompt(error: Exception) -> str:
    return (
        "<container>\nThis chat's container is unavailable right now, so there are no terminal or file tools "
        f'this turn ({error}). Tell the user if the task needs them.\n</container>'
    )


# -- the built-in terminal connection and housekeeping ------------------------------------------------


def is_container_connection(connection: dict | None) -> bool:
    return bool(connection) and connection.get('server_type') == SERVER_TYPE


def normalize_connections(connections: list[dict], enabled: bool) -> list[dict]:
    """Keep the built-in connection present (when nodes exist) and its fields canonical; an admin can
    rename or switch it off, nothing else."""
    result = []
    found = False
    for connection in connections or []:
        if connection.get('id') == CONNECTION_ID or is_container_connection(connection):
            if found or not enabled:
                continue
            found = True
            result.append(
                {
                    **CONNECTION,
                    'name': connection.get('name') or CONNECTION['name'],
                    'enabled': connection.get('enabled', True),
                }
            )
        else:
            result.append(connection)
    if enabled and not found:
        result.insert(0, {**CONNECTION, 'enabled': True})
    return result


async def sync_connection() -> None:
    """Add, update or remove the built-in terminal connection to match the node settings."""
    connections = await Config.get('terminal_server.connections', []) or []
    normalized = normalize_connections(connections, await containers_enabled())
    if normalized != connections:
        await Config.upsert({'terminal_server.connections': normalized})


async def tool_server_data() -> dict:
    """The tool list for the built-in connection, from Open Terminal's API description on any node."""
    from open_webui.utils.tools import get_tool_servers_data

    nodes = await get_nodes()
    statuses = await node_statuses(nodes)
    for node in sorted(nodes, key=lambda node: statuses.get(node['id']) is None):
        data = await get_tool_servers_data(
            [
                {
                    'url': f'{node["url"]}/v1',
                    'path': '/openapi.json',
                    'key': node['key'],
                    'auth_type': 'bearer',
                    'spec_type': 'url',
                    'config': {'enable': True},
                    'info': {'id': CONNECTION_ID, 'name': CONNECTION['name']},
                }
            ]
        )
        data = [item for item in data if item and not item.get('error') and item.get('specs')]
        if data:
            return data[0]
    raise NodeUnavailable('No container node answered.')


_spec_cache: tuple[float, dict] | None = None


async def cached_tool_server_data() -> dict:
    global _spec_cache
    if _spec_cache and time.monotonic() - _spec_cache[0] < 3600:
        return _spec_cache[1]
    data = await tool_server_data()
    _spec_cache = (time.monotonic(), data)
    return data


async def cleanup_once() -> None:
    from open_webui.models.chats import Chat

    now = int(time.time())
    for row in await ChatContainers.all():
        context = row['context']
        gone = False
        if row['chat_id']:
            async with get_async_db_context() as db:
                exists = (await db.execute(select(Chat.id).where(Chat.id == row['chat_id']))).scalar_one_or_none()
            gone = exists is None
        if gone or now - row['last_active_at'] > ROW_TTL:
            await delete_container(context)


async def cleanup_loop() -> None:
    """Delete the containers of deleted chats, and forget containers the nodes already removed."""
    await asyncio.sleep(60)
    while True:
        try:
            if await get_nodes():
                await cleanup_once()
        except Exception:
            log.exception('Container cleanup failed')
        await asyncio.sleep(CLEANUP_INTERVAL)
