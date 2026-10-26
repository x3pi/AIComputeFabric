#!/usr/bin/env python3
"""Data-plane load test (plan B.7 / P2.2): real llama-server managed by a real
worker-guard, reached through a real site-gateway. For each concurrency level it
runs a closed-loop streaming load and records TTFT, tok/s and rejections; it also
runs llama-server DIRECTLY at the same levels to isolate the proxy's overhead.
One GPU, one worker: this measures admission control and proxy cost, not fleet scale."""
import argparse, json, subprocess, time, urllib.request
from pathlib import Path

def get(url):
    with urllib.request.urlopen(url, timeout=3) as r:
        return json.load(r)

def main():
    ap = argparse.ArgumentParser()
    for k in ("gateway", "guard", "server", "model", "inferload", "out", "workdir"):
        ap.add_argument("--" + k, required=True)
    ap.add_argument("--slots", type=int, default=8)
    ap.add_argument("--ctx", type=int, default=1024)
    ap.add_argument("--levels", default="1,8,32,64")
    ap.add_argument("--direct-levels", default="1,8")
    ap.add_argument("--duration", default="30s")
    ap.add_argument("--guard-args", default="", help="extra worker-guard flags, e.g. '--flash-attn off'")
    ap.add_argument("--max-queue", type=int, default=16)
    a = ap.parse_args()
    wd = Path(a.workdir); wd.mkdir(parents=True, exist_ok=True)
    procs = []
    rows = {"via_gateway": [], "direct_engine": []}
    try:
        procs.append(subprocess.Popen([a.gateway, "-grpc", "127.0.0.1:17643", "-http", "127.0.0.1:18380",
                                       "-max-queue", str(a.max_queue), "-max-wait", "10s"],
                                      stdout=open(wd / "gw.log", "w"), stderr=subprocess.STDOUT))
        time.sleep(1)
        procs.append(subprocess.Popen([a.guard, "--llama-server", a.server, "--model", a.model, "--ctx", str(a.ctx),
                                       "--parallel", str(a.slots), "--headroom-mib", "512", "--engine-port", "18390",
                                       "--listen", "127.0.0.1:18399", "--gateway", "127.0.0.1:17643", "--worker-id", "pc-001"] + a.guard_args.split(),
                                      stdout=open(wd / "guard.log", "w"), stderr=subprocess.STDOUT))
        t0 = time.time()
        while time.time() - t0 < 240:
            try:
                ws = get("http://127.0.0.1:18380/api/workers")
                if ws and ws[0]["state"] == "running":
                    break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            raise SystemExit("worker never running")
        time.sleep(3)  # router cache refresh + warm
        model = ws[0]["model"]

        def run(kind, url, c):
            extra = ["-no-keepalive"] if kind == "direct_engine" else []  # llama-server keep-alive conns go stale
            r = subprocess.run([a.inferload, "-url", url, "-model", model, "-c", str(c), "-duration", a.duration] + extra,
                               capture_output=True, text=True)
            row = json.loads(r.stdout)
            if kind == "via_gateway":
                met = urllib.request.urlopen("http://127.0.0.1:18380/metrics").read().decode()
                row["gateway_metrics"] = {l.split(" ")[0]: float(l.split(" ")[-1]) for l in met.splitlines()
                                          if l and not l.startswith("#")}
            rows[kind].append(row)
            print(kind, c, json.dumps({k: row[k] for k in ("aggregate_tok_s", "ttft_ms_p50", "ttft_ms_p95", "status_counts")}), flush=True)

        for c in map(int, a.direct_levels.split(",")):
            run("direct_engine", "http://127.0.0.1:18390/v1/chat/completions", c)
        for c in map(int, a.levels.split(",")):
            run("via_gateway", "http://127.0.0.1:18380/v1/chat/completions", c)
    finally:
        for p in reversed(procs):
            p.terminate()
        time.sleep(2)
        subprocess.run(["pkill", "-x", "llama-server"], check=False)
    Path(a.out).write_text(json.dumps({"schema_version": 1, "model": Path(a.model).name, "slots": a.slots,
                                       "ctx_per_slot": a.ctx, "max_queue": a.max_queue, "rows": rows}, indent=2))

if __name__ == "__main__":
    main()
