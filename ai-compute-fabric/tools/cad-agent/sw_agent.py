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
- If a tool returns ERROR, read it, change the arguments and retry (at most 2 retries for the same step), then explain the problem.
- Do not save, export, close or delete unless the user asked; those calls need the person's confirmation anyway.
- Reply to the user in their language (Vietnamese if they write Vietnamese), briefly: what you built, dimensions, and the verification numbers."""


def llm(messages):
    body = {"model": MODEL, "messages": messages, "tools": TOOLS, "max_tokens": 4000, "temperature": 0.3, "stream": True,
            "chat_template_kwargs": {"enable_thinking": think}}
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


def turn(msgs):
    for _ in range(MAX_STEPS):
        if SHOULD_STOP():
            EMIT("info", text="  (stopped by the user)")
            return
        trim_images(msgs)
        m = llm(msgs)
        msgs.append({k: v for k, v in m.items() if k in ("role", "content", "tool_calls")})
        calls = m.get("tool_calls") or []
        if not calls:
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
            EMIT("tool_result", name=name, result=out, images=imgs)
            msgs.append({"role": "tool", "tool_call_id": c["id"], "content": out[:MAX_TOOL_CHARS] or "(no output)"})
            pending_imgs += imgs
        if pending_imgs:
            msgs.append({"role": "user", "content": [{"type": "text", "text": "Screenshot(s) from SolidWorks:"}] +
                         [{"type": "image_url", "image_url": {"url": u}} for u in pending_imgs]})
    EMIT("step_limit", text="  (step limit reached: %d steps; say 'continue' to let it go on)" % MAX_STEPS, steps=MAX_STEPS)


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
