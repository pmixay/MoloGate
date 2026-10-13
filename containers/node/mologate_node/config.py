"""Settings for a container node, read once from the environment (docs/container-nodes.md)."""

import os
import re
import socket
from dataclasses import dataclass, field

_SIZE_UNITS = {'': 1, 'b': 1, 'k': 1024, 'm': 1024**2, 'g': 1024**3, 't': 1024**4}


def parse_size(value: str | int) -> int:
    """'512m', '1g', '1GiB' or a plain byte count -> bytes."""
    if isinstance(value, int):
        return value
    match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*([kmgt]?)(?:i?b)?\s*', str(value).lower())
    if not match:
        raise ValueError(f'Not a size: {value!r}')
    return int(float(match.group(1)) * _SIZE_UNITS[match.group(2)])


def _env(name: str, default: str = '') -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    return default if not value else value.lower() in {'1', 'true', 'yes', 'on'}


def _read_meminfo_total() -> int:
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemTotal:'):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 4 * 1024**3


@dataclass(frozen=True)
class Settings:
    api_key: str
    node_id: str
    name: str
    host: str = '0.0.0.0'
    port: int = 8090
    # Must be the same path on the host and inside the node's container: sandbox sockets live here.
    data_dir: str = '/var/lib/mologate-node'
    image: str = 'ghcr.io/pmixay/mologate-sandbox:latest'
    # Image with iptables and tc that applies host firewall rules. Empty: run them on this machine.
    helper_image: str = ''
    network: str = 'mologate-sandbox'
    bridge: str = 'mologate-sb0'
    subnet: str = ''
    dns: tuple[str, ...] = ('1.1.1.1', '8.8.8.8')
    runtime: str = ''
    # Limits for each container.
    cpus: float = 1.0
    memory: int = 1024**3
    pids: int = 512
    disk: int = 10 * 1024**3
    tmp_size: int = 256 * 1024**2
    bandwidth: str = '100mibps'
    nofile: int = 4096
    # Guards for the whole node.
    max_active: int = 4
    min_free_disk: int = 5 * 1024**3
    max_connections: int = 256
    new_connections_per_second: int = 50
    block_smtp: bool = True
    guards: bool = True
    max_upload: int = 1024**3
    # Lifecycle, in seconds.
    idle_pause: int = 120
    idle_stop: int = 900
    delete_after: int = 7 * 86400
    spare_ttl: int = 300
    max_spares: int = 1
    private_networks: tuple[str, ...] = field(
        default=(
            '0.0.0.0/8',
            '10.0.0.0/8',
            '100.64.0.0/10',
            '127.0.0.0/8',
            '169.254.0.0/16',
            '172.16.0.0/12',
            '192.0.0.0/24',
            '192.168.0.0/16',
            '198.18.0.0/15',
            '224.0.0.0/4',
            '240.0.0.0/4',
        )
    )

    @property
    def socket_root(self) -> str:
        return os.path.join(self.data_dir, 'run')

    @property
    def spool_dir(self) -> str:
        return os.path.join(self.data_dir, 'spool')

    @property
    def state_path(self) -> str:
        return os.path.join(self.data_dir, 'state.db')


def auto_max_active(memory_limit: int, total_memory: int | None = None) -> int:
    """Containers that may run or sit paused at once when NODE_MAX_ACTIVE isn't set.

    Most containers use a fraction of their memory limit, so a node keeps 1.5 limits' worth per
    gigabyte it can spare, after leaving a quarter of the machine to everything else.
    """
    total = total_memory if total_memory is not None else _read_meminfo_total()
    return max(1, int(total * 0.75 * 1.5 / max(memory_limit, 1)))


def load_settings() -> Settings:
    api_key = _env('NODE_API_KEY')
    if len(api_key) < 24:
        raise SystemExit('NODE_API_KEY must be set to a random secret of at least 24 characters.')

    data_dir = _env('NODE_DATA_DIR', '/var/lib/mologate-node')
    # Sandbox sockets live at <data_dir>/run/<20 chars>/terminal.sock; Unix socket paths stop at 107.
    if len(os.path.join(data_dir, 'run', 'x' * 20, 'terminal.sock')) > 100:
        raise SystemExit('NODE_DATA_DIR is too long for Unix socket paths; keep it under about 60 characters.')

    memory = parse_size(_env('NODE_CONTAINER_MEMORY', '1g'))
    bandwidth = _env('NODE_CONTAINER_BANDWIDTH', '100mibps')
    if bandwidth:
        from mologate_node.guards import rate_bytes_per_second

        rate_bytes_per_second(bandwidth)
    max_active = int(_env('NODE_MAX_ACTIVE', '0') or 0) or auto_max_active(memory)
    dns = tuple(item.strip() for item in _env('NODE_CONTAINER_DNS', '1.1.1.1,8.8.8.8').split(',') if item.strip())

    return Settings(
        api_key=api_key,
        node_id=_env('NODE_ID') or socket.gethostname(),
        name=_env('NODE_NAME') or _env('NODE_ID') or socket.gethostname(),
        host=_env('NODE_HOST', '0.0.0.0'),
        port=int(_env('NODE_PORT', '8090')),
        data_dir=data_dir,
        image=_env('NODE_SANDBOX_IMAGE', 'ghcr.io/pmixay/mologate-sandbox:latest'),
        helper_image=_env('NODE_HELPER_IMAGE'),
        network=_env('NODE_NETWORK', 'mologate-sandbox'),
        bridge=_env('NODE_BRIDGE', 'mologate-sb0'),
        subnet=_env('NODE_SUBNET'),
        dns=dns,
        runtime=_env('NODE_RUNTIME'),
        cpus=float(_env('NODE_CONTAINER_CPUS', '1')),
        memory=memory,
        pids=int(_env('NODE_CONTAINER_PIDS', '512')),
        disk=parse_size(_env('NODE_CONTAINER_DISK', '10g')),
        tmp_size=parse_size(_env('NODE_CONTAINER_TMP', '256m')),
        bandwidth=bandwidth,
        nofile=int(_env('NODE_CONTAINER_NOFILE', '4096')),
        max_active=max_active,
        min_free_disk=parse_size(_env('NODE_MIN_FREE_DISK', '5g')),
        max_connections=int(_env('NODE_MAX_CONNECTIONS', '256')),
        new_connections_per_second=int(_env('NODE_NEW_CONNECTIONS_PER_SECOND', '50')),
        block_smtp=_env_bool('NODE_BLOCK_SMTP', True),
        guards=_env_bool('NODE_GUARDS', True),
        max_upload=parse_size(_env('NODE_MAX_UPLOAD', '1g')),
        idle_pause=int(_env('NODE_IDLE_PAUSE_SECONDS', '120')),
        idle_stop=int(_env('NODE_IDLE_STOP_SECONDS', '900')),
        delete_after=int(_env('NODE_DELETE_AFTER_DAYS', '7')) * 86400,
        spare_ttl=int(_env('NODE_SPARE_TTL_SECONDS', '300')),
        max_spares=int(_env('NODE_MAX_SPARES', '1')),
    )
