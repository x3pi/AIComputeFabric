"""Gateway client (OpenAI-compatible chat completions), stdlib only. URL/model/key come from the shared aicfconf of the CAD agent."""
import json, os, sys, time, urllib.error, urllib.request
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "cad-agent"))
import aicfconf


class LLM:
    def __init__(self, url=None, model=None, key=None, timeout=900):
        cfg = aicfconf.load()
        self.url, self.model = url or aicfconf.url(cfg), model or aicfconf.model(cfg)
        self.key = key or aicfconf.read_key(cfg)
        self.timeout = timeout

    def chat(self, messages, tools=None, think=False, max_tokens=4000, temperature=0.3):
        """-> assistant message dict (content, tool_calls). Retries 429/502/503."""
        body = {"model": self.model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature, "chat_template_kwargs": {"enable_thinking": think}}
        if tools:
            body["tools"] = tools
        req = urllib.request.Request(self.url, json.dumps(body).encode(), {"Content-Type": "application/json", "Authorization": "Bearer " + self.key})
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
        m = d["choices"][0]["message"]
        out = {"role": "assistant", "content": m.get("content") or ""}
        if m.get("tool_calls"):
            out["tool_calls"] = m["tool_calls"]
        out["_usage"] = d.get("usage", {})
        return out
