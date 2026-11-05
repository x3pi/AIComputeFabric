"""Phase 0 probe for the serverless GPU plan (KE_HOACH_SERVERLESS_GPU_MODAL_AICF.md).

Measures on THIS machine, in isolated containers named aicf-sl-test-* (label aicf.sl=1), without touching
the running gateway / worker-guard / llama-server / aicf-nats / aicf-pg:
  gpu      GPU passthrough with the full hardening flags, cold-start of a container (python-only vs torch+CUDA)
  warm     persistent container + length-prefixed frames over stdin/stdout: cold-ready time and per-call latency
  uvbuild  environment build with uv inside a builder container, cold/warm uv cache, sdist refusal, ABI check
  vram     host-side pid->container attribution of GPU memory, and kill->VRAM-freed latency (G-RECLAIM evict path)
  objstore NATS JetStream Object Store throughput (isolated nats on 127.0.0.1:14222)

Results are merged into benchmarks/reports/p_serverless_0_probe.json. Usage: python p0_probe.py <section> [...]"""
from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import statistics
import struct
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "benchmarks/reports/p_serverless_0_probe.json"
SCRATCH = ROOT / ".aicf-work/sl-test"   # NOT /tmp: the dockerd mount namespace does not see this tool's /tmp
SITE_PACKAGES = Path.home() / ".local/lib/python3.12/site-packages"   # torch 2.11.0+cu130 already on this host
IMAGE = "python:3.12-slim"
UV = shutil.which("uv") or str(Path.home() / ".local/bin/uv")


def hardening(name: str, *, tmpfs_mb: int = 512, memory: str = "4g", nnp: bool = False) -> list[str]:
    a = ["--name", name, "--label", "aicf.sl=1", "--network", "none", "--read-only", "--cap-drop", "ALL", "--user", "65534:65534",
         "--pids-limit", "256", "--memory", memory, "--memory-swap", memory, "--cpus", "2",
         "--tmpfs", f"/tmp:size={tmpfs_mb}m,nosuid", "-e", "HOME=/tmp", "-e", "PYTHONDONTWRITEBYTECODE=1"]
    if nnp:
        a += ["--security-opt", "no-new-privileges"]
    return a


# NOTE: `--gpus all` FAILS on this host (CDI mode, no nvidia-container-toolkit CLI); `--runtime=nvidia` + env works.
GPU = ["--runtime=nvidia", "-e", "NVIDIA_VISIBLE_DEVICES=0", "-e", "NVIDIA_DRIVER_CAPABILITIES=compute,utility"]
TORCH = ["-e", "PYTHONPATH=/sp", "-v", f"{SITE_PACKAGES}:/sp:ro"]


