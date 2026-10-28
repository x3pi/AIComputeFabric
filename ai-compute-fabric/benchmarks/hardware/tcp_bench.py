#!/usr/bin/env python3
"""Two-way TCP throughput benchmark between Linux and Windows nodes."""
import argparse
import socket
import time

CHUNK_SIZE = 128 * 1024  # 128 KB
PAYLOAD = b"x" * CHUNK_SIZE


def run_server(port: int):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port))
    s.listen(1)
    print(f"SERVER_LISTENING on {port}", flush=True)
    conn, addr = s.accept()
    t0 = time.time()
    total_bytes = 0
    while True:
        data = conn.recv(CHUNK_SIZE)
        if not data:
            break
        total_bytes += len(data)
    elapsed = time.time() - t0
    conn.close()
    s.close()
    mbps = (total_bytes / (1024 * 1024)) / elapsed if elapsed > 0 else 0
    gbps = (total_bytes * 8 / 1e9) / elapsed if elapsed > 0 else 0
    print(f"RESULT: total_bytes={total_bytes}, elapsed_s={elapsed:.3f}, MB_s={mbps:.2f}, Gbps={gbps:.3f}", flush=True)


def run_client(host: str, port: int, duration_s: float):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((host, port))
    t0 = time.time()
    total_bytes = 0
    while time.time() - t0 < duration_s:
        s.sendall(PAYLOAD)
        total_bytes += len(PAYLOAD)
    elapsed = time.time() - t0
    s.shutdown(socket.SHUT_WR)
    s.close()
    mbps = (total_bytes / (1024 * 1024)) / elapsed if elapsed > 0 else 0
    gbps = (total_bytes * 8 / 1e9) / elapsed if elapsed > 0 else 0
    print(f"CLIENT_RESULT: total_bytes={total_bytes}, elapsed_s={elapsed:.3f}, MB_s={mbps:.2f}, Gbps={gbps:.3f}", flush=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["server", "client"], required=True)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=50055)
    p.add_argument("--duration", type=float, default=10.0)
    args = p.parse_args()
    if args.mode == "server":
        run_server(args.port)
    else:
        run_client(args.host, args.port, args.duration)


if __name__ == "__main__":
    main()
