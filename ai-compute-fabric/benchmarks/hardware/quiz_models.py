#!/usr/bin/env python3
"""Rough quality+speed probe of one GGUF on one card: starts llama-server, asks 20 fixed questions with machine-checkable
answers (greedy, same token budget), records correct/truncated/tok_s/peak VRAM. 20 questions is NOT a benchmark: it only
catches gross differences (a model that cannot finish its reasoning in the budget counts as wrong). Measures only."""
import argparse, json, os, re, subprocess, threading, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

Q = [  # (prompt, expected, kind) kind: num = expected number must be the last number of the final text; kw = keyword in final text
 ("Tính 17 × 23. Chỉ cần đưa ra đáp án cuối cùng.", "391", "num"),
 ("Tổng các số nguyên từ 1 đến 100 là bao nhiêu?", "5050", "num"),
 ("Một tàu chạy 60 km/h trong 2,5 giờ. Quãng đường đi được là bao nhiêu km?", "150", "num"),
 ("15% của 240 là bao nhiêu?", "36", "num"),
 ("Số tiếp theo của dãy 2, 6, 12, 20, 30 là gì?", "42", "num"),
 ("3 quả táo giá 12 nghìn đồng. 7 quả táo giá bao nhiêu nghìn đồng?", "28", "num"),
 ("Hình chữ nhật dài 8 cm, rộng 5 cm. Chu vi bao nhiêu cm?", "26", "num"),
 ("Giải phương trình 3x + 7 = 34. x bằng bao nhiêu?", "9", "num"),
 ("Số Fibonacci thứ 10 là bao nhiêu (dãy bắt đầu 1, 1, 2, 3, ...)?", "55", "num"),
 ("2 mũ 10 bằng bao nhiêu?", "1024", "num"),
 ("Lan có số bi gấp đôi Minh. Cả hai có 36 viên. Minh có bao nhiêu viên?", "12", "num"),
 ("Năm nhuận có bao nhiêu ngày?", "366", "num"),
 ("Đoạn Python sau in ra gì? print(len([x for x in range(20) if x % 3 == 0]))", "7", "num"),
 ("Chuỗi 'AIComputeFabric' có bao nhiêu ký tự?", "15", "num"),
 ("Ước chung lớn nhất của 84 và 36 là bao nhiêu?", "12", "num"),
 ("Có bao nhiêu số nguyên tố nhỏ hơn 30?", "10", "num"),
 ("Thủ đô của Việt Nam là thành phố nào?", "hà nội", "kw"),
 ("Chiến thắng Điện Biên Phủ diễn ra vào năm nào?", "1954", "num"),
 ("Ký hiệu hóa học của vàng là gì?", "au", "kw"),
 ("Trả lời đúng một từ tiếng Việt: lá cây thường có màu gì?", "xanh", "kw"),
]


def final_text(msg):
    c = msg.get("content") or ""
    return c.split("</think>")[-1].strip() if "</think>" in c else c.strip()


def ask(url, prompt, max_tokens):
    b = {"model": "x", "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens, "temperature": 0, "seed": 1}
    t0 = time.time()
    r = json.loads(urllib.request.urlopen(urllib.request.Request(url + "/v1/chat/completions", json.dumps(b).encode(),
                   {"Content-Type": "application/json"}), timeout=1800).read())
    ch = r["choices"][0]
    return {"final": final_text(ch["message"]), "finish": ch["finish_reason"], "tokens": r["usage"]["completion_tokens"],
            "tok_s": (r.get("timings") or {}).get("predicted_per_second"), "wall_s": time.time() - t0}


def correct(final, exp, kind):
    f = final.lower()
    if kind == "kw":
        return exp in f
    nums = re.findall(r"\d+(?:[.,]\d+)?", f.replace(".", "").replace(",", "")) if exp != "0" else []
    return bool(nums) and nums[-1] == exp


def vram():
    try:
        return float(subprocess.run(["nvidia-smi", "-i", "0", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                                    capture_output=True, text=True, timeout=10).stdout.strip())
    except Exception:
        return float("nan")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--server", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--tag", required=True)
    p.add_argument("--extra", default="", help="extra llama-server args, e.g. '-ncmoe 20'")
    p.add_argument("--ctx", type=int, default=8192)
    p.add_argument("--parallel", type=int, default=2)
    p.add_argument("--max-tokens", type=int, default=3000)
    p.add_argument("--port", type=int, default=18191)
    p.add_argument("--out", required=True)
    a = p.parse_args()
    cmd = [a.server, "-m", a.model, "-ngl", "99", "-fa", "on", "-c", str(a.ctx), "-np", str(a.parallel), "--port", str(a.port),
           "--host", "127.0.0.1"] + a.extra.split()
    proc = subprocess.Popen(cmd, stdout=open(a.out + f".{a.tag}.server.log", "w"), stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{a.port}"
    peak = [vram()]
    stop = threading.Event()

    def samp():
        while not stop.is_set():
            peak[0] = max(peak[0], vram()); time.sleep(0.5)
    threading.Thread(target=samp, daemon=True).start()
    try:
        for _ in range(900):
            if proc.poll() is not None:
                raise SystemExit(f"server exited rc={proc.returncode}")
            try:
                if urllib.request.urlopen(url + "/health", timeout=2).status == 200:
                    break
            except Exception:
                time.sleep(1)
        t0 = time.time()
        with ThreadPoolExecutor(a.parallel) as ex:
            rs = list(ex.map(lambda q: ask(url, q[0], a.max_tokens), Q))
        wall = time.time() - t0
    finally:
        stop.set(); proc.terminate(); proc.wait(timeout=60)
    items, ok = [], 0
    for (q, exp, kind), r in zip(Q, rs):
        c = correct(r["final"], exp, kind); ok += c
        items.append({"q": q, "expected": exp, "ok": c, "finish": r["finish"], "tokens": r["tokens"], "final": r["final"][:120]})
    ts = [r["tok_s"] for r in rs if r["tok_s"]]
    res = {"tag": a.tag, "model": os.path.basename(a.model), "extra": a.extra, "ctx": a.ctx, "parallel": a.parallel, "max_tokens": a.max_tokens,
           "correct": ok, "total": len(Q), "truncated": sum(r["finish"] == "length" for r in rs),
           "tokens_total": sum(r["tokens"] for r in rs), "wall_s": round(wall, 1), "median_stream_tok_s": round(sorted(ts)[len(ts) // 2], 1) if ts else None,
           "peak_gpu_used_mib": peak[0], "items": items, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    open(a.out, "a").write(json.dumps(res, ensure_ascii=False) + "\n")
    print(f"{a.tag}: {ok}/{len(Q)} correct, truncated {res['truncated']}, tokens {res['tokens_total']}, wall {res['wall_s']}s, "
          f"median stream {res['median_stream_tok_s']} tok/s, peak VRAM {peak[0]:.0f} MiB", flush=True)


if __name__ == "__main__":
    main()
