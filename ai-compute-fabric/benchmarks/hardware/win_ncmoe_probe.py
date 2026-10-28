#!/usr/bin/env python3
"""Portable (Windows/Linux) -ncmoe probe with llama-server: for each N start the server at the given ctx, check it becomes
healthy (OOM/exit otherwise), run N requests, record decode tok/s and peak GPU memory.used. Measures only."""
import argparse, json, os, re, subprocess, threading, time, urllib.request

OOM = re.compile(r"out of memory|cudaMalloc failed|failed to allocate|CUDA error", re.I)


def vram():
    try:
        return float(subprocess.run(["nvidia-smi", "-i", "0", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                                    capture_output=True, text=True, timeout=15).stdout.strip())
    except Exception:
        return float("nan")


def req(url, n=128):
    b = {"model": "x", "messages": [{"role": "user", "content": "Giải thích ngắn gọn bộ nhớ đệm (cache) là gì."}],
         "max_tokens": n, "temperature": 0, "seed": 1}
    r = json.loads(urllib.request.urlopen(urllib.request.Request(url + "/v1/chat/completions", json.dumps(b).encode(),
                   {"Content-Type": "application/json"}), timeout=600).read())
    return r["timings"]["predicted_per_second"]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--server", required=True); p.add_argument("--model", required=True)
    p.add_argument("--ns", required=True); p.add_argument("--ctx", type=int, default=8192)
    p.add_argument("--np", type=int, default=1); p.add_argument("--port", type=int, default=18291)
    p.add_argument("--reps", type=int, default=3); p.add_argument("--start-timeout", type=int, default=600)
    p.add_argument("--out", required=True); p.add_argument("--logdir", required=True)
    a = p.parse_args()
    os.makedirs(a.logdir, exist_ok=True)
    for n in [int(x) for x in a.ns.split(",")]:
        log = os.path.join(a.logdir, f"ncmoe_N{n}_c{a.ctx}.log")
        cmd = [a.server, "-m", a.model, "-ngl", "99", "-fa", "on", "-c", str(a.ctx), "-np", str(a.np), "--port", str(a.port), "--host", "127.0.0.1"]
        if n > 0:
            cmd += ["-ncmoe", str(n)]
        base = vram(); peak = [base]; stop = threading.Event()
        def samp(stop_ev=stop, pk=peak):
            while not stop_ev.is_set():
                pk[0] = max(pk[0], vram()); time.sleep(0.5)
        threading.Thread(target=samp, daemon=True).start()
        lf = open(log, "w"); proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT)
        url = f"http://127.0.0.1:{a.port}"; res = {"n_cpu_moe": n, "ctx": a.ctx, "np": a.np, "gpu_used_before_mib": base, "healthy": False}
        t0 = time.time()
        while time.time() - t0 < a.start_timeout and proc.poll() is None:
            try:
                if urllib.request.urlopen(url + "/health", timeout=2).status == 200:
                    res["healthy"] = True; break
            except Exception:
                time.sleep(1)
        res["start_s"] = round(time.time() - t0, 1)
        if res["healthy"]:
            try:
                req(url, 16)
                res["tg_tok_s"] = [round(req(url), 1) for _ in range(a.reps)]
            except Exception as e:
                res["error"] = str(e)[:200]
        else:
            res["exit_code"] = proc.poll()
        stop.set(); proc.terminate()
        try: proc.wait(timeout=60)
        except Exception: proc.kill()
        lf.close(); time.sleep(3)
        res["peak_gpu_used_mib"] = peak[0]
        res["oom"] = bool(OOM.search(open(log, errors="replace").read()))
        res["log"] = log; res["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        open(a.out, "a").write(json.dumps(res) + "\n")
        print(json.dumps(res), flush=True)


if __name__ == "__main__":
    main()
