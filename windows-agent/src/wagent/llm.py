"""Client OpenAI-compatible. Model tra ve 1 JSON {"tool":..., "args":{...}} trong text."""
import json, re, requests

class LLM:
    def __init__(self, base_url, model="", api_key="", timeout=300):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model, self.key, self.timeout = model, api_key, timeout
        self.tokens = 0

    def chat(self, messages, max_tokens=400):
        h = {"Content-Type": "application/json"}
        if self.key:
            h["Authorization"] = "Bearer " + self.key
        r = requests.post(self.url, headers=h, timeout=self.timeout, json={
            "model": self.model, "messages": messages, "temperature": 0,
            "max_tokens": max_tokens})
        r.raise_for_status()
        self.tokens += (r.json().get("usage") or {}).get("total_tokens", 0)
        return r.json()["choices"][0]["message"]["content"]

def parse_action(text):
    """Lay object JSON dau tien co khoa 'tool'. Tra (dict|None, loi|None)."""
    t = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    dec = json.JSONDecoder()
    for i, ch in enumerate(t):
        if ch == "{":
            try:
                obj, _ = dec.raw_decode(t[i:])
            except ValueError:
                continue
            if isinstance(obj, dict) and isinstance(obj.get("tool"), str):
                if not isinstance(obj.get("args", {}), dict):
                    return None, "args phai la object"
                obj.setdefault("args", {})
                return obj, None
    return None, "khong tim thay JSON co khoa 'tool'"
