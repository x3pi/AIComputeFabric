#!/usr/bin/env python3
"""Qwen3.8-27B (AI Compute Fabric) <-> SolidWorks via solidworks-mcp (COM), interactive chat.

Runs on the Windows PC that has SolidWorks. The LLM is reached over the LAN (OpenAI-compatible gateway); SolidWorks is
reached locally through the MCP server `solidworks-mcp` (stdio, hjbaard/SolidWorks-MCP, no network code in the package).
Design data never leaves this machine: the only thing the model can call outside the CAD tools is nothing (egress is
OFF here; the advisor tool is a separate, later component).

Safety:
  * tools are an allowlist (read/inspect + modelling); destructive/outward tools (delete_*, save_*, close_part, export,
    make_drawing, ...) ask for a y/N confirmation on the console before running (CONFIRM set below).
  * every tool call and result is appended to audit.jsonl next to this script.
Commands: /reset (forget chat)  /think on|off  /tools  /exit
"""
import json, os, re, subprocess, sys, time, urllib.request, urllib.error

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import aicfconf
CFG = aicfconf.load()
URL = aicfconf.url(CFG)
MODEL = aicfconf.model(CFG)
KEY = aicfconf.read_key(CFG)
MCP_EXE = aicfconf.mcp_exe(CFG)
AUDIT = os.path.join(aicfconf.data_dir(CFG), "audit.jsonl")
MAX_STEPS = int(os.environ.get("AICF_MAX_STEPS") or CFG.get("max_steps") or 30)   # model calls per user message
MAX_TOOL_CHARS = 4000
think = False

# Modelling + inspection tools offered to the model (a subset of the 88: a smaller tool list keeps the prompt short and
# the 27B model accurate). Extend per task.
ALLOW = {
    "get_status", "new_part", "add_box", "add_cylinder", "add_disc", "add_cone", "add_extruded_profile", "add_revolved_profile",
    "add_hole", "add_counterbore_hole", "add_fillet", "add_chamfer", "add_shell", "add_linear_pattern", "add_circular_pattern",
    "add_mirror", "add_plane", "add_sketch", "extrude_sketch", "cut_sketch", "cut_profile", "set_dimension", "list_dimensions",
    "list_features", "list_bodies", "list_faces", "list_edges", "rebuild", "get_mass_properties", "get_bounding_box",
    "measure_distance", "set_material", "set_appearance", "screenshot", "list_documents", "activate_document",
    "delete_feature", "suppress_feature", "save_part", "export", "close_part", "open_part",
}
# Need an explicit y/N from the person at the console (destructive or writes outside memory).
CONFIRM = {"delete_feature", "save_part", "export", "close_part", "open_part", "delete_component", "delete_mate", "make_drawing"}

# ---- advisor: a second opinion that can only ANSWER IN TEXT (no tools, no files, never edits the part) ---------------------
# Modes: off | claude (external, via config.json "advisor": url/model/key_file) | qwen_think | qwen_nothink (the local model itself).
# There is NO input filter here by the owner's decision: what the agent writes in `question` is sent as written (length-capped, audited,
# and shown in the chat as the tool card). Local modes never leave the PC.
import advisor as ADV
ADVISOR_MODES = ("off", "auto", "claude", "qwen_think", "qwen_nothink")
ADVISOR_MODE = str(os.environ.get("AICF_ADVISOR") or (CFG.get("advisor") or {}).get("default_mode") or "off")
if ADVISOR_MODE not in ADVISOR_MODES:
    ADVISOR_MODE = "off"
ADVISOR_MAX_CALLS = int(os.environ.get("AICF_ADVISOR_MAX") or (CFG.get("advisor") or {}).get("max_calls") or 5)   # per user message
adv_calls = 0
ADVISOR_TOOL = {"type": "function", "function": {"name": "ask_advisor", "description":
    "Ask a second, stronger-reasoning advisor when you are stuck, a feature keeps failing, or you need to plan a tricky modelling order. "
    "The advisor CANNOT see the part or SolidWorks and cannot change anything: put ALL the facts it needs in `question` (what you want, "
    "what you tried, exact error text, key dimensions). Its answer is advice only: verify it against the real model state before acting.",
    "parameters": {"type": "object", "properties": {"question": {"type": "string", "description": "self-contained question with all context"}},
                   "required": ["question"]}}}