def pct(xs: list[float], p: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


def stats(xs: list[float]) -> dict:
    return {"n": len(xs), "min": round(min(xs), 3), "p50": round(pct(xs, 50), 3), "p95": round(pct(xs, 95), 3),
            "p99": round(pct(xs, 99), 3), "max": round(max(xs), 3), "mean": round(statistics.mean(xs), 3)}


def save(section: str, data: dict) -> None:
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    cur = json.loads(REPORT.read_text()) if REPORT.exists() else {}
    cur.setdefault("meta", {}).update({"host": os.uname().nodename, "image": IMAGE, "label": "[ĐO]"})
    cur[section] = data
    REPORT.write_text(json.dumps(cur, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({section: data}, indent=2, ensure_ascii=False))


def docker(*args: str, timeout: float = 120, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=check)


def vram_used_mib() -> int:
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"], capture_output=True, text=True).stdout
    return int(out.strip().splitlines()[0])


def cleanup() -> None:
    ids = docker("ps", "-aq", "--filter", "label=aicf.sl=1").stdout.split()
    if ids:
        docker("rm", "-f", *ids)


# --------------------------------------------------------------------------------------------- gpu
def sec_gpu(n: int = 30) -> None:
    out: dict = {"flags": "hardening()+GPU+TORCH", "gpus_flag_works": None}
    r = docker("run", "--rm", "--gpus", "all", "--network", "none", IMAGE, "true")
    out["gpus_flag_works"] = r.returncode == 0
    out["gpus_flag_error"] = r.stderr.strip()[:300]
    base = ["docker", "run", "--rm"]
    r = subprocess.run([*base, *hardening("aicf-sl-test-nnp", nnp=True), IMAGE, "python", "-c", "pass"], capture_output=True, text=True)
    out["no_new_privileges_works"] = r.returncode == 0
    nnp = out["no_new_privileges_works"]

    def timed(code: str, gpu: bool) -> tuple[float, str]:
        name = "aicf-sl-test-" + uuid.uuid4().hex[:8]
        cmd = [*base, *hardening(name, nnp=nnp), *(GPU + TORCH if gpu else []), IMAGE, "python", "-c", code]
        t = time.perf_counter()
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        dt = (time.perf_counter() - t) * 1000
        if p.returncode:
            raise RuntimeError(p.stderr[-400:])
        return dt, p.stdout.strip()

    pyonly = [timed("pass", False)[0] for _ in range(n)]
    out["container_python_only_ms"] = stats(pyonly)
    code = "import torch;x=torch.ones(1024,1024,device='cuda');torch.cuda.synchronize();print(torch.cuda.is_available(),torch.__version__,torch.cuda.get_device_name(0),float(x.sum()))"
    first, msg = timed(code, True)
    out["torch_cuda_first_run_ms"] = round(first, 1)
    out["torch_cuda_output"] = msg
    runs = [timed(code, True)[0] for _ in range(n)]
    out["torch_cuda_cold_start_ms"] = stats(runs)
    out["torch_arch_list_note"] = "torch 2.11.0+cu130 arch list sm_75..sm_120 incl. sm_86 (RTX3060) and sm_120 (RTX5070): one wheel covers both GPUs [ĐO: torch.cuda.get_arch_list()]"
    save("gpu", out)


# --------------------------------------------------------------------------------------------- warm
RUNNER = r'''
import json, struct, sys, time
import torch
x = torch.ones(1024, 1024, device="cuda"); torch.cuda.synchronize()
rd, wr = sys.stdin.buffer, sys.stdout.buffer
def recv():
    h = rd.read(4)
    if len(h) < 4: return None
    return rd.read(struct.unpack(">I", h)[0])
def send(b):
    wr.write(struct.pack(">I", len(b)) + b); wr.flush()
send(b'{"ready":true}')
while True:
    m = recv()
    if m is None: break
    q = json.loads(m)
    if q["op"] == "noop": r = {"ok": True}
    elif q["op"] == "gpu":
        y = x @ x; torch.cuda.synchronize(); r = {"ok": True, "s": float(y[0, 0])}
    elif q["op"] == "hold":
        z = torch.empty(q["mib"] * 1024 * 1024, dtype=torch.uint8, device="cuda"); z.fill_(1); torch.cuda.synchronize(); r = {"ok": True}
    send(json.dumps(r).encode())
'''


class Warm:
    def __init__(self, name: str):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        SCRATCH.chmod(0o755)
        p = SCRATCH / "runner_probe.py"
        p.write_text(RUNNER)
        p.chmod(0o644)
        self.name = name
        t = time.perf_counter()
        self.p = subprocess.Popen(["docker", "run", "--rm", "-i", *hardening(name), *GPU, *TORCH, "-v", f"{p}:/run/runner.py:ro",
                                   IMAGE, "python", "/run/runner.py"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.recv()
        self.ready_ms = (time.perf_counter() - t) * 1000

    def send(self, o: dict) -> None:
        b = json.dumps(o).encode()
        self.p.stdin.write(struct.pack(">I", len(b)) + b)
        self.p.stdin.flush()

    def recv(self) -> dict:
        h = self.p.stdout.read(4)
        if len(h) < 4:
            raise RuntimeError("runner died: " + self.p.stderr.read().decode()[-400:])
        return json.loads(self.p.stdout.read(struct.unpack(">I", h)[0]))

    def call(self, o: dict) -> float:
        t = time.perf_counter()
        self.send(o)
        r = self.recv()
        assert r["ok"]
        return (time.perf_counter() - t) * 1000

    def close(self) -> None:
        docker("kill", self.name)
        self.p.wait(timeout=30)


def sec_warm(n: int = 300) -> None:
    w = Warm("aicf-sl-test-warm")
    try:
        for _ in range(20):
            w.call({"op": "noop"})
        noop = [w.call({"op": "noop"}) for _ in range(n)]
        gpu = [w.call({"op": "gpu"}) for _ in range(n)]
        out = {"container_ready_ms_incl_import_torch_and_cuda_init": round(w.ready_ms, 1), "warm_noop_roundtrip_ms": stats(noop),
               "warm_gpu_matmul1024_roundtrip_ms": stats(gpu),
               "note": "pipe round-trip only (no NATS, no HTTP, no cloudpickle). Add NATS 1.17ms p50 [ĐO ADR-002] + proxy for end-to-end."}
    finally:
        w.close()
    save("warm", out)


# --------------------------------------------------------------------------------------------- uv build
def sec_uvbuild() -> None:
    root = SCRATCH / "uv"
    if root.exists():   # files created by uid 65534 inside containers: remove through a container
        subprocess.run(["docker", "run", "--rm", "-v", f"{root.parent}:/w", "busybox", "rm", "-rf", "/w/uv"], capture_output=True)
    for d in ("cache", "env1", "env2", "envbad"):
        (root / d).mkdir(parents=True)
        (root / d).chmod(0o777)
    out: dict = {"uv_version": subprocess.run([UV, "--version"], capture_output=True, text=True).stdout.strip()}

    def build(name: str, pkgs: list[str], extra: list[str] | None = None) -> tuple[float, subprocess.CompletedProcess]:
        sh = f"uv venv /env/venv -q && uv pip install -q --python /env/venv/bin/python --only-binary :all: {' '.join(pkgs)}"
        cmd = ["docker", "run", "--rm", "--name", "aicf-sl-test-b" + uuid.uuid4().hex[:6], "--label", "aicf.sl=1", "--cap-drop", "ALL",
               "--user", "65534:65534", "--read-only", "--memory", "2g", "--pids-limit", "256", "--cpus", "2", "--tmpfs", "/tmp:size=512m",
               "-e", "HOME=/tmp", "-e", "UV_CACHE_DIR=/cache", "-e", "UV_LINK_MODE=copy", "-v", f"{UV}:/usr/local/bin/uv:ro",
               "-v", f"{root / 'cache'}:/cache", "-v", f"{root / name}:/env", *(extra or []), IMAGE, "sh", "-c", sh]
        t = time.perf_counter()
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        return (time.perf_counter() - t) * 1000, p

    pk = ["numpy", "pillow", "cloudpickle"]
    t1, p1 = build("env1", pk)
    out["build_cold_uv_cache_ms"] = round(t1, 1)
    out["build_cold_ok"] = p1.returncode == 0
    t2, p2 = build("env2", pk)
    out["build_warm_uv_cache_ms"] = round(t2, 1)
    out["build_warm_ok"] = p2.returncode == 0
    out["packages"] = pk
    tb, pb = build("envbad", ["sgmllib3k"])
    out["sdist_only_package_refused"] = pb.returncode != 0
    out["sdist_refusal_msg"] = " ".join(pb.stderr.split())[:300]
    r = subprocess.run(["docker", "run", "--rm", "--name", "aicf-sl-test-abi", "--label", "aicf.sl=1", "--network", "none", "--read-only",
                        "--cap-drop", "ALL", "--user", "65534:65534", "-v", f"{root / 'env2'}:/env:ro", IMAGE, "/env/venv/bin/python", "-c",
                        "import numpy,PIL,cloudpickle;print(numpy.__version__,cloudpickle.__version__)"], capture_output=True, text=True)
    out["venv_mounted_ro_in_runtime_container_ok"] = r.returncode == 0
    out["venv_mounted_ro_output"] = r.stdout.strip() or r.stderr.strip()[-200:]
    t = time.perf_counter()
    subprocess.run(["docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL", "--user", "65534:65534",
                    "-v", f"{root / 'env2'}:/env:ro", "--label", "aicf.sl=1", IMAGE, "/env/venv/bin/python", "-c", "pass"], capture_output=True)
    out["runtime_start_with_mounted_venv_ms"] = round((time.perf_counter() - t) * 1000, 1)
    out["env_size_mb"] = round(sum(f.stat().st_size for f in (root / "env2").rglob("*") if f.is_file()) / 1e6, 1)
    out["alpine_note"] = "python:3.12-alpine is musl: manylinux torch/numpy wheels do not load there (see sandbox.py default image)"
    save("uvbuild", out)


# --------------------------------------------------------------------------------------------- vram
def host_pid_vram() -> dict[int, int]:
    out = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"], capture_output=True, text=True).stdout
    res = {}
    for line in out.strip().splitlines():
        a, b = [x.strip() for x in line.split(",")]
        res[int(a)] = int(b)
    return res


def pid_container(pid: int) -> str | None:
    try:
        cg = Path(f"/proc/{pid}/cgroup").read_text()
    except OSError:
        return None
    for tok in cg.replace("/", " ").replace(".", " ").replace("-", " ").split():
        if len(tok) == 64 and all(c in "0123456789abcdef" for c in tok):
            return tok
    return None


def sec_vram(reps: int = 10) -> None:
    out: dict = {}
    baseline = vram_used_mib()
    out["baseline_used_mib"] = baseline
    kills_ms, detect_ms, attrib_ok = [], [], []
    for _ in range(reps):
        name = "aicf-sl-test-v" + uuid.uuid4().hex[:6]
        w = Warm(name)
        w.call({"op": "hold", "mib": 1024})
        cid = docker("inspect", "-f", "{{.Id}}", name).stdout.strip()
        t = time.perf_counter()
        found = None
        while time.perf_counter() - t < 5:
            for pid, mib in host_pid_vram().items():
                if pid_container(pid) == cid:
                    found = (pid, mib)
            if found:
                break
        detect_ms.append((time.perf_counter() - t) * 1000)
        attrib_ok.append(found is not None)
        used_before = vram_used_mib()
        t = time.perf_counter()
        docker("kill", name)
        while vram_used_mib() > baseline + 64 and time.perf_counter() - t < 10:
            time.sleep(0.005)
        kills_ms.append((time.perf_counter() - t) * 1000)
        out.setdefault("used_mib_while_holding_1GiB", used_before)
        try:
            w.p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    out["pid_to_container_attribution_ok_all"] = all(attrib_ok)
    out["attribution_ok_count"] = f"{sum(attrib_ok)}/{reps}"
    out["attribution_found_after_ms_excl_nvidia_smi_spawn"] = stats(detect_ms)
    out["docker_kill_to_vram_freed_ms_single"] = stats(kills_ms)
    # three containers at once (evict-all path)
    ws = [Warm("aicf-sl-test-m%d" % i) for i in range(3)]
    for w in ws:
        w.call({"op": "hold", "mib": 1024})
    t = time.perf_counter()
    docker("kill", *[w.name for w in ws])
    while vram_used_mib() > baseline + 64 and time.perf_counter() - t < 10:
        time.sleep(0.005)
    out["docker_kill_3containers_3GiB_to_vram_freed_ms"] = round((time.perf_counter() - t) * 1000, 1)
    out["note"] = ("vram sampling uses nvidia-smi spawn (~30ms/call [ĐỌC MÃ gpu.go]); freed-threshold = baseline+64MiB; "
                   "pid->container via /proc/<pid>/cgroup on the HOST. Windows/WSL2 not covered here.")
    save("vram", out)


# --------------------------------------------------------------------------------------------- objstore
async def _obj(sizes: list[tuple[int, int]]) -> dict:
    import nats
    nc = await nats.connect("nats://127.0.0.1:14222")
    js = nc.jetstream()
    obs = await js.create_object_store("aicf_sl_probe")
    res = {}
    for mib, reps in sizes:
        data = os.urandom(mib * 1024 * 1024)
        put, get, getw = [], [], []
        for i in range(reps):
            t = time.perf_counter()
            await obs.put(f"o{mib}-{i}", data)
            put.append(time.perf_counter() - t)
            t = time.perf_counter()
            r = await obs.get(f"o{mib}-{i}")
            assert len(r.data) == len(data)
            get.append(time.perf_counter() - t)
            t = time.perf_counter()
            bio = io.BytesIO()
            await obs.get(f"o{mib}-{i}", writeinto=bio)
            getw.append(time.perf_counter() - t)
            await obs.delete(f"o{mib}-{i}")
        res[f"{mib}MiB"] = {"put_MBps_p50": round(mib * 1.048576 / statistics.median(put), 1),
                            "get_MBps_p50": round(mib * 1.048576 / statistics.median(get), 1),
                            "get_writeinto_MBps_p50": round(mib * 1.048576 / statistics.median(getw), 1), "reps": reps,
                            "put_s_p50": round(statistics.median(put), 3), "get_s_p50": round(statistics.median(get), 3)}
    await nc.close()
    return res


def sec_objstore() -> None:
    docker("rm", "-f", "aicf-sl-test-nats")
    r = docker("run", "-d", "--name", "aicf-sl-test-nats", "--label", "aicf.sl=1", "-p", "127.0.0.1:14222:4222", "--tmpfs", "/data:size=3g",
               "nats:2.10-alpine", "-js", "-sd", "/data")
    assert r.returncode == 0, r.stderr
    time.sleep(1.5)
    try:
        res = asyncio.run(_obj([(1, 20), (8, 5), (32, 2), (64, 1)]))
    finally:
        docker("rm", "-f", "aicf-sl-test-nats")
    save("objstore", {"results": res, "note": "loopback to dockerised nats (tmpfs store), python nats-py 2.16 client; NOT the 2.5GbE LAN path"})


SECTIONS = {"gpu": sec_gpu, "warm": sec_warm, "uvbuild": sec_uvbuild, "vram": sec_vram, "objstore": sec_objstore}

if __name__ == "__main__":
    todo = sys.argv[1:] or list(SECTIONS)
    try:
        for s in todo:
            SECTIONS[s]()
    finally:
        cleanup()
