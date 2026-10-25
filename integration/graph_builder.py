 import json
import urllib.request
import matplotlib.pyplot as plt
import networkx as nx

# url for ryu app
RYU_BASE_URL = "http://localhost:8080"

# parameters used by ryu
HOP_WEIGHT = 1.0
CONGESTION_WEIGHT = 100.0


# fetch data from ryu api, falling back to mock data in case ryu is offline
def fetch_json(endpoint, fallback):
    try:
        with urllib.request.urlopen(f"{RYU_BASE_URL}{endpoint}", timeout=2.0) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        print("falling back to mock values for " + endpoint)
        return fallback


# create networkx graph using ryu api data
def generate_graph(switches, links, hosts, diag_links):
    G = nx.Graph()

    for sw in switches:
        sw_name = f"s{int(sw['dpid'], 16)}"  # convert base 16 string to human readable (0000000000000001 becomes s1)
        G.add_node(sw_name, node_type="switch", dpid=sw["dpid"])

    for host in hosts:
        # last section of MAC address corresponds to host in Mininet
        # (00:00:00:00:00:01 corresponds to h1)
        h_name = f"h{int(host['mac'].split(':')[-1], 16)}"
        sw_name = f"s{int(host['port']['dpid'], 16)}"
        ip_addr = host["ipv4"][0] if host.get("ipv4") else None
        G.add_node(h_name, node_type="host", mac=host["mac"], ip=ip_addr)
        G.add_edge(h_name, sw_name, port=int(host["port"]["port_no"], 16), cost=1.0, utilization_percent=0.0)

    for link in links:
        u = f"s{int(link['src']['dpid'], 16)}"
        v = f"s{int(link['dst']['dpid'], 16)}"
        G.add_edge(u, v, cost=HOP_WEIGHT, utilization_percent=0.0, tx_mbps=0.0, congested=False)

    # use live utilisation percent and cost from ryu api
    for dlink in diag_links:
        u, v = dlink["src_switch"], dlink["dst_switch"]
        util_ratio = dlink["utilization_percent"] / 100.0

        # same formula used by ryu app to calculate cost
        cost = HOP_WEIGHT + CONGESTION_WEIGHT * (util_ratio ** 2)

        if G.has_edge(u, v):
            if dlink["utilization_percent"] >= G[u][v].get("utilization_percent", 0.0):
                G[u][v]["utilization_percent"] = dlink["utilization_percent"]
                G[u][v]["tx_mbps"] = dlink["tx_mbps"]
                G[u][v]["congested"] = dlink["congested"]
                G[u][v]["cost"] = cost

    return G


# try using active path reported by /v1.0/diag/paths, if not fall back to networkx shortest path
def resolve_active_switch_path(G, src_host, dst_host, diag_paths):
    src_mac = G.nodes[src_host].get("mac")
    dst_mac = G.nodes[dst_host].get("mac")

    for entry in diag_paths:
        if entry.get("src_mac") == src_mac and entry.get("dst_mac") == dst_mac:
            return entry["path"], entry.get("max_link_utilization_percent", 0.0), entry.get("congested", False)

    # fallback: networkx shortest path using cost formula
    full_path = nx.shortest_path(G, source=src_host, target=dst_host, weight="cost")
    sw_path = full_path[1:-1]

    max_util = max(
        (G[sw_path[i]][sw_path[i + 1]].get("utilization_percent", 0.0) for i in range(len(sw_path) - 1)),
        default=0.0,
    )

    return sw_path, max_util, max_util >= 70.0


