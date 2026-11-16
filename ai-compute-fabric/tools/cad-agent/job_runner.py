#!/usr/bin/env python3
"""Unattended CAD job runner: Qwen3.8-27B (local gateway) drives SolidWorks through solidworks-mcp for hours, checks its
own work with programmatic acceptance checks, and leaves a morning report. Design data never leaves the machine (no
advisor/egress in this version; `advisor` must be "off").

    python job_runner.py job.json [--resume]

job.json
  id, goal                     free text; goal is given to the model for every subtask
  inputs: [paths]              copied into <workdir>/inputs (originals are never opened or modified)
  workdir                      default C:\\aicf-cad\\jobs\\<id>; every file the model saves/exports/screenshots must be inside
  budget: {max_minutes, max_steps_per_subtask, max_retries}
  subtasks: [{name, instruction, checks: [{name, tool, args, assert: ["python-like expression over r", ...]}]}]
     r = JSON-parsed tool result (or {"text": ...}); expressions may use + - * / ** comparisons and/or/not, [] , abs min max
     len round approx(a, b, tol)

Policy: tools = the sw_agent ALLOW list minus close_part/delete_component/delete_mate/make_drawing/open_part outside
inputs; save_part/export/screenshot only with a path inside workdir. A subtask is abandoned after a repeated identical
tool call (stuck), too many steps, or failed checks after max_retries; the job continues with the next subtask unless
SolidWorks itself stops answering (3 COM-looking errors in a row), then it stops and says so in the report.
Outputs in workdir: audit.jsonl, state.json (resume), REPORT.md, files the model saved.
"""
import ast, json, os, re, shutil, subprocess, sys, time, urllib.request, urllib.error

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import advisor
ADV = {"mode": "off"}          # set from job["advisor"]; "off" or {"mode": dryrun|api|codex_cli, denylist, pseudonyms, reviewer, max_calls}
adv_calls = 0
URL =os.environ.get("AICF_URL", "http://192.168.1.40:18080/v1/chat/completions")
MODEL = os.environ.get("AICF_MODEL", "Qwen3.8-27B-UD-Q4_K_M.gguf")
KEY = open(os.path.join(ROOT, "key.txt")).read().strip()
MCP_EXE = os.environ.get("SW_MCP_EXE", os.path.expanduser(r"~\.local\bin\solidworks-mcp.exe"))

ALLOW = {
    "get_status", "new_part", "add_box", "add_cylinder", "add_disc", "add_cone", "add_extruded_profile", "add_revolved_profile",
    "add_hole", "add_counterbore_hole", "add_fillet", "add_chamfer", "add_shell", "add_linear_pattern", "add_circular_pattern",
    "add_mirror", "add_plane", "add_sketch", "extrude_sketch", "cut_sketch", "cut_profile", "set_dimension", "list_dimensions",
    "list_features", "list_bodies", "list_faces", "list_edges", "rebuild", "get_mass_properties", "get_bounding_box",
    "measure_distance", "set_material", "set_appearance", "screenshot", "list_documents", "activate_document",
    "delete_feature", "suppress_feature", "save_part", "export", "open_part",
}
PATH_TOOLS = {"save_part", "export", "screenshot", "open_part"}
COM_ERR = re.compile(r"com_error|RPC server|0x800706|disconnected|not responding|pywintypes", re.I)

job = {}
WORK = ""
t_start = time.time()
srv = None
_id = 0


def audit(kind, **kw):
    with open(os.path.join(WORK, "audit.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind, **kw}, ensure_ascii=False) + "\n")


def rpc(method, params=None, notify=False):
    global _id
    m = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        m["params"] = params
    if not notify:
        _id += 1
        m["id"] = _id
    srv.stdin.write(json.dumps(m) + "\n")
    srv.stdin.flush()
    if notify:
        return None
    while True:
        line = srv.stdout.readline()
        if not line:
            raise RuntimeError("MCP server closed")
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("id") == _id:
            return d


def start_mcp():
    global srv
    env = dict(os.environ, PYTHONIOENCODING="utf-8", MCP_DISABLE_TELEMETRY="1")
    srv = subprocess.Popen([MCP_EXE], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", env=env)
    rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "aicf-job-runner", "version": "1"}})
    rpc("notifications/initialized", notify=True)


