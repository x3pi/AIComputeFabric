#!/usr/bin/env python3
"""Does sending a conversation's next turn to the SAME worker pay off? (plan I.10)
Two real llama-server processes on one GPU. Per trial: a fresh long conversation prefix, turn 1 on
server A, then turn 2 (prefix + answer + new question) once to A (warm prefix cache) and, on a
separate conversation, once to B (cold). TTFT is the time to the first streamed token."""
import argparse, json, statistics, subprocess, time, urllib.request, random
from pathlib import Path

def health(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:
            return r.status == 200
    except Exception:
        return False

def chat(port, messages, max_tokens=24):
    body = json.dumps({"stream": True, "max_tokens": max_tokens, "temperature": 0, "messages": messages,
                       "chat_template_kwargs": {"enable_thinking": False}}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions", data=body, headers={"Content-Type": "application/json"})
    t0, ttft, text = time.time(), None, ""
    with urllib.request.urlopen(req, timeout=120) as r:
        for line in r:
            line = line.decode().strip()
            if not line.startswith("data: ") or line.endswith("[DONE]"):
                continue
            d = json.loads(line[6:])["choices"][0]["delta"]
            piece = (d.get("content") or "") + (d.get("reasoning_content") or "")
            if piece and ttft is None:
                ttft = (time.time() - t0) * 1000
            text += d.get("content") or ""
    return ttft, text

def prefix(seed, words):
    rnd = random.Random(seed)
    vocab = ["gpu", "worker", "tenant", "hàng đợi", "máy chủ", "bộ nhớ", "mô hình", "yêu cầu", "độ trễ", "văn phòng", "dung lượng", "lập lịch"]
    return " ".join(rnd.choice(vocab) + str(rnd.randint(0, 99)) for _ in range(words))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", required=True); ap.add_argument("--model", required=True)
    ap.add_argument("--out", required=True); ap.add_argument("--trials", type=int, default=6)
    ap.add_argument("--words", type=int, default=900)
    a = ap.parse_args()
    ports = (18701, 18702)
    procs = [subprocess.Popen([a.server, "-m", a.model, "-c", "16384", "-np", "2", "-ngl", "99", "--port", str(p), "--host", "127.0.0.1",
                               "--no-warmup", "--fit", "off"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for p in ports]
    rows = []
    try:
        t0 = time.time()
        while not all(health(p) for p in ports):
            if time.time() - t0 > 240:
                raise SystemExit("servers never healthy")
            time.sleep(0.5)
        chat(ports[0], [{"role": "user", "content": "hi"}]); chat(ports[1], [{"role": "user", "content": "hi"}])  # warm CUDA
        for i in range(a.trials):
            for mode, turn2_port in (("same_worker", ports[0]), ("other_worker", ports[1])):
                sysmsg = {"role": "system", "content": "Bạn là trợ lý nội bộ. Tài liệu tham khảo: " + prefix(1000 * i + (0 if mode == "same_worker" else 500), a.words)}
                u1 = {"role": "user", "content": "Tóm tắt tài liệu trong một câu."}
                _, ans = chat(ports[0], [sysmsg, u1])                       # turn 1 always on A
                msgs = [sysmsg, u1, {"role": "assistant", "content": ans}, {"role": "user", "content": "Còn ý chính thứ hai là gì?"}]
                ttft, _ = chat(turn2_port, msgs)                              # turn 2: A (warm) or B (cold)
                rows.append({"trial": i, "mode": mode, "turn2_ttft_ms": round(ttft, 1)})
                print(rows[-1], flush=True)
    finally:
        for p in procs:
            p.terminate()
        time.sleep(2)
        subprocess.run(["pkill", "-x", "llama-server"], check=False)
    by = {m: [r["turn2_ttft_ms"] for r in rows if r["mode"] == m] for m in ("same_worker", "other_worker")}
    summ = {m: {"median_ms": statistics.median(v), "min_ms": min(v), "max_ms": max(v), "n": len(v)} for m, v in by.items()}
    summ["speedup_median"] = round(summ["other_worker"]["median_ms"] / summ["same_worker"]["median_ms"], 2)
    Path(a.out).write_text(json.dumps({"schema_version": 1, "model": Path(a.model).name, "prefix_words": a.words, "summary": summ, "rows": rows}, indent=2))
    print(json.dumps(summ))

if __name__ == "__main__":
    main()
