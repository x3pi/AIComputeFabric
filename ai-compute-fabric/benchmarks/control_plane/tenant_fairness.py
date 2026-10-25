#!/usr/bin/env python3
"""Multi-tenant fairness on a real GPU (plan I.9). A NOISY tenant keeps 24 closed-loop
clients on one worker (8 slots) while a QUIET tenant sends 2 closed-loop clients.
Same gateway binary, same engine, run twice: -fair=false (one shared FIFO, the v1
behaviour) and -fair=true (weighted fair queuing + per-tenant queue share).
One GPU, one worker, two tenants: shows the queueing policy, not fleet behaviour."""
import argparse, json, subprocess, time, urllib.request
from pathlib import Path

def get(url):
    with urllib.request.urlopen(url, timeout=3) as r:
        return json.load(r)

def run_mode(a, fair, wd):
    procs = []
    try:
        procs.append(subprocess.Popen([a.gateway, "-grpc", "127.0.0.1:17743", "-http", "127.0.0.1:18480", "-max-queue", "16",
                                       "-max-wait", "20s", f"-fair={'true' if fair else 'false'}"],
                                      stdout=open(wd / f"gw_{fair}.log", "w"), stderr=subprocess.STDOUT))
        time.sleep(1)
        procs.append(subprocess.Popen([a.guard, "--llama-server", a.server, "--model", a.model, "--ctx", "1024", "--parallel", "8",
                                       "--headroom-mib", "512", "--engine-port", "18490", "--listen", "127.0.0.1:18499",
                                       "--gateway", "127.0.0.1:17743", "--worker-id", "pc-001"],
                                      stdout=open(wd / f"guard_{fair}.log", "w"), stderr=subprocess.STDOUT))
        t0 = time.time()
        while time.time() - t0 < 240:
            try:
                ws = get("http://127.0.0.1:18480/api/workers")
                if ws and ws[0]["state"] == "running":
                    break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            raise SystemExit("worker never running")
        time.sleep(3)
        url = "http://127.0.0.1:18480/v1/chat/completions"
        common = ["-url", url, "-model", ws[0]["model"], "-duration", a.duration]
        noisy = subprocess.Popen([a.inferload] + common + ["-c", "24", "-tenant", "noisy"], stdout=subprocess.PIPE, text=True)
        time.sleep(2)  # the noisy tenant is already queueing when the quiet one arrives
        quiet = subprocess.Popen([a.inferload] + common + ["-c", "2", "-tenant", "quiet"], stdout=subprocess.PIPE, text=True)
        n, q = json.loads(noisy.communicate()[0]), json.loads(quiet.communicate()[0])
        met = urllib.request.urlopen("http://127.0.0.1:18480/metrics").read().decode()
        return {"fair": fair, "noisy": n, "quiet": q,
                "gateway_metrics": {l.split(" ")[0]: float(l.split(" ")[-1]) for l in met.splitlines() if l and not l.startswith("#")}}
    finally:
        for p in reversed(procs):
            p.terminate()
        time.sleep(2)
        subprocess.run(["pkill", "-x", "llama-server"], check=False)

def main():
    ap = argparse.ArgumentParser()
    for k in ("gateway", "guard", "server", "model", "inferload", "out", "workdir"):
        ap.add_argument("--" + k, required=True)
    ap.add_argument("--duration", default="40s")
    a = ap.parse_args()
    wd = Path(a.workdir); wd.mkdir(parents=True, exist_ok=True)
    rows = [run_mode(a, f, wd) for f in (False, True)]
    for r in rows:
        print(r["fair"], "quiet:", {k: r["quiet"][k] for k in ("ok", "status_counts", "ttft_ms_p50", "ttft_ms_p95")},
              "noisy:", {k: r["noisy"][k] for k in ("ok", "aggregate_tok_s")}, flush=True)
    Path(a.out).write_text(json.dumps({"schema_version": 1, "model": Path(a.model).name, "slots": 8, "max_queue": 16,
                                       "noisy_clients": 24, "quiet_clients": 2, "modes": rows}, indent=2))

if __name__ == "__main__":
    main()