def advisor_available():
    c = CFG.get("advisor") or {}
    return {"off": True, "auto": True, "qwen_think": True, "qwen_nothink": True,
            "claude": bool(c.get("url") and c.get("model") and c.get("key_file") and os.path.isfile(os.path.expandvars(os.path.expanduser(c["key_file"]))))}


def active_tools():
    return TOOLS + [ADVISOR_TOOL] if ADVISOR_MODE != "off" else TOOLS


ADVISOR_DEADLINE_S = int(os.environ.get("AICF_ADVISOR_DEADLINE") or (CFG.get("advisor") or {}).get("deadline_s") or 300)   # total time one local advisor call may take


def _qwen_advice(q, thinking, deadline=None):
    deadline = deadline or time.time() + ADVISOR_DEADLINE_S
    body = {"model": MODEL, "max_tokens": 4000 if thinking else 1500, "temperature": 0.3, "chat_template_kwargs": {"enable_thinking": thinking},
            "messages": [{"role": "system", "content": ADV.ADVISOR_SYSTEM}, {"role": "user", "content": q}]}
    req = urllib.request.Request(URL, json.dumps(body).encode(), {"Content-Type": "application/json", "Authorization": "Bearer " + KEY})
    for attempt in range(6):
        left = deadline - time.time()
        if left < 15 or SHOULD_STOP():                     # never hold the whole agent hostage: fail fast and let it carry on
            return "", "advisor gave up (%s)" % ("stopped by the user" if SHOULD_STOP() else "time budget used up")
        try:
            d = json.loads(urllib.request.urlopen(req, timeout=min(left, 240)).read())
            text = (d["choices"][0]["message"].get("content") or "").strip()
            if not text and thinking:                      # all tokens went into the reasoning: ask again without thinking rather than return nothing
                return _qwen_advice(q, False, deadline)
            return text, ""
        except urllib.error.HTTPError as e:
            if e.code not in (429, 502, 503) or attempt == 5:
                return "", "advisor HTTP %d" % e.code
            time.sleep(min(5 + 3 * attempt, max(1, deadline - time.time() - 15)))
        except Exception as e:
            return "", "advisor request failed (%s)" % type(e).__name__
    return "", "advisor busy"


def _ask_mode(q, mode):
    """One advisor round trip in the given mode. Returns (text, error). Text only: no tools, never edits the part."""
    if mode == "claude":
        c = dict(CFG.get("advisor") or {}, mode="anthropic", sanitize="off")
        res = ADV.ask({"kind": "agent_question", "api_names": [], "error_class": "", "question": q}, c)
        return res.get("reply", ""), ("" if res.get("ok") else res.get("reason", "unknown"))
    if mode in ("qwen_think", "qwen_nothink"):
        ADV.audit("outbound", mode=mode, payload=q[:6000], report={"sanitize": "off", "local": True})
        text, err = _qwen_advice(q, mode == "qwen_think")
        if text:
            ADV.audit("inbound", reply=text[:4000])
        return text, err
    return "", "advisor is off"


def eff_mode(kind, n=0):
    """`auto` = plan and checks use the strongest advisor available; ordinary consults go local first, then escalate."""
    if ADVISOR_MODE != "auto":
        return ADVISOR_MODE
    strong = "claude" if advisor_available()["claude"] else "qwen_think"
    if kind in ("plan", "gate"):
        return strong
    return "qwen_think" if n == 0 else strong


# ---- advisor architecture: (1) plan first, (2) numbers checked by CODE, (3) review before "done" ------------------------
PLAN, REQUEST, gates_used = None, "", 0
GATE_MAX = 2          # failed final checks per request before the agent may report anyway
VOL_TOL = 0.005       # relative tolerance for the final volume
BBOX_TOL_MM = 0.5
PLAN_PROMPT = (
    "You are planning a CAD job that a smaller model will carry out through the tools listed below. Read the USER REQUEST and reply with ONE JSON "
    "object and nothing else:\n"
    '{"steps": ["short imperative step naming the tool to use"], "expected": {"volume_mm3": <number or null>, "bbox_size_mm": [x, y, z] or null}, '
    '"pitfalls": ["what commonly goes wrong with this request and how to check it"]}\n'
    "Rules: compute `expected` yourself from the request with exact arithmetic (volume of the FINAL solid in mm3, and its overall bounding-box size); "
    "use null when the request does not determine it. Prefer the simplest tool that does the job. Say which axis or plane each step uses, because the "
    "executor does not know the tools' geometry conventions. Do not invent requirements.")


