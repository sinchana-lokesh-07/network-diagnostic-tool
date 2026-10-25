#!/usr/bin/env python3
"""Diamond Mininet topology for congestion-aware routing demo."""
import json
import os
import sys
import time
import urllib.request
from functools import partial

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import info, setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController
from mininet.topo import Topo

REST = 'http://127.0.0.1:8080'


class DiamondTopo(Topo):
    def build(self):
        self.addHost('h1', ip='10.0.0.1/24', mac='00:00:00:00:00:01')
        self.addHost('h2', ip='10.0.0.2/24', mac='00:00:00:00:00:02')
        s1 = self.addSwitch('s1', dpid='0000000000000001')
        s2 = self.addSwitch('s2', dpid='0000000000000002')
        s3 = self.addSwitch('s3', dpid='0000000000000003')
        s4 = self.addSwitch('s4', dpid='0000000000000004')
        self.addLink('h1', s1)  # s1-eth1
        self.addLink('h2', s4)  # s4-eth1
        # Upper path: lower latency, 10 Mbps per link.
        self.addLink(s1, s2, cls=TCLink, bw=10, delay='5ms')
        self.addLink(s2, s4, cls=TCLink, bw=10, delay='5ms')
        # Lower path: higher latency, same capacity.
        self.addLink(s1, s3, cls=TCLink, bw=10, delay='20ms')
        self.addLink(s3, s4, cls=TCLink, bw=10, delay='20ms')


def wait_for_topology(expected_links=8, timeout=40):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(REST + '/v1.0/topology/links', timeout=2) as response:
                if len(json.loads(response.read().decode())) >= expected_links:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


def main():
    if os.geteuid() != 0:
        sys.exit('Run with sudo: sudo python3 topo_diamond.py')
    setLogLevel('info')
    net = Mininet(
        topo=DiamondTopo(),
        switch=partial(OVSSwitch, protocols='OpenFlow13'),
        controller=None,
        link=TCLink,
        autoSetMacs=False,
        build=False
    )
    net.addController('c0', controller=RemoteController, ip='127.0.0.1', port=6653)
    try:
        net.start()
        net.waitConnected(timeout=20)
        info('*** Waiting for Ryu topology discovery (8 directed links)\n')
        if not wait_for_topology():
            info('!!! Topology not fully discovered; check logs/ryu.log\n')
        h1, h2 = net.get('h1', 'h2')
        info('*** Warm-up ping so Ryu learns the hosts\n')
        for _ in range(5):
            if net.ping([h1, h2], timeout='2') == 0:
                break
            time.sleep(1)
        info('\n*** READY\n')
        info('*** Topology REST: %s/v1.0/topology/{switches,links,hosts}\n' % REST)
        info('*** Active paths:   %s/v1.0/diag/paths\n' % REST)
        info('*** Link metrics:   %s/v1.0/diag/links\n\n' % REST)
        CLI(net)
    finally:
        net.stop()


if __name__ == '__main__':
    main()
