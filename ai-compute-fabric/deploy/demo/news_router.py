#!/usr/bin/env python3
"""AICF news router: sits IN FRONT of the AICF chat proxy and adds fresh news to chat requests that ask for news.

  client -> news_router (this) -> lan_chat_proxy (:18080, keeps its path allow-list and admin protection) -> gateway -> Qwen

For POST /v1/chat/completions whose last user message asks for news, it (1) checks the caller's API key against the upstream, (2) asks the
Langflow retrieval flow for the latest articles, (3) puts them into the system context and forwards the request UNCHANGED otherwise, so the
chat model answers (streaming included). Everything else is relayed untouched. Fail-open: if anything about the news lookup fails the
request goes through with a note that fresh news could not be fetched (so the model does not invent it).
Python standard library only.
"""
from __future__ import annotations

import argparse
import copy
import http.client
import json
import os
import re
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY = 2 << 20
MAX_QUESTION_CHARS = 600           # a long message is a document/code, not a news question
MAX_NEWS_CHARS = 5000
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade", "content-length", "host"}

# Precision over recall: a wrong "news" decision only adds context, but costs a Langflow call and a few seconds.
NEWS_RX = [re.compile(p, re.I) for p in (
    r"\btin\s*tức\b", r"\bthời\s*sự\b", r"\btin\s*(mới|nóng|nhanh|hot)\b", r"\bbản\s*tin\b",
    r"\b(news|headlines?|breaking)\b", r"\b(có|xảy\s*ra)\s+(chuyện\s+|việc\s+)?gì\s+(mới|hot|nóng|hôm\s*nay)\b",
    r"\bcập\s*nhật\s+(tình\s*hình|diễn\s*biến|tin)\b",
    r"^\s*tin\s+(về|đồn|vụ|gì|ceo|của)\b", r"\btin\s*đồn\b", r"\b(có\s*thật|có\s*đúng)\s*(không|ko)\b",
    r"\bcó\s+(tin|thông\s*tin)\s+(gì|mới|nào)\b", r"\btin\s+gì\s+(mới|hot|nóng)\b",
    r"\bvụ\s+(việc\s+)?\w+.*\b(mới|hôm\s*nay|vừa)\b", r"\b(scandal|bê\s*bối|ngoại\s*tình|bị\s*bắt|qua\s*đời|từ\s*chức)\b",
)]
FORCE_ON, FORCE_OFF = ("/tin ", "/news "), ("/nonews ",)


def user_text(msg: dict) -> str:
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
    return ""


def detect(body: dict) -> tuple[bool, str]:
    """-> (wants_news, question). Never raises on odd input. Tool/function-calling traffic (agents) is never touched."""
    msgs = body.get("messages")
    if not isinstance(msgs, list) or not msgs or body.get("tools") or body.get("functions"):
        return False, ""
    last = msgs[-1]
    if not isinstance(last, dict) or last.get("role") != "user":
        return False, ""
    text = user_text(last).strip()
    low = text.lower()
    for p in FORCE_OFF:
        if low.startswith(p):
            return False, text[len(p):].strip()
    for p in FORCE_ON:
        if low.startswith(p):
            return True, text[len(p):].strip() or text
    if not text or len(text) > MAX_QUESTION_CHARS:
        return False, ""
    return any(r.search(text) for r in NEWS_RX), text