def catalog():
    lines = []
    for t in TOOLS:
        f = t["function"]
        props = list(((f.get("parameters") or {}).get("properties") or {}))
        lines.append("- %s(%s): %s" % (f["name"], ", ".join(props[:10]), (f.get("description") or "").split("\n")[0][:150]))
    return "\n".join(lines)


def state_snapshot():
    """Read-only look at the real part, done by the harness (not by the model): (text, volume_mm3, bbox_size_mm)."""
    vol = bbox = None
    parts = []
    for name in ("list_features", "get_mass_properties"):
        try:
            out, _ = mcp_call(name, {})
        except Exception as e:
            parts.append("%s: unavailable (%s)" % (name, type(e).__name__))
            continue
        parts.append("%s -> %s" % (name, out[:1800]))
        if name == "get_mass_properties":
            try:
                mp = json.loads(out)["mass_properties"]
                vol, bbox = float(mp["volume_mm3"]), [float(x) for x in mp["bounding_box_mm"]["size_mm"]]
            except Exception:
                pass
    return "\n".join(parts), vol, bbox


def advise(args):
    global adv_calls
    q = str((args or {}).get("question") or "").strip()
    if not q:
        return "ERROR: ask_advisor needs a non-empty `question`."
    if adv_calls >= ADVISOR_MAX_CALLS:
        return "ERROR: advisor call budget for this request is used up (%d). Decide yourself or report what blocks you." % ADVISOR_MAX_CALLS
    mode = eff_mode("consult", adv_calls)
    adv_calls += 1
    snap, _, _ = state_snapshot()          # the advisor cannot see SolidWorks: the harness attaches the real state
    full = ("ORIGINAL USER REQUEST:\n%s\n\nQUESTION FROM THE EXECUTOR:\n%s\n\nCURRENT STATE OF THE PART (read by the harness; authoritative):\n%s\n\n"
            "TOOLS THE EXECUTOR HAS:\n%s" % (REQUEST or "(not recorded)", q[:12000], snap, catalog()))[:30000]
    text, err = _ask_mode(full, mode)
    audit("advisor", mode=mode, ok=not err, reason=err, q_chars=len(full), reply_chars=len(text))
    if err or not text:
        return "ERROR: advisor unavailable (%s). Solve it yourself with the tools you have." % (err or "empty reply")
    return "ADVISOR REPLY (%s; advice only, NOT executed; verify against the real part state):\n%s" % (mode, text[:3500])


def start_request(msgs):
    """Once per user message: ask the strongest advisor for a plan with checkable numbers, and put it in front of the executor."""
    global PLAN, REQUEST, gates_used
    gates_used = 0
    if ADVISOR_MODE == "off":
        PLAN = None
        return
    last = [m for m in msgs if m["role"] == "user"][-1]["content"]
    text = last if isinstance(last, str) else ""
    if PLAN and len(text) < 40:                      # a short follow-up ("continue") keeps the current plan
        return
    REQUEST, PLAN = text, None
    mode = eff_mode("plan")
    EMIT("tool_call", name="advisor_plan", args={"advisor": mode})
    reply, err = _ask_mode("%s\n\nTOOLS:\n%s\n\nUSER REQUEST:\n%s" % (PLAN_PROMPT, catalog(), REQUEST), mode)
    audit("advisor_plan", mode=mode, ok=not err, reason=err, reply_chars=len(reply))
    if err or not reply:
        EMIT("tool_result", name="advisor_plan", result="(no plan: %s)" % (err or "empty reply"), images=[])
        return
    try:
        plan = json.loads(reply[reply.index("{"):reply.rindex("}") + 1])
        if not isinstance(plan, dict):
            raise ValueError
    except ValueError:
        EMIT("tool_result", name="advisor_plan", result="(plan was not valid JSON; used as plain advice)\n" + reply[:2500], images=[])
        msgs.append({"role": "user", "content": "ADVISOR PLAN (advice only):\n" + reply[:3000]})
        return
    PLAN = plan
    exp = plan.get("expected") or {}
    body = ("Steps:\n" + "\n".join("%d. %s" % (k + 1, x) for k, x in enumerate(plan.get("steps") or [])) +
            "\nPitfalls:\n" + "\n".join("- %s" % x for x in (plan.get("pitfalls") or [])) +
            "\nExpected final result (the advisor's own calculation): volume_mm3=%s, bbox_size_mm=%s" % (exp.get("volume_mm3"), exp.get("bbox_size_mm")))
    EMIT("tool_result", name="advisor_plan", result=body, images=[])
    msgs.append({"role": "user", "content": "ADVISOR PLAN (advice only; verify each step on the real part). The harness will compare the final part with the "
                 "expected numbers below. If you are certain an expected number is wrong, do not bend the part to it: say so with the arithmetic.\n" + body})


