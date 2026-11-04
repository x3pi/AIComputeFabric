"""Thang prompt dai: moi bac do prefill/decode + VRAM 2 may; khi loi thi luu log engine."""
import sys, time, subprocess, requests, shutil
B = "http://10.42.0.1:18090"
LOG = "/mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/.demo/logs/engine-pc001.log"
def vram():
    w = subprocess.run(["ssh", "fios02@10.42.0.225", "nvidia-smi --query-gpu=memory.used --format=csv,noheader"], capture_output=True, text=True).stdout.strip()
    l = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader"], capture_output=True, text=True).stdout.strip()
    o = subprocess.run(["ssh","fios02@10.42.0.225","tasklist /fi \"IMAGENAME eq ollama.exe\" | find /c \"ollama\""],capture_output=True,text=True,stdin=subprocess.DEVNULL).stdout.strip()
    return f"win {w} | linux {l} | ollama_models={o}"
for _ in range(60):
    try:
        if requests.get(B + "/health", timeout=5).status_code == 200: break
    except Exception: pass
    time.sleep(5)
print("ctx", requests.get(B + "/props").json()["default_generation_settings"]["n_ctx"], flush=True)
words = " ".join(f"item{i} alpha beta gamma delta {i*7%97}" for i in range(120000))
toks = len(requests.post(B + "/tokenize", json={"content": words}).json()["tokens"])
for fill in [int(a) for a in sys.argv[1:]]:
    if fill == 0:
        ds = []
        for _ in range(3):
            t = requests.post(B + "/completion", json={"prompt": "Write a long story about a robot.", "n_predict": 128, "temperature": 0, "cache_prompt": False}, timeout=300).json()["timings"]
            ds.append(round(t["predicted_per_second"], 1))
        print(f"short prompt: decode={ds} tok/s | {vram()}", flush=True); continue
    p = words[: int(len(words) * fill / toks)] + "\nSummarize the text above in one sentence:"
    t0 = time.time()
    try:
        r = requests.post(B + "/completion", json={"prompt": p, "n_predict": 64, "temperature": 0, "cache_prompt": False}, timeout=int(__import__('os').environ.get('LADDER_TIMEOUT','400')))
        r.raise_for_status(); t = r.json()["timings"]
        print(f"fill~{fill}: prompt_n={t['prompt_n']} prefill={t['prompt_per_second']:.0f} tok/s decode={t['predicted_per_second']:.1f} tok/s wall={time.time()-t0:.0f}s | {vram()}", flush=True)
    except Exception as e:
        print(f"fill~{fill}: LOI sau {time.time()-t0:.0f}s: {str(e)[:150]} | {vram()}", flush=True)
        time.sleep(3); shutil.copy(LOG, "/tmp/claude-1000/cmp_out/engine_crash.log"); break
