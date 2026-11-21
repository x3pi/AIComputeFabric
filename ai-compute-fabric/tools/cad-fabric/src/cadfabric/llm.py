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
