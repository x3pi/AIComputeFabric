#!/usr/bin/env python3
"""Offline tests (no gateway). Run: python -m unittest discover -s tests"""
import json, math, os, sys, time, unittest
from cadfabric.cq_backend import CadQueryBackend
from cadfabric import agent, tasks, planners, config
from cadfabric import verifier as V

CYL = "result = cq.Workplane('XY').circle(20).extrude(50)"


class Backend(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.b = CadQueryBackend(timeout=20)
    @classmethod
    def tearDownClass(cls): cls.b.close()
    def setUp(self): self.b.reset()

    def test_measure_after_cell(self):
        r = self.b.execute(CYL)
        self.assertTrue(r.ok and r.changed); self.assertAlmostEqual(r.measure.volume, math.pi * 400 * 50, places=3)
        self.assertEqual((r.measure.faces, r.measure.solids, r.measure.valid), (3, 1, True)); self.assertAlmostEqual(r.measure.com[2], 25, places=6)

    def test_state_persists_and_error_does_not_corrupt(self):
        self.b.execute("w = cq.Workplane('XY').circle(20).extrude(50)\nresult = w")
        r = self.b.execute("result = w.faces('>Z').workplane().hole(10)\nraise ValueError('boom')")
        self.assertFalse(r.ok); self.assertIn("ValueError", r.error); self.assertIn("line 2", r.error)
        self.assertAlmostEqual(self.b.measure().volume, math.pi * 400 * 50, places=3, msg="a failed cell must not change the part")
        self.assertTrue(self.b.execute("result = w.faces('>Z').workplane().hole(10)").ok)
        self.assertAlmostEqual(self.b.measure().volume, math.pi * (400 * 50 - 25 * 50), places=3)

    def test_undo_replays_history(self):
        self.b.execute(CYL); self.b.execute("result = result.faces('>Z').workplane().hole(10)")
        self.assertTrue(self.b.undo()); self.assertAlmostEqual(self.b.measure().volume, math.pi * 400 * 50, places=3)
        self.assertTrue(self.b.undo()); self.assertIsNone(self.b.measure()); self.assertFalse(self.b.undo())

    def test_blocked_code(self):
        for bad in ("import os", "import subprocess", "open('/etc/passwd')", "__import__('os')", "().__class__.__bases__", "eval('1')", "import sys", "x = getattr(cq, 'Workplane')",
                    "from os import path", "exec('1')"):
            r = self.b.execute(bad); self.assertFalse(r.ok, bad)
        self.assertTrue(self.b.execute("import math\nimport cadquery as c2\nx = math.pi").ok)

    def test_timeout_restores_session(self):
        b = CadQueryBackend(timeout=4)
        try:
            b.execute(CYL); r = b.execute("while True:\n    pass")
            self.assertFalse(r.ok); self.assertIn("timed out", r.error)
            self.assertAlmostEqual(b.measure().volume, math.pi * 400 * 50, places=3)
        finally: b.close()

    def test_crash_restores_session(self):
        b = CadQueryBackend(timeout=20)
        try:
            b.execute(CYL); b.proc.kill(); b.proc.wait()
            r = b.execute("result = result.faces('>Z').workplane().hole(10)")
            self.assertTrue(r.ok or "restored" in r.error)
            if not r.ok: self.assertTrue(b.execute("result = result.faces('>Z').workplane().hole(10)").ok)
            self.assertAlmostEqual(b.measure().volume, math.pi * (400 * 50 - 25 * 50), places=3)
        finally: b.close()

    def test_bare_workplane_name_works(self):
        self.assertTrue(self.b.execute("result = Workplane('XY').box(1,1,1)").ok)

    def test_step_export(self):
        import tempfile
        self.b.execute(CYL); p = os.path.join(tempfile.mkdtemp(), "a.step"); self.b.export_step(p)
        self.assertGreater(os.path.getsize(p), 500)

    def test_reference_solutions_match_closed_forms(self):
        for t, d in tasks.TASKS.items():
            e = tasks.exact(self.b, t)
            if d["analytic"] is not None: self.assertAlmostEqual(e / d["analytic"], 1, places=9, msg=t)
        self.assertAlmostEqual(tasks.exact(self.b, "cup_side_hole") / 19921.7514, 1, delta=5e-6)   # the SolidWorks kernel's number


class FakeLLM:
    """Scripted executor: each entry is a list of (tool, args) or a final string."""
    def __init__(self, script): self.script, self.i, self.seen = script, 0, []
    def chat(self, messages, tools=None, think=False, **kw):
        self.seen.append(messages[-1]["content"] if messages else "")
        s = self.script[min(self.i, len(self.script) - 1)]; self.i += 1
        if isinstance(s, str): return {"role": "assistant", "content": s, "_usage": {"completion_tokens": 1}}
        return {"role": "assistant", "content": "", "tool_calls": [{"id": "c%d" % self.i, "type": "function", "function": {"name": n, "arguments": json.dumps(a)}} for n, a in s],
                "_usage": {"completion_tokens": 1}}


PLAN = {"steps": [{"do": "cylinder", "volume_after": "pi*20**2*50"}, {"do": "hole", "volume_after": "pi*20**2*50 - pi*5**2*50"}], "final_bbox_size": ["40", "40", "50"]}
PLANNER = lambda r: (V.parse_spec(PLAN), "raw")
HOLE = "result = result.faces('>Z').workplane().hole(10)"


class Agent(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.b = CadQueryBackend(timeout=20)
    @classmethod
    def tearDownClass(cls): cls.b.close()

    def run_script(self, script, **kw):
        llm = FakeLLM(script); r = agent.run(llm, self.b, "req", planner=PLANNER, log=lambda s: None, **kw); return r, llm

    def test_good_run_has_no_warning(self):
        r, _ = self.run_script([[("execute_cadquery", {"code": CYL})], [("execute_cadquery", {"code": HOLE})], "done"])
        self.assertEqual((r["warnings"], r["gates"], r["steps"]), (0, 0, 2)); self.assertAlmostEqual(r["measure"]["volume"], math.pi * (400 - 25) * 50, places=3)

    def test_wrong_volume_is_flagged_in_the_tool_result(self):
        r, llm = self.run_script([[("execute_cadquery", {"code": CYL})], [("execute_cadquery", {"code": "result = result.faces('>Z').workplane().hole(14)"})], "done"])
        self.assertGreaterEqual(r["warnings"], 1)
        self.assertTrue(any("HARNESS CHECK" in s and "Less material than planned" in s for s in llm.seen))

    def test_gate_sends_the_model_back_once_then_accepts(self):
        r, llm = self.run_script([[("execute_cadquery", {"code": CYL})], "done too early", [("execute_cadquery", {"code": HOLE})], "done"])
        self.assertEqual(r["gates"], 1); self.assertAlmostEqual(r["measure"]["volume"], math.pi * (400 - 25) * 50, places=3)
        self.assertTrue(any("FINAL CHECK" in s for s in llm.seen))

    def test_gate_is_bounded(self):
        r, _ = self.run_script([[("execute_cadquery", {"code": CYL})], "x"] * 6)
        self.assertEqual(r["gates"], agent.GATE_MAX); self.assertEqual(r["final"], "x")

    def test_plan_off_means_no_checks(self):
        r, llm = self.run_script([[("execute_cadquery", {"code": CYL})], "done"], plan_mode="off")
        self.assertEqual((r["checks"], r["gates"], r["plan"]), (0, 0, None))

    def test_no_change_warning_for_feature_cell(self):
        r, llm = self.run_script([[("execute_cadquery", {"code": CYL})], [("execute_cadquery", {"code": "c = cq.Workplane('XY').box(1,1,1).cut(cq.Workplane('XY').box(1,1,1))"})],
                                  "done"], plan_mode="think")
        self.assertTrue(any("did not change the volume" in s for s in llm.seen))

    def test_undo_tool(self):
        r, _ = self.run_script([[("execute_cadquery", {"code": CYL})], [("execute_cadquery", {"code": HOLE})], [("undo", {})], [("execute_cadquery", {"code": HOLE})], "done"])
        self.assertAlmostEqual(r["measure"]["volume"], math.pi * (400 - 25) * 50, places=3); self.assertEqual(r["gates"], 0)




class Planners(unittest.TestCase):
    def test_extract_json(self):
        self.assertEqual(planners.extract_json('noise {"a": 1} tail'), {"a": 1}); self.assertIsNone(planners.extract_json("no json")); self.assertIsNone(planners.extract_json("{bad"))

    def test_codex_failure_falls_back_and_says_so(self):
        class L:
            cfg = {"relay": {}, "codex_bin": None}
            def chat(self, messages, **kw): return {"role": "assistant", "content": json.dumps(PLAN)}
        old = planners.find_codex; planners.find_codex = lambda cfg: None
        try:
            spec, raw = planners.make_plan(L(), "req", "codex")
        finally: planners.find_codex = old
        self.assertTrue(raw.startswith("(codex failed")); self.assertEqual(len(spec["milestones"]), 2)

    def test_relay_needs_an_empty_workdir(self):
        t, e = planners.ask_relay("q", {"relay": {"host": "u@h"}}); self.assertEqual(t, ""); self.assertIn("EMPTY", e)


class WorkerEnv(unittest.TestCase):
    def test_keeps_what_windows_needs_and_drops_secrets(self):
        from cadfabric.cq_backend import worker_env
        e = worker_env({"PATH": "p", "USERPROFILE": "C:\\Users\\x", "APPDATA": "a", "SystemRoot": "C:\\Windows", "CADFABRIC_KEY": "k", "SSHPASS": "s", "AWS_SECRET_ACCESS_KEY": "z", "OPENAI_API_KEY": "o"})
        self.assertEqual(sorted(e), ["APPDATA", "PATH", "SystemRoot", "USERPROFILE"])          # Windows env names are case-insensitive: SystemRoot must survive


class Events(unittest.TestCase):
    def test_plan_event_carries_the_advisors_pitfalls(self):
        b = CadQueryBackend(timeout=20); ev = []
        try:
            raw = json.dumps(dict(PLAN, pitfalls=["wording could mean 40 overall", "use radii"]))
            agent.run(FakeLLM([[("execute_cadquery", {"code": CYL})], "ok"]), b, "req", planner=lambda r: (V.parse_spec(PLAN), raw), log=lambda s: None,
                      on_event=lambda kind, **kw: ev.append((kind, kw)))
        finally: b.close()
        plan = [kw for k, kw in ev if k == "plan"][0]
        self.assertTrue(plan["ok"]); self.assertEqual(plan["milestones"], 2); self.assertEqual(plan["pitfalls"][0], "wording could mean 40 overall")
        self.assertEqual([k for k, _ in ev if k == "cell"], ["cell"])
        self.assertEqual([kw["what"] for k, kw in ev if k == "phase"][:4], ["plan", "llm", "cad", "llm"])      # later llm phases = the final gate sending the model back


class Configure(unittest.TestCase):
    def test_configure_command_writes_config_and_key_separately(self):
        import io, tempfile
        from cadfabric import cli
        d = tempfile.mkdtemp(); os.environ["CADFABRIC_CONFIG"] = os.path.join(d, "c.json"); old = sys.stdin
        try:
            sys.stdin = io.StringIO("topsecret\n")
            self.assertEqual(cli.main(["configure", "--url", "http://h:1/v1/chat/completions", "--model", "m", "--planner", "off", "--key-stdin"]), 0)
            txt = open(os.path.join(d, "c.json")).read(); self.assertNotIn("topsecret", txt)
            c = config.load(); self.assertEqual((c["url"], c["model"], c["planner"]), ("http://h:1/v1/chat/completions", "m", "off")); self.assertEqual(config.read_key(c), "topsecret")
            self.assertEqual(cli.main(["configure", "--relay-host=-oBad"]), 2)
        finally:
            sys.stdin = old; os.environ.pop("CADFABRIC_CONFIG")


class Config(unittest.TestCase):
    def test_defaults_env_and_bad_planner(self):
        import tempfile
        d = tempfile.mkdtemp(); p = os.path.join(d, "c.json"); open(p, "w").write(json.dumps({"planner": "off", "relay": {"host": "x"}}))
        os.environ["CADFABRIC_CONFIG"] = p
        try:
            c = config.load(); self.assertEqual((c["planner"], c["relay"]["host"], c["relay"]["codex"]), ("off", "x", "codex"))
            os.environ["CADFABRIC_PLANNER"] = "think"; self.assertEqual(config.load()["planner"], "think")
            os.environ["CADFABRIC_PLANNER"] = "bogus"
            with self.assertRaises(SystemExit): config.load()
        finally:
            for k in ("CADFABRIC_CONFIG", "CADFABRIC_PLANNER"): os.environ.pop(k, None)

    def test_key_from_env_then_file(self):
        import tempfile
        f = tempfile.NamedTemporaryFile("w", delete=False); f.write(" sekret \n"); f.close()
        self.assertEqual(config.read_key({"key_file": f.name}), "sekret")
        os.environ["CADFABRIC_KEY"] = "fromenv"
        try: self.assertEqual(config.read_key({"key_file": f.name}), "fromenv")
        finally: os.environ.pop("CADFABRIC_KEY")
        self.assertEqual(config.read_key({}), "")


class Web(unittest.TestCase):
    """The web chat with a scripted LLM, a real CadQuery backend, over real HTTP."""
    @classmethod
    def setUpClass(cls):
        import threading, http.client
        from cadfabric import web
        cls.web, cls.http = web, http.client
        cls.llm = FakeLLM([[("execute_cadquery", {"code": CYL})], "xong"])
        cls.srv = web.serve(cls.llm, {"planner": "off", "relay": {}}, port=0, token="tok123", backend=CadQueryBackend(timeout=20), quiet=True)
        cls.port = cls.srv.port; cls.srv.hosts.add("127.0.0.1:%d" % cls.port); cls.srv.hosts.add("localhost:%d" % cls.port)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
    @classmethod
    def tearDownClass(cls): cls.srv.shutdown(); cls.srv.sess.close()

    def req(self, method, path, body=None, headers=None, host=None):
        c = self.http.HTTPConnection("127.0.0.1", self.port, timeout=20)
        h = {"Host": host or "127.0.0.1:%d" % self.port}; h.update(headers or {})
        c.request(method, path, json.dumps(body) if body is not None else None, h); r = c.getresponse(); data = r.read(); return r.status, data, dict(r.getheaders())

    AUTH = {"Cookie": "cft=tok123", "X-Requested-With": "cadfabric", "Content-Type": "application/json"}

    def test_auth_host_csrf_and_size(self):
        self.assertEqual(self.req("GET", "/api/state")[0], 401)
        self.assertEqual(self.req("GET", "/api/state?t=wrong")[0], 401)
        self.assertEqual(self.req("GET", "/api/state", headers={"Cookie": "cft=tok123"}, host="evil.example:80")[0], 403)
        s, _, h = self.req("GET", "/?t=tok123"); self.assertEqual(s, 200); self.assertIn("cft=tok123", h.get("Set-Cookie", ""))
        self.assertEqual(self.req("POST", "/api/stop", {}, {"Cookie": "cft=tok123"})[0], 403)                       # no X-Requested-With
        self.assertEqual(self.req("POST", "/api/stop", {}, dict(self.AUTH, Origin="http://evil.example"))[0], 403)
        self.assertEqual(self.req("POST", "/api/send", {"text": "x" * 9000, "planner": "off"}, self.AUTH)[0], 400)
        self.assertEqual(self.req("POST", "/api/send", {"text": "a", "planner": "bogus"}, self.AUTH)[0], 400)

    def test_external_planner_needs_confirmation(self):
        s, d, _ = self.req("POST", "/api/send", {"text": "a", "planner": "codex"}, self.AUTH); self.assertEqual(s, 409)

    def test_chat_flow_preview_step_and_follow_up(self):
        self.assertEqual(self.req("POST", "/api/reset", {}, self.AUTH)[0], 200)
        self.assertEqual(self.req("POST", "/api/send", {"text": "tao tru", "planner": "off"}, self.AUTH)[0], 200)
        for _ in range(60):
            st = json.loads(self.req("GET", "/api/state?since=0", headers=self.AUTH)[1])
            if not st["busy"]: break
            time.sleep(0.5)
        self.assertIn("final", [e["kind"] for e in st["events"]]); self.assertAlmostEqual(st["measure"]["volume"], math.pi * 400 * 50, places=3); self.assertTrue(st["follow_up"])
        cell = [e for e in st["events"] if e["kind"] == "cell"][0]
        self.assertEqual((cell["n"], cell["ok"], cell["code"]), (1, True, CYL)); self.assertAlmostEqual(cell["volume"], math.pi * 400 * 50, places=3)
        s, svg, h = self.req("GET", "/api/part.svg", headers=self.AUTH); self.assertEqual(s, 200); self.assertIn(b"<svg", svg)
        self.assertIn(b'viewBox="0 0 640 440"', svg[:300])                                    # without a viewBox the drawing is cropped when scaled
        s, step, _ = self.req("GET", "/api/part.step", headers=self.AUTH); self.assertEqual(s, 200); self.assertGreater(len(step), 500)
        self.llm.script, self.llm.i = [[("execute_cadquery", {"code": HOLE})], "da khoan"], 0                             # follow-up edits the same part
        self.assertEqual(self.req("POST", "/api/send", {"text": "khoan lo 10", "planner": "off"}, self.AUTH)[0], 200)
        for _ in range(60):
            st = json.loads(self.req("GET", "/api/state?since=0", headers=self.AUTH)[1])
            if not st["busy"]: break
            time.sleep(0.5)
        self.assertAlmostEqual(st["measure"]["volume"], math.pi * (400 - 25) * 50, places=3)
        self.assertEqual(self.req("POST", "/api/reset", {}, self.AUTH)[0], 200); self.assertIsNone(json.loads(self.req("GET", "/api/state", headers=self.AUTH)[1])["measure"])


class Settings(unittest.TestCase):
    """Settings dialog API: validation, key handling, remote refusal, connection test against a fake OpenAI server."""
    @classmethod
    def setUpClass(cls):
        import tempfile, threading, http.client, http.server
        from cadfabric import web
        cls.http = http.client
        cls.dir = tempfile.mkdtemp(); os.environ["CADFABRIC_CONFIG"] = os.path.join(cls.dir, "config.json")
        class Fake(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def _j(self, code, o): b = json.dumps(o).encode(); self.send_response(code); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
            def do_GET(self):
                self._j(200, {"data": [{"id": "qwen-test"}]}) if self.headers.get("Authorization") == "Bearer good" else self._j(401, {})
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0)); self._j(200, {"choices": [{"message": {"content": "OK"}}]})
        cls.fake = http.server.HTTPServer(("127.0.0.1", 0), Fake); threading.Thread(target=cls.fake.serve_forever, daemon=True).start()
        cls.fake_url = "http://127.0.0.1:%d/v1/chat/completions" % cls.fake.server_address[1]
        cls.web = web
        cls.srv = web.serve(FakeLLM(["x"]), config.load(), port=0, token="tok", backend=CadQueryBackend(timeout=20), quiet=True)
        cls.port = cls.srv.port; cls.srv.hosts.add("127.0.0.1:%d" % cls.port)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.sess.close(); cls.fake.shutdown(); os.environ.pop("CADFABRIC_CONFIG", None)

    AUTH = {"Cookie": "cft=tok", "X-Requested-With": "cadfabric", "Content-Type": "application/json"}
    def req(self, method, path, body=None, headers=None):
        c = self.http.HTTPConnection("127.0.0.1", self.port, timeout=30)
        c.request(method, path, json.dumps(body) if body is not None else None, headers or self.AUTH); r = c.getresponse(); return r.status, json.loads(r.read() or b"{}")

    def test_save_hot_reloads_and_never_writes_the_key_into_the_json(self):
        body = {"url": self.fake_url, "model": "qwen-test", "key": "good", "planner": "nothink", "relay": {"host": "", "workdir": "", "codex": "codex"}}
        s, d = self.req("POST", "/api/settings", body); self.assertEqual(s, 200, d)
        cfgtxt = open(os.path.join(self.dir, "config.json")).read(); self.assertNotIn("good", cfgtxt); self.assertIn("qwen-test", cfgtxt)
        kp = os.path.join(self.dir, "key.txt"); self.assertEqual(open(kp).read().strip(), "good")
        if os.name != "nt": self.assertEqual(os.stat(kp).st_mode & 0o777, 0o600)
        self.assertEqual(self.srv.sess.llm.model, "qwen-test"); self.assertEqual(self.srv.sess.llm.key, "good")
        s, d = self.req("GET", "/api/config"); self.assertTrue(d["settings"]["has_key"]); self.assertNotIn("good", json.dumps(d))
        s, d = self.req("POST", "/api/settings", dict(body, key=""))                                    # empty key keeps the saved one
        self.assertEqual(self.srv.sess.llm.key, "good")

    def test_rejects_values_that_could_reach_a_command_line(self):
        base = {"url": self.fake_url, "model": "m", "planner": "codex-relay"}
        for rel in ({"host": "-oProxyCommand=evil"}, {"host": "u@h", "workdir": 'C:\\w" & calc'}, {"host": "u@h", "workdir": "ok", "codex": "codex; rm -rf /"}):
            self.assertEqual(self.req("POST", "/api/settings", dict(base, relay=rel))[0], 400, rel)
        self.assertEqual(self.req("POST", "/api/settings", dict(base, url="file:///etc/passwd", relay={}))[0], 400)
        self.assertEqual(self.req("POST", "/api/settings", dict(base, planner="bogus", relay={}))[0], 400)
        self.assertEqual(self.req("POST", "/api/settings", dict(base, planner="off"), {"Cookie": "cft=tok"})[0], 403)    # CSRF header missing

    def test_connection_test(self):
        s, d = self.req("POST", "/api/test", {"url": self.fake_url, "model": "qwen-test", "key": "good", "planner": "off"})
        self.assertEqual(s, 200); self.assertTrue(d["ok"]); self.assertEqual(d["models"], ["qwen-test"])
        s, d = self.req("POST", "/api/test", {"url": self.fake_url, "model": "", "key": "bad", "planner": "off"})
        self.assertFalse(d["ok"]); self.assertIn("rejected", d["detail"])
        s, d = self.req("POST", "/api/test", {"url": "http://127.0.0.1:1/v1/chat/completions", "model": "", "key": "", "planner": "off"})
        self.assertFalse(d["ok"]); self.assertIn("cannot reach", d["detail"])

    def test_slow_server_gets_a_readable_message(self):
        import socket, threading
        from cadfabric import llm
        srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(5); port = srv.getsockname()[1]
        def sink():                                                                   # accepts and never answers
            while True:
                try: c, _ = srv.accept()
                except OSError: return
        threading.Thread(target=sink, daemon=True).start()
        try:
            r = llm.probe("http://127.0.0.1:%d/v1/chat/completions" % port, "m", "", timeout=2)
        finally: srv.close()
        self.assertFalse(r["ok"]); self.assertTrue("cannot reach" in r["detail"] or "no answer within" in r["detail"], r)

    def test_remote_mode_cannot_change_settings_or_quit(self):
        self.srv.sess.local = False
        try:
            for path in ("/api/settings", "/api/test", "/api/quit"):
                self.assertEqual(self.req("POST", path, {"url": self.fake_url, "model": "", "planner": "off"})[0], 403, path)
        finally: self.srv.sess.local = True

    def test_quit_sets_the_flag(self):
        self.assertEqual(self.req("POST", "/api/quit", {})[0], 200); self.assertTrue(self.srv.sess.quit); self.srv.sess.quit = False


if __name__ == "__main__":
    unittest.main(verbosity=1)
