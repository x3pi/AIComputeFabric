"""Chat client for the llama-server that answers questions. One class so tests and other engines can replace it."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from studio.errors import unavailable


class ChatClient:
    def __init__(self, url: str, timeout: float = 120.0):
        self.url, self.timeout = url.rstrip("/"), timeout

    def available(self) -> bool:
        return bool(self.url)

    def chat(self, messages: list, max_tokens: int = 400, temperature: float = 0.0, json_mode: bool = False) -> str:
        if not self.url:
            raise unavailable("no language model is configured (--llm-url)")
        body = {"messages": messages, "max_tokens": max_tokens, "temperature": temperature, "chat_template_kwargs": {"enable_thinking": False}}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(self.url + "/v1/chat/completions", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.load(r)["choices"][0]["message"]["content"].strip()
        except (urllib.error.URLError, OSError, TimeoutError, KeyError, ValueError) as e:
            raise unavailable(f"the language model did not answer: {e}") from e
