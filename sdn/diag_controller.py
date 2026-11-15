"""Congestion-aware Ryu controller for the PS13 diamond-topology project."""
import heapq
import json
import time

from ryu.app.wsgi import ControllerBase, WSGIApplication, route
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, DEAD_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.lib import hub
from ryu.lib.packet import arp, ether_types, ethernet, ipv4, packet
from ryu.ofproto import ofproto_v1_3
from ryu.topology import event
from ryu.topology.api import get_link, get_switch
from webob import Response

FLOW_COOKIE = 0xD1A6
OFP_PORT_MAX = 0xFFFFFF00
STATS_INTERVAL = 1.0
LINK_CAPACITY_MBPS = 10.0       # Keep this consistent with topo_diamond.py
CONGESTION_THRESHOLD = 0.70    # Trigger when active path's busiest link reaches 70%
CLEAR_THRESHOLD = 0.55          # Alternative path must be below 55%
CONGESTION_WEIGHT = 100.0
HOP_WEIGHT = 1.0
REROUTE_COOLDOWN = 3.0
MIN_UTILIZATION_SAMPLES = 2
STATS_MAX_AGE = 5.0


class DiagController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {'wsgi': WSGIApplication}

    def __init__(self, *args, **kwargs):
        super(DiagController, self).__init__(*args, **kwargs)
        self.datapaths = {}
        self.adj = {}              # adj[src_dpid][dst_dpid] = source output port
        self.inter_ports = {}      # switch dpid -> set of inter-switch port numbers
        self.host_loc = {}         # MAC -> (switch dpid, host-facing port)
        self.host_ip = {}          # MAC -> IP address
        self.active_paths = {}     # (src_mac, dst_mac) -> [switch dpids]
        self.link_stats = {}       # (src_dpid, src_port, dst_dpid) -> utilization stats
        self.previous_port_stats = {}  # (dpid, port) -> previous tx_bytes and timestamp
        self.last_reroute = {}     # sorted MAC pair -> timestamp
        self.path_history = {}
        kwargs['wsgi'].register(DiagRest, {'diag_app': self})
        self.monitor_thread = hub.spawn(self._monitor)
        self.logger.info('Congestion-aware controller started: capacity=%.1f Mbps, threshold=%.0f%%',
                         LINK_CAPACITY_MBPS, CONGESTION_THRESHOLD * 100)

    # ----------------------------- switch connection and table-miss flow
    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change_handler(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dp.id] = dp
            self.logger.info('Switch connected: s%d', dp.id)
        elif ev.state == DEAD_DISPATCHER:
            self.datapaths.pop(dp.id, None)
            self.logger.info('Switch disconnected: s%d', dp.id)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        parser = dp.ofproto_parser
        actions = [parser.OFPActionOutput(dp.ofproto.OFPP_CONTROLLER,
                                          dp.ofproto.OFPCML_NO_BUFFER)]
        self._add_flow(dp, 0, parser.OFPMatch(), actions, cookie=0)

    # --------------------------------------------- topology discovery
    @set_ev_cls([event.EventSwitchEnter, event.EventSwitchLeave,
                 event.EventLinkAdd, event.EventLinkDelete])
    def topology_change_handler(self, ev):
        adj, inter = {}, {}
        for sw in get_switch(self, None):
            adj.setdefault(sw.dp.id, {})
            inter.setdefault(sw.dp.id, set())
        for link in get_link(self, None):
            src, dst = link.src, link.dst
            adj.setdefault(src.dpid, {})[dst.dpid] = src.port_no
            adj.setdefault(dst.dpid, {})
            inter.setdefault(src.dpid, set()).add(src.port_no)
            inter.setdefault(dst.dpid, set()).add(dst.port_no)
        changed = adj != self.adj
        self.adj, self.inter_ports = adj, inter
        valid_link_keys = {(src, port, dst)
                           for src, nbrs in adj.items() for dst, port in nbrs.items()}
        self.link_stats = {k: v for k, v in self.link_stats.items() if k in valid_link_keys}
        valid_ports = {(src, port) for src, nbrs in adj.items() for port in nbrs.values()}
        self.previous_port_stats = {k: v for k, v in self.previous_port_stats.items()
                                    if k in valid_ports}
        if changed:
            self.logger.info('Topology updated: %s', {
                's%d' % src: ['s%d' % dst for dst in sorted(nbrs)]
                for src, nbrs in adj.items()})
            self._clear_flows()

    # --------------------------------------------- statistics polling
    def _monitor(self):
        while True:
            for dp in list(self.datapaths.values()):
                try:
                    req = dp.ofproto_parser.OFPPortStatsRequest(
                        dp, 0, dp.ofproto.OFPP_ANY)
                    dp.send_msg(req)
                except Exception as exc:
                    self.logger.warning('Could not request stats from s%d: %s', dp.id, exc)
            hub.sleep(STATS_INTERVAL)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev):
        dpid = ev.msg.datapath.id
        now = time.time()
        for stat in ev.msg.body:
            port = stat.port_no
            if port >= OFP_PORT_MAX:
                continue
            key = (dpid, port)
            old = self.previous_port_stats.get(key)
            self.previous_port_stats[key] = {'tx_bytes': stat.tx_bytes, 'time': now}
            if old is None:
                continue
            elapsed = now - old['time']
            delta = stat.tx_bytes - old['tx_bytes']
            if elapsed <= 0 or delta < 0:
                continue
            tx_mbps = delta * 8.0 / elapsed / 1_000_000.0
            utilization = min(tx_mbps / LINK_CAPACITY_MBPS, 1.0)
            for dst, out_port in self.adj.get(dpid, {}).items():
                if out_port != port:
                    continue
                lk = (dpid, port, dst)
                prev = self.link_stats.get(lk, {})
                samples = prev.get('samples', 0) + 1
                old_util = prev.get('utilization', utilization)
                smooth = utilization if samples == 1 else 0.5 * utilization + 0.5 * old_util
                self.link_stats[lk] = {
                    'tx_mbps': tx_mbps, 'utilization': smooth,
                    'samples': samples, 'updated': now
                }
        self._check_active_paths()

    # --------------------------------------------- link and path costs
    def _link_utilization(self, src, dst):
        port = self.adj.get(src, {}).get(dst)
        if port is None:
            return 1.0
        stats = self.link_stats.get((src, port, dst))
        if not stats or stats.get('samples', 0) < MIN_UTILIZATION_SAMPLES:
            return 0.0  # unknown metrics: don't block initial connectivity
        if time.time() - stats.get('updated', 0) > STATS_MAX_AGE:
            return 0.0  # stale metrics are not trusted
        return stats['utilization']

    def _link_cost(self, src, dst):
        util = self._link_utilization(src, dst)
        return HOP_WEIGHT + CONGESTION_WEIGHT * util * util

    def _best_path(self, src, dst):
        if src == dst:
            return [src]
        distances, previous = {src: 0.0}, {}
        heap = [(0.0, src)]
        while heap:
            cost, node = heapq.heappop(heap)
            if cost > distances.get(node, float('inf')):
                continue
            if node == dst:
                break
            for nbr in sorted(self.adj.get(node, {})):
                new_cost = cost + self._link_cost(node, nbr)
                if new_cost < distances.get(nbr, float('inf')):
                    distances[nbr] = new_cost
                    previous[nbr] = node
                    heapq.heappush(heap, (new_cost, nbr))
        if dst not in distances:
            return None
        path = [dst]
        while path[-1] != src:
            parent = previous.get(path[-1])
            if parent is None:
                return None
            path.append(parent)
        return list(reversed(path))

    def _path_utilization(self, path):
        if not path or len(path) < 2:
            return 0.0
        return max(self._link_utilization(path[i], path[i + 1])
                   for i in range(len(path) - 1))

    def _check_active_paths(self):
        checked = set()
        for (src_mac, dst_mac), path in list(self.active_paths.items()):
            pair = tuple(sorted((src_mac, dst_mac)))
            if pair in checked:
                continue
            checked.add(pair)
            src_loc, dst_loc = self.host_loc.get(src_mac), self.host_loc.get(dst_mac)
            if not src_loc or not dst_loc or not path:
                continue
            current_util = self._path_utilization(path)
            if current_util < CONGESTION_THRESHOLD:
                continue
            now = time.time()
            if now - self.last_reroute.get(pair, 0) < REROUTE_COOLDOWN:
                continue
            new_path = self._best_path(src_loc[0], dst_loc[0])
            if not new_path or new_path == path:
                continue
            new_util = self._path_utilization(new_path)
            if new_util >= CLEAR_THRESHOLD:
                self.logger.warning('Congestion detected on %s -> %s, but no measured alternative below %.0f%%; best=%s at %.1f%%',
                                    src_mac, dst_mac, CLEAR_THRESHOLD * 100,
                                    new_path, new_util * 100)
                continue
            self.logger.warning('CONGESTION REROUTE %s -> %s: %s -> %s (%.1f%% -> %.1f%% max link utilization)',
                                src_mac, dst_mac, path, new_path,
                                current_util * 100, new_util * 100)
            self._replace_path(src_mac, dst_mac, new_path)
            self.last_reroute[pair] = now

    def _replace_path(self, src_mac, dst_mac, path):
        src_loc, dst_loc = self.host_loc.get(src_mac), self.host_loc.get(dst_mac)
        if not src_loc or not dst_loc:
            return
        self._delete_pair_flows(src_mac, dst_mac)
        self._delete_pair_flows(dst_mac, src_mac)
        self._install_path(path, src_mac, dst_mac, dst_loc[1])
        self._install_path(path[::-1], dst_mac, src_mac, src_loc[1])
        self.active_paths[(src_mac, dst_mac)] = path
        self.active_paths[(dst_mac, src_mac)] = path[::-1]
        self.path_history.setdefault('%s->%s' % (src_mac, dst_mac), []).append({
            'time': time.time(), 'path': path})

    # --------------------------------------------- host learning / routing
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg, dp = ev.msg, ev.msg.datapath
        in_port = msg.match['in_port']
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype in (ether_types.ETH_TYPE_LLDP, ether_types.ETH_TYPE_IPV6):
            return
        if in_port >= OFP_PORT_MAX or in_port in self.inter_ports.get(dp.id, set()):
            return
        if not self.adj or not any(self.adj.values()):
            return
        src, dst = eth.src, eth.dst
        self.host_loc[src] = (dp.id, in_port)
        arp_pkt, ip_pkt = pkt.get_protocol(arp.arp), pkt.get_protocol(ipv4.ipv4)
        if arp_pkt and arp_pkt.src_ip != '0.0.0.0':
            self.host_ip[src] = arp_pkt.src_ip
        elif ip_pkt and ip_pkt.src != '0.0.0.0':
            self.host_ip[src] = ip_pkt.src
        multicast = bool(int(dst.split(':')[0], 16) & 1)
        if not multicast and dst in self.host_loc:
            self._route(dp, in_port, msg.data, src, dst)
        else:
            self._flood_edge_ports(dp.id, in_port, msg.data)

    def _route(self, dp, in_port, data, src, dst):
        src_dpid, src_port = self.host_loc[src]
        dst_dpid, dst_port = self.host_loc[dst]
        path = self._best_path(src_dpid, dst_dpid)
        if path is None:
            self.logger.warning('NO PATH %s -> %s', src, dst)
            return
        if self.active_paths.get((src, dst)) != path:
            self._delete_pair_flows(src, dst)
            self._delete_pair_flows(dst, src)
            self._install_path(path, src, dst, dst_port)
            self._install_path(path[::-1], dst, src, src_port)
            self.active_paths[(src, dst)] = path
            self.active_paths[(dst, src)] = path[::-1]
            self.logger.info('SELECTED PATH %s -> %s: %s', src, dst, ['s%d' % d for d in path])
        out_port = dst_port if len(path) == 1 else self.adj[path[0]][path[1]]
        self._packet_out(dp, in_port, out_port, data)

    def _install_path(self, path, src_mac, dst_mac, last_port):
        for i in reversed(range(len(path))):
            dp = self.datapaths.get(path[i])
            if dp is None:
                continue
            if i == len(path) - 1:
                out_port = last_port
            else:
                out_port = self.adj.get(path[i], {}).get(path[i + 1])
                if out_port is None:
                    continue
            parser = dp.ofproto_parser
            match = parser.OFPMatch(eth_src=src_mac, eth_dst=dst_mac)
            self._add_flow(dp, 10, match, [parser.OFPActionOutput(out_port)], FLOW_COOKIE)

    def _delete_pair_flows(self, src_mac, dst_mac):
        for dp in list(self.datapaths.values()):
            ofp, parser = dp.ofproto, dp.ofproto_parser
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp, cookie=FLOW_COOKIE, cookie_mask=0xffffffffffffffff,
                table_id=ofp.OFPTT_ALL, command=ofp.OFPFC_DELETE,
                out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
                match=parser.OFPMatch(eth_src=src_mac, eth_dst=dst_mac)))

    def _clear_flows(self):
        for dp in list(self.datapaths.values()):
            ofp, parser = dp.ofproto, dp.ofproto_parser
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp, cookie=FLOW_COOKIE, cookie_mask=0xffffffffffffffff,
                table_id=ofp.OFPTT_ALL, command=ofp.OFPFC_DELETE,
                out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
                match=parser.OFPMatch()))
        self.active_paths.clear()

    def _add_flow(self, dp, priority, match, actions, cookie):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        instructions = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(parser.OFPFlowMod(datapath=dp, cookie=cookie,
                                      priority=priority, match=match,
                                      instructions=instructions))

    def _packet_out(self, dp, in_port, out_port, data):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        dp.send_msg(parser.OFPPacketOut(
            datapath=dp, buffer_id=ofp.OFP_NO_BUFFER, in_port=in_port,
            actions=[parser.OFPActionOutput(out_port)], data=data))

    def _flood_edge_ports(self, src_dpid, in_port, data):
        for sw in get_switch(self, None):
            dp = self.datapaths.get(sw.dp.id)
            if dp is None:
                continue
            inter = self.inter_ports.get(sw.dp.id, set())
            for port in sw.ports:
                pno = port.port_no
                if pno >= OFP_PORT_MAX or pno in inter:
                    continue
                if sw.dp.id == src_dpid and pno == in_port:
                    continue
                self._packet_out(dp, dp.ofproto.OFPP_CONTROLLER, pno, data)


