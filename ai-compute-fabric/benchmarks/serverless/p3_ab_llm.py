"""Phase 3 A/B LLM co-tenancy & VRAM admission benchmark on real RTX 3060 (`pc-001`).

1. Starts an isolated `llama-server` on 127.0.0.1:<free_port> with `Qwen3-4B-Q4_K_M.gguf` (-ngl 99).
2. Verifies GpuAdmission rejects `vram_mb=8192` when `free - 8192 < headroom` and `llama-server` stays healthy (no OOM).
3. Measures N=15 streaming `/v1/chat/completions` requests (TTFT ms & tok/s) in:
   - Baseline (llama-server alone)
   - Co-tenant (warm GPU Serverless container holding 1024 MiB VRAM on GPU 0 via CUDA Driver API + continuous warm Serverless invocations)
4. Writes `benchmarks/reports/p_serverless_3_ab_llm.json`.
"""
import json
import os
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from agents.env_cache import EnvCache, EnvSpec
from agents.sandbox import GpuDockerSandbox, NoCapacity, ServerlessLimits, WarmDockerPool, WarmKey
from agents.sl_gpu import GpuAdmission, GpuProbe
from tests.serverless.util import bundle_of, by_value, frame

by_value(__name__)
ROOT = Path(__file__).resolve().parents[2]
LLAMA_BIN = Path("/mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/.aicf-work/bin/llama-server")
MODEL = ROOT / ".demo/models/Qwen3-4B-Q4_K_M.gguf"
STORE = ROOT / ".aicf-work/sl-test/store"
OUT = ROOT / "benchmarks/reports/p_serverless_3_ab_llm.json"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def pct(xs: list[float], p: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1))))]


