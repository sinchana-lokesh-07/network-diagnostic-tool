#!/usr/bin/env python3
"""Check topology discovery, learned paths, and congestion metrics."""
import json
import sys
import urllib.request

BASE = 'http://127.0.0.1:8080/v1.0/'


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=5) as response:
        return json.loads(response.read().decode())


try:
    switches = get('topology/switches')
    links = get('topology/links')
    hosts = get('topology/hosts')
    paths = get('diag/paths')
    metrics = get('diag/links')
except Exception as exc:
    print('FAIL - cannot reach Ryu REST:', exc)
    sys.exit(1)

checks = [
    ('4 switches discovered', len(switches) == 4),
    ('8 directed links discovered', len(links) == 8),
    ('2 hosts discovered', len(hosts) >= 2),
    ('bidirectional host paths installed', len(paths) >= 2),
    ('congestion metrics endpoint available', isinstance(metrics, list)),
]
for name, ok in checks:
    print(('PASS' if ok else 'FAIL'), '-', name)

print('\nActive paths:')
if not paths:
    print('  No paths learned yet. Generate traffic between h1 and h2, then retry.')
for path in paths:
    print('  %s -> %s via %s (max link utilization: %s%%)' % (
        path.get('src_ip') or path.get('src_mac'),
        path.get('dst_ip') or path.get('dst_mac'),
        path.get('path'),
        path.get('max_link_utilization_percent', 'unknown')))

print('\nLink metrics:')
if not metrics:
    print('  No inter-switch links discovered yet.')
for link in metrics:
    print('  %s -> %s: %s Mbps, %s%% utilization, samples=%s' % (
        link['src_switch'], link['dst_switch'], link['tx_mbps'],
        link['utilization_percent'], link['samples']))

sys.exit(0 if all(ok for _, ok in checks) else 1)
