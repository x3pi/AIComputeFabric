#!/usr/bin/env python3
"""Single RTX 3060 (Linux): llama.cpp (GGUF Q4_K_M) vs SGLang (AWQ 4-bit), same streaming client (openai_stream_bench.py).
Usage: sglang_vs_llamacpp_bench.py <engine: llamacpp|sglang> <size: 4b|8b>   (production must be paused first)"""
import json, os, signal, subprocess, sys, time, urllib.request
from pathlib import Path

ROOT = Path("/mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric")
HERE = Path(__file__).resolve().parent
OUT = ROOT / "ai-compute-fabric" / "benchmarks" / "reports" / "sglang_cmp"
WORK = Path("/mnt/Data/aicf-sglang-cmp")
PORT = 18190
GGUF = {"4b": "/mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/.aicf-work/Qwen3-4B-Q4_K_M.gguf", "8b": "/mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/.aicf-work/Qwen3-8B-Q4_K_M.gguf"}
HF = {"4b": "Qwen/Qwen3-4B-AWQ", "8b": "Qwen/Qwen3-8B-AWQ"}
SGL_VENV = ROOT / ".aicf-work" / "venv-sglang"


def ready(url: str, secs: int, proc: subprocess.Popen) -> float:
    t = time.time()
    while time.time() - t < secs:
        if proc.poll() is not None:
            raise RuntimeError("server exited early")
        try:
            if urllib.request.urlopen(url, timeout=2).status == 200:
                return time.time() - t
        except Exception:
            time.sleep(1)
    raise TimeoutError(url)


def main() -> None:
    eng, size = sys.argv[1], sys.argv[2]
    OUT.mkdir(parents=True, exist_ok=True)
    log = open(WORK / f"{eng}_{size}.log", "w")
    env = os.environ.copy()
    if eng == "llamacpp":
        llama = ROOT / ".aicf-work" / "bin" / "llama-server"
        env["LD_LIBRARY_PATH"] = f"{llama.parent}:{env.get('LD_LIBRARY_PATH', '')}"
        cmd = [str(llama), "-m", GGUF[size], "-c", "16384", "-np", "4", "-ngl", "99", "-fa", "on", "-ctk", "f16", "-ctv", "f16",
               "--no-warmup", "--fit", "off", "--host", "127.0.0.1", "--port", str(PORT)]
        model, health = "x", f"http://127.0.0.1:{PORT}/health"
    else:
        cache = ROOT / ".aicf-work" / "cache"
        env.update({"HF_HOME": str(WORK / "hf"), "HF_HUB_OFFLINE": "1", "TMPDIR": "/tmp", "XDG_CACHE_HOME": str(cache),
                    "SGLANG_CACHE_DIR": str(cache / "sglang"), "FLASHINFER_WORKSPACE_BASE": str(cache / "flashinfer"),
                    "VLLM_USE_FLASHINFER_SAMPLER": "0"})
        ch = str(SGL_VENV / "lib" / "python3.12" / "site-packages" / "nvidia" / "cu13")
        env["CUDA_HOME"] = ch; env["PATH"] = f"{ch}/bin:" + env["PATH"]
        # --max-running-requests above the client concurrency: avoids the v0.5.19 admission double-count (fixed upstream in v0.5.23)
        cmd = [str(SGL_VENV / "bin" / "python"), "-m", "sglang.launch_server", "--model-path", HF[size], "--served-model-name", "sgl",
               "--host", "127.0.0.1", "--port", str(PORT), "--context-length", "16384", "--max-running-requests", "20",
               "--mem-fraction-static", "0.80", "--dtype", "float16", "--attention-backend", "triton", "--sampling-backend", "pytorch",
               "--cuda-graph-max-bs-decode", "8", "--disable-prefill-cuda-graph"]
        model, health = "sgl", f"http://127.0.0.1:{PORT}/health"
    for _ in range(90):                                              # previous engine may still be shutting down
        if subprocess.run(["ss", "-ltn", f"sport = :{PORT}"], capture_output=True, text=True).stdout.count("LISTEN") == 0:
            break
        time.sleep(2)
    t0 = time.time()
    pr = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        load = ready(health, 1500, pr)
        out = OUT / f"res_{eng}_{size}.json"
        r = subprocess.run([sys.executable, str(HERE / "openai_stream_bench.py"), "--base", f"http://127.0.0.1:{PORT}", "--model", model,
                            "--label", f"{eng}_{size}", "--out", str(out)], capture_output=True, text=True)
        print(r.stdout[-300:], r.stderr[-300:])
        d = json.loads(out.read_text()); d["load_s"] = round(load, 1); d["cmd"] = " ".join(cmd)
        out.write_text(json.dumps(d, indent=1))
        print("LOAD_S", d["load_s"], json.dumps(d["decode_single"]))
    finally:
        os.killpg(pr.pid, signal.SIGTERM)
        time.sleep(5)


if __name__ == "__main__":
    main()