def gate(final_text):
    """Before the agent may say 'done': compare the real part with the plan's numbers (by code). On mismatch, consult the advisor with the facts."""
    global gates_used
    if ADVISOR_MODE == "off" or not PLAN or gates_used >= GATE_MAX:
        return None
    exp = PLAN.get("expected") or {}
    ev, eb = exp.get("volume_mm3"), exp.get("bbox_size_mm")
    snap, vol, bbox = state_snapshot()
    bad = []
    if isinstance(ev, (int, float)) and not isinstance(ev, bool) and ev > 0 and vol is not None and abs(vol - ev) / ev > VOL_TOL:
        bad.append("volume %.1f mm3 measured vs %.1f expected (%+.2f%%)" % (vol, ev, 100 * (vol - ev) / ev))
    if isinstance(eb, list) and len(eb) == 3 and bbox and all(isinstance(x, (int, float)) for x in eb):
        if any(abs(a - b) > BBOX_TOL_MM for a, b in zip(sorted(bbox), sorted(float(x) for x in eb))):
            bad.append("bounding box %s measured vs %s expected" % ([round(x, 2) for x in sorted(bbox)], sorted(eb)))
    if not bad:
        return None
    gates_used += 1
    mode = eff_mode("gate")
    EMIT("tool_call", name="advisor_check", args={"mismatch": bad, "advisor": mode})
    q = ("A CAD executor says it is finished, but the numbers do not match your plan.\n\nORIGINAL USER REQUEST:\n%s\n\nYOUR EXPECTED RESULT: %s\n\n"
         "MEASURED (by the harness): %s\n\nCURRENT STATE:\n%s\n\nEXECUTOR'S FINAL MESSAGE:\n%s\n\nDecide which is wrong: the part or your expectation "
         "(recompute it). If the part is wrong, say what is most likely wrong and give the next 1-3 concrete tool steps. Be brief."
         % (REQUEST, json.dumps(exp), "; ".join(bad), snap, (final_text or "")[:1500]))
    text, err = _ask_mode(q, mode)
    audit("advisor_check", mode=mode, mismatch=bad, ok=not err, reason=err)
    advice = text if text else "(advisor unavailable: %s)" % err
    EMIT("tool_result", name="advisor_check", result="; ".join(bad) + "\n\n" + advice[:3000], images=[])
    return ("VERIFICATION FAILED: the real part does not match what the request implies: %s.\nAdvisor analysis (advice, NOT executed): %s\n"
            "Read the current state, find the cause, fix it and verify again. If you are certain the expected value is wrong, do not change the part: "
            "explain with the arithmetic in your final answer." % ("; ".join(bad), advice[:2500]))


# ---- front-end hooks: the console below is the default; ui_server.py replaces them to drive a web page -------------------
def _console_emit(kind, **kw):
    if kind == "text":
        print(kw["text"], end="", flush=True)
    elif kind == "text_end":
        print(flush=True)
    elif kind in ("info", "step_limit"):
        print(kw["text"], flush=True)
    elif kind == "tool_call":
        print(f"  [solidworks] {kw['name']}({json.dumps(kw['args'], ensure_ascii=False)[:160]})", flush=True)


def _console_confirm(name, args):
    return input(f"  ?? model wants {name}({json.dumps(args, ensure_ascii=False)[:200]}) - allow? [y/N] ").strip().lower() == "y"


AUTO_ALLOW = str(os.environ.get("AICF_AUTO_ALLOW") or CFG.get("auto_allow") or "").lower() in ("1", "true", "yes", "on")
PATH_KEYS = ("path", "file", "file_path", "filename", "output", "output_path", "save_path")