class DiagRest(ControllerBase):
    def __init__(self, req, link, data, **config):
        super(DiagRest, self).__init__(req, link, data, **config)
        self.app = data['diag_app']

    @route('diag_paths', '/v1.0/diag/paths', methods=['GET'])
    def list_paths(self, req, **kwargs):
        result = []
        for (src, dst), path in sorted(self.app.active_paths.items()):
            util = self.app._path_utilization(path)
            result.append({
                'src_mac': src, 'dst_mac': dst,
                'src_ip': self.app.host_ip.get(src),
                'dst_ip': self.app.host_ip.get(dst),
                'path': ['s%d' % d for d in path],
                'path_dpids': path,
                'max_link_utilization_percent': round(util * 100, 2),
                'congested': util >= CONGESTION_THRESHOLD
            })
        return Response(content_type='application/json',
                        body=json.dumps(result, indent=2).encode('utf-8'))

    @route('diag_links', '/v1.0/diag/links', methods=['GET'])
    def list_links(self, req, **kwargs):
        result = []
        for src, neighbors in sorted(self.app.adj.items()):
            for dst, port in sorted(neighbors.items()):
                stats = self.app.link_stats.get((src, port, dst), {})
                util = stats.get('utilization', 0.0)
                result.append({
                    'src_switch': 's%d' % src, 'dst_switch': 's%d' % dst,
                    'src_port': port, 'tx_mbps': round(stats.get('tx_mbps', 0.0), 3),
                    'utilization_percent': round(util * 100, 2),
                    'congested': util >= CONGESTION_THRESHOLD,
                    'samples': stats.get('samples', 0)
                })
        return Response(content_type='application/json',
                        body=json.dumps(result, indent=2).encode('utf-8'))
