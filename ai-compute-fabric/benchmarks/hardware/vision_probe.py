#!/usr/bin/env python3
"""Image-input probe for the OpenAI-compatible endpoint (engine direct by default, or the gateway with --key).
Synthetic images with a known answer (printed invoice number, counted shapes, colour/position) at several resolutions;
reports correctness, image tokens (prompt_tokens minus the text part), TTFT / total time and peak VRAM of both cards.
Measures only. Usage: vision_probe.py LABEL OUT.json [--base URL] [--key KEY] [--model NAME] [--reps N]"""
import argparse, base64, io, json, random, subprocess, threading, time, urllib.request
from PIL import Image, ImageDraw, ImageFont

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def font(sz):
    try:
        return ImageFont.truetype(FONT, sz)
    except OSError:
        return ImageFont.load_default()


def img_text(w, h, code):
    im = Image.new("RGB", (w, h), "white"); d = ImageDraw.Draw(im); s = max(18, w // 18)
    d.text((w // 20, h // 5), f"INVOICE {code}", fill="black", font=font(s * 2))
    d.text((w // 20, h // 2), "TOTAL 1520.75 USD", fill=(40, 40, 160), font=font(s))
    d.text((w // 20, int(h * 0.75)), "Customer: ACME Trading Co.", fill=(60, 60, 60), font=font(s))
    return im


def img_count(w, h, n_red, n_blue, seed):
    rnd = random.Random(seed); im = Image.new("RGB", (w, h), "white"); d = ImageDraw.Draw(im); r = max(14, w // 22)
    pts = []
    def spot():
        for _ in range(500):
            x, y = rnd.randint(r, w - r), rnd.randint(r, h - r)
            if all((x - a) ** 2 + (y - b) ** 2 > (3 * r) ** 2 for a, b in pts):
                pts.append((x, y)); return x, y
        raise RuntimeError("no space")
    for _ in range(n_red):
        x, y = spot(); d.ellipse([x - r, y - r, x + r, y + r], fill=(220, 20, 20))
    for _ in range(n_blue):
        x, y = spot(); d.rectangle([x - r, y - r, x + r, y + r], fill=(20, 40, 220))
    return im


def img_pos(w, h):
    im = Image.new("RGB", (w, h), "white"); d = ImageDraw.Draw(im)
    d.rectangle([w // 12, h // 12, w // 3, h // 3], fill=(20, 160, 40))       # green square, top-left
    d.ellipse([int(w * .6), int(h * .6), int(w * .9), int(h * .9)], fill=(240, 200, 0))  # yellow circle, bottom-right
    return im


def b64(im):
    b = io.BytesIO(); im.save(b, "PNG"); return base64.b64encode(b.getvalue()).decode(), len(b.getvalue())


def ask(base, key, model, im, q, max_tokens=48):
    data, nbytes = b64(im)
    body = {"model": model, "temperature": 0, "max_tokens": max_tokens, "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + data}},
                                                      {"type": "text", "text": q}]}]}
    h = {"Content-Type": "application/json"}
    if key:
        h["Authorization"] = "Bearer " + key
    t0 = time.time()
    try:
        d = json.loads(urllib.request.urlopen(urllib.request.Request(base + "/v1/chat/completions", json.dumps(body).encode(), h), timeout=600).read())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read()[:200].decode(errors='replace')}", "png_bytes": nbytes}
    except Exception as e:
        return {"error": str(e)[:200], "png_bytes": nbytes}
    tm = d.get("timings", {})
    return {"answer": (d["choices"][0]["message"].get("content") or "").strip(), "prompt_tokens": d["usage"]["prompt_tokens"],
            "seconds": round(time.time() - t0, 2), "prefill_tok_s": round(tm.get("prompt_per_second", 0), 1), "png_bytes": nbytes}


class Vram:
    def __init__(self):
        self.peak = {"linux": 0, "win": 0}; self.stop = False
        threading.Thread(target=self.run, daemon=True).start()
    def run(self):
        while not self.stop:
            try:
                self.peak["linux"] = max(self.peak["linux"], int(subprocess.check_output(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"], text=True).split()[0]))
                self.peak["win"] = max(self.peak["win"], int(subprocess.check_output(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", "fios02@10.42.0.225", "nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits"], text=True, timeout=8).split()[0]))
            except Exception:
                pass
            time.sleep(1)


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("label"); ap.add_argument("out")
    ap.add_argument("--base", default="http://127.0.0.1:18090"); ap.add_argument("--key", default="")
    ap.add_argument("--model", default="Qwen3.8-27B-UD-Q4_K_M.gguf"); ap.add_argument("--sizes", default="512x384,1024x768,1920x1080")
    a = ap.parse_args()
    v = Vram(); rows = []; ask(a.base, a.key, a.model, img_pos(256, 256), "What colour is the square?", 8)  # warm-up
    for sz in a.sizes.split(","):
        w, h = map(int, sz.split("x"))
        cases = [("text_invoice", img_text(w, h, "48213"), "What is the invoice number? Answer with the number only.", lambda s: "48213" in s),
                 ("count_red", img_count(w, h, 7, 4, 1), "How many red circles are in the image? Answer with the number only.", lambda s: s.strip(" .") == "7" or s.lower().startswith("7") or "seven" in s.lower()),
                 ("count_blue", img_count(w, h, 7, 4, 1), "How many blue squares are in the image? Answer with the number only.", lambda s: s.strip(" .") == "4" or s.lower().startswith("4") or "four" in s.lower()),
                 ("position", img_pos(w, h), "What colour is the shape in the top-left corner? One word.", lambda s: "green" in s.lower())]
        for name, im, q, chk in cases:
            r = ask(a.base, a.key, a.model, im, q); r.update(size=sz, case=name); r["correct"] = bool(r.get("answer") is not None and "error" not in r and chk(r["answer"]))
            rows.append(r); print(json.dumps(r), flush=True)
    v.stop = True
    ok = [r for r in rows if "error" not in r]
    res = {"label": a.label, "correct": sum(r["correct"] for r in rows), "total": len(rows), "errors": sum("error" in r for r in rows),
           "vram_peak_MiB": v.peak, "rows": rows}
    json.dump(res, open(a.out, "w"), indent=1, ensure_ascii=False); print("DONE", res["correct"], "/", res["total"], "errors", res["errors"], v.peak)


if __name__ == "__main__":
    main()