def inject(body: dict, articles: str | None, now: str) -> dict:
    """Return a copy of body with the news (or a 'could not fetch' note) added to the system context; strips a /tin prefix."""
    out = copy.deepcopy(body)
    if articles:
        note = (f"Tin tức mới nhất (nguồn Google News, lấy lúc {now}; chỉ gồm tiêu đề và đoạn trích):\n{articles[:MAX_NEWS_CHARS]}\n\n"
                "Khi trả lời câu hỏi về tin tức, hãy dựa vào danh sách trên, nêu tên báo/nguồn, không bịa chi tiết ngoài danh sách; không khẳng định một tin là thật hay giả nếu các nguồn trên không nói vậy; nếu thông tin chưa đủ thì nói rõ.")
    else:
        note = ("Hệ thống không lấy được tin tức mới lúc này. Đừng tự đoán hay bịa tin mới: hãy nói rõ với người dùng rằng chưa tra cứu được "
                "tin tức hiện tại, tuyệt đối không khẳng định tin đó là thật hay giả, và chỉ trả lời phần kiến thức chung nếu có.")
    msgs = out["messages"]
    if msgs[0].get("role") == "system" and isinstance(msgs[0].get("content"), str):
        msgs[0]["content"] += "\n\n" + note
    elif msgs[0].get("role") == "system" and isinstance(msgs[0].get("content"), list):
        msgs[0]["content"].append({"type": "text", "text": note})
    else:
        msgs.insert(0, {"role": "system", "content": note})
    last = msgs[-1]
    if isinstance(last.get("content"), str):
        low = last["content"].lower()
        for p in FORCE_ON:
            if low.startswith(p):
                last["content"] = last["content"][len(p):].strip() or last["content"]
    return out


class Config:
    upstream = ("192.168.1.40", 18080)
    langflow = "http://127.0.0.1:7860"
    flow_id = ""
    langflow_key = ""
    lookup_timeout = 40.0


_CACHE: dict[str, tuple[float, str, str]] = {}   # key -> (monotonic time, articles, wall-clock label)
_TOPICS: dict[str, tuple[str, float]] = {}       # key -> (original question, monotonic time of last request)
REFRESH_EVERY = 300.0    # topics people asked about are re-fetched in the background every 5 min, so answers are never older than that
CACHE_TTL = REFRESH_EVERY + 60.0   # serve from cache while the background refresh keeps it fresh (grace for a slow refresh)
TOPIC_IDLE_MAX = 3600.0  # stop refreshing a topic nobody asked about for 1 h
MAX_TOPICS = 10          # bound the Google/Qwen load
STALE_MAX = 3600.0       # Google rate-limits (HTTP 429) now and then: a result up to 1 h old beats "no news", and is labelled with its real time
RETRY_WAITS = (3.0, 8.0)   # pauses before retry 1 and 2
RETRY_BUDGET_S = 25.0      # do not start another retry after this much time


def fetch_news(question: str, cfg: type[Config] = Config) -> tuple[str | None, str | None]:
    """-> (articles, wall-clock time they were fetched). Fresh cache, then the flow (one retry after a FAST failure such as a 429), then a stale
    cache entry. (None, None) when nothing is available; the caller fails open."""
    key = " ".join(question.lower().split())
    _TOPICS[key] = (question, time.monotonic())
    if len(_TOPICS) > MAX_TOPICS:
        del _TOPICS[min(_TOPICS, key=lambda k: _TOPICS[k][1])]
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL:
        return hit[1], hit[2]
    t0 = time.monotonic()
    news = _fetch_news(question, cfg)
    for wait in RETRY_WAITS:  # Google's 429 is intermittent (per-IP burst limit): wait a little and ask again, within a total time budget
        if news or time.monotonic() - t0 > RETRY_BUDGET_S:
            break
        time.sleep(wait)
        news = _fetch_news(question, cfg)
    if news:
        if len(_CACHE) > 200:
            _CACHE.clear()
        when = time.strftime("%H:%M %d/%m/%Y")
        _CACHE[key] = (time.monotonic(), news, when)
        return news, when
    if hit and time.monotonic() - hit[0] < STALE_MAX:
        return hit[1], hit[2] + " (bản lưu cũ, dịch vụ tin tức đang bận)"
    return None, None


def refresh_loop(cfg: type[Config] = Config) -> None:
    """Background thread: every REFRESH_EVERY seconds re-fetch each recently asked topic (spaced out to stay under Google's rate limit)."""
    while True:
        time.sleep(REFRESH_EVERY)
        try:
            import aicf_tools_config as _C
            if not _C.tool_on("news"):
                continue  # news (Langflow) switched off by the admin: do not call Langflow in the background either
        except ImportError:
            pass
        now = time.monotonic()
        for key, (question, last) in list(_TOPICS.items()):
            if now - last > TOPIC_IDLE_MAX:
                _TOPICS.pop(key, None)
                _CACHE.pop(key, None)
                continue
            news = _fetch_news(question, cfg)
            for wait in (20, 60):  # transient failures (Google 429): retry twice with growing pauses
                if news:
                    break
                time.sleep(wait)
                news = _fetch_news(question, cfg)
            if news:
                _CACHE[key] = (time.monotonic(), news, time.strftime("%H:%M %d/%m/%Y"))
            print(f"news refresh: {'ok' if news else 'failed (kept old)'}", file=sys.stderr, flush=True)
            time.sleep(5)