def needs_human(name, args):
    """In auto-allow mode a call still goes to the person when it would write or open a file OUTSIDE the user's own profile
    folder and the data folder (system folders, other users, network shares)."""
    if name not in ("save_part", "export", "open_part"):
        return False
    homes = [os.path.realpath(os.path.expanduser("~")), os.path.realpath(aicfconf.data_dir(CFG))]
    for k in PATH_KEYS:
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            p = os.path.realpath(os.path.expandvars(v))
            if not any(os.path.commonpath([p, h]) == h for h in homes if os.path.splitdrive(p)[0].lower() == os.path.splitdrive(h)[0].lower()):
                return True
    return False


EMIT = _console_emit              # EMIT(kind, **data): text | think | text_end | info | tool_call | tool_result | usage
CONFIRM_FN = _console_confirm     # CONFIRM_FN(name, args) -> bool
SHOULD_STOP = lambda: False       # polled between steps; True aborts the turn

env = dict(os.environ, PYTHONIOENCODING="utf-8", DISABLE_TELEMETRY="1", MCP_DISABLE_TELEMETRY="1")
srv = subprocess.Popen([MCP_EXE], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                       encoding="utf-8", env=env)
_id = 0


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


def audit(kind, **kw):
    with open(AUDIT, "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind, **kw}, ensure_ascii=False) + "\n")


rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "aicf-sw-agent", "version": "1"}})
rpc("notifications/initialized", notify=True)


def load_tools():
    r = rpc("tools/list")
    tools = []
    for t in r["result"]["tools"]:
        if t["name"] in ALLOW:
            tools.append({"type": "function", "function": {"name": t["name"], "description": (t.get("description") or "")[:700],
                                                           "parameters": t.get("inputSchema") or {"type": "object", "properties": {}}}})
    return tools


TOOLS = load_tools()


def mcp_call(name, args):
    r = rpc("tools/call", {"name": name, "arguments": args})
    if "error" in r:
        return f"ERROR: {r['error']}", []
    res = r["result"]
    text, imgs = [], []
    for c in res.get("content", []):
        if c.get("type") == "text":
            text.append(c.get("text", ""))
        elif c.get("type") == "image":
            imgs.append("data:%s;base64,%s" % (c.get("mimeType", "image/png"), c["data"]))
    out = "\n".join(text)
    if res.get("isError"):
        out = "ERROR: " + out
    return out, imgs


SYSTEM = """You are a mechanical CAD assistant controlling SolidWorks live through tools (COM). All units are what the tools state (usually mm). You work only with the tools you are given; you have no internet and must never ask for or send design data anywhere.
Rules:
- Start with get_status, then new_part (unless a part is already active). Build step by step: one feature per tool call, with explicit dimensions.
- After features, verify with get_bounding_box, get_mass_properties, list_features and rebuild; compare numbers with what the user asked. Never claim something is correct without a tool result that shows it.
- screenshot: do not worry about a file path (the app chooses it). The picture is shown to the user AND to you: LOOK at it to check your work, and take a second view (view='front'/'right'/'top', or from_dir=[x,y,z]) when one angle cannot show a hole or an opening.
- A cut or hole is only done when it is proven: compare the volume before and after (get_mass_properties) with the volume you expected to remove, and look at a picture (at most 3 verification pictures per task: pick the views that can show the feature, then conclude and report). If the volume drops by clearly less than expected (under about 90 percent) or the picture shows no opening, the cut went the wrong way or only part of the depth: redo it (reverse=True, or omit depth_mm for through-all) and check again. Do not report success on "ok": true alone.
- Any feature that must be open (hole, window, slot, pocket, through-cut) must be clear of neighbouring walls and leave real material around it; state what you verified (volume change, picture) for each such feature.
- Before changing an EXISTING part, read its current state (list_features, get_mass_properties) and compare it with the request: if the request is already satisfied, say so and show the evidence instead of redoing it; if it is partly done, build only the missing part.
- Work in the part's OWN frame: after the first body, read its bounding box and note which axis is its height/length/width; every offset, plane and sketch position must be expressed in that frame and compared with the bounding box before you use it.
- If a tool returns ERROR, read it and retry with changed arguments at most once. If the same error comes back, stop varying numbers: your picture of the situation is wrong (frame, plane position, face, direction, units). Name two possible reasons, check the cheapest one with a read-only query (bounding box, list_faces, the plane origin/normal the tool returned), then act. If still stuck, explain the problem.
- Notes marked "HARNESS CHECK" come from the program, not from SolidWorks: they are measured facts, take them seriously.
- Do not save, export, close or delete unless the user asked; those calls need the person's confirmation anyway.
- Reply to the user in their language (Vietnamese if they write Vietnamese), briefly: what you built, dimensions, and the verification numbers."""


