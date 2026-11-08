#!/usr/bin/env python3
"""TCP proxy that makes one link behave like the measured Wi-Fi link (user-space emulation, no root needed).

Why: the RTX 5070 PC has no Wi-Fi adapter present, so "5070 over Wi-Fi" cannot be measured for real.
Per forwarded chunk the proxy applies (a) a serialisation delay at a per-direction rate cap and (b) a one-way delay
resampled from real ping RTT/2 samples of the Linux<->fi-ai03 Wi-Fi link, keeping TCP byte order (FIFO).

Usage: wifi_link_emulator.py --listen 127.0.0.1:50160 --upstream 10.42.0.225:50152 \
         --rtt-samples wifi_rtt_samples.txt --up-MBps 36.7 --down-MBps 50.6
"up" = client (master) -> upstream (rpc-server), "down" = upstream -> client.
"""
import argparse
import asyncio
import random
import socket
import time


def nodelay(w: asyncio.StreamWriter) -> None:
    s = w.get_extra_info("socket")
    if s is not None:
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)


async def pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter, rate: float, delays: list, stats: dict, key: str) -> None:
    q: asyncio.Queue = asyncio.Queue(maxsize=32)
    busy = [0.0]
    last = [0.0]

    async def reader() -> None:
        try:
            while True:
                data = await r.read(65536)
                if not data:
                    break
                now = time.monotonic()
                busy[0] = max(busy[0], now) + len(data) / rate            # link serialisation at the rate cap
                due = max(busy[0] + random.choice(delays), last[0])      # + propagation/jitter, never reorder
                last[0] = due
                await q.put((due, data))
        finally:
            await q.put(None)

    async def writer() -> None:
        try:
            while True:
                item = await q.get()
                if item is None:
                    break
                due, data = item
                wait = due - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                w.write(data)
                await w.drain()
                stats[key] += len(data)
        finally:
            try:
                w.close()
            except Exception:
                pass

    await asyncio.gather(reader(), writer())


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", required=True)
    ap.add_argument("--upstream", required=True)
    ap.add_argument("--rtt-samples", required=True)
    ap.add_argument("--up-MBps", type=float, default=36.7)
    ap.add_argument("--down-MBps", type=float, default=50.6)
    a = ap.parse_args()
    delays = [float(x) / 2000.0 for x in open(a.rtt_samples).read().split()]   # RTT ms -> one-way seconds
    lh, lp = a.listen.rsplit(":", 1)
    uh, up = a.upstream.rsplit(":", 1)
    stats = {"up": 0, "down": 0}

    async def handle(cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
        ur, uw = await asyncio.open_connection(uh, int(up))
        nodelay(cw); nodelay(uw)
        await asyncio.gather(pipe(cr, uw, a.up_MBps * 1e6, delays, stats, "up"),
                             pipe(ur, cw, a.down_MBps * 1e6, delays, stats, "down"))

    srv = await asyncio.start_server(handle, lh, int(lp))
    print(f"wifi-emulator listening on {a.listen} -> {a.upstream}; mean one-way {1000*sum(delays)/len(delays):.2f} ms, "
          f"up {a.up_MBps} MB/s, down {a.down_MBps} MB/s", flush=True)
    async with srv:
        await srv.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