def start_refresher(cfg: type[Config] = Config) -> None:
    import threading
    threading.Thread(target=refresh_loop, args=(cfg,), daemon=True, name="news-refresh").start()


def _fetch_news(question: str, cfg: type[Config] = Config) -> str | None:
    try:
        req = urllib.request.Request(f"{cfg.langflow}/api/v1/run/{cfg.flow_id}?stream=false", method="POST",
                                     data=json.dumps({"input_value": question, "input_type": "chat", "output_type": "chat"}).encode(),
                                     headers={"Content-Type": "application/json", "x-api-key": cfg.langflow_key})
        with urllib.request.urlopen(req, timeout=cfg.lookup_timeout) as r:
            text = json.loads(r.read())["outputs"][0]["outputs"][0]["results"]["message"]["text"].strip()
        # NewsSearch reports its own failures as a pseudo-article titled "Error" / "No articles found": that is NOT news.
        if not text or text.startswith("- Error") or text.startswith("- No articles found"):
            print(f"news flow returned no articles: {text[:120]!r}", file=sys.stderr, flush=True)
            return None
        return text
    except Exception as e:  # noqa: BLE001 - any failure means "no news", never a failed chat
        print(f"news lookup failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return None


def key_is_valid(auth: str, cfg: type[Config] = Config) -> bool:
    """Only callers the gateway would serve may trigger a lookup (the port can be reachable from the internet): ask the upstream."""
    if not auth:
        return False
    try:
        c = http.client.HTTPConnection(*cfg.upstream, timeout=8)
        c.request("GET", "/v1/models", headers={"Authorization": auth})
        ok = c.getresponse().status == 200
        c.close()
        return ok
    except Exception:  # noqa: BLE001
        return False


def strip_off_prefix(body: dict) -> dict | None:
    """If the last user message starts with /nonews, return a copy without the prefix (else None)."""
    last = body["messages"][-1]
    if isinstance(last.get("content"), str) and last["content"].lower().lstrip().startswith(FORCE_OFF):
        out = copy.deepcopy(body)
        t = last["content"].lstrip()
        out["messages"][-1]["content"] = t[len(FORCE_OFF[0]):].lstrip()
        return out
    return None


def _log_decision(body: dict, wants: bool, has_auth: bool) -> None:
    """One line per chat request, WITHOUT the content: why it was or was not treated as a news question."""
    msgs = body.get("messages") if isinstance(body.get("messages"), list) else []
    last = msgs[-1] if msgs and isinstance(msgs[-1], dict) else {}
    why = ("news" if wants else "tools" if (body.get("tools") or body.get("functions")) else "last-not-user" if last.get("role") != "user"
           else "too-long" if len(user_text(last)) > MAX_QUESTION_CHARS else "no-news-pattern")
    print(f"news decision: {why} (last user chars={len(user_text(last))}, msgs={len(msgs)}, auth={'yes' if has_auth else 'no'})", file=sys.stderr, flush=True)


def process(raw: bytes, auth: str, cfg: type[Config] = Config) -> tuple[bytes, str | None]:
    """Entry point for the AICF chat proxy hook: (request body, Authorization) -> (body to forward, value for X-AICF-News or None).
    NEVER raises: any problem returns the original body untouched."""
    try:
        body = json.loads(raw)
        wants, question = detect(body)
        _log_decision(body, wants, bool(auth))
        if not wants:
            stripped = strip_off_prefix(body) if isinstance(body.get("messages"), list) and body["messages"] and isinstance(body["messages"][-1], dict) else None
            return (json.dumps(stripped).encode(), None) if stripped else (raw, None)
        if not key_is_valid(auth, cfg):
            return raw, "skipped-auth"
        news, when = fetch_news(question, cfg)
        return json.dumps(inject(body, news, when or "")).encode(), ("injected" if news else "unavailable")
    except Exception as e:  # noqa: BLE001
        print(f"news hook error (request passed through): {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return raw, None


def configure_from_env(upstream: tuple[str, int]) -> None:
    """Configure for use inside the chat proxy (env vars NEWS_LANGFLOW_URL, NEWS_FLOW_ID_FILE, NEWS_LANGFLOW_KEY_FILE)."""
    Config.upstream = upstream
    Config.langflow = os.environ.get("NEWS_LANGFLOW_URL", "http://127.0.0.1:7860").rstrip("/")
    Config.flow_id = open(os.environ.get("NEWS_FLOW_ID_FILE", "/mnt/Data/langflow/news_retrieve_flow.id")).read().strip()
    Config.langflow_key = open(os.environ.get("NEWS_LANGFLOW_KEY_FILE", "/mnt/Data/langflow/api.key")).read().strip()
    start_refresher()


class Handler(BaseHTTPRequestHandler):
    server_version = "aicf-news-router"

    def log_message(self, fmt, *args):  # one line per request; never the content
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def _relay(self, body: bytes | None, extra: dict | None = None) -> None:
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        if body is not None:
            headers["Content-Length"] = str(len(body))
        conn = http.client.HTTPConnection(*Config.upstream, timeout=600)
        try:
            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() not in HOP:
                    self.send_header(k, v)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.send_header("Connection", "close")  # HTTP/1.0 style: body ends at EOF, so SSE streams relay as they arrive
            self.end_headers()
            while chunk := resp.read(2048):
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            try:
                self.send_error(502, f"upstream error: {type(e).__name__}")
            except Exception:  # noqa: BLE001
                pass
        finally:
            conn.close()

    def _handle(self) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            return self.send_error(413, "body too large")
        raw = self.rfile.read(n) if n else None
        extra: dict = {}
        if self.command == "POST" and self.path.split("?")[0] == "/v1/chat/completions" and raw:
            try:
                body = json.loads(raw)
                wants, question = detect(body)
            except Exception:  # noqa: BLE001
                wants, question = False, ""
            if wants:
                t0 = time.monotonic()
                if key_is_valid(self.headers.get("Authorization", "")):
                    news, when = fetch_news(question)
                    raw = json.dumps(inject(body, news, when or "")).encode()
                    extra["X-AICF-News"] = "injected" if news else "unavailable"
                else:
                    extra["X-AICF-News"] = "skipped-auth"
                self.log_message("news route: %s in %.1fs", extra["X-AICF-News"], time.monotonic() - t0)
        self._relay(raw, extra)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _handle


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", default=os.environ.get("NEWS_ROUTER_LISTEN", "127.0.0.1:18090"))
    ap.add_argument("--upstream", default=os.environ.get("NEWS_ROUTER_UPSTREAM", "192.168.1.40:18080"), help="the AICF chat proxy (NOT the bare gateway: it would bypass the proxy's admin protection)")
    ap.add_argument("--langflow", default=os.environ.get("NEWS_LANGFLOW_URL", "http://127.0.0.1:7860"))
    ap.add_argument("--flow-id-file", default=os.environ.get("NEWS_FLOW_ID_FILE", "/mnt/Data/langflow/news_retrieve_flow.id"))
    ap.add_argument("--langflow-key-file", default=os.environ.get("NEWS_LANGFLOW_KEY_FILE", "/mnt/Data/langflow/api.key"))
    a = ap.parse_args()
    uh, up = a.upstream.rsplit(":", 1)
    Config.upstream, Config.langflow = (uh, int(up)), a.langflow.rstrip("/")
    Config.flow_id = open(a.flow_id_file).read().strip()
    Config.langflow_key = open(a.langflow_key_file).read().strip()
    h, p = a.listen.rsplit(":", 1)
    print(f"news router on {a.listen} -> {a.upstream}; langflow {Config.langflow} flow {Config.flow_id[:8]}", flush=True)
    ThreadingHTTPServer((h, int(p)), Handler).serve_forever()


if __name__ == "__main__":
    main()
