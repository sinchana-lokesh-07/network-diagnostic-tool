import argparse
import socket


def main():
    ap = argparse.ArgumentParser(description="UDP echo server")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=5005)
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.bind, args.port))
    print(f"[server] echoing UDP on {args.bind}:{args.port}", flush=True)

    count = 0
    try:
        while True:
            data, addr = sock.recvfrom(65535)
            sock.sendto(data, addr)
            count += 1
    except KeyboardInterrupt:
        print(f"\n[server] stopped, echoed {count} packets")
    finally:
        sock.close()


if __name__ == "__main__":
    main()
