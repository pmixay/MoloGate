"""Creates, wakes, limits, moves and cleans up the chat containers on one node."""

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import time
from collections import defaultdict
from collections.abc import AsyncIterator

import aiohttp
import docker
from docker.errors import APIError, ImageNotFound, NotFound
from docker.types import IPAMConfig, IPAMPool, LogConfig, Ulimit

from mologate_node import __version__
from mologate_node.config import Settings
from mologate_node.guards import POLICE_UNAVAILABLE, bandwidth_script, firewall_script, host_bandwidth_script
from mologate_node.lifecycle import ACTIVE_STATES, eviction_order, lifecycle_action
from mologate_node.snapshot import SCRIPT as SNAPSHOT_SCRIPT
from mologate_node.state import Sandbox, StateStore

log = logging.getLogger('mologate_node')

CONTEXT_RE = re.compile(r'^(chat|automation|spare):[A-Za-z0-9._-]{1,128}$')
SNAPSHOT_ID_RE = re.compile(r'^[A-Za-z0-9._-]{1,128}$')
WORKSPACE = '/home/user'
SOCKET_NAME = 'terminal.sock'
LABEL = 'mologate.managed'
# Docker's default set minus NET_RAW (raw sockets, spoofing), MKNOD, AUDIT_WRITE and SETFCAP.
CAPABILITIES = [
    'CHOWN',
    'DAC_OVERRIDE',
    'FOWNER',
    'FSETID',
    'KILL',
    'SETGID',
    'SETUID',
    'SETPCAP',
    'NET_BIND_SERVICE',
    'SYS_CHROOT',
]
TICK_SECONDS = 15
DISK_CHECK_SECONDS = 600


class NodeError(Exception):
    status = 500
    code = 'node_error'


class NodeFull(NodeError):
    status = 503
    code = 'node_full'


class NotFoundError(NodeError):
    status = 404
    code = 'not_found'


class Busy(NodeError):
    status = 409
    code = 'busy'


class DiskLimit(NodeError):
    status = 507
    code = 'disk_limit'


def valid_context(context: str) -> bool:
    return bool(CONTEXT_RE.match(context or ''))


def _slug(context: str) -> str:
    return hashlib.sha256(context.encode()).hexdigest()[:20]


