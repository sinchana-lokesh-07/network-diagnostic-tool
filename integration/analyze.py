import argparse
import json
import os
import sys
import time
import urllib.request

os.environ.setdefault("MPLBACKEND", "Agg")

import networkx as nx

import graph_builder as gb


def get(base, endpoint):
    try:
        with urllib.request.urlopen(base + endpoint, timeout=3) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        sys.exit(f"cannot reach Ryu at {base}{endpoint}: {exc}")


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def switch_links(G):
    """Undirected switch-to-switch links currently in the graph, as sorted pairs."""
    return {tuple(sorted((u, v))) for u, v in G.edges
            if G.nodes[u].get("node_type") == "switch" and G.nodes[v].get("node_type") == "switch"}


def attached_switch(G, host):
    for nbr in G.neighbors(host):
        if G.nodes[nbr].get("node_type") == "switch":
            return nbr
    return None


def find_failures(G, baseline, src, dst):
    """Compare the live topology with the healthy baseline.

    Returns (failed_links, failed_paths). A failed path is a baseline switch path
    between the hosts' switches that uses at least one failed link.
    """
    base_links = {tuple(l) for l in baseline["links"]}
    failed = sorted(base_links - switch_links(G))
    if not failed:
        return [], []

    B = nx.Graph()
    B.add_edges_from(base_links)
    s_src, s_dst = attached_switch(G, src), attached_switch(G, dst)
    failed_set = set(failed)
    failed_paths = []
    if s_src in B and s_dst in B:
        for p in nx.all_simple_paths(B, s_src, s_dst):
            hops = {tuple(sorted(pair)) for pair in zip(p[:-1], p[1:])}
            if hops & failed_set:
                failed_paths.append(p)
    return failed, failed_paths


def update_history(path, probe, sw_path):
    """Append this probe to the history (once per probe) and return (history, warning).

    A probe measures one path at one moment. If the same probe was already recorded
    for a different path, it is stale (taken before a reroute) and is not recorded again.
    """
    history = load_json(path, [])
    ts = probe.get("timestamp")
    for h in history:
        if h["src"] == probe["src"] and h["dst"] == probe["dst"] and ts is not None \
                and h.get("probe_timestamp") == ts:
            if h["path"] != list(sw_path):
                return history, ("this probe was already recorded for path %s; it is stale for the "
                                 "current path %s, so it was NOT added to the comparison. "
                                 "Re-run the probe." % (" -> ".join(h["path"]), " -> ".join(sw_path)))
            return history, None
    history.append({
        "src": probe["src"], "dst": probe["dst"], "path": list(sw_path),
        "probe_timestamp": ts,
        "rtt_min_ms": probe.get("rtt_min_ms"), "rtt_avg_ms": probe.get("rtt_avg_ms"),
        "rtt_max_ms": probe.get("rtt_max_ms"), "jitter_ms": probe.get("jitter_ms"),
        "loss_pct": probe.get("loss_pct"),
    })
    save_json(path, history)
    return history, None


def compare_paths(history, src, dst, active_path, failed_paths=()):
    """Latest measurement per distinct path for this host pair, best (lowest RTT) first."""
    failed = {tuple(p) for p in failed_paths}
    latest = {}
    for h in history:
        if h["src"] == src and h["dst"] == dst:
            latest[tuple(h["path"])] = h  # later entries overwrite earlier ones
    rows = []
    for path, h in latest.items():
        rows.append({
            "path": list(path),
            "rtt_avg_ms": h["rtt_avg_ms"], "rtt_min_ms": h["rtt_min_ms"],
            "rtt_max_ms": h["rtt_max_ms"], "jitter_ms": h["jitter_ms"],
            "loss_pct": h["loss_pct"],
            "active": list(path) == list(active_path),
            "failed": path in failed,
        })
    # A path that currently contains a failed link can never be "best".
    usable = [r for r in rows if not r["failed"]
              and r["rtt_avg_ms"] is not None and r["loss_pct"] < 100]
    rows.sort(key=lambda r: (r["rtt_avg_ms"] is None, r["rtt_avg_ms"] or 0.0))
    if usable:
        best = min(usable, key=lambda r: (r["loss_pct"], r["rtt_avg_ms"]))
        for r in rows:
            r["best"] = r is best
    else:
        for r in rows:
            r["best"] = False
    return rows