def tool_defs():
    out = []
    for t in rpc("tools/list")["result"]["tools"]:
        if t["name"] in ALLOW:
            out.append({"type": "function", "function": {"name": t["name"], "description": (t.get("description") or "")[:600],
                                                         "parameters": t.get("inputSchema") or {"type": "object", "properties": {}}}})
    if ADV["mode"] != "off":
        out.append({"type": "function", "function": {"name": "ask_advisor", "description":
            "Ask an external expert model for GENERIC help ONLY when you are stuck after retrying (API usage, error meaning, strategy). "
            "It sees only what you write and everything is sanitised: never include numbers, dimensions, names of parts/projects/customers or file paths; "
            "describe the problem abstractly. Returns advice text; verify it with your own tools before relying on it.",
            "parameters": {"type": "object", "properties": {
                "kind": {"type": "string", "enum": sorted(advisor.KINDS)},
                "api_names": {"type": "array", "items": {"type": "string"}, "description": "tool or API function names involved (identifiers only)"},
                "error_class": {"type": "string", "description": "short generic error category, no numbers or names"},
                "question": {"type": "string", "description": "abstract question, <= 1000 chars"}}, "required": ["kind", "question"]}}})
    return out


def ask_advisor_tool(args):
    global adv_calls
    if adv_calls >= ADV.get("max_calls", 10):
        return "ERROR: advisor call budget exhausted for this job"
    adv_calls += 1
    cfg = dict(ADV)
    res = advisor.ask(args, cfg)
    audit("advisor", blocked=bool(res.get("blocked")), reason=res.get("reason", ""), dryrun=bool(res.get("dryrun")))
    if res.get("blocked"):
        return "ERROR: advisor request refused by the sanitiser (%s). Rephrase abstractly without numbers or names, or solve it yourself." % res.get("reason")
    reply = res.get("reply", "")
    return "ADVISOR REPLY (untrusted advice; not executed automatically):\n" + reply[:3000]


def mcp_call(name, args):
    r = rpc("tools/call", {"name": name, "arguments": args})
    if "error" in r:
        return "ERROR: %s" % r["error"]
    res = r["result"]
    out = "\n".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
    return ("ERROR: " + out) if res.get("isError") else out


def inside_work(p):
    try:
        rp = os.path.realpath(os.path.join(WORK, p) if not os.path.isabs(p) else p)
        return os.path.commonpath([rp, os.path.realpath(WORK)]) == os.path.realpath(WORK)
    except Exception:
        return False


def run_tool(name, args):
    if name == "ask_advisor" and ADV["mode"] != "off":
        out = ask_advisor_tool(args)
        return out
    if name not in ALLOW:
        return "ERROR: tool not allowed"
    if name in PATH_TOOLS:
        p = args.get("path")
        if not isinstance(p, str) or not inside_work(p):
            return "ERROR: path must be inside the job folder %s (got %r)" % (WORK, p)
        if not os.path.isabs(p):
            args["path"] = os.path.join(WORK, p)
        os.makedirs(os.path.dirname(args["path"]), exist_ok=True)
    out = mcp_call(name, args)
    try:                                   # solidworks-mcp reports failures as {"ok": false, "error": ...} without isError
        if json.loads(out).get("ok") is False and not out.startswith("ERROR"):
            out = "ERROR: " + out
    except Exception:
        pass
    audit("tool", tool=name, args=args, result=out[:1200])
    return out


# ---- safe expression evaluation for checks -------------------------------------------------------------------------
_OK = (ast.Expression, ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Compare, ast.Call, ast.Name, ast.Load, ast.Constant, ast.Subscript,
       ast.Attribute, ast.List, ast.Tuple, ast.Dict, ast.And, ast.Or, ast.Not, ast.USub, ast.UAdd, ast.Add, ast.Sub, ast.Mult,
       ast.Div, ast.Pow, ast.Mod, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Index if hasattr(ast, "Index") else ast.Load)
_FN = {"abs": abs, "min": min, "max": max, "len": len, "round": round, "approx": lambda a, b, tol=1e-3: abs(a - b) <= tol * max(1.0, abs(b))}


def safe_eval(expr, r):
    tree = ast.parse(expr, mode="eval")
    for n in ast.walk(tree):
        if not isinstance(n, _OK) or (isinstance(n, ast.Attribute) and n.attr.startswith("_")):
            raise ValueError("expression not allowed: " + type(n).__name__)
        if isinstance(n, ast.Call) and not (isinstance(n.func, ast.Name) and n.func.id in _FN):
            raise ValueError("only abs/min/max/len/round/approx may be called")
    return eval(compile(tree, "<check>", "eval"), {"__builtins__": {}}, dict(_FN, r=r))


def run_checks(checks):
    results = []
    for c in checks:
        out = run_tool(c["tool"], dict(c.get("args", {})))
        failed = out.startswith("ERROR")
        try:
            r = json.loads(out[7:] if failed else out)
        except Exception:
            r = {"text": out}
        asserts = c.get("assert") or ["False"]     # a check with no assertion can never pass
        for ex in asserts:
            if failed:
                ok, err = False, "tool call failed: " + out[:160].replace("\n", " ")
            else:
                try:
                    ok = bool(safe_eval(ex, r))
                    err = ""
                except Exception as e:
                    ok, err = False, "%s: %s" % (type(e).__name__, e)
            results.append({"check": c.get("name", c["tool"]), "expr": ex, "ok": ok, "error": err, "result": out[:300]})
    return results