def gpu_telemetry() -> dict:
    r = subprocess.run(
        ["nvidia-smi", "--id=0", "--query-gpu=temperature.gpu,clocks.current.graphics,power.draw,memory.used,memory.free", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, check=True,
    )
    parts = [p.strip() for p in r.stdout.strip().split(",")]
    return {
        "temp_c": float(parts[0]),
        "clock_mhz": float(parts[1]),
        "power_w": float(parts[2]),
        "used_mb": int(float(parts[3])),
        "free_mb": int(float(parts[4])),
    }


def stream_chat(port: int, prompt: str, max_tokens: int = 48) -> tuple[float, float, int]:
    url = f"http://127.0.0.1:{port}/v1/chat/completions"
    body = json.dumps({
        "model": "qwen3-4b",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "seed": 42,
        "stream": True,
    }).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.perf_counter()
    t_first = None
    tokens = 0
    with urllib.request.urlopen(req, timeout=60) as resp:
        for raw in resp:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data: "):
                continue
            payload = line[6:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except ValueError:
                continue
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            if delta.get("content") or delta.get("reasoning_content"):
                if t_first is None:
                    t_first = time.perf_counter()
                tokens += 1
    t_end = time.perf_counter()
    if t_first is None:
        t_first = t_end
    ttft_ms = (t_first - t0) * 1000.0
    gen_s = max(1e-6, t_end - t_first)
    tok_s = max(1, tokens - 1) / gen_s if tokens > 1 else tokens / max(1e-6, t_end - t0)
    return ttft_ms, tok_s, tokens


_HOLDER_STATE = {"ready": False, "ctx": None, "ptr": None}


def cotenant_step(x: int) -> int:
    import ctypes
    if not _HOLDER_STATE["ready"]:
        cu = ctypes.CDLL("libcuda.so.1")
        dev, ctx, ptr = ctypes.c_int(), ctypes.c_void_p(), ctypes.c_ulonglong()
        for name, rc in (("cuInit", cu.cuInit(0)), ("cuDeviceGet", cu.cuDeviceGet(ctypes.byref(dev), 0)),
                         ("cuCtxCreate", cu.cuCtxCreate_v2(ctypes.byref(ctx), 0, dev))):
            if rc != 0:
                raise RuntimeError(f"{name} failed rc={rc}")
        rc = cu.cuMemAlloc_v2(ctypes.byref(ptr), ctypes.c_size_t(1024 * 1024 * 1024))
        if rc != 0:
            raise MemoryError(f"cuMemAlloc failed rc={rc}")
        _HOLDER_STATE["ctx"] = ctx
        _HOLDER_STATE["ptr"] = ptr
        _HOLDER_STATE["ready"] = True
    s = 0
    for i in range(500):
        s += (i * x) & 0xFF
    return s


def main() -> None:
    port = free_port()
    env = os.environ.copy()
    ld = str(LLAMA_BIN.parent)
    env["LD_LIBRARY_PATH"] = f"{ld}:{env.get('LD_LIBRARY_PATH', '')}"
    proc = subprocess.Popen(
        [str(LLAMA_BIN), "-m", str(MODEL), "--host", "127.0.0.1", "--port", str(port), "-ngl", "99", "-c", "2048", "-np", "1"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(120):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(0.25)
        else:
            raise RuntimeError("llama-server did not become healthy")

        # Warmup
        for _ in range(3):
            stream_chat(port, "Count from 1 to 10 separated by commas.", max_tokens=24)

        probe = GpuProbe()
        free_mb, total_mb = probe.mem(0)
        headroom_mb = max(2048, free_mb - 4096)
        adm = GpuAdmission(probe, headroom_mib=headroom_mb)
        refused = False
        refuse_msg = ""
        try:
            adm.reserve("call-8192", 0, 8192)
        except NoCapacity as e:
            refused = True
            refuse_msg = str(e)
        assert refused, "8192 MiB must be refused when free - 8192 < headroom"
        # Verify llama-server still responds cleanly
        _, tok_chk, _ = stream_chat(port, "Say hello.", max_tokens=16)
        assert tok_chk > 0

        prompts = [
            f"Explain concept #{i} of distributed computing in two concise sentences."
            for i in range(15)
        ]

        tel_before_a = gpu_telemetry()
        base_ttft, base_toks = [], []
        for p in prompts:
            ttft, toks, _ = stream_chat(port, p, max_tokens=48)
            base_ttft.append(ttft)
            base_toks.append(toks)
        tel_after_a = gpu_telemetry()

        # Start warm GPU Serverless container holding 1024 MiB VRAM + periodic warm calls
        spec = EnvSpec()
        env_path, _ = EnvCache(STORE).ensure(spec)
        env_hash = spec.env_hash()
        pool = WarmDockerPool(GpuDockerSandbox(), max_containers=4, max_per_key=2, worker_id="p3-ab-worker")
        pool.admission = adm
        sha, bundle = bundle_of(cotenant_step)
        lim = ServerlessLimits(timeout_s=30.0, start_timeout_s=30.0, keep_warm_s=120.0, gpu_index=0, vram_mb=1200)
        key = WarmKey("t-ab", env_hash, "cotenant_step")

        # Cold call + 1024 MiB CUDA allocation before A/B loop
        res0, cold0 = pool.call(key, env_path, lim, frame(sha, 1), bundle)
        assert res0.get("ok") is True and cold0 is True, (res0, cold0)

        stop_bg = threading.Event()
        bg_calls = {"n": 0, "errors": 0}

        def bg_loop():
            i = 2
            while not stop_bg.is_set():
                try:
                    r, _ = pool.call(key, env_path, lim, frame(sha, i), bundle)
                    if r.get("ok"):
                        bg_calls["n"] += 1
                    else:
                        bg_calls["errors"] += 1
                except Exception:
                    bg_calls["errors"] += 1
                i += 1
                time.sleep(0.1)

        bg_thread = threading.Thread(target=bg_loop, daemon=True)
        bg_thread.start()
        try:
            tel_before_b = gpu_telemetry()
            co_ttft, co_toks = [], []
            for p in prompts:
                ttft, toks, _ = stream_chat(port, p, max_tokens=48)
                co_ttft.append(ttft)
                co_toks.append(toks)
            tel_after_b = gpu_telemetry()
        finally:
            stop_bg.set()
            bg_thread.join(timeout=5)
            pool.close()

        base_ttft_p95 = pct(base_ttft, 95)
        co_ttft_p95 = pct(co_ttft, 95)
        base_toks_p50 = pct(base_toks, 50)
        co_toks_p50 = pct(co_toks, 50)

        ttft_degradation_pct = ((co_ttft_p95 - base_ttft_p95) / base_ttft_p95) * 100.0
        toks_degradation_pct = ((base_toks_p50 - co_toks_p50) / base_toks_p50) * 100.0

        report = {
            "label": "[ĐO]",
            "host": "pc-001",
            "gpu": f"NVIDIA GeForce RTX 3060 ({total_mb} MiB)",
            "model": MODEL.name,
            "n_requests_per_arm": len(prompts),
            "max_tokens": 48,
            "admission_8192mb_when_insufficient_headroom": {
                "free_mb_with_llm": free_mb,
                "headroom_mb": headroom_mb,
                "requested_vram_mb": 8192,
                "refused": refused,
                "message": refuse_msg,
                "llama_server_healthy_after_rejection": True,
            },
            "baseline_llm_only": {
                "ttft_ms_p50": round(pct(base_ttft, 50), 2),
                "ttft_ms_p95": round(base_ttft_p95, 2),
                "tok_per_s_p50": round(base_toks_p50, 2),
                "tok_per_s_p95": round(pct(base_toks, 95), 2),
                "telemetry_start": tel_before_a,
                "telemetry_end": tel_after_a,
            },
            "cotenant_with_serverless_gpu_1024mb": {
                "ttft_ms_p50": round(pct(co_ttft, 50), 2),
                "ttft_ms_p95": round(co_ttft_p95, 2),
                "tok_per_s_p50": round(co_toks_p50, 2),
                "tok_per_s_p95": round(pct(co_toks, 95), 2),
                "concurrent_serverless_warm_calls": bg_calls["n"],
                "concurrent_serverless_errors": bg_calls["errors"],
                "telemetry_start": tel_before_b,
                "telemetry_end": tel_after_b,
            },
            "comparison": {
                "ttft_p95_degradation_pct": round(ttft_degradation_pct, 2),
                "tok_per_s_p50_degradation_pct": round(toks_degradation_pct, 2),
                "threshold_pct": 5.0,
                "pass": ttft_degradation_pct <= 5.0 and toks_degradation_pct <= 5.0,
                "noise_factors": "Dynamic GPU boost clock (P2 state), thermal drift across runs, Xorg background (~15 MiB).",
            },
        }
        OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        print(json.dumps(report, indent=2, ensure_ascii=False))
        assert report["comparison"]["pass"], report["comparison"]
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
