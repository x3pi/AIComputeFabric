#!/usr/bin/env python3
"""Offline tests for the Anthropic-compatible advisor backend, using a local mock server (no network, no real key).
Checks: request shape (path, auth header, version header, no tools, only sanitised text), error handling, key never in audit/reply,
and that a blocked request never reaches the network. Run: python3 test_advisor_anthropic.py"""
import json, os, sys, tempfile, threading, unittest, http.server

os.environ["ADVISOR_AUDIT"] = os.path.join(tempfile.mkdtemp(), "audit.jsonl")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import advisor as A

KEY = "sk-test-0123456789abcdef-NOT-A-REAL-KEY"
SEEN = []
MODE = {"status": 200, "body": None}


class Mock(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        SEEN.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": json.loads(self.rfile.read(n))})
        body = json.dumps(MODE["body"] if MODE["body"] is not None else {"content": [{"type": "text", "text": "Use FeatureFillet3 with a tangent edge set.\n```python\nimport math\nprint(math.pi)\n```"}]}).encode()
        self.send_response(MODE["status"]); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)


class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), Mock)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        d = tempfile.mkdtemp(); cls.keyf = os.path.join(d, "k"); open(cls.keyf, "w").write(KEY + "\n")
        cls.cfg = {"mode": "anthropic", "url": "http://127.0.0.1:%d" % cls.srv.server_address[1], "model": "m-test", "key_file": cls.keyf,
                   "denylist": ["ZX-CRN-7741", "CARRIE_V1"], "pseudonyms": []}

    def setUp(self):
        SEEN.clear(); MODE.update(status=200, body=None)

    def req(self, q="How do I pass a tapered edge set to FeatureFillet3?"):
        return {"kind": "api_usage", "api_names": ["FeatureFillet3"], "error_class": "", "question": q}

    def test_request_shape_and_reply(self):
        r = A.ask(self.req(), self.cfg)
        self.assertTrue(r["ok"], r)
        self.assertIn("FeatureFillet3", r["reply"])
        s = SEEN[0]
        self.assertEqual(s["path"], "/v1/messages")
        self.assertEqual(s["headers"]["authorization"], "Bearer " + KEY)
        self.assertEqual(s["headers"]["anthropic-version"], "2023-06-01")
        self.assertTrue(s["headers"]["user-agent"].startswith("aicf-cad-advisor/"))      # urllib default UA is blocked by Cloudflare (1010)
        self.assertNotIn("tools", s["body"])                        # the advisor has no tools
        self.assertEqual(s["body"]["model"], "m-test")
        self.assertEqual(len(s["body"]["messages"]), 1)

    def test_x_api_key_variant(self):
        A.ask(self.req(), dict(self.cfg, auth="x-api-key"))
        self.assertEqual(SEEN[0]["headers"]["x-api-key"], KEY)
        self.assertNotIn("authorization", SEEN[0]["headers"])

    def test_only_sanitised_text_leaves(self):
        r = A.ask(self.req("Fillet fails on part ZX-CRN-7741 in CARRIE_V1, length 1823.5 mm"), self.cfg)
        sent = json.dumps(SEEN[0]["body"]) if SEEN else ""
        for secret in ("ZX-CRN-7741", "CARRIE_V1", "1823"):
            self.assertNotIn(secret, sent)

    def test_blocked_request_never_reaches_network(self):
        r = A.ask({"kind": "free_chat", "api_names": [], "error_class": "", "question": "hi"}, self.cfg)
        self.assertTrue(r.get("blocked"))
        self.assertEqual(SEEN, [])

    def test_http_error_is_reported_without_key(self):
        MODE["status"] = 401; MODE["body"] = {"error": "bad key " + KEY}
        r = A.ask(self.req(), self.cfg)
        self.assertFalse(r["ok"]); self.assertIn("401", r["reason"]); self.assertNotIn(KEY, json.dumps(r))

    def test_reply_without_text_blocks(self):
        MODE["body"] = {"content": [{"type": "tool_use", "name": "x"}]}
        r = A.ask(self.req(), self.cfg)
        self.assertFalse(r["ok"])

    def test_thinking_eats_all_tokens(self):
        MODE["body"] = {"content": [{"type": "thinking", "thinking": "", "signature": "x"}], "stop_reason": "max_tokens"}
        r = A.ask(self.req(), self.cfg)
        self.assertFalse(r["ok"]); self.assertIn("max_tokens", r["reason"])

    def test_default_max_tokens_leaves_room_for_thinking(self):
        A.ask(self.req(), self.cfg)
        self.assertGreaterEqual(SEEN[0]["body"]["max_tokens"], 4000)

    def test_missing_key_file(self):
        r = A.ask(self.req(), dict(self.cfg, key_file="/nonexistent/key"))
        self.assertFalse(r["ok"]); self.assertEqual(SEEN, [])       # no request without a key

    def test_connection_refused(self):
        r = A.ask(self.req(), dict(self.cfg, url="http://127.0.0.1:9"))
        self.assertFalse(r["ok"]); self.assertNotIn(KEY, json.dumps(r))

    def test_key_never_in_audit_log(self):
        A.ask(self.req(), self.cfg)
        MODE["status"] = 500
        A.ask(self.req(), self.cfg)
        self.assertNotIn(KEY, open(A.AUDIT, encoding="utf-8").read())
        self.assertTrue(A.verify_audit())

    def test_reply_code_checker_still_guards(self):
        ok, _ = A.check_code("import math\nprint(math.pi)")
        bad, _ = A.check_code("import os\nos.system('x')")
        self.assertTrue(ok); self.assertFalse(bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
