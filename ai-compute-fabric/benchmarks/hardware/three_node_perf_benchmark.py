#!/usr/bin/env python3
"""Performance-only benchmark: GGML-RPC `merged` with 2 cards vs 3 cards (no code-quality test).

Nodes: Linux master RTX 3060 (CUDA0) + Windows RTX 5070 over Ethernet (RPC0) [+ Windows RTX 3060 over Wi-Fi (RPC1)].
Per (model, cards) it records: cold load time and bytes pushed per link, prefill tok/s, single-stream
decode tok/s / TPOT / TTFT, aggregate decode throughput at concurrency 2 and 4, per-link network bytes per
token, and VRAM per card. Link speed (RTT + raw TCP bandwidth) is measured separately by --net.

Secrets: the fi-ai03 ssh password is taken from the SSHPASS environment variable only (never written to disk).
Usage: SSHPASS=... python3 three_node_perf_benchmark.py [--net] [--models a,b] [--cards 2,3]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[3]            # .../AIComputeFabric
LLAMA = ROOT / ".aicf-work" / "bin" / "llama-server"
DEMO_MODELS = ROOT / "ai-compute-fabric" / ".demo" / "models"
OUT = ROOT / "ai-compute-fabric" / "benchmarks" / "reports" / "perf3node"
WORK = Path("/mnt/Data/aicf-3node")
OUT.mkdir(parents=True, exist_ok=True)
WORK.mkdir(parents=True, exist_ok=True)

MODELS = {
    "Qwen3.8-27B": DEMO_MODELS / "Qwen3.8-27B-UD-Q4_K_M.gguf",
    "Qwen3-Coder-30B-A3B": DEMO_MODELS / "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf",
    "GLM-4-32B": Path("/mnt/Data/aicf-glm/GLM-4-32B-0414-Q4_K_M.gguf"),
    "Qwen3-30B-A3B": DEMO_MODELS / "Qwen3-30B-A3B-Q4_K_M.gguf",
    "Qwen3-Coder-30B-A3B-Q6_K": WORK / "models" / "Qwen3-Coder-30B-A3B-Instruct-Q6_K.gguf",   # 25.1 GB: too big for 2 cards
}
# name -> (ssh target, rpc host, nic on the Linux master, human label)
NODES = {
    "win5070": {"ssh": ["ssh", "-o", "ConnectTimeout=8", "fios02@10.42.0.225"], "host": "10.42.0.225",
                "nic": "enp133s0", "label": "RTX 5070 (Windows) - Ethernet 2.5 GbE"},
    # same RTX 5070 PC, but the master reaches it through wifi_link_emulator.py (the PC has no Wi-Fi adapter present)
    "win5070w": {"ssh": ["ssh", "-o", "ConnectTimeout=8", "fios02@10.42.0.225"], "host": "10.42.0.225", "emu": True,
                 "nic": "enp133s0", "label": "RTX 5070 (Windows) - Ethernet bi ep thanh Wi-Fi (mo phong)"},
    # same RTX 5070 PC over its real Wi-Fi adapter (192.168.1.78); ssh control still goes through Ethernet
    "win5070r": {"ssh": ["ssh", "-o", "ConnectTimeout=8", "fios02@10.42.0.225"], "host": "192.168.1.78",
                 "nic": "wlp132s0", "label": "RTX 5070 (Windows) - Wi-Fi that (802.11ax)"},
    "win3060": {"ssh": ["sshpass", "-e", "ssh", "-o", "ConnectTimeout=8", "vantien@192.168.1.175"], "host": "192.168.1.175",
                "nic": "wlp132s0", "label": "RTX 3060 (Windows, fi-ai03) - Wi-Fi"},
}
RPC_PORT = 50152
EMU_PORT = 50160   # local port of wifi_link_emulator.py (RTX 5070 link forced to behave like the measured Wi-Fi)
BW_PORT = 50153
# (nodes, tensor-split). llama-server orders devices [RPC0, RPC1, ..., CUDA0 (Linux master)], so the split is
# written in that order. 2c = production ratio (Linux 3060 48 %, RTX 5070 52 %). Extras only for Coder-30B.
CONFIGS = {
    "2c": (["win5070"], "52,48", []),                         # [5070, linux3060]
    "3c": (["win5070", "win3060"], "36,32,32", []),           # [5070, fi-ai03 3060 (Wi-Fi), linux3060]
    "2c-wifi": (["win3060"], "50,50", []),                    # control: same 2-card shape, remote card on Wi-Fi
    "3c-wifilight": (["win5070", "win3060"], "42,18,40", []),
    # model larger than 2 cards: 2 cards + 12 MoE expert layers spilled to the master CPU (P-cores 0-7), the realistic 2-card fallback
    "2c-wifireal": (["win5070r"], "52,48", []),             # RTX 5070 + Linux 3060, 5070 on its real Wi-Fi adapter
    "2c-wifiemu": (["win5070w"], "52,48", []),             # RTX 5070 + Linux 3060 with the 5070 link emulating Wi-Fi
    "2c-cpuoff": (["win5070"], "65,35", ["-ncmoe", "14", "-t", "8", "-Cr", "0-7", "--cpu-strict", "1"]),   # split is by layer COUNT: layers 0-13 (CPU experts) land on RPC0, so it gets more layers # control: fewer layers on the Wi-Fi card
}
MAIN = ["2c", "3c"]
EXTRA = ["2c-wifi", "3c-wifilight"]
CTX_PER_SLOT, NPARALLEL, PORT = 4096, 4, 18190
BASE = f"http://127.0.0.1:{PORT}"


def sh(cmd: List[str], timeout: float = 60) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.replace("\r", "")


def remote(node: str, cmd: str, timeout: float = 60) -> str:
    return sh(NODES[node]["ssh"] + [cmd], timeout)


def nic_bytes(nic: str) -> Dict[str, int]:
    for line in Path("/proc/net/dev").read_text().splitlines():
        if line.strip().startswith(nic + ":"):
            f = line.split(":")[1].split()
            return {"rx": int(f[0]), "tx": int(f[8])}
    return {"rx": 0, "tx": 0}


def nic_delta(a: Dict[str, Dict[str, int]], b: Dict[str, Dict[str, int]]) -> Dict[str, Dict[str, int]]:
    return {n: {"rx": b[n]["rx"] - a[n]["rx"], "tx": b[n]["tx"] - a[n]["tx"]} for n in a}


def vram_mib(node: Optional[str]) -> int:
    q = "nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits"
    out = sh(q.split()) if node is None else remote(node, q)
    m = re.search(r"\d+", out)
    return int(m.group()) if m else -1


# ---------------------------------------------------------------- network characterisation
def ping_stats(host: str, n: int = 50) -> Dict[str, float]:
    out = sh(["ping", "-c", str(n), "-i", "0.2", "-W", "2", host], 60)
    m = re.search(r"min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)", out)
    loss = re.search(r"([\d.]+)% packet loss", out)
    return {"rtt_min_ms": float(m.group(1)), "rtt_avg_ms": float(m.group(2)), "rtt_max_ms": float(m.group(3)),
            "loss_pct": float(loss.group(1)) if loss else -1} if m else {}


BW_SERVER = ("import socket,time\ns=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)\n"
             f"s.bind(('0.0.0.0',{BW_PORT}));s.listen(1);c,_=s.accept();n=0;t=time.time()\n"
             "while True:\n b=c.recv(1<<20)\n if not b:break\n n+=len(b)\n"
             "d=time.time()-t;print('RECV_MBPS',round(n/d/1e6,1),flush=True)\n")
BW_CLIENT = ("import socket,sys,time\ns=socket.create_connection((sys.argv[1],int(sys.argv[2])));b=b'x'*(1<<20)\n"
             "t=time.time();n=0\nwhile time.time()-t<6:\n s.sendall(b);n+=len(b)\ns.close()\n"
             "print('SENT_MBPS',round(n/(time.time()-t)/1e6,1),flush=True)\n")


def bandwidth(node: str) -> Dict[str, float]:
    """Raw single-stream TCP MB/s in both directions between the Linux master and a Windows node."""
    res: Dict[str, float] = {}
    (WORK / "bw_server.py").write_text(BW_SERVER)
    (WORK / "bw_client.py").write_text(BW_CLIENT)
    host = NODES[node]["host"]
    # Linux -> node : server on node, client here
    scp = ["scp", "-q"] if node in ("win5070", "win5070r") else ["sshpass", "-e", "scp", "-q"]
    tgt = ("fios02@10.42.0.225" if node in ("win5070", "win5070r") else "vantien@192.168.1.175")
    sh(scp + [str(WORK / "bw_server.py"), f"{tgt}:C:/aicf/bw_server.py"])
    sh(scp + [str(WORK / "bw_client.py"), f"{tgt}:C:/aicf/bw_client.py"])
    py = "python"
    p = subprocess.Popen(NODES[node]["ssh"] + [f"{py} C:\\aicf\\bw_server.py"], stdout=subprocess.PIPE, text=True)
    time.sleep(3)
    sent = subprocess.run([sys.executable, str(WORK / "bw_client.py"), host, str(BW_PORT)], capture_output=True, text=True, timeout=60).stdout
    try:
        line = p.stdout.readline()
    except Exception:
        line = ""
    p.terminate()
    m = re.search(r"SENT_MBPS ([\d.]+)", sent)
    r = re.search(r"RECV_MBPS ([\d.]+)", line or "")
    res["linux_to_node_MBps"] = float(r.group(1)) if r else (float(m.group(1)) if m else -1)
    # node -> Linux : server here, client on node
    srv = subprocess.Popen([sys.executable, str(WORK / "bw_server.py")], stdout=subprocess.PIPE, text=True)
    time.sleep(1)
    out = remote(node, f"{py} C:\\aicf\\bw_client.py {'10.42.0.1' if node == 'win5070' else '192.168.1.40'} {BW_PORT}", 60)
    try:
        l2 = srv.stdout.readline()
    except Exception:
        l2 = ""
    srv.terminate()
    r2 = re.search(r"RECV_MBPS ([\d.]+)", l2 or "")
    m2 = re.search(r"SENT_MBPS ([\d.]+)", out)
    res["node_to_linux_MBps"] = float(r2.group(1)) if r2 else (float(m2.group(1)) if m2 else -1)
    return res


def net_probe() -> Dict[str, Any]:
    info: Dict[str, Any] = {}
    for n, d in NODES.items():
        info[n] = {"label": d["label"], "host": d["host"], "nic_on_master": d["nic"], **ping_stats(d["host"]),
                   **bandwidth(n)}
        print(n, json.dumps(info[n]), flush=True)
    return info


# ---------------------------------------------------------------- rpc-server lifecycle on Windows nodes
def rpc_start(node: str) -> None:
    exe = r"C:\aicf\llama\ggml-rpc-server.exe"
    remote(node, "schtasks /end /tn aicfbench_rpc")
    remote(node, f'schtasks /create /tn aicfbench_rpc /tr "{exe} -H 0.0.0.0 -p {RPC_PORT}" /sc once /st 23:59 /ru SYSTEM /f')
    remote(node, "schtasks /run /tn aicfbench_rpc")


def rpc_stop(node: str) -> None:
    remote(node, "schtasks /end /tn aicfbench_rpc")


def wait_port(host: str, port: int, secs: int = 40) -> bool:
    import socket
    t = time.time()
    while time.time() - t < secs:
        try:
            socket.create_connection((host, port), 2).close()
            return True
        except OSError:
            time.sleep(1)
    return False


class PowerSampler:
    """Streams nvidia-smi power.draw (W) from the local and remote GPUs while a window is open."""
    Q = "nvidia-smi --query-gpu=power.draw --format=csv,noheader,nounits -lms 500"

    def __init__(self, nodes: List[str]):
        self.keys = ["linux3060"] + nodes
        self.procs: Dict[str, subprocess.Popen] = {}
        self.samples: Dict[str, List[tuple]] = {k: [] for k in self.keys}
        self.run = True
        for k in self.keys:
            cmd = self.Q.split() if k == "linux3060" else NODES[k]["ssh"] + [self.Q]
            self.procs[k] = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
            threading.Thread(target=self._read, args=(k,), daemon=True).start()

    def _read(self, k: str) -> None:
        for line in self.procs[k].stdout:
            try:
                self.samples[k].append((time.time(), float(line.strip())))
            except ValueError:
                pass

    def window(self, t0: float, t1: float) -> Dict[str, float]:
        return {k: round(statistics.mean([w for t, w in v if t0 <= t <= t1] or [0.0]), 1) for k, v in self.samples.items()}

    def close(self) -> None:
        for pr in self.procs.values():
            pr.terminate()


# ---------------------------------------------------------------- llama-server measurements
def post_stream(prompt: str, n_predict: int) -> Dict[str, Any]:
    payload = {"prompt": prompt, "n_predict": n_predict, "temperature": 0.0, "seed": 1234, "stream": True,
               "cache_prompt": False, "ignore_eos": True}
    req = urllib.request.Request(f"{BASE}/completion", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.perf_counter()
    t_first = t_last = None
    n = 0
    gaps: List[float] = []
    timings: Dict[str, Any] = {}
    with urllib.request.urlopen(req, timeout=900) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            d = line[5:].strip()
            if d == "[DONE]":
                break
            try:
                ch = json.loads(d)
            except json.JSONDecodeError:
                continue
            if isinstance(ch.get("timings"), dict):
                timings = ch["timings"]
            if ch.get("content"):
                now = time.perf_counter()
                if t_last is not None:
                    gaps.append((now - t_last) * 1000)
                t_first = t_first or now
                t_last = now
                n += 1
    t_end = time.perf_counter()
    t_first = t_first or t_end
    out = int(timings.get("predicted_n") or n)
    return {"ttft_ms": (t_first - t0) * 1000, "total_s": t_end - t0, "out_tok": out,
            "tpot_ms": ((t_last or t_end) - t_first) * 1000 / max(1, out - 1),
            "decode_tok_s": float(timings.get("predicted_per_second") or 0),
            "prefill_tok_s": float(timings.get("prompt_per_second") or 0), "gaps": gaps,
            "prompt_tok": int(timings.get("prompt_n") or 0)}


_TEXT = ("Distributed inference splits a neural network across several accelerators. Each device keeps a slice of the "
         "layers and passes small activation tensors to the next one over the network. ")


def make_prompt(chars: int, salt: int) -> str:
    return (f"[{salt}] " + _TEXT * (chars // len(_TEXT) + 1))[:chars]


def med(xs: List[float]) -> float:
    return round(statistics.median(xs), 2) if xs else 0.0


def run_config(model: str, cfg: str) -> Dict[str, Any]:
    path = MODELS[model]
    nodes, ts, extra = CONFIGS[cfg]
    res: Dict[str, Any] = {"model": model, "config": cfg, "cards": 1 + len(nodes), "split": ts, "size_gb": round(path.stat().st_size / 1e9, 2),
                           "links": {n: NODES[n]["label"] for n in nodes}}
    emu_procs: List[subprocess.Popen] = []
    for n in nodes:
        rpc_start(n)
    for n in nodes:
        if not wait_port(NODES[n]["host"], RPC_PORT):
            res["error"] = f"rpc-server on {n} not reachable"
            return res
        if NODES[n].get("emu"):
            emu_procs.append(subprocess.Popen([sys.executable, str(Path(__file__).with_name("wifi_link_emulator.py")),
                "--listen", f"127.0.0.1:{EMU_PORT}", "--upstream", f"{NODES[n]['host']}:{RPC_PORT}",
                "--rtt-samples", str(WORK / "wifi_rtt_samples.txt"), "--up-MBps", "36.7", "--down-MBps", "50.6"],
                stdout=open(WORK / "emu_run.log", "w"), stderr=subprocess.STDOUT))
            if not wait_port("127.0.0.1", EMU_PORT):
                res["error"] = "wifi emulator did not start"
                return res
    rpc = ",".join((f"127.0.0.1:{EMU_PORT}" if NODES[n].get("emu") else f"{NODES[n]['host']}:{RPC_PORT}") for n in nodes)
    args = [str(LLAMA), "-m", str(path), "-c", str(CTX_PER_SLOT * NPARALLEL), "-np", str(NPARALLEL), "-ngl", "99",
            "--port", str(PORT), "--host", "127.0.0.1", "-fa", "on", "-ctk", "f16", "-ctv", "f16", "--no-warmup",
            "--fit", "off", "-lv", "4", "--rpc", rpc, "--split-mode", "layer", "--tensor-split", ts] + extra
    log_path = WORK / f"engine_{model}_{cfg}.log"
    nics = {n: NODES[n]["nic"] for n in nodes}
    # idle baseline of each NIC (Wi-Fi carries other traffic): bytes/s over 5 s
    b0 = {n: nic_bytes(nic) for n, nic in nics.items()}
    time.sleep(5)
    b1 = {n: nic_bytes(nic) for n, nic in nics.items()}
    idle = {n: (b1[n]["rx"] - b0[n]["rx"] + b1[n]["tx"] - b0[n]["tx"]) / 5 for n in nics}
    res["idle_nic_Bps"] = {n: round(v) for n, v in idle.items()}

    n0 = {n: nic_bytes(nic) for n, nic in nics.items()}
    t_launch = time.perf_counter()
    proc = subprocess.Popen(args, stdout=open(log_path, "w"), stderr=subprocess.STDOUT)
    ok = False
    while time.perf_counter() - t_launch < 1500:
        if proc.poll() is not None:
            break
        try:
            if urllib.request.urlopen(f"{BASE}/health", timeout=3).status == 200:
                ok = True
                break
        except Exception:
            time.sleep(1)
    load_s = time.perf_counter() - t_launch
    n1 = {n: nic_bytes(nic) for n, nic in nics.items()}
    if not ok:
        res["error"] = "engine failed to become healthy"
        res["log_tail"] = log_path.read_text(errors="replace")[-1500:]
        proc.terminate()
        for e in emu_procs:
            e.terminate()
        for n in nodes:
            rpc_stop(n)
        return res
    d = nic_delta(n0, n1)
    res["load_s"] = round(load_s, 1)
    res["load_net"] = {n: {"tx_MB": round((d[n]["tx"] - idle[n] * load_s * 0) / 1e6, 1), "rx_MB": round(d[n]["rx"] / 1e6, 1),
                           "tx_MBps_avg": round(d[n]["tx"] / 1e6 / load_s, 1)} for n in nics}
    bufs = re.findall(r"(CUDA\d|RPC\d)\[?[\d.:]*\]?\s+model buffer size\s*=\s*([\d.]+) MiB", log_path.read_text(errors="replace"))
    res["model_buffers_MiB"] = {k: float(v) for k, v in bufs}
    res["vram_used_MiB"] = {"linux3060": vram_mib(None), **{n: vram_mib(n) for n in nodes}}

    post_stream(make_prompt(400, 0), 8)                       # warm path (graph build, sockets)
    # ---- prefill (≈1k tokens), 3 reps; each with a fresh salt so nothing is cached
    ps = PowerSampler(nodes)
    time.sleep(2)
    pre: List[Dict[str, Any]] = []
    a = {n: nic_bytes(nic) for n, nic in nics.items()}; t = time.perf_counter()
    for i in range(3):
        pre.append(post_stream(make_prompt(4200, 100 + i), 1))
    dt = time.perf_counter() - t; bb = {n: nic_bytes(nic) for n, nic in nics.items()}
    dd = nic_delta(a, bb); ptoks = sum(r["prompt_tok"] for r in pre)
    res["prefill"] = {"prompt_tok": pre[0]["prompt_tok"], "tok_s_median": med([r["prefill_tok_s"] for r in pre]),
                      "ttft_ms_median": med([r["ttft_ms"] for r in pre]),
                      "net_bytes_per_prompt_tok": {n: round(((dd[n]["rx"] + dd[n]["tx"]) - idle[n] * dt) / max(1, ptoks)) for n in nics}}
    pre_l: List[Dict[str, Any]] = []
    a = {n: nic_bytes(nic) for n, nic in nics.items()}; t = time.perf_counter()
    for i in range(2):
        pre_l.append(post_stream(make_prompt(12000, 150 + i), 1))
    dt2 = time.perf_counter() - t; bb = {n: nic_bytes(nic) for n, nic in nics.items()}
    dd2 = nic_delta(a, bb); ptoks2 = sum(r["prompt_tok"] for r in pre_l)
    res["prefill_long"] = {"prompt_tok": pre_l[0]["prompt_tok"], "tok_s_median": med([r["prefill_tok_s"] for r in pre_l]),
                           "ttft_ms_median": med([r["ttft_ms"] for r in pre_l]),
                           "net_bytes_per_prompt_tok": {n: round(((dd2[n]["rx"] + dd2[n]["tx"]) - idle[n] * dt2) / max(1, ptoks2)) for n in nics},
                           "net_avg_Mbps": {n: round(((dd2[n]["rx"] + dd2[n]["tx"]) - idle[n] * dt2) * 8 / 1e6 / dt2, 1) for n in nics}}
    # ---- single-stream decode (192 tokens after a 128-char prompt), 3 reps
    dec: List[Dict[str, Any]] = []
    a = {n: nic_bytes(nic) for n, nic in nics.items()}; t = time.perf_counter(); tw0 = time.time()
    for i in range(3):
        dec.append(post_stream(make_prompt(300, 200 + i), 192))
    dt = time.perf_counter() - t; bb = {n: nic_bytes(nic) for n, nic in nics.items()}; tw1 = time.time()
    dd = nic_delta(a, bb); dtoks = sum(r["out_tok"] for r in dec)
    allgaps = sorted(g for r in dec for g in r["gaps"])
    pw = ps.window(tw0, tw1)
    res["decode_single"] = {"tok_s_median": med([r["decode_tok_s"] for r in dec]), "tpot_ms_median": med([r["tpot_ms"] for r in dec]),
                            "ttft_ms_median": med([r["ttft_ms"] for r in dec]),
                            "token_gap_ms": {"p50": round(allgaps[len(allgaps) // 2], 1), "p95": round(allgaps[int(len(allgaps) * .95)], 1),
                                             "p99": round(allgaps[int(len(allgaps) * .99)], 1), "max": round(allgaps[-1], 1)},
                            "gpu_power_W": pw, "joule_per_out_tok": round(sum(pw.values()) * dt / max(1, dtoks), 3),
                            "net_bytes_per_out_tok": {n: round(((dd[n]["rx"] + dd[n]["tx"]) - idle[n] * dt) / max(1, dtoks)) for n in nics},
                            "net_avg_Mbps": {n: round(((dd[n]["rx"] + dd[n]["tx"]) - idle[n] * dt) * 8 / 1e6 / dt, 2) for n in nics}}
    # ---- aggregate decode throughput at concurrency 2 and 4 (128 tokens each)
    for c in (2, 4):
        aggs = []
        for rep in range(2):
            rs: List[Dict[str, Any]] = []
            ths = [threading.Thread(target=lambda k=k: rs.append(post_stream(make_prompt(300, 300 + 10 * rep + k), 128))) for k in range(c)]
            t0 = time.perf_counter(); [x.start() for x in ths]; [x.join() for x in ths]
            aggs.append(sum(r["out_tok"] for r in rs) / (time.perf_counter() - t0))
        res[f"throughput_c{c}_tok_s"] = round(statistics.mean(aggs), 2)
    ps.close()
    res["vram_peak_MiB"] = {"linux3060": vram_mib(None), **{n: vram_mib(n) for n in nodes}}
    proc.terminate()
    try:
        proc.wait(30)
    except Exception:
        proc.kill()
    for e in emu_procs:
        e.terminate()
    for n in nodes:
        rpc_stop(n)
    time.sleep(3)
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--net", action="store_true")
    ap.add_argument("--models", default="Qwen3.8-27B,Qwen3-Coder-30B-A3B")
    ap.add_argument("--configs", default=",".join(MAIN))
    ap.add_argument("--extras", action="store_true", help="also run the Wi-Fi control configs on Qwen3-Coder-30B")
    a = ap.parse_args()
    if a.net:
        (OUT / "network_links.json").write_text(json.dumps(net_probe(), indent=1))
        return
    todo = [(m, c) for m in a.models.split(",") for c in a.configs.split(",")]
    if a.extras:
        todo += [("Qwen3-Coder-30B-A3B", c) for c in EXTRA]
    if True:
        for m, c in todo:
            f = OUT / f"{m}__{c}.json"
            if f.exists() and "error" not in json.loads(f.read_text()):
                print("skip", f.name)
                continue
            print(f"== {m} x {c} cards", flush=True)
            r = run_config(m, c)
            f.write_text(json.dumps(r, indent=1))
            print(json.dumps({k: r.get(k) for k in ("load_s", "prefill", "decode_single", "throughput_c2_tok_s", "throughput_c4_tok_s", "error")})[:600], flush=True)


if __name__ == "__main__":
    main()