# draw the created graph using matplotlib, if source and target are specified, highlight a path from source to target
# show possible link outages, suggest alternate paths in merged output if source and target are specified
def draw_graph(G, active_full_path, probe_data, max_util_pct, is_congested, output_filename,
               failed_links=None, print_json=True):
    failed_links = failed_links or []

    # layout includes failed links so node positions stay the same before/after an outage
    layout_graph = G.copy()
    layout_graph.add_edges_from(failed_links)
    pos = nx.spring_layout(layout_graph, seed=67)

    # separate switches and hosts from networkx graph
    switch_nodes = [n for n, attr in G.nodes(data=True) if attr.get("node_type") == "switch"]
    host_nodes = [n for n, attr in G.nodes(data=True) if attr.get("node_type") == "host"]

    # add elements to graph (smaller square and circle sizes)
    nx.draw_networkx_nodes(G, pos, nodelist=switch_nodes, node_color="#0000ff", node_shape="s", node_size=450, label="Switches")
    nx.draw_networkx_nodes(G, pos, nodelist=host_nodes, node_color="#409040", node_shape="o", node_size=350, label="Hosts")
    nx.draw_networkx_edges(G, pos, edge_color="#cccccc", width=2)
    if failed_links:
        nx.draw_networkx_edges(layout_graph, pos, edgelist=failed_links, edge_color="#ff0000",
                               width=2, style="dashed")
        nx.draw_networkx_edge_labels(
            layout_graph, pos, edge_labels={tuple(e): "DOWN" for e in failed_links},
            font_size=8, font_color="#ff0000", bbox=dict(facecolor="white", edgecolor="none", pad=1.5))
    nx.draw_networkx_labels(G, pos, font_size=9, font_weight="bold", font_color="white")

    # create list of edges by zipping everything except the last node, and everything except the first node
    path_edges = list(zip(active_full_path[:-1], active_full_path[1:]))

    # check whether path is congested from using either probe or ryu
    is_unhealthy = probe_data["loss_pct"] > 10.0 or is_congested
    path_color = "#ff0000" if is_unhealthy else "#2ECC71"

    # draw active path edges BEFORE edge labels so the red/green line stays underneath the text
    nx.draw_networkx_edges(G, pos, edgelist=path_edges, edge_color=path_color, width=3.5)

    # get labels for each edge from networkx graph and draw with a white background box so they stay readable
    edge_labels = {
        (u, v): f"{attr.get('utilization_percent', 0.0):.1f}%"
        for u, v, attr in G.edges(data=True)
        if G.nodes[u].get("node_type") == "switch" and G.nodes[v].get("node_type") == "switch"
        # hide idle links: only label links carrying real traffic (>= 1% utilization)
        and attr.get("utilization_percent", 0.0) >= 1.0
    }
    if edge_labels:
        nx.draw_networkx_edge_labels(
            G,
            pos,
            edge_labels=edge_labels,
            font_size=8,
            bbox=dict(facecolor="white", edgecolor="none", pad=1.5),
        )

    # ignore hosts to provide path with only switches, so if path is ["h1", "s1", "s2", "s4", "h2"],
    # active_switch_path is ["s1", "s2", "s4"]
    active_switch_path = active_full_path[1:-1]
    all_switch_paths = [p[1:-1] for p in nx.all_simple_paths(G, source=probe_data["src"], target=probe_data["dst"])]

    path_text = " -> ".join(active_full_path) if len(active_full_path) > 2 else "NO PATH"
    plt.title(
        f"Path: {path_text} | RTT: {probe_data['rtt_avg_ms']} ms | "
        f"Loss: {probe_data['loss_pct']}% | Max Link Util: {max_util_pct}%",
        fontsize=9.5,
    )
    plt.legend(markerscale=0.55, labelspacing=0.6, borderpad=0.6)
    plt.axis("off")
    plt.tight_layout()
    plt.savefig(output_filename, dpi=150)
    plt.close()

    merged_output = {
        "src": probe_data["src"],
        "dst": probe_data["dst"],
        "path": active_switch_path,
        "alternative_paths": [p for p in all_switch_paths if p != active_switch_path],
        "rtt_avg_ms": probe_data["rtt_avg_ms"],
        "loss_pct": probe_data["loss_pct"],
        "max_link_utilization_percent": max_util_pct,
        "congested": is_congested,
    }

    if print_json:
        print(json.dumps(merged_output, indent=2))
    return merged_output
