"""OpenAI-compatible chat client (stdlib only)."""
import json, time, urllib.error, urllib.request
from . import config


class LLM:
    def __init__(self, cfg=None, timeout=900):
        self.cfg = cfg or config.load()
        self.url, self.model, self.key, self.timeout = self.cfg["url"], self.cfg["model"], config.read_key(self.cfg), timeout

    def chat(self, messages, tools=None, think=False, max_tokens=4000, temperature=0.3):
        """-> assistant message dict (content, tool_calls, _usage). Retries 429/502/503."""
        body = {"messages": messages, "max_tokens": max_tokens, "temperature": temperature, "chat_template_kwargs": {"enable_thinking": think}}
        if self.model:
            body["model"] = self.model
        if tools:
            body["tools"] = tools
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        req = urllib.request.Request(self.url, json.dumps(body).encode(), headers)
        for attempt in range(10):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    d = json.loads(r.read())
                break
            except urllib.error.HTTPError as e:
                msg = e.read()[:300]
                if e.code not in (429, 502, 503) or attempt == 9:
                    raise RuntimeError("LLM HTTP %d: %r" % (e.code, msg))
                time.sleep(int(e.headers.get("Retry-After") or 5) + 2 * attempt)
            except urllib.error.URLError as e:
                raise RuntimeError("cannot reach the LLM at %s: %s" % (self.url, e.reason))
        m = d["choices"][0]["message"]
        out = {"role": "assistant", "content": m.get("content") or ""}
        if m.get("tool_calls"):
            out["tool_calls"] = m["tool_calls"]
        out["_usage"] = d.get("usage", {})
        return out


def probe(url, model="", key="", timeout=25):
    """Check an OpenAI-compatible endpoint without saving anything. -> {ok, models, detail}. Never raises."""
    base = url.split("/chat/completions")[0].rstrip("/")
    hdr = {"Authorization": "Bearer " + key} if key else {}
    models, detail = [], ""
    try:
        with urllib.request.urlopen(urllib.request.Request(base + "/models", headers=hdr), timeout=timeout) as r:
            models = [m.get("id", "") for m in json.loads(r.read()).get("data", []) if m.get("id")][:50]
    except urllib.error.HTTPError as e:
        detail = "GET /models -> HTTP %d" % e.code
        if e.code in (401, 403):
            return {"ok": False, "models": [], "detail": "server rejected the key (HTTP %d)" % e.code}
    except Exception as e:                                                   # noqa: BLE001 - report, do not crash the UI
        return {"ok": False, "models": [], "detail": "cannot reach %s (%s)" % (base, getattr(e, "reason", None) or type(e).__name__)}
    try:
        cfg = {"url": url, "model": model, "key_file": None}
        c = LLM.__new__(LLM); c.cfg, c.url, c.model, c.key, c.timeout = cfg, url, model, key, timeout
        m = c.chat([{"role": "user", "content": "Reply with the single word OK."}], max_tokens=16)
        return {"ok": True, "models": models, "detail": "answered: %r" % m["content"][:30]}
    except Exception as e:                                                   # noqa: BLE001
        return {"ok": False, "models": models, "detail": (detail + "; " if detail else "") + str(e)[:200]}