def _read_proc(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ''


def host_metrics(data_dir: str) -> dict:
    meminfo = {}
    for line in _read_proc('/proc/meminfo').splitlines():
        parts = line.split()
        if len(parts) >= 2:
            meminfo[parts[0].rstrip(':')] = int(parts[1]) * 1024
    load = _read_proc('/proc/loadavg').split()
    try:
        disk = shutil.disk_usage(data_dir)
        disk_total, disk_free = disk.total, disk.free
    except OSError:
        disk_total = disk_free = 0
    return {
        'cpus': os.cpu_count() or 1,
        'load1': float(load[0]) if load else 0.0,
        'memory_total': meminfo.get('MemTotal', 0),
        'memory_available': meminfo.get('MemAvailable', meminfo.get('MemFree', 0)),
        'disk_total': disk_total,
        'disk_free': disk_free,
    }


def safe_workspace_path(path: str) -> str | None:
    """A normalized absolute path inside /home/user, or None."""
    if not path or '\x00' in path or '\\' in path:
        return None
    normalized = os.path.normpath('/' + path.lstrip('/')) if not path.startswith(WORKSPACE) else os.path.normpath(path)
    if not normalized.startswith(WORKSPACE + '/'):
        return None
    return normalized


class SandboxManager:
    def __init__(self, settings: Settings, client: docker.DockerClient | None = None, store: StateStore | None = None):
        self.settings = settings
        os.makedirs(settings.socket_root, exist_ok=True)
        os.makedirs(settings.spool_dir, exist_ok=True)
        self.client = client or docker.from_env(timeout=120)
        self.store = store or StateStore(settings.state_path)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._capacity_lock = asyncio.Lock()
        self.activity: dict[str, float] = {}
        self.inflight: dict[str, int] = defaultdict(int)
        self.states: dict[str, str] = {}
        self.usage: dict[str, int] = {}
        self.exporting: set[str] = set()
        # Contexts stopped for using more than their disk limit while the node ran short of disk.
        self.disk_blocked: set[str] = set()
        self.features: dict[str, bool | None] = {'guards': None, 'bandwidth': None, 'disk_quota': None}
        self._sessions: dict[str, aiohttp.ClientSession] = {}
        self._spec: dict | None = None
        self._last_disk_check = 0.0
        self._last_urgent_disk_check = 0.0
        self._tasks: list[asyncio.Task] = []

    # -- setup ---------------------------------------------------------------------------------

    async def start(self) -> None:
        await asyncio.to_thread(self._ensure_network)
        if self.settings.guards:
            ok, output = await self.run_host_script(firewall_script(self.settings))
            self.features['guards'] = ok
            if ok:
                log.info('Network guards applied: %s', output.strip().splitlines()[-1:] or '')
            else:
                log.error('Network guards could not be applied; sandboxes still start. %s', output.strip())
        else:
            self.features['guards'] = False
        await self.tick()
        self._tasks.append(asyncio.create_task(self._loop()))
        self._tasks.append(asyncio.create_task(asyncio.to_thread(self._pull_image_if_missing)))

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self.flush_activity()
        for session in self._sessions.values():
            await session.close()

    def _pull_image_if_missing(self) -> None:
        try:
            self.client.images.get(self.settings.image)
        except ImageNotFound:
            log.info('Pulling %s', self.settings.image)
            try:
                self.client.images.pull(self.settings.image)
            except APIError as e:
                log.error('Could not pull %s: %s', self.settings.image, e)

    def _ensure_network(self) -> None:
        try:
            self.client.networks.get(self.settings.network)
            return
        except NotFound:
            pass
        ipam = None
        if self.settings.subnet:
            ipam = IPAMConfig(pool_configs=[IPAMPool(subnet=self.settings.subnet)])
        self.client.networks.create(
            self.settings.network,
            driver='bridge',
            options={
                'com.docker.network.bridge.name': self.settings.bridge,
                # Containers on the bridge can't reach each other.
                'com.docker.network.bridge.enable_icc': 'false',
                'com.docker.network.bridge.enable_ip_masquerade': 'true',
            },
            ipam=ipam,
            labels={LABEL: '1'},
        )
        log.info('Created network %s (bridge %s)', self.settings.network, self.settings.bridge)

    async def run_host_script(self, script: str) -> tuple[bool, str]:
        """Run a shell script in the host's network namespace with NET_ADMIN."""
        if self.settings.helper_image:

            def run() -> tuple[bool, str]:
                try:
                    output = self.client.containers.run(
                        self.settings.helper_image,
                        ['sh', '-c', script],
                        entrypoint=[],
                        network_mode='host',
                        cap_drop=['ALL'],
                        cap_add=['NET_ADMIN', 'NET_RAW'],
                        remove=True,
                        stdout=True,
                        stderr=True,
                        labels={'mologate.helper': '1'},
                    )
                    return True, output.decode(errors='replace')
                except docker.errors.ContainerError as e:
                    return False, (e.stderr or b'').decode(errors='replace') or str(e)
                except APIError as e:
                    return False, str(e)

            return await asyncio.to_thread(run)

        def run_local() -> tuple[bool, str]:
            try:
                result = subprocess.run(['sh', '-c', script], capture_output=True, text=True, timeout=60)
            except (OSError, subprocess.TimeoutExpired) as e:
                return False, str(e)
            return result.returncode == 0, result.stdout + result.stderr

        return await asyncio.to_thread(run_local)

    # -- helpers -------------------------------------------------------------------------------

    def lock(self, context: str) -> asyncio.Lock:
        return self._locks[context]

    def touch(self, context: str) -> None:
        self.activity[context] = time.time()

    def begin(self, context: str) -> None:
        self.inflight[context] += 1
        self.touch(context)

    def end(self, context: str) -> None:
        self.inflight[context] = max(0, self.inflight[context] - 1)
        self.touch(context)

    def flush_activity(self) -> None:
        pending, self.activity = self.activity, {}
        self.store.touch_many(pending)
        # Keep the latest values in memory too, so idle times stay exact between flushes.
        for context, ts in pending.items():
            self.activity.setdefault(context, ts)

    def last_active(self, sandbox: Sandbox) -> float:
        return max(sandbox.last_active, self.activity.get(sandbox.context, 0))

    def socket_path(self, sandbox: Sandbox) -> str:
        return os.path.join(sandbox.socket_dir, SOCKET_NAME)

    def session_for(self, sandbox: Sandbox) -> aiohttp.ClientSession:
        session = self._sessions.get(sandbox.context)
        if session is None or session.closed:
            session = aiohttp.ClientSession(
                connector=aiohttp.UnixConnector(path=self.socket_path(sandbox)),
                timeout=aiohttp.ClientTimeout(total=None, connect=10, sock_read=900),
                auto_decompress=False,
            )
            self._sessions[sandbox.context] = session
        return session

    async def _drop_session(self, context: str) -> None:
        session = self._sessions.pop(context, None)
        if session is not None:
            await session.close()

    def _container(self, sandbox: Sandbox):
        try:
            return self.client.containers.get(sandbox.container_id)
        except NotFound:
            return None

    def _active_count(self) -> int:
        return sum(1 for state in self.states.values() if state in ACTIVE_STATES)

    def _free_disk(self) -> int:
        return host_metrics(self.settings.data_dir)['disk_free']

    # -- create / start ------------------------------------------------------------------------

    def _create_container(self, context: str, name: str, volume: str, socket_dir: str, api_key: str, network: bool):
        kwargs = dict(
            image=self.settings.image,
            name=name,
            hostname='sandbox',
            detach=True,
            environment={'OPEN_TERMINAL_API_KEY': api_key},
            labels={LABEL: '1', 'mologate.node': self.settings.node_id, 'mologate.context': context},
            volumes={
                volume: {'bind': WORKSPACE, 'mode': 'rw'},
                socket_dir: {'bind': '/run/mologate', 'mode': 'rw'},
            },
            network=self.settings.network,
            nano_cpus=int(self.settings.cpus * 1e9),
            # Half the default weight: under contention the site and other services come first.
            cpu_shares=512,
            mem_limit=self.settings.memory,
            memswap_limit=self.settings.memory,
            pids_limit=self.settings.pids,
            shm_size='64m',
            tmpfs={'/tmp': f'rw,nosuid,nodev,size={self.settings.tmp_size}'},
            ulimits=[
                Ulimit(name='nofile', soft=self.settings.nofile, hard=self.settings.nofile * 2),
                Ulimit(name='core', soft=0, hard=0),
            ],
            cap_drop=['ALL'],
            cap_add=CAPABILITIES,
            security_opt=['no-new-privileges:true'],
            # Under memory pressure the kernel kills sandbox processes before anything else.
            oom_score_adj=500,
            log_config=LogConfig(type='json-file', config={'max-size': '1m', 'max-file': '1'}),
            dns=list(self.settings.dns),
        )
        if self.settings.runtime:
            kwargs['runtime'] = self.settings.runtime
        if self.features['disk_quota'] is not False and self.settings.disk > 0:
            try:
                container = self.client.containers.create(storage_opt={'size': str(self.settings.disk)}, **kwargs)
                self.features['disk_quota'] = True
            except APIError as e:
                if 'storage' not in str(e).lower() and 'quota' not in str(e).lower():
                    raise
                log.info('Disk quotas unsupported by this Docker storage driver; usage is watched instead.')
                self.features['disk_quota'] = False
                container = self.client.containers.create(**kwargs)
        else:
            container = self.client.containers.create(**kwargs)
        if not network:
            self.client.networks.get(self.settings.network).disconnect(container, force=True)
        return container

    async def _create(self, context: str, network: bool, spare: bool = False) -> Sandbox:
        if self._free_disk() < self.settings.min_free_disk:
            raise NodeFull('Not enough free disk on this node.')
        slug = _slug(f'{self.settings.node_id}/{context}')
        name = f'mologate-{slug}'
        volume = f'mologate-ws-{slug}'
        socket_dir = os.path.join(self.settings.socket_root, slug)
        os.makedirs(socket_dir, exist_ok=True)
        api_key = secrets.token_urlsafe(32)

        def create():
            return self._create_container(context, name, volume, socket_dir, api_key, network)

        container = await asyncio.to_thread(create)
        sandbox = Sandbox(
            context=context,
            container_id=container.id,
            name=name,
            volume=volume,
            socket_dir=socket_dir,
            api_key=api_key,
            network=network,
            spare=spare,
        )
        self.store.put(sandbox)
        self.states[context] = 'created'
        log.info('Created %s for %s', name, context)
        return sandbox

    async def _recreate(self, sandbox: Sandbox) -> Sandbox:
        """The container vanished (removed by hand?): make a new one on the same files."""

        def create():
            return self._create_container(
                sandbox.context, sandbox.name, sandbox.volume, sandbox.socket_dir, sandbox.api_key, sandbox.network
            )

        try:
            await asyncio.to_thread(lambda: self.client.containers.get(sandbox.name).remove(force=True))
        except NotFound:
            pass
        os.makedirs(sandbox.socket_dir, exist_ok=True)
        container = await asyncio.to_thread(create)
        sandbox.container_id = container.id
        self.store.put(sandbox)
        self.states[sandbox.context] = 'created'
        return sandbox

    async def _make_room(self, exclude: str) -> None:
        """Stop least recently used idle containers until one more can start."""
        if self._active_count() < self.settings.max_active:
            return
        now = time.time()
        candidates = [
            (sb.context, self.states.get(sb.context, 'missing'), now - self.last_active(sb), self.inflight[sb.context])
            for sb in self.store.all()
            if sb.context != exclude
        ]
        for context in eviction_order(candidates):
            if self._active_count() < self.settings.max_active:
                return
            sandbox = self.store.get(context)
            if sandbox is None:
                continue
            if sandbox.spare:
                await self.delete(context)
            else:
                await self._stop(sandbox)
            log.info('Stopped %s to make room', context)
        if self._active_count() >= self.settings.max_active:
            raise NodeFull('Every container on this node is busy.')

    async def _wait_ready(self, sandbox: Sandbox, timeout: float = 45) -> None:
        deadline = time.monotonic() + timeout
        session = self.session_for(sandbox)
        while time.monotonic() < deadline:
            try:
                async with session.get('http://sandbox/health', timeout=aiohttp.ClientTimeout(total=2)) as response:
                    if response.status == 200:
                        return
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
                pass
            await asyncio.sleep(0.2)
        raise NodeError('The container did not become ready in time.')

    async def apply_bandwidth(self, sandbox: Sandbox) -> None:
        if not sandbox.network or not self.settings.bandwidth:
            return
        container = await asyncio.to_thread(self._container, sandbox)
        if container is None:
            return

        def run():
            # A privileged exec gets NET_ADMIN for this one command; the container never has it.
            return container.exec_run(
                ['sh', '-c', bandwidth_script(self.settings.bandwidth)], privileged=True, user='root'
            )

        try:
            result = await asyncio.to_thread(run)
        except APIError as e:
            log.warning('Bandwidth limit failed for %s: %s', sandbox.context, e)
            self.features['bandwidth'] = False
            return
        output = result.output.decode(errors='replace')
        ok = result.exit_code == 0
        if result.exit_code == POLICE_UNAVAILABLE:
            match = re.search(r'iflink=(\d+)', output)
            if match:
                ok, output = await self.run_host_script(
                    host_bandwidth_script(self.settings.bandwidth, int(match.group(1)))
                )
        if not ok:
            log.warning('Bandwidth limit failed for %s: %s', sandbox.context, output.strip())
        self.features['bandwidth'] = ok

    async def _start(self, sandbox: Sandbox) -> None:
        container = await asyncio.to_thread(self._container, sandbox)
        if container is None:
            sandbox = await self._recreate(sandbox)
            container = await asyncio.to_thread(self._container, sandbox)
        status = container.status
        if status == 'running':
            self.states[sandbox.context] = 'running'
            return
        if status == 'paused':
            await asyncio.to_thread(container.unpause)
            self.states[sandbox.context] = 'running'
            return
        async with self._capacity_lock:
            await self._make_room(exclude=sandbox.context)
            await self._drop_session(sandbox.context)
            await asyncio.to_thread(container.start)
            self.states[sandbox.context] = 'running'
        await self._wait_ready(sandbox)
        await self.apply_bandwidth(sandbox)

    async def _adopt_spare(self, context: str, network: bool) -> Sandbox | None:
        for spare in self.store.all():
            if not spare.spare or self.states.get(spare.context) != 'running':
                continue
            async with self.lock(spare.context):
                if self.store.get(spare.context) is None:
                    continue
                self.store.rename(spare.context, context)
                self.states[context] = self.states.pop(spare.context, 'running')
                session = self._sessions.pop(spare.context, None)
                if session is not None:
                    self._sessions[context] = session
            sandbox = self.store.get(context)
            if sandbox.network != network:
                await self._set_network(sandbox, network)
            log.info('Gave spare %s to %s', sandbox.name, context)
            return sandbox
        return None

    async def ensure(self, context: str, network: bool | None = None, start: bool = True) -> Sandbox:
        """The context's container, created (or taken from the spares) and started as needed."""
        if context in self.exporting:
            raise Busy('This container is being moved to another node.')
        if start and context in self.disk_blocked:
            used = self.usage.get(context, 0) / 1024**3
            raise DiskLimit(
                f"This chat's container holds {used:.1f} GiB, over its {self.settings.disk / 1024**3:.0f} GiB limit, "
                'and the node is short of disk, so it stays stopped. An admin can free space on the node.'
            )
        async with self.lock(context):
            sandbox = self.store.get(context)
            if sandbox is None:
                wanted = True if network is None else network
                sandbox = (await self._adopt_spare(context, wanted) if start else None) or await self._create(
                    context, wanted
                )
            elif network is not None and network != sandbox.network:
                await self._set_network(sandbox, network)
                sandbox = self.store.get(context)
            if start:
                await self._start(sandbox)
            self.touch(context)
            return self.store.get(context)

    async def create_spare(self) -> dict:
        """Start a container ahead of a new chat, unless enough are waiting or the node is full."""
        spares = [sb for sb in self.store.all() if sb.spare]
        if len(spares) >= self.settings.max_spares:
            return {'spare': True, 'created': False}
        if self._active_count() >= self.settings.max_active:
            return {'spare': False, 'created': False}
        sandbox = await self._create(f'spare:{secrets.token_hex(6)}', network=True, spare=True)
        try:
            async with self.lock(sandbox.context):
                await self._start(sandbox)
        except NodeError:
            await self.delete(sandbox.context)
            raise
        return {'spare': True, 'created': True}

    # -- network -------------------------------------------------------------------------------

    async def _set_network(self, sandbox: Sandbox, enabled: bool) -> None:
        container = await asyncio.to_thread(self._container, sandbox)
        if container is not None:
            network = await asyncio.to_thread(self.client.networks.get, self.settings.network)
            paused = container.status == 'paused'
            if paused:
                await asyncio.to_thread(container.unpause)
            attached = self.settings.network in (container.attrs.get('NetworkSettings', {}).get('Networks') or {})
            try:
                if enabled and not attached:
                    await asyncio.to_thread(network.connect, container)
                elif not enabled and attached:
                    await asyncio.to_thread(network.disconnect, container, force=True)
            finally:
                if paused:
                    await asyncio.to_thread(container.pause)
        self.store.set_network(sandbox.context, enabled)
        sandbox.network = enabled
        if enabled and container is not None and container.status in ('running', 'paused'):
            if container.status == 'paused':
                await asyncio.to_thread(container.unpause)
                self.states[sandbox.context] = 'running'
            await self.apply_bandwidth(sandbox)
        log.info('Network %s for %s', 'on' if enabled else 'off', sandbox.context)

    async def set_network(self, context: str, enabled: bool) -> Sandbox:
        async with self.lock(context):
            sandbox = self.store.get(context)
            if sandbox is None:
                raise NotFoundError('No container for this chat yet.')
            await self._set_network(sandbox, enabled)
            self.touch(context)
            return self.store.get(context)

    # -- stop / delete -------------------------------------------------------------------------

    async def _pause(self, sandbox: Sandbox) -> None:
        container = await asyncio.to_thread(self._container, sandbox)
        if container is not None and container.status == 'running':
            await asyncio.to_thread(container.pause)
            self.states[sandbox.context] = 'paused'

    async def _stop(self, sandbox: Sandbox) -> None:
        container = await asyncio.to_thread(self._container, sandbox)
        if container is None:
            return
        if container.status == 'paused':
            await asyncio.to_thread(container.unpause)
        if container.status in ('running', 'paused', 'restarting'):
            await asyncio.to_thread(container.stop, timeout=5)
        await self._drop_session(sandbox.context)
        self.states[sandbox.context] = 'exited'

    async def pause(self, context: str) -> None:
        async with self.lock(context):
            sandbox = self.store.get(context)
            if sandbox is None:
                raise NotFoundError('No container for this chat.')
            await self._pause(sandbox)

    async def stop(self, context: str) -> None:
        async with self.lock(context):
            sandbox = self.store.get(context)
            if sandbox is None:
                raise NotFoundError('No container for this chat.')
            await self._stop(sandbox)

    async def delete(self, context: str) -> bool:
        async with self.lock(context):
            sandbox = self.store.get(context)
            if sandbox is None:
                return False
            await self._drop_session(context)

            def remove():
                try:
                    self.client.containers.get(sandbox.container_id).remove(force=True)
                except NotFound:
                    pass
                try:
                    self.client.volumes.get(sandbox.volume).remove(force=True)
                except NotFound:
                    pass

            await asyncio.to_thread(remove)
            shutil.rmtree(sandbox.socket_dir, ignore_errors=True)
            self.store.delete(context)
            self.states.pop(context, None)
            self.activity.pop(context, None)
            self.usage.pop(context, None)
            self.disk_blocked.discard(context)
            log.info('Deleted %s (%s)', sandbox.name, context)
            return True

    # -- files and moving between nodes --------------------------------------------------------

    async def _spool(self, reader: aiohttp.StreamReader, limit: int) -> str:
        fd, path = tempfile.mkstemp(dir=self.settings.spool_dir)
        size = 0
        try:
            with os.fdopen(fd, 'wb') as f:
                async for chunk in reader.iter_chunked(1024 * 1024):
                    size += len(chunk)
                    if size > limit:
                        raise NodeError('Upload is too large.')
                    await asyncio.to_thread(f.write, chunk)
        except BaseException:
            os.unlink(path)
            raise
        return path

    async def put_file(self, context: str, path: str, reader: aiohttp.StreamReader) -> dict:
        """Write a file into the container's workspace; works whether it runs or not."""
        target = safe_workspace_path(path)
        if target is None:
            raise NodeError('Files can only be written inside /home/user.')
        spooled = await self._spool(reader, self.settings.max_upload)
        tar_path = spooled + '.tar'
        try:
            size = os.path.getsize(spooled)
            relative = os.path.relpath(target, WORKSPACE)

            def build_tar():
                now = time.time()
                with tarfile.open(tar_path, 'w') as tar:
                    parts = relative.split('/')[:-1]
                    for index in range(len(parts)):
                        info = tarfile.TarInfo('/'.join(parts[: index + 1]))
                        info.type, info.mode, info.mtime = tarfile.DIRTYPE, 0o755, now
                        tar.addfile(info)
                    info = tarfile.TarInfo(relative)
                    info.size, info.mode, info.mtime = size, 0o644, now
                    with open(spooled, 'rb') as f:
                        tar.addfile(info, f)

            await asyncio.to_thread(build_tar)
            sandbox = await self.ensure(context, start=False)
            container = await asyncio.to_thread(self._container, sandbox)
            if container is None:
                raise NotFoundError('The container is gone.')

            def put():
                with open(tar_path, 'rb') as f:
                    if not container.put_archive(WORKSPACE, f):
                        raise NodeError('Docker refused the file.')

            await asyncio.to_thread(put)
            self.touch(context)
            return {'path': target, 'size': size}
        finally:
            for item in (spooled, tar_path):
                try:
                    os.unlink(item)
                except FileNotFoundError:
                    pass

    async def export(self, context: str) -> AsyncIterator[bytes]:
        """Stop the container and stream its workspace as a tar (for moving it to another node)."""
        async with self.lock(context):
            sandbox = self.store.get(context)
            if sandbox is None:
                raise NotFoundError('No container for this chat.')
            if self.inflight[context] > 0:
                raise Busy('The container is in use.')
            self.exporting.add(context)
            try:
                await self._stop(sandbox)
                container = await asyncio.to_thread(self._container, sandbox)
                if container is None:
                    raise NotFoundError('The container is gone.')
                stream, _ = await asyncio.to_thread(container.get_archive, WORKSPACE, 1024 * 1024)
            except BaseException:
                self.exporting.discard(context)
                raise

        iterator = iter(stream)
        sentinel = object()

        async def chunks():
            try:
                while True:
                    chunk = await asyncio.to_thread(next, iterator, sentinel)
                    if chunk is sentinel:
                        return
                    yield chunk
            finally:
                self.exporting.discard(context)

        return chunks()

    async def import_(self, context: str, reader: aiohttp.StreamReader, network: bool) -> Sandbox:
        """Create the context's container from a workspace tar made by ``export`` on another node."""
        spooled = await self._spool(reader, 64 * 1024**3)
        try:
            if self.store.get(context) is not None:
                await self.delete(context)
            async with self.lock(context):
                sandbox = await self._create(context, network)
                container = await asyncio.to_thread(self._container, sandbox)

                def put():
                    with open(spooled, 'rb') as f:
                        if not container.put_archive('/home', f):
                            raise NodeError('Docker refused the workspace.')

                await asyncio.to_thread(put)
                self.touch(context)
                return sandbox
        finally:
            os.unlink(spooled)

    # -- what a response changed ---------------------------------------------------------------

    async def _snapshot_exec(self, context: str, mode: str, snap_id: str) -> dict:
        if not SNAPSHOT_ID_RE.match(snap_id or ''):
            raise NodeError('Bad snapshot id.')
        sandbox = await self.ensure(context)
        container = await asyncio.to_thread(self._container, sandbox)
        if container is None:
            raise NotFoundError('The container is gone.')
        self.begin(context)
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    container.exec_run, ['python3', '-c', SNAPSHOT_SCRIPT, mode, snap_id], user='root', demux=True
                ),
                timeout=120,
            )
        except asyncio.TimeoutError:
            raise NodeError('Reading the workspace took too long.')
        finally:
            self.end(context)
        stdout, stderr = result.output if isinstance(result.output, tuple) else (result.output, b'')
        if result.exit_code != 0:
            log.warning('Snapshot %s of %s failed: %s', mode, context, (stderr or b'').decode(errors='replace')[-500:])
            raise NodeError('Could not read the workspace.')
        try:
            return json.loads((stdout or b'').decode().strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise NodeError('Could not read the workspace.')

    async def snapshot(self, context: str, snap_id: str) -> dict:
        """Record the workspace when a response starts."""
        return await self._snapshot_exec(context, 'snapshot', snap_id)

    async def changes(self, context: str, snap_id: str) -> dict:
        """Files changed since ``snapshot(snap_id)``, with lines added and removed."""
        result = await self._snapshot_exec(context, 'changes', snap_id)
        if result.get('error'):
            raise NotFoundError(result['error'])
        return result

    # -- OpenAPI -------------------------------------------------------------------------------

    async def openapi(self) -> dict:
        """Open Terminal's API description, read from the image without starting a chat container."""
        if self._spec is None:

            def read():
                output = self.client.containers.run(
                    self.settings.image,
                    ['cat', '/opt/open-terminal/openapi.json'],
                    entrypoint=[],
                    network_mode='none',
                    remove=True,
                    stdout=True,
                    stderr=False,
                )
                return json.loads(output)

            self._spec = await asyncio.to_thread(read)
        return self._spec

    # -- status and the lifecycle loop ---------------------------------------------------------

    def status(self) -> dict:
        metrics = host_metrics(self.settings.data_dir)
        counts: dict[str, int] = defaultdict(int)
        spares = {sb.context for sb in self.store.all() if sb.spare}
        for context, state in self.states.items():
            if context in spares:
                counts['spare'] += 1
            counts[state] += 1
        active = self._active_count()
        return {
            'id': self.settings.node_id,
            'name': self.settings.name,
            'version': __version__,
            **metrics,
            'containers': {
                'running': counts.get('running', 0),
                'paused': counts.get('paused', 0),
                'stopped': counts.get('exited', 0) + counts.get('created', 0),
                'spare': counts.get('spare', 0),
                'total': len(self.states),
            },
            'active': active,
            'max_active': self.settings.max_active,
            'accepting': metrics['disk_free'] >= self.settings.min_free_disk,
            'features': dict(self.features),
            'limits': {
                'cpus': self.settings.cpus,
                'memory': self.settings.memory,
                'pids': self.settings.pids,
                'disk': self.settings.disk,
                'bandwidth': self.settings.bandwidth,
            },
            'lifecycle': {
                'idle_pause': self.settings.idle_pause,
                'idle_stop': self.settings.idle_stop,
                'delete_after': self.settings.delete_after,
            },
        }

    def info(self, sandbox: Sandbox) -> dict:
        return {
            **sandbox.as_dict(),
            'last_active': self.last_active(sandbox),
            'state': self.states.get(sandbox.context, 'missing'),
            'disk_used': self.usage.get(sandbox.context),
            'disk_limit': self.settings.disk,
            'disk_blocked': sandbox.context in self.disk_blocked,
            'node': self.settings.node_id,
        }

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            try:
                await self.tick()
            except Exception:
                log.exception('Lifecycle tick failed')

    async def tick(self) -> None:
        self.flush_activity()

        def list_containers():
            return self.client.api.containers(all=True, filters={'label': f'{LABEL}=1'})

        listed = await asyncio.to_thread(list_containers)
        by_id = {item['Id']: item for item in listed}
        known = set()
        now = time.time()
        for sandbox in self.store.all():
            item = by_id.get(sandbox.container_id)
            state = item['State'] if item else 'missing'
            known.add(sandbox.container_id)
            if not self.lock(sandbox.context).locked():
                self.states[sandbox.context] = state
            action = lifecycle_action(
                state,
                now - self.last_active(sandbox),
                self.inflight[sandbox.context],
                sandbox.spare,
                now - sandbox.created_at,
                self.settings,
            )
            if action is None or self.lock(sandbox.context).locked() or sandbox.context in self.exporting:
                continue
            try:
                if action == 'pause':
                    async with self.lock(sandbox.context):
                        await self._pause(sandbox)
                elif action == 'stop':
                    async with self.lock(sandbox.context):
                        await self._stop(sandbox)
                elif action == 'delete':
                    await self.delete(sandbox.context)
                log.info('Idle %s: %s', action, sandbox.context)
            except Exception:
                log.exception('Could not %s %s', action, sandbox.context)

        # Containers this node made but no longer knows about (lost state): stop them, and delete them
        # once they've sat unused as long as any other.
        for item in listed:
            if item['Id'] in known or (item.get('Labels') or {}).get('mologate.node') != self.settings.node_id:
                continue
            created = item.get('Created') or now
            if item['State'] in ACTIVE_STATES:
                await asyncio.to_thread(lambda: self.client.containers.get(item['Id']).stop(timeout=5))
            elif now - created >= self.settings.delete_after:
                await asyncio.to_thread(lambda: self.client.containers.get(item['Id']).remove(force=True))

        if now - self._last_disk_check >= DISK_CHECK_SECONDS:
            self._last_disk_check = now
            await self._measure_disk()
        if self._free_disk() < self.settings.min_free_disk and now - self._last_urgent_disk_check >= 60:
            self._last_urgent_disk_check = now
            await self._measure_disk()
            await self._enforce_disk()

    async def _measure_disk(self) -> None:
        """Each container's files: the agent sees them, and they matter where Docker has no quotas."""

        def measure():
            usage = {}
            df = self.client.df()
            volumes = {v['Name']: (v.get('UsageData') or {}).get('Size', 0) for v in df.get('Volumes') or []}
            layers = {c['Id']: c.get('SizeRw', 0) or 0 for c in df.get('Containers') or []}
            for sandbox in self.store.all():
                usage[sandbox.context] = max(0, volumes.get(sandbox.volume, 0)) + max(
                    0, layers.get(sandbox.container_id, 0)
                )
            return usage

        try:
            self.usage = await asyncio.to_thread(measure)
        except APIError as e:
            log.warning('Could not measure disk use: %s', e)

    async def _enforce_disk(self) -> None:
        """The node is short of disk: stop every container over its own limit and keep it stopped."""
        for context, used in self.usage.items():
            if used <= self.settings.disk:
                continue
            sandbox = self.store.get(context)
            if sandbox is None:
                continue
            log.warning('%s holds %s bytes, over its disk limit, while the node is short of disk', context, used)
            self.disk_blocked.add(context)
            async with self.lock(context):
                await self._stop(sandbox)
