import argparse
import json
import os
import socket
import struct
import time

HDR = struct.Struct("!IQ")


def run_probes(dst_ip, port, count, interval, timeout, size):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    pad = b"\x00" * max(0, size - HDR.size)

    rtts = []
    lost = []
    late = 0

    for seq in range(count):
        t_send = time.perf_counter_ns()
        sock.sendto(HDR.pack(seq, t_send) + pad, (dst_ip, port))

        deadline = time.monotonic() + timeout
        got = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                data, _ = sock.recvfrom(65535)
            except socket.timeout:
                break
            t_recv = time.perf_counter_ns()
            if len(data) < HDR.size:
                continue
            r_seq, r_ts = HDR.unpack(data[:HDR.size])
            if r_seq != seq:
                late += 1
                continue
            rtts.append((t_recv - r_ts) / 1e6)
            got = True
            break

        if not got:
            lost.append(seq)

        if seq < count - 1:
            time.sleep(interval)

    sock.close()
    return rtts, lost, late


def summarize(rtts, lost, count):
    recv = len(rtts)
    res = {
        "sent": count,
        "recv": recv,
        "lost": len(lost),
        "loss_pct": round(100.0 * len(lost) / count, 2) if count else 0.0,
        "rtt_min_ms": None,
        "rtt_avg_ms": None,
        "rtt_max_ms": None,
        "jitter_ms": None,
    }
    if rtts:
        res["rtt_min_ms"] = round(min(rtts), 3)
        res["rtt_avg_ms"] = round(sum(rtts) / recv, 3)
        res["rtt_max_ms"] = round(max(rtts), 3)
    if recv > 1:
        diffs = [abs(rtts[i] - rtts[i - 1]) for i in range(1, recv)]
        res["jitter_ms"] = round(sum(diffs) / len(diffs), 3)
    return res


def main():
    ap = argparse.ArgumentParser(description="UDP diagnostic probe client")
    ap.add_argument("--dst", required=True)
    ap.add_argument("--port", type=int, default=5005)
    ap.add_argument("--count", type=int, default=100)
    ap.add_argument("--interval", type=float, default=0.1)
    ap.add_argument("--timeout", type=float, default=1.0)
    ap.add_argument("--size", type=int, default=64)
    ap.add_argument("--src-name", default="h1")
    ap.add_argument("--dst-name", default="h2")
    ap.add_argument("--out")
    args = ap.parse_args()

    print(f"[client] probing {args.dst}:{args.port} x{args.count} "
          f"(interval {args.interval}s, timeout {args.timeout}s)")
    rtts, lost, late = run_probes(args.dst, args.port, args.count,
                                  args.interval, args.timeout, args.size)

    result = {"src": args.src_name, "dst": args.dst_name, "dst_ip": args.dst,
              "port": args.port, "timestamp": int(time.time())}
    result.update(summarize(rtts, lost, args.count))
    result["lost_seqs"] = lost

    print(f"[client] sent={result['sent']} recv={result['recv']} "
          f"loss={result['loss_pct']}%")
    if rtts:
        print(f"[client] RTT min/avg/max = {result['rtt_min_ms']}/"
              f"{result['rtt_avg_ms']}/{result['rtt_max_ms']} ms, "
              f"jitter = {result['jitter_ms']} ms")
    else:
        print("[client] no replies received")
    if late:
        print(f"[client] ignored {late} late/duplicate replies")

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w") as f:
            json.dump(result, f, indent=2)
        print(f"[client] wrote {args.out}")


if __name__ == "__main__":
    main()
