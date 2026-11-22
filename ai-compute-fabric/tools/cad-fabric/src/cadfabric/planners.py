"""Planners: who writes the plan whose volume formulas the harness evaluates.
  off          no plan (the executor works alone)
  nothink      the gateway model, thinking off (fast; plan quality = the model's)
  think        the gateway model, thinking on (slow; can exceed gateway request limits on hard prompts)
  codex        the Codex CLI on THIS machine, text only, run in an empty temp dir with a read-only sandbox. The question leaves the machine.
  codex-relay  the Codex CLI on another machine over ssh (config relay.host / relay.codex); credentials only from the ssh agent/keys or $SSHPASS.
A planner failure never loses the plan: it falls back to `nothink` and says so in the raw text."""
import json, os, re, shutil, subprocess, tempfile
from . import advisors, verifier as V
from .cq_backend import NOWIN

PREFIX = ("You are an advisor for a CAD automation agent. Answer ONLY from the text below, in plain text, concisely. "
          "Do not run any command, do not read or write any file, do not browse.\n\n")
ENV_NOTE = ("TOOLS: execute_cadquery(code) runs CadQuery code and returns the measured volume, bounding box, centre of mass and topology counts; undo(). "
            "There are no named CAD operations; every step is a short piece of CadQuery code (extrude, revolve, cut, hole, shell, fillet, ...).")


def extract_json(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def plan_prompt(request):
    return "%s\n\n%s\n\nUSER REQUEST:\n%s" % (V.PLAN_PROMPT, ENV_NOTE, request)


def find_codex(cfg):
    for c in (os.environ.get("CADFABRIC_CODEX"), cfg.get("codex_bin"), shutil.which("codex")):
        if c and (os.path.isfile(c) or shutil.which(c)):
            return c
    if os.name == "nt":
        import glob
        hits = sorted(glob.glob(os.path.join(os.environ.get("LOCALAPPDATA", ""), "OpenAI", "Codex", "bin", "*", "codex.exe")), key=os.path.getmtime)
        if hits:
            return hits[-1]
    return None


def ask_codex(prompt, cfg, timeout=300, raw=False):
    """-> (text, err). Codex local."""
    exe = find_codex(cfg)
    if not exe:
        return "", "codex CLI not found (install it and log in, or set codex_bin)"
    try:
        with tempfile.TemporaryDirectory(prefix="cadfabric-codex-") as wd:
            r = subprocess.run([exe, "exec", "--ephemeral", "--skip-git-repo-check", "-s", "read-only", "-C", wd, "-"], input=(prompt[:30000] if raw else PREFIX + prompt[:30000]),
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, cwd=wd, **NOWIN)
    except subprocess.TimeoutExpired:
        return "", "codex timed out after %ds" % timeout
    except OSError as e:
        return "", "codex could not start (%s)" % type(e).__name__
    return _result(r)


def ask_relay(prompt, cfg, timeout=300):
    """-> (text, err). Codex on another machine: `ssh host <codex> exec ... -` with the prompt on stdin (no files copied, nothing stored remotely)."""
    rel = cfg.get("relay") or {}
    if not rel.get("host") or not rel.get("workdir"):
        return "", "relay.host and relay.workdir (an EMPTY folder on the remote machine, so Codex has nothing to read) must be configured"
    cmd = '"%s" exec --ephemeral --skip-git-repo-check -s read-only -C "%s" -' % (rel.get("codex") or "codex", rel["workdir"])
    argv = (["sshpass", "-e"] if os.environ.get("SSHPASS") and shutil.which("sshpass") else []) + \
           ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15", rel["host"], cmd]
    try:
        r = subprocess.run(argv, input=PREFIX + prompt[:30000], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, **NOWIN)
    except subprocess.TimeoutExpired:
        return "", "codex relay timed out after %ds" % timeout
    except OSError as e:
        return "", "relay could not start (%s)" % type(e).__name__
    return _result(r)


def _result(r):
    out = (r.stdout or "").strip()
    if r.returncode != 0 or not out:
        return "", "codex failed (exit %d: %s)" % (r.returncode, ((r.stderr or "").strip().splitlines() or [""])[-1][:120])
    return out, ""


def make_plan(llm, request, mode, cfg=None):
    """-> (spec or None, raw text)."""
    cfg = cfg or llm.cfg
    note = ""
    adv = advisors.find(cfg, mode)
    if adv or mode in ("codex", "codex-relay"):
        text, err = advisors.ask(adv, plan_prompt(request), cfg) if adv else (ask_codex if mode == "codex" else ask_relay)(plan_prompt(request), cfg)
        plan = extract_json(text) if text else None
        if plan:
            return V.parse_spec(plan), text
        note = "(%s failed: %s; used nothink) " % (mode, err or "no JSON in the reply")
        mode = "nothink"
    msg = llm.chat([{"role": "user", "content": plan_prompt(request)}], think=(mode == "think"), max_tokens=6000 if mode == "think" else 3000, temperature=0.2)
    plan = extract_json(msg["content"])
    return (V.parse_spec(plan) if plan else None), note + msg["content"]
