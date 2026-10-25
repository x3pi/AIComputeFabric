#!/usr/bin/env python3
"""Gateway capacity test: N synthetic workers @1 Hz + M SSE viewers against a real
site-gateway process. Reports heartbeat latency and the gateway's own CPU/RSS
(from /proc). Workers are simulated on purpose: this measures the control plane
only. Loadgen and gateway share one machine, so absolute numbers are pessimistic
for the gateway (they compete for CPU) and say nothing about real WAN latency."""
import argparse, json, os, subprocess, time

def cpu_ticks(pid):
    f = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
    return int(f[11]) + int(f[12])

def rss_mb(pid):
    for ln in open(f"/proc/{pid}/status"):
        if ln.startswith("VmRSS"):
            return int(ln.split()[1]) / 1024

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", required=True); ap.add_argument("--loadgen", required=True)
    ap.add_argument("--scenarios", required=True, help="workers:viewers,workers:viewers,...")
    ap.add_argument("--duration", type=int, default=20); ap.add_argument("--out", required=True)
    a = ap.parse_args()
    rows = []
    for sc in a.scenarios.split(","):
        n, v = map(int, sc.split(":"))
        g = subprocess.Popen([a.gateway, "-grpc", "127.0.0.1:17543", "-http", "127.0.0.1:18180"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)
        t0, c0 = time.time(), cpu_ticks(g.pid)
        r = subprocess.run([a.loadgen, "-grpc", "127.0.0.1:17543", "-http", "127.0.0.1:18180", "-workers", str(n), "-viewers", str(v),
                            "-duration", f"{a.duration}s"], capture_output=True, text=True)
        wall = time.time() - t0
        row = json.loads(r.stdout) if r.stdout.strip().startswith("{") else {"workers": n, "viewers": v, "error": r.stderr[-300:]}
        row["gateway_cpu_cores_avg"] = round((cpu_ticks(g.pid) - c0) / os.sysconf("SC_CLK_TCK") / wall, 3)
        row["gateway_rss_mb"] = round(rss_mb(g.pid), 1)
        g.terminate(); g.wait(10)
        rows.append(row); print(json.dumps(row), flush=True)
    json.dump({"schema_version": 1, "host_cpus": os.cpu_count(), "note": "loadgen + gateway on the same host",
               "scenarios": rows}, open(a.out, "w"), indent=2)

if __name__ == "__main__":
    main()
