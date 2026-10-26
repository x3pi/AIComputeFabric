#!/usr/bin/env python3
"""Conversation affinity through the real gateway with TWO real workers (plan I.10).
Several conversations run concurrently, each with a long prefix and 3 turns; turns >= 2 are what
affinity is meant to speed up. Run once with affinity off and once on; same engines, same load.
Two workers share one GPU: this shows the routing policy, not fleet behaviour."""
import argparse, collections, json, urllib.error, random, statistics, subprocess, threading, time, urllib.request
from pathlib import Path

def get(url):
    with urllib.request.urlopen(url, timeout=3) as r:
        return json.load(r)

def prefix(rnd, words):
    vocab = ["gpu", "worker", "tenant", "hàng đợi", "máy chủ", "bộ nhớ", "mô hình", "yêu cầu", "độ trễ", "văn phòng", "dung lượng", "lập lịch"]
    return " ".join(rnd.choice(vocab) + str(rnd.randint(0, 999)) for _ in range(words))

def turn(url, messages, max_tokens=24, tenant="demo"):
    body = json.dumps({"stream": True, "max_tokens": max_tokens, "temperature": 0, "messages": messages,
                       "chat_template_kwargs": {"enable_thinking": False}}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "X-Tenant-ID": tenant})
    t0, ttft, text = time.time(), None, ""
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            worker = r.headers.get("X-AICF-Worker")
            for line in r:
                line = line.decode().strip()
                if not line.startswith("data: ") or line.endswith("[DONE]"):
                    continue
                d = json.loads(line[6:])["choices"][0]["delta"]
                if (d.get("content") or d.get("reasoning_content")) and ttft is None:
                    ttft = (time.time() - t0) * 1000
                text += d.get("content") or ""
        return worker, ttft, text, 200
    except urllib.error.HTTPError as e:
        return None, None, "", e.code

def run_mode(a, patience, wd):
    procs = []
    try:
        procs.append(subprocess.Popen([a.gateway, "-grpc", "127.0.0.1:17943", "-http", "127.0.0.1:18680", "-max-queue", "32",
                                       "-max-wait", "30s", "-affinity-patience", patience], stdout=open(wd / f"gw_{patience}.log", "w"), stderr=subprocess.STDOUT))
        time.sleep(1)
        for i in (1, 2):
            procs.append(subprocess.Popen([a.guard, "--llama-server", a.server, "--model", a.model, "--ctx", "4096", "--parallel", "2",
                                           "--headroom-mib", "512", "--engine-port", str(18690 + i), "--listen", f"127.0.0.1:{18699 + i}",
                                           "--gateway", "127.0.0.1:17943", "--worker-id", f"pc-{i:03d}"],
                                          stdout=open(wd / f"guard{i}_{patience}.log", "w"), stderr=subprocess.STDOUT))
        t0 = time.time()
        while time.time() - t0 < 240:
            try:
                ws = get("http://127.0.0.1:18680/api/workers")
                if len(ws) == 2 and all(w["state"] == "running" for w in ws):
                    break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            raise SystemExit("workers never running")
        time.sleep(3)
        url = "http://127.0.0.1:18680/v1/chat/completions"
        errors = collections.Counter()
        rows, lock, stop_at = [], threading.Lock(), time.time() + a.duration
        def conv(cid):
            rnd = random.Random(cid)
            n = 0
            while time.time() < stop_at:
                n += 1
                sysmsg = {"role": "system", "content": "Bạn là trợ lý nội bộ. Tài liệu: " + prefix(rnd, a.words)}
                msgs, last = [sysmsg, {"role": "user", "content": "Tóm tắt tài liệu trong một câu."}], None
                for k in (1, 2, 3):
                    if time.time() > stop_at:
                        return
                    w, ttft, text, code = turn(url, msgs)
                    if code != 200:
                        errors[code] += 1
                        time.sleep(0.3); break
                    with lock:
                        rows.append({"conv": f"{cid}.{n}", "turn": k, "worker": w, "ttft_ms": round(ttft, 1), "same_as_prev": (w == last) if k > 1 else None})
                    last = w
                    msgs += [{"role": "assistant", "content": text}, {"role": "user", "content": f"Còn ý chính số {k + 1} là gì?"}]
        def noise(nid):  # unrelated long generations: they keep one worker busier so least-loaded would split conversations
            rnd = random.Random(1000 + nid)
            while time.time() < stop_at:
                turn(url, [{"role": "user", "content": "Viết một đoạn dài về " + prefix(rnd, 20)}], max_tokens=160, tenant="noise")
        ts = [threading.Thread(target=conv, args=(c,)) for c in range(a.convs)]
        ns = [threading.Thread(target=noise, args=(n,), daemon=True) for n in range(a.noise)]
        [t.start() for t in ts + ns]; [t.join() for t in ts]
        print("errors:", dict(errors), flush=True)
        return rows
    finally:
        for p in reversed(procs):
            p.terminate()
        time.sleep(2)
        subprocess.run(["pkill", "-x", "llama-server"], check=False)

def summarize(rows):
    later = [r for r in rows if r["turn"] >= 2]
    t = [r["ttft_ms"] for r in later]
    t.sort()
    return {"turns_total": len(rows), "later_turns": len(later), "later_ttft_p50_ms": statistics.median(t) if t else None,
            "later_ttft_p95_ms": t[int(0.95 * (len(t) - 1))] if t else None,
            "later_turns_on_same_worker_pct": round(100 * sum(1 for r in later if r["same_as_prev"]) / len(later), 1) if later else None}

def main():
    ap = argparse.ArgumentParser()
    for k in ("gateway", "guard", "server", "model", "out", "workdir"):
        ap.add_argument("--" + k, required=True)
    ap.add_argument("--duration", type=int, default=90); ap.add_argument("--convs", type=int, default=6); ap.add_argument("--words", type=int, default=350); ap.add_argument("--noise", type=int, default=0); ap.add_argument("--patience", default="300ms")
    a = ap.parse_args()
    wd = Path(a.workdir); wd.mkdir(parents=True, exist_ok=True)
    out = {}
    for name, patience in (("affinity_off", "0s"), ("affinity_on", a.patience)):
        rows = run_mode(a, patience, wd)
        out[name] = {"summary": summarize(rows), "rows": rows}
        print(name, json.dumps(out[name]["summary"]), flush=True)
    Path(a.out).write_text(json.dumps({"schema_version": 1, "model": Path(a.model).name, "conversations_in_parallel": a.convs,
                                       "prefix_words": a.words, "noise_streams": a.noise, "patience_on": a.patience, "modes": out}, indent=2))

if __name__ == "__main__":
    main()
