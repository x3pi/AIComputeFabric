"""User-defined advisors: who writes the verification plan. Two kinds, both TEXT ONLY (the advisor never gets tools or files):
  api  an HTTP endpoint: `openai` (any OpenAI-compatible /chat/completions) or `anthropic` (Messages API). The key lives in its own file.
  cli  a command line program on THIS machine. Presets: Codex CLI, Claude Code CLI (flags checked against the real CLIs), or a custom command.
       Run without a shell, in an EMPTY temporary directory, prompt on stdin (or in a {prompt} argument), with a timeout.
An advisor flagged `external` sends the request text to a third party, so the UI asks for confirmation before every use."""
import json, os, re, shlex, shutil, subprocess, tempfile, time, urllib.error, urllib.parse, urllib.request
from .cq_backend import NOWIN

BUILTIN = ("off", "nothink", "think", "codex", "codex-relay")
BUILTIN_LABELS = {"off": "Không cố vấn", "nothink": "Qwen lập kế hoạch (nhanh)", "think": "Qwen suy nghĩ sâu (chậm)", "codex": "Codex trên máy này", "codex-relay": "Codex trên máy khác (ssh)"}
BUILTIN_EXTERNAL = ("codex", "codex-relay")

# model_flag / effort_flag: how the CLI takes a model and a reasoning effort (checked against `codex exec --help` and `claude --help`).
# `at`: where the extra flags go (codex needs the final "-" to stay last, so they go right after "exec").
CLI_PRESETS = {
    "codex": {"label": "Codex CLI", "exe": "codex", "args": ["exec", "--ephemeral", "--skip-git-repo-check", "-s", "read-only", "-C", "{cwd}", "-"],
              "model_flag": ["-m"], "effort_flag": lambda e: ["-c", 'model_reasoning_effort="%s"' % e], "efforts": ("low", "medium", "high"), "at": 1, "models": ()},
    "claude": {"label": "Claude Code CLI", "exe": "claude", "args": ["-p", "--tools", "", "--no-session-persistence", "--disable-slash-commands", "--strict-mcp-config",
                                                                      "--output-format", "text"],
               "model_flag": ["--model"], "effort_flag": lambda e: ["--effort", e], "efforts": ("low", "medium", "high", "xhigh", "max"), "at": None,
               "models": ("haiku", "sonnet", "opus")},
}
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@\-]{0,79}$")
API_KINDS = ("openai", "anthropic")
PREFIX = ("You are an advisor for a CAD automation agent. Answer ONLY from the text below, in plain text, concisely. "
          "Do not run any command, do not read or write any file, do not browse.\n\n")
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}$")


def find_exe(name, cfg=None):
    """Resolve a CLI program: PATH first, then (for codex) the desktop app's folder on Windows."""
    if name == "codex":
        from . import planners
        return planners.find_codex(cfg or {})
    p = shutil.which(name)
    if p:
        return p
    if os.name == "nt" and name == "claude":
        for c in (os.path.join(os.environ.get("USERPROFILE", ""), ".local", "bin", "claude.exe"), os.path.join(os.environ.get("APPDATA", ""), "npm", "claude.cmd")):
            if os.path.isfile(c):
                return c
    return None


def presets_info():
    """What the UI needs to build the model / effort pickers."""
    return {k: {"label": v["label"], "efforts": list(v["efforts"]), "models": list(v["models"])} for k, v in CLI_PRESETS.items()}


def detected(cfg=None):
    return {k: find_exe(v["exe"], cfg) for k, v in CLI_PRESETS.items()}


def is_private_host(url):
    try:
        h = urllib.parse.urlparse(url).hostname or ""
    except ValueError:
        return False
    if h in ("localhost", "::1") or h.endswith(".local"):
        return True
    m = re.match(r"^(\d+)\.(\d+)\.\d+\.\d+$", h)
    return bool(m and (m.group(1) in ("10", "127") or (m.group(1) == "192" and m.group(2) == "168") or (m.group(1) == "172" and 16 <= int(m.group(2)) <= 31)))