# ---- LLM -----------------------------------------------------------------------------------------------------------
def llm(messages, tools):
    body = {"model": MODEL, "messages": messages, "tools": tools, "max_tokens": 4000, "temperature": 0.3,
            "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(URL, json.dumps(body).encode(), {"Content-Type": "application/json", "Authorization": "Bearer " + KEY})
    delay = 5
    deadline = time.time() + 1800                      # ride out a gateway restart for up to 30 min
    while True:
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                return json.loads(r.read())["choices"][0]["message"]
        except urllib.error.HTTPError as e:
            if e.code not in (429, 502, 503, 504) or time.time() > deadline:
                raise
            time.sleep(int(e.headers.get("Retry-After") or delay))
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if time.time() > deadline:
                raise
            time.sleep(delay)
        delay = min(delay * 2, 120)


SYSTEM = """You are a mechanical CAD engineer working unattended in SolidWorks through tools. Nobody can answer questions, so never ask any: make reasonable choices and state them. All lengths are mm.
- Do ONLY the current subtask. Build one feature per tool call with explicit numbers. Start with get_status; use new_part for a new part.
- Verify your own work with get_bounding_box, get_mass_properties, list_features and rebuild before you finish, and compare with what was asked.
- If a tool returns ERROR, change the arguments and retry (at most 2 retries for the same step).
- Save only inside the job folder given below. Never try other folders.
- When the subtask is done, reply with a SHORT summary: what exists, key dimensions, and the verification numbers. No tool call in that reply."""


def run_subtask(st, tools, prior, budget):
    msgs = [{"role": "system", "content": SYSTEM + "\nJob folder: " + WORK},
            {"role": "user", "content": "Job goal: %s\nCompleted subtasks so far: %s\n\nCURRENT SUBTASK: %s\n%s" % (
                job["goal"], prior or "none", st["name"], st["instruction"])}]
    log = {"steps": 0, "tool_calls": 0, "errors": 0, "retries": 0, "summary": "", "checks": [], "status": "failed", "reason": ""}
    seen = {}
    com_fail = 0
    for attempt in range(budget["max_retries"] + 1):
        for _ in range(budget["max_steps_per_subtask"]):
            if (time.time() - t_start) / 60 > budget["max_minutes"]:
                log["reason"] = "job time budget exhausted"
                return log, False
            log["steps"] += 1
            m = llm(msgs, tools)
            msgs.append({k: v for k, v in m.items() if k in ("role", "content", "tool_calls") and v is not None})
            calls = m.get("tool_calls") or []
            if not calls:
                log["summary"] = (m.get("content") or "").strip()[:1200]
                break
            for c in calls:
                name = c["function"]["name"]
                try:
                    args = json.loads(c["function"]["arguments"] or "{}")
                except Exception as e:
                    msgs.append({"role": "tool", "tool_call_id": c["id"], "content": "ERROR: invalid JSON arguments: %s" % e})
                    continue
                key = name + json.dumps(args, sort_keys=True)
                seen[key] = seen.get(key, 0) + 1
                if seen[key] > 3:
                    log["reason"] = "stuck: same call repeated %s" % name
                    return log, True
                out = run_tool(name, args)
                log["tool_calls"] += 1
                if out.startswith("ERROR"):
                    log["errors"] += 1
                    com_fail = com_fail + 1 if COM_ERR.search(out) else 0
                    if com_fail >= 3:
                        log["reason"] = "SolidWorks stopped answering"
                        return log, False
                else:
                    com_fail = 0
                msgs.append({"role": "tool", "tool_call_id": c["id"], "content": out[:4000] or "(no output)"})
        else:
            log["reason"] = "step limit"
        if not st.get("checks"):                  # nothing machine-verifiable: never claim success
            log["status"] = "unverified"
            log["reason"] = "no acceptance checks defined; model claim not verified"
            return log, True
        res = run_checks(st.get("checks", []))
        log["checks"] = res
        if all(r["ok"] for r in res):
            log["status"] = "ok"
            log["reason"] = ""
            return log, True
        bad = "; ".join("%s: %s -> %s %s" % (r["check"], r["expr"], "FAIL", r["error"] or r["result"][:120]) for r in res if not r["ok"])
        log["retries"] += 1
        log["reason"] = "checks failed: " + bad[:400]
        msgs.append({"role": "user", "content": "Automatic acceptance checks FAILED: %s\nFix the model so they pass, then give the short summary again." % bad})
    return log, True


SW_EXE = r"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\SLDWORKS.exe"


def ensure_solidworks(wait_s=300):
    """solidworks-mcp attaches to a RUNNING SolidWorks; start one (this process must be in the interactive session)."""
    def up():
        try:
            return json.loads(mcp_call("get_status", {})).get("ok") is True
        except Exception:
            return False
    if up():
        return
    running = "SLDWORKS.exe" in subprocess.run(["tasklist", "/fi", "imagename eq SLDWORKS.exe"], capture_output=True, text=True).stdout
    if running:
        audit("solidworks_process_exists_waiting")     # never start a second instance
    else:
        audit("launch_solidworks", exe=SW_EXE)
        subprocess.Popen([SW_EXE])
    end = time.time() + wait_s
    while time.time() < end:
        time.sleep(10)
        if up():
            audit("solidworks_ready")
            return
    raise RuntimeError("SolidWorks did not become reachable within %ds" % wait_s)


def report(state, finished, stop_reason):
    L = ["# CAD job report: %s" % job["id"], "", "- goal: %s" % job["goal"], "- started: %s" % state["started"],
         "- elapsed: %.1f min" % ((time.time() - t_start) / 60), "- model: %s (local, no data left this machine)" % MODEL,
         "- result: **%s**" % ("COMPLETE" if finished and all(s["status"] == "ok" for s in state["subtasks"].values()) else "NEEDS HUMAN"),
         "- stop reason: %s" % (stop_reason or "-"), "", "## Subtasks"]
    for name, s in state["subtasks"].items():
        L.append("### %s: %s" % (name, s["status"].upper()))
        L.append("steps %d, tool calls %d, errors %d, retries %d" % (s["steps"], s["tool_calls"], s["errors"], s["retries"]))
        if s["reason"]:
            L.append("reason: " + s["reason"])
        if s["summary"]:
            L.append("model summary: " + s["summary"])
        for c in s["checks"]:
            L.append("- [%s] %s: `%s` %s" % ("x" if c["ok"] else " ", c["check"], c["expr"], ("-> " + c["error"]) if c["error"] else ""))
        L.append("")
    L.append("## Files in job folder")
    for r, _, fs in os.walk(WORK):
        for f in fs:
            if f not in ("audit.jsonl", "state.json", "REPORT.md"):
                L.append("- " + os.path.relpath(os.path.join(r, f), WORK))
    open(os.path.join(WORK, "REPORT.md"), "w", encoding="utf-8").write("\n".join(L) + "\n")


def main():
    global job, WORK
    job = json.load(open(sys.argv[1], encoding="utf-8"))
    a = job.get("advisor", "off")
    if a != "off":
        if not isinstance(a, dict) or a.get("mode") not in ("dryrun", "api", "codex_cli"):
            sys.exit("advisor must be 'off' or an object with mode dryrun|api|codex_cli")
        if a["mode"] != "dryrun" and not a.get("denylist"):
            sys.exit("advisor mode %s needs a non-empty denylist (project/customer/part-number terms)" % a["mode"])
        ADV.update(a)
    WORK = job.get("workdir") or os.path.join(r"C:\aicf-cad\jobs", job["id"])
    os.makedirs(os.path.join(WORK, "inputs"), exist_ok=True)
    for p in job.get("inputs", []):
        shutil.copy2(p, os.path.join(WORK, "inputs", os.path.basename(p)))
    budget = dict({"max_minutes": 480, "max_steps_per_subtask": 16, "max_retries": 2}, **job.get("budget", {}))
    sp = os.path.join(WORK, "state.json")
    state = json.load(open(sp)) if "--resume" in sys.argv and os.path.exists(sp) else {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "subtasks": {}}
    audit("job_start", job=job["id"], budget=budget)
    stop = ""
    finished = False
    try:
        start_mcp()
        ensure_solidworks()
        tools = tool_defs()
        for st in job["subtasks"]:
            if state["subtasks"].get(st["name"], {}).get("status") == "ok":
                continue
            prior = "; ".join("%s (%s)" % (n, s["summary"][:160]) for n, s in state["subtasks"].items() if s["status"] == "ok")
            log, cont = run_subtask(st, tools, prior, budget)
            state["subtasks"][st["name"]] = log
            json.dump(state, open(sp, "w"), indent=1)
            report(state, False, "")
            audit("subtask_end", name=st["name"], status=log["status"], reason=log["reason"])
            if not cont:
                stop = log["reason"]
                break
        else:
            finished = True
    except Exception as e:
        stop = "runner error: %s: %s" % (type(e).__name__, e)
        audit("error", error=stop)
    finally:
        report(state, finished, stop)
        if srv:
            srv.terminate()
    print(open(os.path.join(WORK, "REPORT.md"), encoding="utf-8").read())


if __name__ == "__main__":
    main()