def main():
    ap = argparse.ArgumentParser(description="merge probe result with SDN path")
    ap.add_argument("--probe", required=True)
    ap.add_argument("--out", default="results/topology_path.png")
    ap.add_argument("--ryu", default="http://127.0.0.1:8080")
    ap.add_argument("--baseline", default="../results/baseline_topology.json",
                    help="healthy topology snapshot used to detect failed links")
    ap.add_argument("--save-baseline", action="store_true",
                    help="save the current (healthy) topology as the baseline")
    ap.add_argument("--history", default="../results/history.json",
                    help="probe history used to compare paths")
    ap.add_argument("--max-probe-age", type=float, default=120.0,
                    help="warn if the probe file is older than this many seconds")
    args = ap.parse_args()

    with open(args.probe) as f:
        probe = json.load(f)

    switches = get(args.ryu, "/v1.0/topology/switches")
    links = get(args.ryu, "/v1.0/topology/links")
    hosts = get(args.ryu, "/v1.0/topology/hosts")
    diag_links = get(args.ryu, "/v1.0/diag/links")
    diag_paths = get(args.ryu, "/v1.0/diag/paths")

    G = gb.generate_graph(switches, links, hosts, diag_links)

    src, dst = probe["src"], probe["dst"]
    for name in (src, dst):
        if name not in G:
            sys.exit(f"{name} not in topology yet, ping between hosts first so Ryu learns them")

    warnings = []
    age = time.time() - probe["timestamp"] if probe.get("timestamp") else None
    if age is not None and age > args.max_probe_age:
        warnings.append(f"probe is {age:.0f}s old; re-run the probe after any topology change "
                        "so RTT/loss describe the current path")

    if args.save_baseline:
        save_json(args.baseline, {"switches": sorted(n for n, a in G.nodes(data=True)
                                                   if a.get("node_type") == "switch"),
                                  "links": sorted(switch_links(G))})
        print(f"saved baseline topology to {args.baseline}", file=sys.stderr)

    baseline = load_json(args.baseline, None)
    failed_links, failed_paths = ([], [])
    if baseline:
        failed_links, failed_paths = find_failures(G, baseline, src, dst)
    else:
        warnings.append("no baseline topology; run once on the healthy network with --save-baseline "
                        "to enable failed-link detection")

    try:
        sw_path, max_util, congested = gb.resolve_active_switch_path(G, src, dst, diag_paths)
        sw_path = list(sw_path)
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        sw_path, max_util, congested = [], 0.0, False
    full_path = [src] + sw_path + [dst] if sw_path else [src]

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    report = gb.draw_graph(G, full_path, probe, max_util, congested, args.out,
                           failed_links=failed_links, print_json=False)

    if not sw_path:
        status = "NO_PATH"
    elif failed_links:
        status = "REROUTED_AFTER_FAILURE"
    elif probe["loss_pct"] > 10.0 or congested:
        status = "DEGRADED"
    else:
        status = "OK"

    if sw_path:
        history, hist_warning = update_history(args.history, probe, sw_path)
        if hist_warning:
            warnings.append(hist_warning)
    else:
        history = load_json(args.history, [])
    report.update({
        "status": status,
        "failed_links": [list(l) for l in failed_links],
        "failed_paths": failed_paths,
        "path_comparison": compare_paths(history, src, dst, sw_path, failed_paths),
        "probe_age_s": round(age, 1) if age is not None else None,
        "warnings": warnings,
    })

    print(json.dumps(report, indent=2))
    print(f"saved {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
