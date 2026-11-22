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


class WorkerProtocol(unittest.TestCase):
    def test_c_level_stdout_noise_never_reaches_the_protocol(self):
        import subprocess
        from cadfabric import cq_backend
        env = dict(cq_backend.worker_env(), CADFABRIC_TEST_NOISE="1")
        p = subprocess.Popen([sys.executable, "-I", os.path.join(os.path.dirname(cq_backend.__file__), "worker.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env)
        try:
            p.stdin.write('{"op": "noise"}\n{"op": "ping"}\n'); p.stdin.flush()
            self.assertEqual(json.loads(p.stdout.readline()), {"ok": True}); self.assertEqual(json.loads(p.stdout.readline()), {"ok": True})
        finally: p.kill(); p.wait()


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
            for preset in ("codex", "claude"):                                                                     # "codex" is a built-in id: the CLI advisor must not collide with it
                self.assertEqual(cli.main(["configure", "--advisor-cli", preset, "--planner", preset + "-cli"]), 0)
            c = config.load(); self.assertEqual([a["id"] for a in c["advisors"]], ["codex-cli", "claude-cli"]); self.assertEqual(c["planner"], "claude-cli")
            self.assertEqual(cli.main(["configure", "--advisor-cli", "claude"]), 0); self.assertEqual(len(config.load()["advisors"]), 2)   # idempotent
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
        import tempfile; cls.ddir = tempfile.mkdtemp()
        cls.srv = web.serve(cls.llm, {"planner": "off", "relay": {}, "data_dir": cls.ddir}, port=0, token="tok123", backend=CadQueryBackend(timeout=20), quiet=True)
        cls.port = cls.srv.port; cls.srv.hosts.add("127.0.0.1:%d" % cls.port); cls.srv.hosts.add("localhost:%d" % cls.port)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
    @classmethod
    def tearDownClass(cls): cls.srv.shutdown(); cls.srv.sess.close()

    def req(self, method, path, body=None, headers=None, host=None):
        c = self.http.HTTPConnection("127.0.0.1", self.port, timeout=20)
        h = {"Host": host or "127.0.0.1:%d" % self.port}; h.update(headers or {})
        c.request(method, path, json.dumps(body) if body is not None else None, h); r = c.getresponse(); data = r.read(); return r.status, data, dict(r.getheaders())

    AUTH = {"Cookie": "cft=tok123", "X-Requested-With": "cadfabric", "Content-Type": "application/json"}

    def test_save_after_turn_reopen_and_refusals(self):
        H = self.web.history_mod; sess = self.srv.sess
        self.assertEqual(self.req("POST", "/api/history/clear", {}, self.AUTH)[0], 200); self.assertEqual(self.req("POST", "/api/reset", {}, self.AUTH)[0], 200)
        self.assertEqual(json.loads(self.req("GET", "/api/history", None, self.AUTH)[1])["sessions"], [])
        self.llm.script, self.llm.i = [[("execute_cadquery", {"code": CYL})], "xong"], 0
        self.assertEqual(self.req("POST", "/api/send", {"text": "Tạo trụ để thử lịch sử", "planner": "off"}, self.AUTH)[0], 200)
        for _ in range(100):
            if not sess.busy and sess.measure: break
            time.sleep(0.2)
        self.assertTrue(sess.measure, "the scripted build did not finish")
        d = json.loads(self.req("GET", "/api/history", None, self.AUTH)[1]); self.assertEqual(len(d["sessions"]), 1)
        row = d["sessions"][0]; self.assertEqual(row["title"], "Tạo trụ để thử lịch sử"); self.assertAlmostEqual(row["volume"], sess.measure["volume"], 3); vol = sess.measure["volume"]
        self.req("POST", "/api/reset", {}, self.AUTH); self.assertIsNone(sess.measure)
        s, b, _ = self.req("POST", "/api/history/load", {"id": row["id"]}, self.AUTH); self.assertEqual(s, 200, b)
        self.assertAlmostEqual(sess.measure["volume"], vol, 3)                                                  # the part was rebuilt by replaying the saved cells
        st = json.loads(self.req("GET", "/api/state?since=0", None, self.AUTH)[1]); self.assertTrue(st["has_part"]); self.assertTrue(st["follow_up"])
        self.assertIn("Tạo trụ để thử lịch sử", json.dumps(st["events"], ensure_ascii=False))
        self.assertEqual(self.req("POST", "/api/history/load", {"id": "../../etc/passwd"}, self.AUTH)[0], 400)
        self.assertEqual(self.req("POST", "/api/history/load", {"id": "20200101-000000-aaaa"}, self.AUTH)[0], 404)
        self.assertEqual(self.req("POST", "/api/history/load", {"id": row["id"]}, {"Cookie": "cft=tok123"})[0], 403)  # CSRF header missing
        sess.local = False
        try:
            self.assertEqual(self.req("POST", "/api/history/load", {"id": row["id"]}, self.AUTH)[0], 403)
            self.assertEqual(json.loads(self.req("GET", "/api/history", None, self.AUTH)[1])["sessions"], [])
        finally: sess.local = True
        self.assertEqual(self.req("POST", "/api/history/delete", {"id": row["id"]}, self.AUTH)[0], 200)
        self.assertEqual(json.loads(self.req("GET", "/api/history", None, self.AUTH)[1])["sessions"], [])

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
        cls.dir = tempfile.mkdtemp(); os.environ["CADFABRIC_CONFIG"] = os.path.join(cls.dir, "config.json"); os.environ["XDG_DATA_HOME"] = cls.dir
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
        cls.srv.shutdown(); cls.srv.sess.close(); cls.fake.shutdown(); os.environ.pop("CADFABRIC_CONFIG", None); os.environ.pop("XDG_DATA_HOME", None)

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


def _recording_server(handler_fn):
    """A tiny HTTP server: handler_fn(path, headers, body_dict) -> (status, reply_dict). Returns (server, url, seen list)."""
    import http.server, threading
    seen = []
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            seen.append({"path": self.path, "h": {k.lower(): v for k, v in self.headers.items()}, "body": body})
            code, rep = handler_fn(self.path, self.headers, body); b = json.dumps(rep).encode()
            self.send_response(code); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    srv = http.server.HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1], seen


FAKE_CLI = """import os, sys
assert os.listdir('.') == [], 'cwd must be empty: %r' % os.listdir('.')
text = sys.stdin.read() if '--arg' not in sys.argv else sys.argv[-1]
print('STDIN-MODE' if '--arg' not in sys.argv else 'ARG-MODE', len(text), 'ADVISOR PREFIX OK' if text.startswith('You are an advisor') else 'NO PREFIX')
"""


class Advisors(unittest.TestCase):
    def test_validation(self):
        from cadfabric import advisors as A
        ok = lambda a: A.validate(a)[1] == ""
        self.assertTrue(ok({"id": "mine", "type": "cli", "preset": "claude"}))
        self.assertTrue(ok({"id": "q2", "type": "api", "kind": "openai", "url": "http://10.0.0.5:1/v1/chat/completions"}))
        for bad in ({"id": "codex", "type": "cli", "preset": "codex"}, {"id": "Bad Id", "type": "cli", "preset": "claude"}, {"id": "x", "type": "wat"},
                    {"id": "x", "type": "cli", "preset": "custom", "command": "--evil"}, {"id": "x", "type": "cli", "preset": "custom", "command": ""},
                    {"id": "x", "type": "cli", "preset": "custom", "command": "a\nb"}, {"id": "x", "type": "api", "kind": "openai", "url": "file:///etc/passwd"},
                    {"id": "x", "type": "api", "kind": "anthropic", "url": "https://h/v1/messages", "model": ""}, {"id": "x", "type": "api", "kind": "bogus", "url": "http://h/"},
                    {"id": "x", "type": "cli", "preset": "nope"}):
            self.assertFalse(ok(bad), bad)
        self.assertIn("duplicate", A.validate_list([{"id": "a", "type": "cli", "preset": "claude"}, {"id": "a", "type": "cli", "preset": "claude"}])[1])
        self.assertTrue(A.validate({"id": "l", "type": "api", "kind": "openai", "url": "http://192.168.1.5:1/v1"})[0]["external"] is False)    # private LAN: not external
        self.assertTrue(A.validate({"id": "p", "type": "api", "kind": "openai", "url": "https://api.example.com/v1"})[0]["external"])          # public host: external

    def test_cli_model_and_effort(self):
        import tempfile
        from cadfabric import advisors as A
        d = tempfile.mkdtemp(); fake = os.path.join(d, "claude"); open(fake, "w").write("#!/bin/sh\ncat >/dev/null; echo \"$@\"\n"); os.chmod(fake, 0o755)
        old = os.environ["PATH"]; os.environ["PATH"] = d + os.pathsep + old
        try:
            t, e = A.ask({"type": "cli", "preset": "claude", "model": "haiku", "effort": "low"}, "q"); self.assertEqual(e, "")
            self.assertIn("--model haiku", t); self.assertIn("--effort low", t); self.assertTrue(t.index("--output-format") < t.index("--model"))
            t, e = A.ask({"type": "cli", "preset": "claude"}, "q"); self.assertNotIn("--model", t); self.assertNotIn("--effort", t)      # nothing added when unset
        finally: os.environ["PATH"] = old
        argv, _ = A.cli_argv({"preset": "codex", "model": "m1", "effort": "high"}, {"codex_bin": sys.executable}, "/w") if A.find_exe("codex", {"codex_bin": sys.executable}) else (None, 0)
        if argv: self.assertEqual(argv[-1], "-"); self.assertEqual(argv[argv.index("-m") + 1], "m1"); self.assertIn('model_reasoning_effort="high"', argv)   # prompt marker stays last
        ok = lambda a: A.validate(dict({"id": "x", "type": "cli"}, **a))[1] == ""
        self.assertTrue(ok({"preset": "claude", "model": "claude-sonnet-4-5", "effort": "max"})); self.assertTrue(ok({"preset": "codex", "model": "gpt-5.1", "effort": "high"}))
        for bad in ({"preset": "claude", "model": "-rm"}, {"preset": "claude", "model": "a b"}, {"preset": "claude", "model": 'x"y'}, {"preset": "claude", "effort": "ultra"}, {"preset": "codex", "effort": "max"}):
            self.assertFalse(ok(bad), bad)
        self.assertIn("haiku", A.presets_info()["claude"]["models"])
        self.assertEqual(A.presets_info()["claude"]["efforts"], ["low", "medium", "high", "xhigh", "max"])

    def test_api_openai_and_anthropic_shapes_and_keys(self):
        from cadfabric import advisors as A
        def fn(path, h, body):
            if path == "/bad": return 401, {"error": "nope"}
            if path == "/anth": return 200, {"content": [{"type": "thinking", "thinking": "x"}, {"type": "text", "text": "ANTHROPIC-ANSWER"}]}
            return 200, {"choices": [{"message": {"content": "OPENAI-ANSWER"}}]}
        srv, base, seen = _recording_server(fn)
        try:
            t, e = A.ask({"type": "api", "kind": "openai", "url": base + "/v1/chat/completions", "model": "m1"}, "question", key="K1")
            self.assertEqual((t, e), ("OPENAI-ANSWER", "")); self.assertEqual(seen[-1]["h"]["authorization"], "Bearer K1"); self.assertEqual(seen[-1]["body"]["model"], "m1")
            self.assertTrue(seen[-1]["body"]["messages"][0]["content"].startswith("You are an advisor")); self.assertTrue(seen[-1]["body"]["messages"][0]["content"].endswith("question"))
            t, e = A.ask({"type": "api", "kind": "anthropic", "url": base + "/anth", "model": "claude-x"}, "q", key="K2")
            self.assertEqual(t, "ANTHROPIC-ANSWER"); h = seen[-1]["h"]
            self.assertEqual((h["x-api-key"], h["anthropic-version"], h["user-agent"]), ("K2", "2023-06-01", "cadfabric")); self.assertNotIn("authorization", h)
            self.assertGreaterEqual(seen[-1]["body"]["max_tokens"], 4000)                                 # thinking models need room
            t, e = A.ask({"type": "api", "kind": "openai", "url": base + "/bad"}, "q", key="K"); self.assertEqual(t, ""); self.assertIn("HTTP 401", e)
            t, e = A.ask({"type": "api", "kind": "openai", "url": "http://127.0.0.1:1/x"}, "q"); self.assertEqual(t, ""); self.assertIn("cannot reach", e)
        finally: srv.shutdown()

    def test_custom_cli_runs_in_an_empty_dir_with_stdin_or_arg(self):
        import tempfile
        from cadfabric import advisors as A
        d = tempfile.mkdtemp(); sc = os.path.join(d, "fake.py"); open(sc, "w").write(FAKE_CLI)
        t, e = A.ask({"type": "cli", "preset": "custom", "command": '"%s" "%s"' % (sys.executable, sc)}, "hello")
        self.assertEqual(e, ""); self.assertIn("STDIN-MODE", t); self.assertIn("ADVISOR PREFIX OK", t)
        t, e = A.ask({"type": "cli", "preset": "custom", "command": '"%s" "%s" --arg {prompt}' % (sys.executable, sc)}, "hello there")
        self.assertEqual(e, ""); self.assertIn("ARG-MODE", t); self.assertIn("ADVISOR PREFIX OK", t)
        t, e = A.ask({"type": "cli", "preset": "custom", "command": "definitely-not-a-program-xyz"}, "q"); self.assertEqual(t, ""); self.assertIn("not found", e)
        t, e = A.ask({"type": "cli", "preset": "custom", "command": '"%s" -c "import sys; sys.exit(3)"' % sys.executable}, "q"); self.assertEqual(t, ""); self.assertIn("exit 3", e)
        t, e = A.ask({"type": "cli", "preset": "custom", "command": '"%s" -c "print(\'Not logged in\'); raise SystemExit(1)"' % sys.executable}, "q")
        self.assertEqual(t, ""); self.assertIn("Not logged in", e)                                              # the reason printed on stdout is not lost

    def test_preset_argv_is_text_only(self):
        import tempfile, stat
        from cadfabric import advisors as A
        d = tempfile.mkdtemp(); fake = os.path.join(d, "claude"); open(fake, "w").write("#!/bin/sh\ncat >/dev/null; echo hi\n"); os.chmod(fake, 0o755)
        old = os.environ["PATH"]; os.environ["PATH"] = d + os.pathsep + old
        try:
            argv, in_args = A.cli_argv({"preset": "claude"}, {}, "/w"); self.assertFalse(in_args)
            self.assertEqual(argv[1:4], ["-p", "--tools", ""]); self.assertIn("--no-session-persistence", argv)                  # no tools: text only
            t, e = A.ask({"type": "cli", "preset": "claude"}, "q"); self.assertEqual((t, e), ("hi", ""))
        finally: os.environ["PATH"] = old
        argv, _ = A.cli_argv({"preset": "codex"}, {"codex_bin": sys.executable}, "/w") if A.find_exe("codex", {"codex_bin": sys.executable}) else (["x", "-C", "/w", "-s", "read-only"], 0)
        self.assertIn("read-only", argv); self.assertEqual(argv[argv.index("-C") + 1], "/w")

    def test_plan_through_a_custom_advisor_and_fallback_note(self):
        import tempfile
        from cadfabric import advisors as A
        d = tempfile.mkdtemp(); sc = os.path.join(d, "plan.py"); open(sc, "w").write("import sys, json\nsys.stdin.read()\nprint(json.dumps(%r))\n" % (PLAN,))
        cfg = {"planner": "mine", "relay": {}, "advisors": [{"id": "mine", "label": "M", "type": "cli", "preset": "custom", "command": '"%s" "%s"' % (sys.executable, sc), "external": True}]}
        class NoThink:
            def chat(self, *a, **k): return {"role": "assistant", "content": json.dumps(PLAN)}
        spec, raw = planners.make_plan(NoThink(), "req", "mine", cfg); self.assertEqual(len(spec["milestones"]), 2); self.assertFalse(raw.startswith("("))
        cfg["advisors"][0]["command"] = "definitely-not-a-program-xyz"
        spec, raw = planners.make_plan(NoThink(), "req", "mine", cfg); self.assertTrue(raw.startswith("(mine failed")); self.assertTrue(spec)           # fell back to nothink, said so


class AdvisorWeb(Settings):
    """Advisor settings through the real HTTP API (reuses the Settings fixture: temp config dir, fake model server)."""
    def test_roundtrip_keys_external_flag_and_removal(self):
        srv, base, seen = _recording_server(lambda p, h, b: (200, {"choices": [{"message": {"content": "OK"}}]}))
        try:
            adv = [{"id": "cloudy", "label": "Cloudy", "type": "api", "kind": "openai", "url": "https://api.example.com/v1/chat/completions", "model": "m"},
                   {"id": "lan", "label": "Lan", "type": "api", "kind": "openai", "url": base + "/v1/chat/completions", "model": "m"},
                   {"id": "cc", "label": "Claude", "type": "cli", "preset": "claude"}]
            body = {"url": self.fake_url, "model": "qwen-test", "planner": "nothink", "advisors": adv, "advisor_keys": {"cloudy": "SECRET-CLOUDY"}}
            s, d = self.req("POST", "/api/settings", body); self.assertEqual(s, 200, d)
            txt = open(os.path.join(self.dir, "config.json")).read(); self.assertNotIn("SECRET-CLOUDY", txt)
            kp = os.path.join(self.dir, "key-cloudy.txt"); self.assertEqual(open(kp).read().strip(), "SECRET-CLOUDY")
            if os.name != "nt": self.assertEqual(os.stat(kp).st_mode & 0o777, 0o600)
            s, d = self.req("GET", "/api/config"); m = {a["id"]: a for a in d["advisors"]}
            self.assertTrue(m["cloudy"]["external"]); self.assertFalse(m["lan"]["external"]); self.assertTrue(m["cc"]["external"]); self.assertTrue(m["cloudy"]["has_key"]); self.assertFalse(m["lan"]["has_key"])
            self.assertNotIn("SECRET-CLOUDY", json.dumps(d)); self.assertIn("cloudy", d["external"]); self.assertNotIn("lan", d["external"]); self.assertIn("lan", d["planners"])
            self.assertEqual(self.req("POST", "/api/send", {"text": "a", "planner": "cloudy"})[0], 409)             # external: needs confirm_external
            self.assertEqual(self.req("POST", "/api/send", {"text": "a", "planner": "ghost"})[0], 400)
            self.assertEqual(self.req("POST", "/api/settings", dict(body, planner="lan"))[0], 200)                  # a custom advisor can be the default
            # keep the saved key when the form leaves the key empty; removing the advisor deletes its key file
            self.assertEqual(self.req("POST", "/api/settings", dict(body, planner="nothink", advisor_keys={}))[0], 200); self.assertTrue(os.path.isfile(kp))
            self.assertEqual(self.req("POST", "/api/settings", dict(body, planner="nothink", advisors=adv[1:], advisor_keys={}))[0], 200); self.assertFalse(os.path.exists(kp))
        finally: srv.shutdown()

    def test_browser_cannot_choose_which_file_is_read_as_a_key(self):
        import tempfile
        secret = os.path.join(tempfile.mkdtemp(), "secret.txt"); open(secret, "w").write("TOP-SECRET-FILE")
        srv, base, seen = _recording_server(lambda p, h, b: (200, {"choices": [{"message": {"content": "OK"}}]}))
        try:
            a = {"id": "evil", "label": "E", "type": "api", "kind": "openai", "url": base + "/v1/chat/completions", "model": "m", "key_file": secret}
            s, d = self.req("POST", "/api/advisor_test", {"advisor": a}); self.assertEqual(s, 200); self.assertTrue(d["ok"])
            self.assertNotIn("authorization", seen[-1]["h"]); self.assertNotIn("TOP-SECRET", json.dumps(seen))
            self.assertEqual(self.req("POST", "/api/settings", {"url": self.fake_url, "model": "q", "planner": "nothink", "advisors": [a]})[0], 200)
            self.assertNotIn(secret, open(os.path.join(self.dir, "config.json")).read())
            s, d = self.req("POST", "/api/advisor_test", {"advisor": {"id": "evil", "type": "api", "kind": "openai", "url": base + "/x", "model": "m"}}); self.assertTrue(d["ok"])
            self.assertNotIn("authorization", seen[-1]["h"])
        finally: srv.shutdown()

    def test_advisor_test_endpoint_with_typed_key_and_remote_refusal(self):
        srv, base, seen = _recording_server(lambda p, h, b: (200, {"content": [{"type": "text", "text": "OK"}]}))
        try:
            a = {"id": "an", "label": "A", "type": "api", "kind": "anthropic", "url": base + "/v1/messages", "model": "claude-x"}
            s, d = self.req("POST", "/api/advisor_test", {"advisor": a, "key": "TYPED"}); self.assertTrue(d["ok"]); self.assertEqual(seen[-1]["h"]["x-api-key"], "TYPED")
            self.assertEqual(self.req("POST", "/api/advisor_test", {"advisor": {"id": "x", "type": "wat"}})[0], 400)
            self.srv.sess.local = False
            try: self.assertEqual(self.req("POST", "/api/advisor_test", {"advisor": a})[0], 403)
            finally: self.srv.sess.local = True
        finally: srv.shutdown()


MARK_VIEWER = """import sys
open(sys.argv[1], 'w').write(open(sys.argv[2], 'rb').read()[:20].decode('latin1'))
"""


class Viewers(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.d = tempfile.mkdtemp(); self.script = os.path.join(self.d, "viewer.py"); open(self.script, "w").write(MARK_VIEWER)
        self.mark = os.path.join(self.d, "mark.txt"); self.step = os.path.join(self.d, "p.step"); open(self.step, "w").write("ISO-10303-21;\nHEADER;")
        self.cfg = {"viewer": {"kind": "custom", "command": '"%s" "%s" "%s" {file}' % (sys.executable, self.script, self.mark)}}

    def wait_mark(self):
        for _ in range(50):
            if os.path.exists(self.mark) and os.path.getsize(self.mark): return open(self.mark).read()
            time.sleep(0.1)
        return ""

    def test_validate(self):
        from cadfabric import viewers as W
        self.assertEqual(W.validate({"kind": "solidworks"})[0], {"kind": "solidworks", "command": ""})
        self.assertEqual(W.validate({})[0]["kind"], "auto")
        for bad in ({"kind": "wat"}, {"kind": "custom"}, {"kind": "custom", "command": "--x"}, {"kind": "custom", "command": "a\nb"}):
            self.assertTrue(W.validate(bad)[1], bad)

    def test_custom_viewer_receives_the_step_file(self):
        from cadfabric import viewers as W
        r = W.open_file(self.step, self.cfg); self.assertTrue(r["ok"], r); self.assertIn("ISO-10303", self.wait_mark())
        r = W.open_file(self.step, {"viewer": {"kind": "custom", "command": "definitely-not-a-program-xyz"}}); self.assertFalse(r["ok"]); self.assertIn("not found", r["detail"])
        self.assertEqual(W.resolve({"viewer": {"kind": "solidworks"}})[0], "solidworks")
        if os.name != "nt": self.assertEqual(W.resolve({})[0], "default")                                          # no SolidWorks off Windows: auto falls back
        if os.name != "nt": self.assertFalse(W.open_file(self.step, {"viewer": {"kind": "solidworks"}})["ok"])

    def test_agent_tool_is_offered_only_with_an_opener_and_reports_honestly(self):
        seen = []
        class L(FakeLLM):
            def chat(self, messages, tools=None, **kw): seen.append([t["function"]["name"] for t in (tools or [])]); return FakeLLM.chat(self, messages, tools=tools, **kw)
        b = CadQueryBackend(timeout=20); ev = []
        try:
            script = [[("execute_cadquery", {"code": CYL})], [("open_in_viewer", {})], "Đã mở."]
            r = agent.run(L(script), b, "mo", plan_mode="off", log=lambda s: None, on_event=lambda k, **kw: ev.append((k, kw)), opener=lambda: {"ok": True, "detail": "sent to X", "viewer": "X", "path": "/p.step"})
            self.assertIn("open_in_viewer", seen[0]); op = [kw for k, kw in ev if k == "open"][0]; self.assertEqual((op["ok"], op["viewer"], op["path"]), (True, "X", "/p.step"))
            self.assertTrue(any("OK: sent to X" in str(m.get("content")) for m in r["history"] if m.get("role") == "tool"))
            seen.clear(); ev.clear()
            r = agent.run(L(script), b, "mo", plan_mode="off", log=lambda s: None, on_event=lambda k, **kw: ev.append((k, kw)), opener=lambda: {"ok": False, "detail": "no viewer"})
            self.assertTrue(any("ERROR: no viewer" in str(m.get("content")) for m in r["history"] if m.get("role") == "tool"))       # the model sees the failure
            seen.clear(); agent.run(L(["ok"]), b, "x", plan_mode="off", log=lambda s: None)
            self.assertNotIn("open_in_viewer", seen[0])                                                                       # remote / no opener: tool not offered
            b.reset(); r = agent.run(L([[("open_in_viewer", {})], "x"]), b, "mo", plan_mode="off", log=lambda s: None, opener=lambda: {"ok": True, "detail": "should not be called"})
            self.assertTrue(any("no part to open" in str(m.get("content")) for m in r["history"] if m.get("role") == "tool"))
        finally: b.close()


class ViewerWeb(Settings):
    def test_open_endpoint_and_settings(self):
        sess = self.srv.sess
        self.assertEqual(self.req("POST", "/api/open", {})[0], 404)                                                    # no part yet
        sess.backend.reset(); sess.backend.execute(CYL); sess.measure = sess.backend.measure().as_dict()
        mark = os.path.join(self.dir, "m.txt"); sc = os.path.join(self.dir, "v.py"); open(sc, "w").write(MARK_VIEWER)
        body = {"url": self.fake_url, "model": "q", "planner": "nothink", "viewer": {"kind": "custom", "command": '"%s" "%s" "%s" {file}' % (sys.executable, sc, mark)}}
        self.assertEqual(self.req("POST", "/api/settings", body)[0], 200)
        s, d = self.req("POST", "/api/open", {}); self.assertEqual(s, 200); self.assertTrue(d["ok"], d); self.assertTrue(d["path"].endswith(".step"))
        for _ in range(60):
            if os.path.exists(mark) and os.path.getsize(mark): break
            time.sleep(0.1)
        self.assertIn("ISO-10303", open(mark).read())                                                                  # a real STEP file reached the viewer
        self.assertEqual(self.req("POST", "/api/settings", dict(body, viewer={"kind": "custom", "command": "--evil"}))[0], 400)
        sess.local = False
        try: self.assertEqual(self.req("POST", "/api/open", {})[0], 403)
        finally: sess.local = True
        s, d = self.req("GET", "/api/config"); self.assertEqual(d["viewer_label"], "chương trình tự đặt")


class HistoryStore(unittest.TestCase):
    def test_roundtrip_ids_prune_and_privacy(self):
        import tempfile, stat
        from cadfabric import history as H
        d = tempfile.mkdtemp(); sid = H.new_id(); self.assertTrue(H.valid(sid))
        H.save(d, {"id": sid, "title": "t", "created": "c", "events": [{"kind": "user", "text": "a"}, {"kind": "phase"}], "cells": ["x=1"], "measure": {"volume": 5.0}, "messages": []})
        r = H.load(d, sid); self.assertEqual([e["kind"] for e in r["events"]], ["user"])                     # progress ticks are dropped
        self.assertEqual(H.list_sessions(d)[0]["volume"], 5.0)
        if os.name != "nt": self.assertEqual(stat.S_IMODE(os.stat(os.path.join(d, "history", sid + ".json")).st_mode), 0o600)
        for bad in ("../x", "..\\x", sid + "/../..", "", None, 5, "20260101-000000-ZZZZ"):
            self.assertFalse(H.valid(bad)); self.assertIsNone(H.load(d, bad) if H.valid(bad) else None)
        with self.assertRaises(ValueError): H.save(d, {"id": "../evil", "events": []})
        open(os.path.join(d, "history", "20200101-000000-aaaa.json"), "w").write("{broken")                    # a corrupt file is skipped, not fatal
        self.assertEqual(len(H.list_sessions(d)), 1)
        H.MAX_SESSIONS = 2
        try:
            for i in range(3): H.save(d, {"id": "2030010%d-000000-aaaa" % i, "events": []})
            self.assertEqual(len([f for f in os.listdir(os.path.join(d, "history")) if f.startswith("2030")]), 2)  # oldest pruned
        finally: H.MAX_SESSIONS = 100
        self.assertIsNone(H.load(d, sid))                                                                          # the oldest was pruned
        self.assertTrue(H.delete(d, "20300102-000000-aaaa")); self.assertIsNone(H.load(d, "20300102-000000-aaaa")); self.assertFalse(H.delete(d, "20300102-000000-aaaa"))


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.root = tempfile.mkdtemp(); self.out = tempfile.mkdtemp()
        w = lambda rel, t, mode="w": (os.makedirs(os.path.dirname(os.path.join(self.root, rel)), exist_ok=True), open(os.path.join(self.root, rel), mode).write(t))
        w("spec.txt", "Flange OD 80, bore 30, 4 holes D6 on PCD 60\n"); w("sub/bom.csv", "part,qty\nbolt,4\n"); w(".env", "SECRET=1"); w("key.txt", "aicf_SECRET"); w("data.bin", b"\x00\x01\x02" * 10, "wb")
        w("big.txt", "x" * 100000); w(".git/config", "[core]"); open(os.path.join(self.out, "outside.txt"), "w").write("OUTSIDE")
        if os.name != "nt": os.symlink(os.path.join(self.out, "outside.txt"), os.path.join(self.root, "link.txt")); os.symlink(self.out, os.path.join(self.root, "linkdir"))

    def test_confinement_and_secrets(self):
        from cadfabric import workspace as W
        self.assertIn("OD 80", W.read_text(self.root, "spec.txt")[0]); self.assertIn("bolt", W.read_text(self.root, "sub/bom.csv")[0])
        for bad in ("../" + os.path.basename(self.out) + "/outside.txt", "..", "/etc/passwd", "C:\\Windows\\win.ini", "~/x", "sub/../../x", ".env", "key.txt", ".git/config", "link.txt", "linkdir/outside.txt", "a\nb"):
            t, e = W.read_text(self.root, bad); self.assertIsNone(t, bad); self.assertTrue(e, bad)
        self.assertTrue(W.read_text(self.root, "data.bin")[1])                                                                     # binary file refused
        t, _ = W.read_text(self.root, "big.txt"); self.assertIn("cut at", t); self.assertLess(len(t), 61000)
        names = [e["name"] for e in W.listing(self.root)[0]["entries"]]
        self.assertEqual(names[0], "sub"); self.assertIn("spec.txt", names)
        for hidden in (".env", "key.txt", ".git", "link.txt", "linkdir"): self.assertNotIn(hidden, names)
        self.assertEqual(W.search(self.root, "bolt")[0], ["sub/bom.csv:2: bolt,4"]); self.assertEqual(W.search(self.root, "SECRET")[0], [])        # secrets are not searchable
        self.assertIsNone(W.listing(self.root, "..")[0]); self.assertIsNone(W.listing(self.root, "linkdir")[0])
        b, used, err = W.attachments(self.root, ["spec.txt", "sub/bom.csv"]); self.assertEqual(err, ""); self.assertIn("FILE: spec.txt", b); self.assertEqual(len(used), 2)
        self.assertTrue(W.attachments(self.root, ["spec.txt", ".env"])[2])
        self.assertEqual(W.check_folder("")[0], ""); self.assertTrue(W.check_folder("/definitely/not/here")[1]); self.assertTrue(W.check_folder(os.path.expanduser("~"))[1]); self.assertTrue(W.check_folder("/")[1])

    def test_new_step_never_overwrites(self):
        from cadfabric import workspace as W
        p, _ = W.new_step_path(self.root, "fl"); open(p, "w").write("x"); q, _ = W.new_step_path(self.root, "fl.step"); self.assertNotEqual(p, q); self.assertEqual(os.path.dirname(q), os.path.realpath(self.root))
        for bad in ("../x", "a/b", ".hidden", "x" * 90): self.assertIsNone(W.new_step_path(self.root, bad)[0], bad)
        self.assertIsNone(W.new_step_path(self.root, "../x")[0]); self.assertIsNone(W.new_step_path(self.root, "a/b")[0])

    def test_agent_tools_only_with_a_folder_and_model_cannot_escape(self):
        seen = []
        class L(FakeLLM):
            def chat(self, messages, tools=None, **kw): seen.append([t["function"]["name"] for t in (tools or [])]); return FakeLLM.chat(self, messages, tools=tools, **kw)
        b = CadQueryBackend(timeout=20)
        try:
            script = [[("read_file", {"path": "spec.txt"}), ("read_file", {"path": "../x"}), ("read_file", {"path": ".env"}), ("list_files", {}), ("search_files", {"text": "bolt"})],
                      [("execute_cadquery", {"code": CYL})], [("save_step", {"name": "out1"})], "ok"]
            ev = []; r = agent.run(L(script), b, "x", plan_mode="off", log=lambda s: None, workspace=self.root, on_event=lambda k, **kw: ev.append((k, kw)))
            tool = [m["content"] for m in r["history"] if m.get("role") == "tool"]
            self.assertIn("OD 80", tool[0]); self.assertTrue(tool[1].startswith("ERROR")); self.assertTrue(tool[2].startswith("ERROR")); self.assertIn("spec.txt", tool[3]); self.assertNotIn(".env", tool[3]); self.assertIn("bom.csv", tool[4])
            self.assertTrue(os.path.isfile(os.path.join(self.root, "out1.step"))); self.assertIn(("file", {"op": "saved", "path": "out1.step"}), [(k, {a: v for a, v in kw.items()}) for k, kw in ev])
            self.assertIn("read_file", seen[0]); seen.clear(); agent.run(L(["ok"]), b, "x", plan_mode="off", log=lambda s: None); self.assertNotIn("read_file", seen[0])    # no folder: no tools
            r = agent.run(L([[("save_step", {"name": "zz"})], "ok"]), b, "x", plan_mode="off", log=lambda s: None, workspace=self.root)
            self.assertFalse(os.path.exists(os.path.join(self.root, "zz.step"))); self.assertIn("no part to save", [m["content"] for m in r["history"] if m.get("role") == "tool"][0])
            r = agent.run(L(["ok"]), b, "ask", plan_mode="off", log=lambda s: None, workspace=self.root, attachments="----- FILE: a -----\nHELLO"); self.assertIn("HELLO", r["history"][1]["content"])
        finally: b.close()

    def test_advisor_web_flags(self):
        from cadfabric import advisors as A
        c, _ = A.cli_argv({"preset": "claude", "web": True}, {}, "/w"); self.assertIn("WebSearch,WebFetch", c); self.assertNotIn("", c[c.index("--tools") + 1:c.index("--tools") + 2])
        c0, _ = A.cli_argv({"preset": "claude"}, {}, "/w"); self.assertEqual(c0[c0.index("--tools") + 1], ""); self.assertNotIn("WebSearch,WebFetch", c0)
        x, _ = A.cli_argv({"preset": "codex", "web": True, "model": "m"}, {}, "/w"); self.assertEqual(x[1], "--search"); self.assertEqual(x[2], "exec"); self.assertEqual(x[-1], "-")
        x0, _ = A.cli_argv({"preset": "codex"}, {}, "/w"); self.assertNotIn("--search", x0)
        ok = A.validate({"id": "w", "type": "cli", "preset": "claude", "web": True}); self.assertTrue(ok[0]["web"]); self.assertNotIn("web", A.validate({"id": "w", "type": "cli", "preset": "claude"})[0])
        self.assertNotIn("web", A.validate({"id": "w", "type": "cli", "preset": "custom", "command": "x", "web": True})[0])           # only presets know how to enable it safely
        self.assertNotIn("web", A.validate({"id": "w", "type": "api", "url": "http://1.2.3.4/v1", "web": True})[0])


class WorkspaceWeb(Settings):
    def test_folder_setting_files_endpoint_and_attachments(self):
        sess = self.srv.sess; root = os.path.join(self.dir, "proj"); os.makedirs(root); open(os.path.join(root, "a.txt"), "w").write("DIAMETER 42"); open(os.path.join(root, ".env"), "w").write("S")
        self.assertEqual(self.req("GET", "/api/files")[0], 400)                                                           # no folder yet
        body = {"url": self.fake_url, "model": "q", "planner": "nothink", "workspace": root}
        self.assertEqual(self.req("POST", "/api/settings", dict(body, workspace="/no/such/dir"))[0], 400); self.assertEqual(self.req("POST", "/api/settings", body)[0], 200)
        s, d = self.req("GET", "/api/files"); self.assertEqual(s, 200); self.assertEqual([e["name"] for e in d["entries"]], ["a.txt"])
        self.assertEqual(self.req("GET", "/api/files?path=..")[0], 400)
        self.assertEqual(self.req("POST", "/api/send", {"text": "x", "planner": "nothink", "files": ["../x"]})[0], 400); self.assertEqual(self.req("POST", "/api/send", {"text": "x", "planner": "nothink", "files": [".env"]})[0], 400)
        sess.local = False
        try: self.assertEqual(self.req("GET", "/api/files")[0], 403); self.assertEqual(self.req("POST", "/api/send", {"text": "x", "planner": "nothink", "files": ["a.txt"]})[0], 400)
        finally: sess.local = True
        s, d = self.req("GET", "/api/config"); self.assertEqual(d["settings"]["workspace"], root)

    def test_native_folder_picker_endpoint(self):
        sess = self.srv.sess
        self.assertFalse(self.req("GET", "/api/config")[1]["picker"]); self.assertEqual(self.req("POST", "/api/pick_folder", {})[0], 404)   # browser mode: no dialog
        sess.picker = lambda: "/chosen/dir"
        try:
            self.assertTrue(self.req("GET", "/api/config")[1]["picker"]); self.assertEqual(self.req("POST", "/api/pick_folder", {}), (200, {"path": "/chosen/dir"}))
            sess.local = False
            try: self.assertEqual(self.req("POST", "/api/pick_folder", {})[0], 403)
            finally: sess.local = True
        finally: sess.picker = None

    def test_browse_lists_only_visible_folders(self):
        root = os.path.join(self.dir, "br"); os.makedirs(os.path.join(root, "sub")); os.makedirs(os.path.join(root, ".hid")); open(os.path.join(root, "f.txt"), "w").write("x")
        s, d = self.req("GET", "/api/browse?path=" + root); self.assertEqual((s, d["dirs"], d["path"]), (200, ["sub"], root))
        self.assertEqual(self.req("GET", "/api/browse?path=" + root + "/f.txt")[0], 400)
        self.srv.sess.local = False
        try: self.assertEqual(self.req("GET", "/api/browse?path=" + root)[0], 403)
        finally: self.srv.sess.local = True


if __name__ == "__main__":
    unittest.main(verbosity=1)