def validate(a):
    """-> (clean advisor dict, error). Raises nothing. `key` is not part of the dict (it goes to its own file)."""
    if not isinstance(a, dict):
        return None, "advisor must be an object"
    aid = str(a.get("id", "")).strip().lower()
    if not _ID.match(aid) or aid in BUILTIN:
        return None, "advisor id must be 1-24 chars of a-z 0-9 _ - and not a built-in name"
    label = str(a.get("label", "")).strip() or aid
    if len(label) > 40 or re.search(r"[\x00-\x1f<>]", label):
        return None, "advisor name is not valid"
    out = {"id": aid, "label": label, "type": a.get("type")}
    if out["type"] == "api":
        kind, url = a.get("kind") or "openai", str(a.get("url", "")).strip()
        if kind not in API_KINDS:
            return None, "api kind must be openai or anthropic"
        if not re.match(r"^https?://[^\s\"'<>]{1,300}$", url):
            return None, "advisor URL must start with http:// or https://"
        model = str(a.get("model", "")).strip()
        if len(model) > 120 or re.search(r"[\s\"'<>]", model) or (kind == "anthropic" and not model):
            return None, "model name is not valid (the Anthropic API needs one)"
        out.update(kind=kind, url=url, model=model, key_file=a.get("key_file") or None)
        out["external"] = bool(a["external"]) if "external" in a else not is_private_host(url)
    elif out["type"] == "cli":
        preset = a.get("preset") or "custom"
        if preset not in CLI_PRESETS and preset != "custom":
            return None, "unknown CLI preset"
        out["preset"] = preset
        if preset != "custom":
            model, effort = str(a.get("model", "") or "").strip(), str(a.get("effort", "") or "").strip()
            if model and not _MODEL.match(model):
                return None, "model name is not valid"
            if effort and effort not in CLI_PRESETS[preset]["efforts"]:
                return None, "effort must be one of: " + ", ".join(CLI_PRESETS[preset]["efforts"])
            out.update({k: v for k, v in (("model", model), ("effort", effort)) if v})
        if preset == "custom":
            cmd = str(a.get("command", "")).strip()
            if not cmd or len(cmd) > 500 or re.search(r"[\x00\r\n]", cmd):
                return None, "custom command is empty or contains control characters"
            try:
                parts = shlex.split(cmd, posix=(os.name != "nt"))
            except ValueError as e:
                return None, "custom command cannot be parsed (%s)" % e
            if not parts or parts[0].startswith("-"):
                return None, "custom command must start with a program"
            out["command"] = cmd
        out["external"] = bool(a["external"]) if "external" in a else True        # a CLI normally talks to a cloud service
    else:
        return None, "advisor type must be api or cli"
    return out, ""


def validate_list(lst):
    if not isinstance(lst, list) or len(lst) > 12:
        return None, "advisors must be a list of at most 12"
    out, seen = [], set()
    for a in lst:
        c, err = validate(a)
        if err:
            return None, "%s: %s" % ((a or {}).get("id", "?") if isinstance(a, dict) else "?", err)
        if c["id"] in seen:
            return None, "duplicate advisor id " + c["id"]
        seen.add(c["id"]); out.append(c)
    return out, ""


def find(cfg, aid):
    for a in (cfg or {}).get("advisors") or []:
        if a.get("id") == aid:
            return a
    return None


def names(cfg):
    return list(BUILTIN) + [a["id"] for a in (cfg.get("advisors") or [])]


def is_external(cfg, aid):
    a = find(cfg, aid)
    return aid in BUILTIN_EXTERNAL if a is None else bool(a.get("external"))


def meta(cfg):
    """What the UI lists: built-ins first, then the user's advisors."""
    out = [{"id": b, "label": BUILTIN_LABELS[b], "external": b in BUILTIN_EXTERNAL, "builtin": True} for b in BUILTIN]
    for a in cfg.get("advisors") or []:
        d = {"id": a["id"], "label": a["label"], "external": bool(a.get("external")), "builtin": False, "type": a["type"]}
        d.update({k: a[k] for k in ("kind", "url", "model", "effort", "preset", "command") if a.get(k)})
        d["has_key"] = bool(a.get("key_file") and os.path.isfile(os.path.expanduser(a["key_file"])))
        out.append(d)
    return out


def _read_key(a):
    p = a.get("key_file")
    if not p:
        return ""
    try:
        with open(os.path.expanduser(p), encoding="utf-8-sig") as f:
            return f.read().strip()
    except OSError:
        return ""


