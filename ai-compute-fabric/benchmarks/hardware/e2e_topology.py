#!/usr/bin/env python3
"""E2E through the site-gateway: apply a list of topology steps via POST /api/cluster/topology, time each switch,
read back GET /api/cluster/topology, and send one chat request to every engine that should answer.
Steps are JSON (list of request bodies) so nothing about workers/models/N is hardcoded here. Measures only."""
import argparse, json, time, urllib.request, urllib.error


def call(base, tok, path, body=None, timeout=900):
    req = urllib.request.Request(base + path, json.dumps(body).encode() if body is not None else None,
                                 {"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:500]


def chat(url, max_tokens=64):
    b = {"model": "x", "messages": [{"role": "user", "content": "Xin chào, 1+1 bằng mấy?"}],
         "max_tokens": max_tokens, "temperature": 0}
    t0 = time.time()
    try:
        r = json.loads(urllib.request.urlopen(urllib.request.Request(url + "/v1/chat/completions", json.dumps(b).encode(),
                       {"Content-Type": "application/json"}), timeout=300).read())
        tm = r.get("timings", {})
        return {"ok": True, "tok_s": round(tm.get("predicted_per_second", 0), 1), "wall_s": round(time.time() - t0, 1),
                "text": r["choices"][0]["message"]["content"][:80]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://127.0.0.1:18080")
    p.add_argument("--token", default="demo-admin-token")
    p.add_argument("--steps", required=True, help="JSON file: list of topology request bodies")
    p.add_argument("--out", required=True)
    a = p.parse_args()
    res = []
    for body in json.load(open(a.steps)):
        t0 = time.time()
        code, resp = call(a.base, a.token, "/api/cluster/topology", body)
        dt = round(time.time() - t0, 1)
        _, st = call(a.base, a.token, "/api/cluster/topology")
        chats = {}
        if code == 200 and isinstance(st, dict):
            for wid, w in st.get("workers", {}).items():
                if w.get("role") in ("master", "standalone") and w.get("engine_url"):
                    chats[wid] = chat(w["engine_url"])
        rec = {"request": body, "http": code, "switch_s": dt, "response": resp if code != 200 else "ok",
               "status": {k: st.get(k) for k in ("mode", "status", "last_error")} if isinstance(st, dict) else st,
               "workers": {k: {x: v.get(x) for x in ("role", "mode", "model", "state", "badge")}
                           for k, v in (st.get("workers", {}) if isinstance(st, dict) else {}).items()},
               "chat": chats, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        print(json.dumps(rec, ensure_ascii=False), flush=True)
        res.append(rec)
    json.dump(res, open(a.out, "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
