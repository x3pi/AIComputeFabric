import json, os, sys, tempfile, pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from wagent.llm import parse_action
from wagent.audit import Audit, verify
from wagent.loop import Agent
from wagent.skills import files

class FakeLLM:
    def __init__(self, replies): self.r = list(replies)
    def chat(self, m, **k): return self.r.pop(0)

class FakeUIA:
    def __init__(self): self.calls = []
    def observe(self): return "1 [Edit] 'x'"
    def set_text(self, id, text, **_): self.calls.append((id, text)); return "ok"

def mk(replies, **kw):
    d = tempfile.mkdtemp()
    u = FakeUIA()
    return Agent(FakeLLM(replies), u, Audit(d + "/a.jsonl"), d + "/sb", log=lambda *_: None, **kw), u, d

def test_parse_variants():
    assert parse_action('```json\n{"tool":"observe"}\n```')[0]["args"] == {}
    assert parse_action('Toi se lam: {"tool":"click","args":{"id":3}} xong')[0]["args"]["id"] == 3
    assert parse_action("khong co")[0] is None
    assert parse_action('{"tool":"x","args":[1]}')[0] is None

def test_happy_path_and_audit_chain():
    a, u, d = mk(['{"tool":"observe"}', '{"tool":"set_text","args":{"id":1,"text":"hi"}}', '{"tool":"done","args":{"summary":"ok"}}'])
    assert a.run("t") == ("done", "ok") and u.calls == [(1, "hi")]
    assert verify(d + "/a.jsonl") == (True, 0)

def test_audit_tamper_detected():
    a, u, d = mk(['{"tool":"done"}'])
    a.run("t")
    s = open(d + "/a.jsonl").read().replace('"task": "t"', '"task": "EVIL"')
    open(d + "/a.jsonl", "w").write(s)
    assert verify(d + "/a.jsonl")[0] is False

def test_unknown_tool_denied():
    a, u, d = mk(['{"tool":"run_shell","args":{"cmd":"del *"}}', '{"tool":"done"}'])
    assert a.run("t")[0] == "done"
    assert '"event": "denied"' in open(d + "/a.jsonl").read()

def test_bad_format_3x_fails():
    a, u, d = mk(["a", "b", "c"])
    assert a.run("t")[0] == "failed"

def test_max_steps():
    a, u, d = mk(['{"tool":"observe"}'] * 3, max_steps=3)
    assert a.run("t")[0] == "max_steps"

def test_sandbox_escape_blocked(tmp_path):
    with pytest.raises(files.PathDenied): files.read_file(str(tmp_path), "../etc/passwd")
    with pytest.raises(files.PathDenied): files.write_file(str(tmp_path), "/etc/x", "a")
    (tmp_path / "out").mkdir(); os.symlink("/etc", tmp_path / "out" / "l")
    with pytest.raises(files.PathDenied): files.read_file(str(tmp_path), "out/l/passwd")
    assert "da ghi" in files.write_file(str(tmp_path), "ok/a.txt", "hi")

def test_escape_via_agent_reports_error():
    a, u, d = mk(['{"tool":"read_file","args":{"path":"../../etc/passwd"}}', '{"tool":"done"}'])
    a.run("t")
    assert "PathDenied" in open(d + "/a.jsonl").read()
