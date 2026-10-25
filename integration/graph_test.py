import graph_builder

#hosts, switches and links will be provided by ryu api

#mock data for testing
switches = [
    {
        "dpid": "0000000000000001",
        "ports": [
            {"dpid": "0000000000000001", "port_no": "00000001", "hw_addr": "0a:b1:00:00:01:01", "name": "s1-eth1"},
            {"dpid": "0000000000000001", "port_no": "00000002", "hw_addr": "0a:b1:00:00:01:02", "name": "s1-eth2"},
            {"dpid": "0000000000000001", "port_no": "00000003", "hw_addr": "0a:b1:00:00:01:03", "name": "s1-eth3"},
        ],
    },
    {
        "dpid": "0000000000000002",
        "ports": [
            {"dpid": "0000000000000002", "port_no": "00000001", "hw_addr": "0a:b1:00:00:02:01", "name": "s2-eth1"},
            {"dpid": "0000000000000002", "port_no": "00000002", "hw_addr": "0a:b1:00:00:02:02", "name": "s2-eth2"},
        ],
    },
    {
        "dpid": "0000000000000003",
        "ports": [
            {"dpid": "0000000000000003", "port_no": "00000001", "hw_addr": "0a:b1:00:00:03:01", "name": "s3-eth1"},
            {"dpid": "0000000000000003", "port_no": "00000002", "hw_addr": "0a:b1:00:00:03:02", "name": "s3-eth2"},
        ],
    },
    {
        "dpid": "0000000000000004",
        "ports": [
            {"dpid": "0000000000000004", "port_no": "00000001", "hw_addr": "0a:b1:00:00:04:01", "name": "s4-eth1"},
            {"dpid": "0000000000000004", "port_no": "00000002", "hw_addr": "0a:b1:00:00:04:02", "name": "s4-eth2"},
            {"dpid": "0000000000000004", "port_no": "00000003", "hw_addr": "0a:b1:00:00:04:03", "name": "s4-eth3"},
        ],
    },
]

links = [
    {
        "src": {"dpid": "0000000000000001", "port_no": "00000002", "hw_addr": "0a:b1:00:00:01:02", "name": "s1-eth2"},
        "dst": {"dpid": "0000000000000002", "port_no": "00000001", "hw_addr": "0a:b1:00:00:02:01", "name": "s2-eth1"},
    },
    {
        "src": {"dpid": "0000000000000002", "port_no": "00000002", "hw_addr": "0a:b1:00:00:02:02", "name": "s2-eth2"},
        "dst": {"dpid": "0000000000000004", "port_no": "00000001", "hw_addr": "0a:b1:00:00:04:01", "name": "s4-eth1"},
    },
    {
        "src": {"dpid": "0000000000000001", "port_no": "00000003", "hw_addr": "0a:b1:00:00:01:03", "name": "s1-eth3"},
        "dst": {"dpid": "0000000000000003", "port_no": "00000001", "hw_addr": "0a:b1:00:00:03:01", "name": "s3-eth1"},
    },
    {
        "src": {"dpid": "0000000000000003", "port_no": "00000002", "hw_addr": "0a:b1:00:00:03:02", "name": "s3-eth2"},
        "dst": {"dpid": "0000000000000004", "port_no": "00000002", "hw_addr": "0a:b1:00:00:04:02", "name": "s4-eth2"},
    },
]

hosts = [
    {
        "mac": "00:00:00:00:00:01",
        "ipv4": ["10.0.0.1"],
        "ipv6": [],
        "port": {"dpid": "0000000000000001", "port_no": "00000001", "hw_addr": "0a:b1:00:00:01:01", "name": "s1-eth1"},
    },
    {
        "mac": "00:00:00:00:00:02",
        "ipv4": ["10.0.0.2"],
        "ipv6": [],
        "port": {"dpid": "0000000000000004", "port_no": "00000003", "hw_addr": "0a:b1:00:00:04:03", "name": "s4-eth3"},
    },
]

mock_diag_links = [
    {"src_switch": "s1", "dst_switch": "s2", "src_port": 2, "tx_mbps": 1.85, "utilization_percent": 18.5, "congested": False, "samples": 10},
    {"src_switch": "s2", "dst_switch": "s4", "src_port": 2, "tx_mbps": 1.82, "utilization_percent": 18.2, "congested": False, "samples": 10},
    {"src_switch": "s1", "dst_switch": "s3", "src_port": 3, "tx_mbps": 0.05, "utilization_percent": 0.5, "congested": False, "samples": 10},
    {"src_switch": "s3", "dst_switch": "s4", "src_port": 2, "tx_mbps": 0.05, "utilization_percent": 0.5, "congested": False, "samples": 10},
]


mock_diag_paths = [
    {
        "src_mac": "00:00:00:00:00:01",
        "dst_mac": "00:00:00:00:00:02",
        "src_ip": "10.0.0.1",
        "dst_ip": "10.0.0.2",
        "path": ["s1", "s2", "s4"],
        "path_dpids": [1, 2, 4],
        "max_link_utilization_percent": 18.5,
        "congested": False,
    }
]

#probe result from diagnostic
probe_result = {
    "src": "h1",
    "dst": "h2",
    "sent": 100,
    "recv": 97,
    "loss_pct": 15.0,
    "rtt_min_ms": 9.8,
    "rtt_avg_ms": 12.4,
    "rtt_max_ms": 19.1,
    "jitter_ms": 1.6,
}

switches_data = graph_builder.fetch_json("/v1.0/topology/switches", switches)
links_data = graph_builder.fetch_json("/v1.0/topology/links", links)
hosts_data = graph_builder.fetch_json("/v1.0/topology/hosts", hosts)
diag_links_data = graph_builder.fetch_json("/v1.0/diag/links", mock_diag_links)
diag_paths_data = graph_builder.fetch_json("/v1.0/diag/paths", mock_diag_paths)

G = graph_builder.generate_graph(switches_data, links_data, hosts_data, diag_links_data)

src, dst = probe_result["src"], probe_result["dst"]
active_sw_path, max_util_pct, is_congested = graph_builder.resolve_active_switch_path(G, src, dst, diag_paths_data)
active_full_path = [src] + active_sw_path + [dst]

graph_builder.draw_graph(G, active_full_path, probe_result, max_util_pct, is_congested, 'test3.png')

