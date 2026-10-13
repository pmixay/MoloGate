"""Network guards: the host firewall rules for the sandbox bridge and each container's bandwidth cap.

Both are plain shell scripts so they can run in a short-lived helper container (the node's own
image, which ships iptables and tc) or straight on the host when the node runs there as root.
"""

import re
import shlex

from mologate_node.config import Settings

FORWARD_CHAIN = 'MOLOGATE-SANDBOX'
INPUT_CHAIN = 'MOLOGATE-SANDBOX-IN'


def firewall_script(settings: Settings) -> str:
    """Idempotent iptables rules for traffic leaving the sandbox bridge.

    - Nothing reaches the host itself, private and link-local ranges (other Docker networks, the
      database, cloud metadata at 169.254.169.254) or multicast; only the public internet.
    - No outbound mail on port 25, so a container can't be used to send spam.
    - At most ``max_connections`` open TCP connections and ``new_connections_per_second`` new
      connections per container, so one can't flood a target or exhaust the host's conntrack table.

    Docker's own rules decide nothing here: these run first, from DOCKER-USER and INPUT. The script
    picks whichever iptables backend (nft or legacy) Docker's DOCKER-USER chain lives in.
    """
    bridge = shlex.quote(settings.bridge)
    rate = max(1, settings.new_connections_per_second)
    lines = [
        'set -eu',
        'IPT=iptables',
        'for candidate in iptables-nft iptables-legacy iptables; do',
        '  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -w -S DOCKER-USER >/dev/null 2>&1; then',
        '    IPT=$candidate; break',
        '  fi',
        'done',
        'FWD=DOCKER-USER',
        '$IPT -w -S DOCKER-USER >/dev/null 2>&1 || FWD=FORWARD',
        f'$IPT -w -N {FORWARD_CHAIN} 2>/dev/null || $IPT -w -F {FORWARD_CHAIN}',
        f'$IPT -w -A {FORWARD_CHAIN} -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN',
    ]
    for network in settings.private_networks:
        lines.append(f'$IPT -w -A {FORWARD_CHAIN} -d {network} -p tcp -j REJECT --reject-with tcp-reset')
        lines.append(f'$IPT -w -A {FORWARD_CHAIN} -d {network} -j REJECT --reject-with icmp-net-prohibited')
    if settings.block_smtp:
        lines.append(f'$IPT -w -A {FORWARD_CHAIN} -p tcp --dport 25 -j REJECT --reject-with tcp-reset')
    lines += [
        f'$IPT -w -A {FORWARD_CHAIN} -p tcp --syn -m connlimit --connlimit-above {settings.max_connections} '
        '--connlimit-mask 32 -j REJECT --reject-with tcp-reset',
        f'$IPT -w -A {FORWARD_CHAIN} -m conntrack --ctstate NEW -m hashlimit --hashlimit-above {rate}/second '
        f'--hashlimit-burst {rate * 4} --hashlimit-mode srcip --hashlimit-name mologate_new -j DROP',
        f'$IPT -w -A {FORWARD_CHAIN} -j RETURN',
        f'$IPT -w -C "$FWD" -i {bridge} -j {FORWARD_CHAIN} 2>/dev/null || $IPT -w -I "$FWD" 1 -i {bridge} -j {FORWARD_CHAIN}',
        # The host: containers resolve names through Docker's embedded DNS inside their own
        # namespace, so they need nothing from the host itself.
        f'$IPT -w -N {INPUT_CHAIN} 2>/dev/null || $IPT -w -F {INPUT_CHAIN}',
        f'$IPT -w -A {INPUT_CHAIN} -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN',
        f'$IPT -w -A {INPUT_CHAIN} -j DROP',
        f'$IPT -w -C INPUT -i {bridge} -j {INPUT_CHAIN} 2>/dev/null || $IPT -w -I INPUT 1 -i {bridge} -j {INPUT_CHAIN}',
        'echo "guards applied with $IPT ($FWD)"',
    ]
    return '\n'.join(lines) + '\n'


POLICE_UNAVAILABLE = 3

_RATE_UNITS = {
    'bit': 1 / 8,
    'kbit': 1000 / 8,
    'mbit': 1000**2 / 8,
    'gbit': 1000**3 / 8,
    'kibit': 1024 / 8,
    'mibit': 1024**2 / 8,
    'gibit': 1024**3 / 8,
    'bps': 1,
    'kbps': 1000,
    'mbps': 1000**2,
    'gbps': 1000**3,
    'kibps': 1024,
    'mibps': 1024**2,
    'gibps': 1024**3,
}


def rate_bytes_per_second(rate: str) -> float:
    """A tc rate such as ``100mibps`` (bytes) or ``800mbit`` (bits) in bytes per second."""
    match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*([a-z]+)\s*', rate.lower())
    if not match or match.group(2) not in _RATE_UNITS:
        raise ValueError(f'Not a tc rate: {rate!r}')
    return float(match.group(1)) * _RATE_UNITS[match.group(2)]


def burst_bytes(rate: str) -> int:
    """About 20 ms of traffic, at least 32 KiB: enough to keep the rate, too little to cheat it."""
    return max(32 * 1024, int(rate_bytes_per_second(rate) / 50))


def bandwidth_script(rate: str, interface: str = 'eth0') -> str:
    """Cap a container's upload and download at ``rate`` (tc units, e.g. ``100mibps`` = 100 MiB/s).

    Upload is shaped with a token bucket on the interface; download is policed on ingress, which
    drops what exceeds the rate so TCP backs off to it. Runs in the container's network namespace
    with NET_ADMIN, which the container itself never has, so the agent can't lift it.

    Kernels without the police action exit with ``POLICE_UNAVAILABLE`` and print the host-side
    interface index, so the caller can shape download there instead (``host_bandwidth_script``).
    """
    dev = shlex.quote(interface)
    burst = burst_bytes(rate)
    rate = shlex.quote(rate)
    return (
        '\n'.join(
            [
                'set -u',
                f'ip link show {dev} >/dev/null 2>&1 || exit 0',
                f'tc qdisc replace dev {dev} root tbf rate {rate} burst {burst} latency 100ms || exit 1',
                f'tc qdisc del dev {dev} ingress 2>/dev/null',
                f'if tc qdisc add dev {dev} handle ffff: ingress && tc filter add dev {dev} parent ffff: protocol all '
                f'prio 1 u32 match u32 0 0 police rate {rate} burst {burst * 2} drop flowid :1; then exit 0; fi',
                f'tc qdisc del dev {dev} ingress 2>/dev/null',
                f'echo "iflink=$(cat /sys/class/net/{interface}/iflink)"',
                f'exit {POLICE_UNAVAILABLE}',
            ]
        )
        + '\n'
    )


def host_bandwidth_script(rate: str, iflink: int) -> str:
    """Shape download on the host side of a container's veth pair (what the host sends is what the
    container receives). Needs only the token bucket qdisc, so it works where policing doesn't."""
    burst = burst_bytes(rate)
    rate = shlex.quote(rate)
    return (
        '\n'.join(
            [
                'set -eu',
                f"IF=$(ip -o link | awk -F': ' '$1 == {int(iflink)} {{ split($2, name, \"@\"); print name[1] }}')",
                '[ -n "$IF" ] || exit 1',
                f'tc qdisc replace dev "$IF" root tbf rate {rate} burst {burst} latency 100ms',
            ]
        )
        + '\n'
    )
