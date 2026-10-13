import pytest

from mologate_node.config import Settings, auto_max_active, parse_size
from mologate_node.guards import (
    bandwidth_script,
    burst_bytes,
    firewall_script,
    host_bandwidth_script,
    rate_bytes_per_second,
)
from mologate_node.lifecycle import eviction_order, lifecycle_action
from mologate_node.manager import safe_workspace_path, valid_context

SETTINGS = Settings(api_key='x' * 32, node_id='n1', name='Node 1', idle_pause=120, idle_stop=900, spare_ttl=300)


def test_parse_size():
    assert parse_size('512m') == 512 * 1024**2
    assert parse_size('1g') == 1024**3
    assert parse_size('1GiB') == 1024**3
    assert parse_size('10G') == 10 * 1024**3
    assert parse_size(42) == 42
    with pytest.raises(ValueError):
        parse_size('lots')


def test_auto_max_active():
    assert auto_max_active(1024**3, 4 * 1024**3) == 4
    assert auto_max_active(1024**3, 512 * 1024**2) == 1


def test_rates():
    assert rate_bytes_per_second('100mibps') == 100 * 1024**2
    assert rate_bytes_per_second('800mbit') == 100 * 1000**2
    assert burst_bytes('100mibps') == 2 * 1024**2
    assert burst_bytes('1kbit') == 32 * 1024
    with pytest.raises(ValueError):
        rate_bytes_per_second('fast')


def test_bandwidth_scripts():
    script = bandwidth_script('100mibps')
    assert 'tbf rate 100mibps' in script
    assert 'police rate 100mibps' in script
    assert 'exit 3' in script
    host = host_bandwidth_script('100mibps', 17)
    assert '$1 == 17' in host and 'tbf rate 100mibps' in host


def test_firewall_script_blocks_private_ranges_and_smtp():
    script = firewall_script(SETTINGS)
    for network in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '169.254.0.0/16', '127.0.0.0/8'):
        assert f'-d {network} ' in script
    assert '--dport 25 ' in script
    assert '--connlimit-above 256' in script
    assert 'hashlimit-above 50/second' in script
    assert '-i mologate-sb0 -j MOLOGATE-SANDBOX-IN' in script


def test_lifecycle_running():
    assert lifecycle_action('running', 10, 0, False, 1000, SETTINGS) is None
    assert lifecycle_action('running', 130, 0, False, 1000, SETTINGS) == 'pause'
    assert lifecycle_action('running', 130, 1, False, 1000, SETTINGS) is None
    assert lifecycle_action('running', 1000, 0, False, 1000, SETTINGS) == 'stop'


def test_lifecycle_paused_and_stopped():
    assert lifecycle_action('paused', 300, 0, False, 1000, SETTINGS) is None
    assert lifecycle_action('paused', 901, 0, False, 1000, SETTINGS) == 'stop'
    assert lifecycle_action('exited', 6 * 86400, 0, False, 0, SETTINGS) is None
    assert lifecycle_action('exited', 7 * 86400, 0, False, 0, SETTINGS) == 'delete'
    assert lifecycle_action('missing', 7 * 86400, 0, False, 0, SETTINGS) == 'delete'


def test_lifecycle_spares_expire():
    assert lifecycle_action('running', 0, 0, True, 100, SETTINGS) is None
    assert lifecycle_action('running', 0, 0, True, 301, SETTINGS) == 'delete'


def test_lifecycle_pause_disabled():
    settings = Settings(api_key='x' * 32, node_id='n', name='n', idle_pause=0, idle_stop=600)
    assert lifecycle_action('running', 300, 0, False, 0, settings) is None
    assert lifecycle_action('running', 601, 0, False, 0, settings) == 'stop'


def test_eviction_order_prefers_paused_and_oldest():
    candidates = [
        ('chat:busy', 'running', 500, 1),
        ('chat:recent', 'running', 5, 0),
        ('chat:old-running', 'running', 400, 0),
        ('chat:old-paused', 'paused', 100, 0),
        ('chat:older-paused', 'paused', 300, 0),
        ('chat:stopped', 'exited', 900, 0),
    ]
    assert eviction_order(candidates) == ['chat:older-paused', 'chat:old-paused', 'chat:old-running']


def test_valid_context():
    assert valid_context('chat:5f0c1d0e-1b1a-4c5e-9d6f-2f2e5b0c9a11')
    assert valid_context('automation:abc')
    assert not valid_context('chat:../../etc')
    assert not valid_context('chat:a/b')
    assert not valid_context('other:abc')
    assert not valid_context('')


def test_safe_workspace_path():
    assert safe_workspace_path('/home/user/uploads/a.txt') == '/home/user/uploads/a.txt'
    assert safe_workspace_path('/home/user/../../etc/passwd') is None
    assert safe_workspace_path('/etc/passwd') is None
    assert safe_workspace_path('/home/user') is None
    assert safe_workspace_path('/home/username/x') is None
    assert safe_workspace_path('/home/user/a\x00b') is None
