#!/usr/bin/env python3
"""Pha 1.1/1.2 helper: start llama-server with one config at a realistic context, wait for /health, send a Vietnamese
chat request a few times, record peak GPU/RAM and llama-server's own timings. One JSON line per config is appended.
Failures (OOM, exit, timeout) are recorded too. Needs the GPU to itself."""
import argparse, json, subprocess, sys, time, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from sweep_ncmoe import Sampler, OOM_RE  # noqa: E402

PROMPT = "Hãy giải thích ngắn gọn bằng tiếng Việt vì sao bầu trời có màu xanh, trong khoảng 120 từ."


def post(url, body, timeout=300):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def run(a, extra, tag):
    here = Path(a.out).parent
    log = here / "logs" / f"p3_server_{tag}.log"
    cmd = [a.server, "-m", a.model, "-c", str(a.ctx), "-np", "1", "-ngl", "99", "-fa", "on", "--no-warmup", "--fit", "off",
           "--port", str(a.port), "--host", "127.0.0.1", "-lv", "4"] + extra
    s = Sampler(0)
    s.start()
    lf = open(log, "w")
    lf.write("$ " + " ".join(cmd) + "\n")
    lf.flush()
    p = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT)
    res = {"tag": tag, "extra": extra, "ctx": a.ctx, "log": str(log), "status": None, "runs": []}
    t0 = time.time()
    try:
        while time.time() - t0 < a.start_timeout:
            if p.poll() is not None:
                res["status"] = f"exited rc={p.returncode} before healthy"
                break
            try:
                if urllib.request.urlopen(f"http://127.0.0.1:{a.port}/health", timeout=2).status == 200:
                    res["status"] = "healthy"
                    res["load_s"] = round(time.time() - t0, 1)
                    break
            except Exception:
                time.sleep(1)
        else:
            res["status"] = "start timeout"
        if res["status"] == "healthy":
            for _ in range(a.reps):
                r = post(f"http://127.0.0.1:{a.port}/v1/chat/completions",
                         {"model": "x", "messages": [{"role": "user", "content": PROMPT}], "max_tokens": a.n_predict,
                          "temperature": 0, "seed": 1})
                tm = r.get("timings", {})
                res["runs"].append({"prompt_n": tm.get("prompt_n"), "prompt_ms": tm.get("prompt_ms"),
                                    "predicted_n": tm.get("predicted_n"), "predicted_per_second": tm.get("predicted_per_second"),
                                    "text_head": r["choices"][0]["message"]["content"][:80]})
            res["peak_gpu_used_mib_after_requests"] = None
    finally:
        s.stop.set(); s.join()
        p.terminate()
        try:
            p.wait(20)
        except Exception:
            p.kill()
        lf.close()
    txt = log.read_text()
    res.update({"peak_gpu_used_mib": s.peak_gpu, "gpu_used_before_mib": s.base_gpu, "min_mem_available_mib": round(s.min_ram),
                "oom_in_log": bool(OOM_RE.search(txt))})
    return res


def main():
    here = Path(__file__).resolve().parents[2]
    p = argparse.ArgumentParser()
    p.add_argument("--server", default=str(here.parent / ".aicf-work/bin/llama-server"))
    p.add_argument("--model", default=str(here.parent / ".aicf-work/Qwen3-30B-A3B-Q4_K_M.gguf"))
    p.add_argument("--ctx", type=int, default=4096)
    p.add_argument("--port", type=int, default=18199)
    p.add_argument("--reps", type=int, default=3)
    p.add_argument("--n-predict", dest="n_predict", type=int, default=128)
    p.add_argument("--start-timeout", dest="start_timeout", type=int, default=600)
    p.add_argument("--configs", required=True, help="semicolon list of tag=extra args, e.g. 'noncmoe=;N18=-ncmoe 18'")
    p.add_argument("--out", default=str(here / "benchmarks/reports/p3_server_cfg.jsonl"))
    a = p.parse_args()
    (Path(a.out).parent / "logs").mkdir(parents=True, exist_ok=True)
    for item in a.configs.split(";"):
        tag, _, extra = item.partition("=")
        r = run(a, extra.split(), tag)
        with open(a.out, "a") as f:
            f.write(json.dumps(r) + "\n")
        tps = [x["predicted_per_second"] for x in r["runs"] if x.get("predicted_per_second")]
        print(tag, r["status"], "peakGPU", r["peak_gpu_used_mib"], "oom", r["oom_in_log"], "tg", [round(x, 1) for x in tps], flush=True)
        time.sleep(3)


if __name__ == "__main__":
    main()