def llm(messages, use_tools=True):
    body = {"model": MODEL, "messages": messages, "max_tokens": 4000, "temperature": 0.3, "stream": True,
            "chat_template_kwargs": {"enable_thinking": think}}
    if use_tools:
        body["tools"] = active_tools()
    req = urllib.request.Request(URL, json.dumps(body).encode(), {"Content-Type": "application/json", "Authorization": "Bearer " + KEY})
    for attempt in range(10):
        try:
            r = urllib.request.urlopen(req, timeout=900)
            break
        except urllib.error.HTTPError as e:
            msg = e.read()[:300]
            if e.code not in (429, 502, 503) or attempt == 9:
                raise RuntimeError(f"LLM HTTP {e.code}: {msg!r}")
            wait = int(e.headers.get("Retry-After") or 5) + 2 * attempt
            EMIT("info", text=f"  (server busy, HTTP {e.code}; retry in {wait}s)")
            time.sleep(wait)
    content, calls = "", {}
    with r:
        for raw in r:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data:") or line == "data: [DONE]":
                continue
            ch = json.loads(line[5:]).get("choices") or []
            if not ch:
                continue
            d = ch[0].get("delta", {})
            rt = d.get("reasoning_content") or d.get("reasoning") or ""
            if rt:
                EMIT("think", text=rt)
            t = d.get("content") or ""
            if t:
                content += t
                EMIT("text", text=t)
            for tc in d.get("tool_calls") or []:
                c = calls.setdefault(tc.get("index", 0), {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                c["id"] = tc.get("id") or c["id"]
                c["function"]["name"] += tc.get("function", {}).get("name") or ""
                c["function"]["arguments"] += tc.get("function", {}).get("arguments") or ""
    EMIT("text_end")
    msg = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = [calls[k] for k in sorted(calls)]
    return msg


def trim_images(msgs, keep=2):
    seen = 0
    for m in reversed(msgs):
        if isinstance(m.get("content"), list):
            seen += 1
            if seen > keep:
                m["content"] = [p for p in m["content"] if p.get("type") != "image_url"] or "[older screenshot removed]"


def run_screenshot(args):
    """solidworks-mcp's screenshot only WRITES a file (it returns no image). So: the app picks the path (the model may not choose
    where to write), then reads the PNG back so it is shown in the chat and given to the model to look at."""
    import base64
    d = os.path.join(aicfconf.data_dir(CFG), "shots")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "shot-%s.png" % time.strftime("%Y%m%d-%H%M%S"))
    args = {k: v for k, v in args.items() if k in ("view", "zoom_mm", "show_planes", "from_dir")}
    out, imgs = mcp_call("screenshot", dict(args, path=path))
    try:
        if os.path.getsize(path) <= 4_000_000:
            with open(path, "rb") as f:
                imgs = imgs + ["data:image/png;base64," + base64.b64encode(f.read()).decode()]
    except OSError:
        pass
    return out, imgs


def run_tool(name, args):
    if name == "ask_advisor":
        return (advise(args) if ADVISOR_MODE != "off" else "ERROR: the advisor is switched off."), []
    if name not in ALLOW:
        return f"ERROR: tool {name} is not allowed", []
    if name in CONFIRM:
        if AUTO_ALLOW and not needs_human(name, args):
            audit("auto_allowed", tool=name, args=args)
            EMIT("info", text="  (auto-allowed: %s)" % name)
        elif not CONFIRM_FN(name, args):
            audit("denied", tool=name, args=args)
            return "DENIED by the user. Do not retry; continue without it.", []
    out, imgs = run_screenshot(args) if name == "screenshot" else mcp_call(name, args)
    audit("tool", tool=name, args=args, result=out[:1500], images=len(imgs))
    return out, imgs


ADVISOR_NUDGE = ("The same kind of step has now failed twice in a row. Before trying it a third time, call ask_advisor ONCE: give it the goal, "
                 "what you tried, the exact error text and the key numbers, then apply what is sensible and verify it on the real part.")


def part_bbox():
    """(min, max) of the active part from SolidWorks, or None. Read-only."""
    try:
        out, _ = mcp_call("get_mass_properties", {})
        b = json.loads(out)["mass_properties"]["bounding_box_mm"]
        return [float(x) for x in b["min_mm"]], [float(x) for x in b["max_mm"]]
    except Exception:
        return None


def annotate(name, args, out):
    """Generic geometry preflight, done by code: a sketch whose profile lies entirely outside the part's bounding box can never cut or join the
    part, however the numbers are tweaked. Tell the model that as a measured fact the moment it happens (not after N failed cuts)."""
    if name != "add_sketch" or not out.lstrip().startswith("{"):
        return out
    try:
        d = json.loads(out)
        if not d.get("ok"):
            return out
        o, xa, ya, pts = d["origin_mm"], d["x_axis"], d["y_axis"], d["points_mm"]
        world = [[o[k] + p[0] * xa[k] + p[1] * ya[k] for k in range(3)] for p in pts]
        lo, hi = [min(w[k] for w in world) for k in range(3)], [max(w[k] for w in world) for k in range(3)]
    except (ValueError, KeyError, TypeError, IndexError):
        return out
    bb = part_bbox()
    if not bb:
        return out
    gap = [max(bb[0][k] - hi[k], lo[k] - bb[1][k], 0.0) for k in range(3)]
    if max(gap) <= 0.01:
        return out
    k = max(range(3), key=lambda i: gap[i])
    return out + ("\nHARNESS CHECK: this sketch lies entirely OUTSIDE the part: along %s its profile spans %.1f..%.1f mm but the part spans %.1f..%.1f mm "
                  "(gap %.1f mm). A cut or extrusion from it cannot touch the part. Move the sketch plane/profile so it passes through the part "
                  "(compare with the part's bounding box and the plane origin/normal) instead of retrying the same operation." %
                  ("XYZ"[k], lo[k], hi[k], bb[0][k], bb[1][k], gap[k]))


def error_signature(name, out):
    """Same tool + same error text (numbers blurred) = same signature."""
    if not failed(out):
        return None
    return name + "|" + re.sub(r"[-+]?\d+(?:\.\d+)?", "#", out.replace("\n", " "))[:140]


DIAGNOSE_NUDGE = ("The same error has come back. Do NOT retry with only different numbers. Write down two possible reasons why the tool rejects your input "
                  "(frame/axis, plane or sketch position relative to the part, face or direction, units, a feature that already changed the part), "
                  "check the cheapest one with a read-only query (get_bounding_box / get_mass_properties, list_faces, list_features, the origin and "
                  "normal the tool returned), and only then act.")


def failed(out):
    return out.startswith("ERROR") or '"ok": false' in out[:60]


LOOP_WINDOW = 8     # look at the last 8 tool calls
NUDGE = ("You seem to be going in circles (same few tools again and again, or creating and deleting the same things). Stop. "
         "Read the CURRENT state of the part (list_features, get_mass_properties), compare it with the request, and decide: "
         "if the request is already satisfied, say so with the evidence; if not, do ONE different, concrete action. "
         "If you cannot make progress, report what is blocking you.")


def circling(recent):
    """General, tool-agnostic signs of a model that is not progressing: very few distinct tools over a window, or repeated
    delete_feature (undoing its own work)."""
    if len(recent) >= 4 and recent[-4:].count("delete_feature") >= 2:     # build / delete / build / delete: undoing its own work
        return True
    w = recent[-LOOP_WINDOW:]
    if len(w) < LOOP_WINDOW:
        return False
    return len(set(w)) <= 3 or w.count("delete_feature") >= 3 or max(w.count(n) for n in set(w)) >= LOOP_WINDOW - 1


FINAL_PROMPT = ("Stop calling tools now. Write your final answer for the user: what you did, what you VERIFIED (with the numbers or "
                "pictures that prove it), what you could NOT verify, and what the user should check. Be honest about uncertainty.")


def repair(msgs):
    """After an interrupted turn (LLM error, MCP crash) make the history valid again: every tool call needs a tool reply."""
    answered = {m.get("tool_call_id") for m in msgs if m.get("role") == "tool"}
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].get("role") == "assistant" and msgs[i].get("tool_calls"):
            missing = [c["id"] for c in msgs[i]["tool_calls"] if c["id"] not in answered]
            msgs[i + 1:i + 1] = [{"role": "tool", "tool_call_id": cid, "content": "ERROR: interrupted before this call finished."} for cid in missing]
            break