def ask(a, prompt, cfg=None, timeout=300, key=None):
    """-> (text, error). Never raises."""
    try:
        if a["type"] == "api":
            return _ask_api(a, prompt, timeout, key if key is not None else _read_key(a))
        return _ask_cli(a, prompt, cfg, timeout)
    except Exception as e:                                                   # noqa: BLE001 - an advisor must never crash the agent
        return "", "%s: %s" % (type(e).__name__, str(e)[:160])


def _ask_api(a, prompt, timeout, key):
    text = PREFIX + prompt[:30000]
    hdr = {"Content-Type": "application/json", "User-Agent": "cadfabric"}
    if a["kind"] == "anthropic":
        if key:
            hdr["x-api-key"] = key
        hdr["anthropic-version"] = "2023-06-01"
        body = {"model": a["model"], "max_tokens": 4096, "messages": [{"role": "user", "content": text}]}
    else:
        if key:
            hdr["Authorization"] = "Bearer " + key
        body = {"messages": [{"role": "user", "content": text}], "max_tokens": 3000, "temperature": 0.2}
        if a.get("model"):
            body["model"] = a["model"]
    try:
        with urllib.request.urlopen(urllib.request.Request(a["url"], json.dumps(body).encode(), hdr), timeout=timeout) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return "", "HTTP %d from %s: %s" % (e.code, urllib.parse.urlparse(a["url"]).netloc, e.read()[:160].decode("utf-8", "replace"))
    except urllib.error.URLError as e:
        return "", "cannot reach %s (%s)" % (a["url"], e.reason)
    if a["kind"] == "anthropic":
        out = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
    else:
        out = ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    return (out.strip(), "") if out.strip() else ("", "the advisor returned no text")


def cli_argv(a, cfg, cwd):
    """-> (argv, prompt_in_args). Pure function (tested)."""
    if a["preset"] in CLI_PRESETS:
        p = CLI_PRESETS[a["preset"]]
        exe = find_exe(p["exe"], cfg)
        if not exe:
            raise RuntimeError("%s not found on this machine (install it and log in)" % p["label"])
        base = [x.replace("{cwd}", cwd) for x in p["args"]]
        extra = (p["model_flag"] + [a["model"]] if a.get("model") else []) + (p["effort_flag"](a["effort"]) if a.get("effort") else [])
        at = len(base) if p["at"] is None else p["at"]
        return [exe] + base[:at] + extra + base[at:], False
    parts = shlex.split(a["command"], posix=(os.name != "nt"))
    exe = shutil.which(parts[0]) or (parts[0] if os.path.isfile(parts[0]) else None)
    if not exe:
        raise RuntimeError("program %r not found" % parts[0])
    in_args = any("{prompt}" in x for x in parts)
    return [exe] + [x.replace("{cwd}", cwd) for x in parts[1:]], in_args


def _ask_cli(a, prompt, cfg, timeout):
    text = PREFIX + prompt[:30000]
    with tempfile.TemporaryDirectory(prefix="cadfabric-adv-") as wd:
        argv, in_args = cli_argv(a, cfg, wd)
        if in_args:
            argv = [x.replace("{prompt}", text) for x in argv]
        try:
            r = subprocess.run(argv, input=None if in_args else text, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, cwd=wd,
                               stdin=subprocess.DEVNULL if in_args else None, **NOWIN)
        except subprocess.TimeoutExpired:
            return "", "timed out after %ds" % timeout
    out = (r.stdout or "").strip()
    if r.returncode != 0 or not out:
        why = ((r.stderr or "").strip().splitlines() or (r.stdout or "").strip().splitlines() or ["no output"])[-1]       # some CLIs explain on stdout
        return "", "exit %d: %s" % (r.returncode, why[:160])
    return out, ""


def self_test(a, cfg=None, key=None, timeout=90):
    """Ask the advisor a trivial question. -> {ok, detail, sec}."""
    t = time.time()
    text, err = ask(a, "Reply with the single word OK.", cfg, timeout, key)
    return {"ok": bool(text), "detail": ("answered: %r" % text[:40]) if text else err, "sec": round(time.time() - t, 1)}