def final_answer(msgs, why):
    """One last model call with tools switched off, so a long or looping task always ends with a report, never silence."""
    EMIT("info", text="  (%s: asking for a final report)" % why)
    msgs.append({"role": "user", "content": FINAL_PROMPT})
    m = llm(msgs, use_tools=False)
    msgs.append({k: v for k, v in m.items() if k in ("role", "content")})


def turn(msgs):
    global adv_calls
    adv_calls = 0
    start_request(msgs)
    recent, nudged = [], False
    streak, adv_nudges = {}, 0
    sigs, diag_sent, pending_note = {}, False, None
    for _ in range(MAX_STEPS):
        if SHOULD_STOP():
            EMIT("info", text="  (stopped by the user)")
            return
        trim_images(msgs)
        m = llm(msgs)
        msgs.append({k: v for k, v in m.items() if k in ("role", "content", "tool_calls")})
        calls = m.get("tool_calls") or []
        if not calls:
            again = gate(m.get("content") or "")
            if again:
                msgs.append({"role": "user", "content": again})
                continue
            return
        pending_imgs = []
        for c in calls:
            name = c["function"]["name"]
            try:
                args = json.loads(c["function"]["arguments"] or "{}")
            except Exception as e:
                msgs.append({"role": "tool", "tool_call_id": c["id"], "content": f"ERROR: bad JSON arguments ({e}); resend with valid JSON."})
                continue
            if SHOULD_STOP():
                msgs.append({"role": "tool", "tool_call_id": c["id"], "content": "Skipped: the user pressed stop."})
                continue
            EMIT("tool_call", name=name, args=args)
            out, imgs = run_tool(name, args)
            out = annotate(name, args, out)
            sig = error_signature(name, out)
            if sig:
                sigs[sig] = sigs.get(sig, 0) + 1
                if sigs[sig] == 2 and not diag_sent:
                    diag_sent = True
                    EMIT("info", text="  (same error twice: the agent is asked to diagnose instead of retrying)")
                    pending_note = DIAGNOSE_NUDGE
            EMIT("tool_result", name=name, result=out, images=imgs)
            msgs.append({"role": "tool", "tool_call_id": c["id"], "content": out[:MAX_TOOL_CHARS] or "(no output)"})
            pending_imgs += imgs
            recent.append(name)
            if name != "ask_advisor":
                streak[name] = streak.get(name, 0) + 1 if failed(out) else 0
        if pending_imgs:
            msgs.append({"role": "user", "content": [{"type": "text", "text": "Screenshot(s) from SolidWorks:"}] +
                         [{"type": "image_url", "image_url": {"url": u}} for u in pending_imgs]})
        if pending_note:
            msgs.append({"role": "user", "content": pending_note})
            pending_note = None
        if ADVISOR_MODE != "off" and adv_calls == 0 and adv_nudges < 1 and any(v >= 2 for v in streak.values()):
            adv_nudges += 1
            EMIT("info", text="  (two failures in a row: the agent is asked to consult the advisor)")
            msgs.append({"role": "user", "content": ADVISOR_NUDGE})
        if circling(recent):
            if nudged:
                return final_answer(msgs, "still circling after a warning")
            nudged = True
            recent.clear()
            EMIT("info", text="  (the agent is going in circles: sent a warning)")
            msgs.append({"role": "user", "content": NUDGE})
    if not SHOULD_STOP():
        EMIT("step_limit", text="  (step limit reached: %d steps; say 'continue' to let it go on)" % MAX_STEPS, steps=MAX_STEPS)
        final_answer(msgs, "step limit")


def main():
    global think
    msgs = [{"role": "system", "content": SYSTEM}]
    print(f"SolidWorks agent ready: {len(TOOLS)} tools exposed, model {MODEL}. /exit to quit.")
    if "--list-tools" in sys.argv:
        print(", ".join(t["function"]["name"] for t in TOOLS))
        return
    while True:
        try:
            u = input("\nbạn> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not u:
            continue
        if u == "/exit":
            break
        if u == "/reset":
            msgs = [{"role": "system", "content": SYSTEM}]
            continue
        if u.startswith("/think"):
            think = u.endswith("on")
            continue
        if u == "/tools":
            print(", ".join(t["function"]["name"] for t in TOOLS))
            continue
        msgs.append({"role": "user", "content": u})
        audit("user", text=u)
        try:
            turn(msgs)
        except Exception as e:
            print(f"ERROR: {e}")
    srv.terminate()


if __name__ == "__main__":
    main()
