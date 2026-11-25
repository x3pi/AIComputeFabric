"""User-defined advisors: who writes the verification plan. Two kinds, both TEXT ONLY (the advisor never gets tools or files):
  api  an HTTP endpoint: `openai` (any OpenAI-compatible /chat/completions) or `anthropic` (Messages API). The key lives in its own file.
  cli  a command line program on THIS machine. Presets: Codex CLI, Claude Code CLI (flags checked against the real CLIs), or a custom command.
       Run without a shell, in an EMPTY temporary directory, prompt on stdin (or in a {prompt} argument), with a timeout.
An advisor flagged `external` sends the request text to a third party, so the UI asks for confirmation before every use."""
import base64, json, os, re, shlex, shutil, subprocess, tempfile, time, urllib.error, urllib.parse, urllib.request
from .cq_backend import NOWIN

BUILTIN = ("off", "qwen", "nothink", "think", "codex", "agy", "codex-relay")
BUILTIN_LABELS = {"off": "Không cố vấn", "qwen": "Qwen API (gateway mặc định)", "nothink": "Qwen lập kế hoạch (nhanh)", "think": "Qwen suy nghĩ sâu (chậm)", "codex": "Codex trên máy này", "agy": "Antigravity CLI trên máy này", "codex-relay": "Codex trên máy khác (ssh)"}
BUILTIN_EXTERNAL = ("codex", "agy", "codex-relay")

# model_flag / effort_flag: how the CLI takes a model and a reasoning effort (checked against `codex exec --help`, `claude --help`, and `agy --help`).
# `at`: where the extra flags go (codex needs the final "-" to stay last, so they go right after "exec").
CLI_PRESETS = {
    "codex": {"label": "Codex CLI", "exe": "codex", "args": ["exec", "--ephemeral", "--skip-git-repo-check", "-s", "read-only", "-C", "{cwd}", "-"],
              "web_args": ["--search"], "web_at": 0, "model_flag": ["-m"], "effort_flag": lambda e: ["-c", 'model_reasoning_effort="%s"' % e], "efforts": ("minimal", "low", "medium", "high", "xhigh", "max"), "at": 1, "models": ()},
    "claude": {"label": "Claude Code CLI", "exe": "claude", "args": ["-p", "--tools", "", "--no-session-persistence", "--disable-slash-commands", "--strict-mcp-config",
                                                                      "--output-format", "text"],
               "web_tools": ["--tools", "WebSearch,WebFetch", "--allowedTools", "WebSearch,WebFetch"], "model_flag": ["--model"], "fallback_flag": ["--fallback-model"],
               "effort_flag": lambda e: ["--effort", e], "efforts": ("low", "medium", "high", "xhigh", "max"), "at": None,
               "models": ("haiku", "sonnet", "opus", "fable")},
    "agy": {"label": "Antigravity CLI (agy)", "exe": "agy",
            "args": ["--disable-slash-commands", "--input-format", "stream-json", "--output-format", "stream-json"],
            "model_flag": ["--model"], "effort_flag": lambda e: ["--effort", e],
            "efforts": ("low", "medium", "high", "xhigh", "max"), "at": 0,
            "models": ("gemini-4-neon", "gemini-4-hydrogen", "gemini-4-carbon", "gemini-3.8-flash", "claude-opus-5-5", "claude-sonnet-5-5")},
}
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@\-]{0,79}$")
_EFFORT = re.compile(r"^[a-z][a-z0-9_-]{0,19}$")
_AGY_SUFFIX = re.compile(r"^(.+?)-(minimal|low|medium|high|xhigh|max)$")
ALL_EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max", "think", "nothink")
_AGY_CACHE = {"models": [], "efforts": [], "catalog": []}
API_KINDS = ("openai", "anthropic")
WEB_PREFIX = ("You are an advisor for a CAD automation agent. You MAY search the web or open public web pages to check standards, standard part dimensions or formulas; "
              "name the source in one short line. Do not run any command and do not read or write any local file. Answer in plain text, concisely.\n\n")
PREFIX = ("You are an advisor for a CAD automation agent. Answer ONLY from the text below, in plain text, concisely. "
          "Do not run any command, do not read or write any file, do not browse.\n\n")
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}$")


def find_exe(name, cfg=None):
    """Resolve a CLI program: PATH first, then (for codex/agy) common install folders."""
    if name == "codex":
        from . import planners
        return planners.find_codex(cfg or {})
    if name == "agy":
        cands = [os.environ.get("CADFABRIC_AGY"), shutil.which("agy"),
                 os.path.expanduser("~/.local/bin/agy"), os.path.expanduser("~/.antigravity/bin/agy")]
        if os.name == "nt":
            cands += [os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Antigravity", "bin", "agy.exe"),
                      os.path.join(os.environ.get("USERPROFILE", ""), ".local", "bin", "agy.exe"),
                      os.path.join(os.environ.get("APPDATA", ""), "npm", "agy.cmd")]
        for c in cands:
            if c and (os.path.isfile(c) or shutil.which(c)):
                return c
        return None
    p = shutil.which(name)
    if p:
        return p
    if os.name == "nt" and name == "claude":
        for c in (os.path.join(os.environ.get("USERPROFILE", ""), ".local", "bin", "claude.exe"), os.path.join(os.environ.get("APPDATA", ""), "npm", "claude.cmd")):
            if os.path.isfile(c):
                return c
    return None


def _valid_efforts(raw, dst):
    """Collect valid lowercase effort identifiers while keeping order."""
    out = []
    for item in raw or ():
        s = str((item.get("id") or item.get("effort") or item.get("reasoningEffort")) if isinstance(item, dict) else (item or "")).strip().lower()
        if s and _EFFORT.match(s):
            if s not in out:
                out.append(s)
            if s not in dst:
                dst.append(s)
    return out


def discover_claude_models(home=None):
    """Read Claude CLI's local model-catalog cache (no secrets) + rolling aliases so new releases appear automatically."""
    cdir = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(home or os.path.expanduser("~"), ".claude")
    mc_dir = os.path.join(cdir, "cache", "model-catalog")
    aliases = ["haiku", "sonnet", "opus", "fable"]
    efforts = list(CLI_PRESETS["claude"]["efforts"])
    main_items, other_items, seen = [], [], set()
    if os.path.isdir(mc_dir):
        files = sorted((os.path.join(mc_dir, f) for f in os.listdir(mc_dir) if f.endswith("-cc.json")), key=os.path.getmtime, reverse=True)
        for fp in files:
            try:
                with open(fp, encoding="utf-8") as f:
                    d = json.load(f)
                ms = ((d.get("catalog") or {}).get("config") or {}).get("models") or []
            except (OSError, ValueError, TypeError):
                ms = []
            for m in ms:
                if not isinstance(m, dict):
                    continue
                mid = str(m.get("id") or "").strip()
                if not mid or not _MODEL.match(mid) or mid in seen:
                    continue
                rt = m.get("runtime") or {}
                short = str(m.get("short_name") or rt.get("family") or "").strip().lower()
                if short and _MODEL.match(short) and short not in aliases:
                    aliases.append(short)
                meffs = _valid_efforts(rt.get("effort_levels") or ((m.get("thinking") or {}).get("effort_options") or []), efforts)
                sec = "main" if m.get("section") == "main" else "overflow"
                item = {"id": mid, "label": str(m.get("name") or mid).strip(), "efforts": meffs or list(efforts), "alias": False, "section": sec}
                seen.add(mid)
                (main_items if sec == "main" else other_items).append(item)
            if main_items or other_items:
                break
        if not (main_items or other_items):
            pfiles = sorted((os.path.join(mc_dir, f) for f in os.listdir(mc_dir) if f.startswith("published-") and f.endswith(".json") and f != "published-floor.json"),
                            key=os.path.getmtime, reverse=True)
            for fp in pfiles:
                try:
                    with open(fp, encoding="utf-8") as f:
                        d = json.load(f)
                    doc = json.loads(base64.b64decode(d.get("documentBytes") or "").decode("utf-8"))
                    cc = ((doc.get("surfaces") or {}).get("cc") or {})
                except (OSError, ValueError, TypeError):
                    cc = {}
                for st in cc.get("model_selector_state") or []:
                    if not isinstance(st, dict):
                        continue
                    cands = [{"id": st.get("model"), "thinking": st.get("thinking")}] + [x for x in (st.get("thinking_by_model") or []) if isinstance(x, dict)]
                    for c in cands:
                        mid = str(c.get("id") or "").strip()
                        if not mid or not _MODEL.match(mid) or mid in seen:
                            continue
                        th = c.get("thinking") or {}
                        _valid_efforts([th.get("effort")] if th.get("effort") else [], efforts)
                        seen.add(mid)
                        main_items.append({"id": mid, "label": mid, "efforts": list(efforts), "alias": False})
                if main_items:
                    break
    catalog = [{"id": a, "label": a + " (mới nhất)", "efforts": list(efforts), "alias": True} for a in aliases]
    for it in main_items + other_items:
        if it["id"] not in aliases:
            catalog.append(it)
    return {"models": [c["id"] for c in catalog], "efforts": efforts, "catalog": catalog}


def discover_codex_models(cfg=None, home=None, probe_rpc=False):
    """Discover Codex CLI models & reasoning efforts from local cache, config.toml, and optionally `codex app-server` JSON-RPC."""
    cdir = os.environ.get("CODEX_HOME") or os.path.join(home or os.path.expanduser("~"), ".codex")
    efforts = list(CLI_PRESETS["codex"]["efforts"])
    catalog, seen = [], set()

    def add(mid, label="", meffs=()):
        s = str(mid or "").strip()
        if not s or not _MODEL.match(s) or s in seen:
            return
        seen.add(s)
        clean_e = _valid_efforts(meffs, efforts)
        catalog.append({"id": s, "label": str(label or s).strip(), "efforts": clean_e or list(efforts), "alias": False})

    cache_p = os.path.join(cdir, "models_cache.json")
    if os.path.isfile(cache_p):
        try:
            with open(cache_p, encoding="utf-8") as f:
                cd = json.load(f)
            ms = cd.get("models") if isinstance(cd, dict) else (cd if isinstance(cd, list) else [])
            for m in ms or []:
                if not isinstance(m, dict):
                    continue
                lv = m.get("supported_reasoning_levels") or []
                _valid_efforts(lv, efforts)
                if m.get("visibility") != "hide":
                    add(m.get("slug") or m.get("id") or m.get("model"), m.get("name") or m.get("description") or "", lv)
        except (OSError, ValueError, TypeError):
            pass

    toml_p = os.path.join(cdir, "config.toml")
    if os.path.isfile(toml_p):
        try:
            with open(toml_p, encoding="utf-8", errors="replace") as f:
                for ln in f:
                    m = re.match(r'^\s*model\s*=\s*["\']([^"\']+)["\']', ln)
                    if m:
                        add(m.group(1), m.group(1) + " (config.toml)")
        except OSError:
            pass

    if probe_rpc:
        exe = find_exe("codex", cfg)
        if exe:
            try:
                msgs = [
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "cadfabric", "version": "0.9.11"}}},
                    {"jsonrpc": "2.0", "method": "initialized", "params": {}},
                    {"jsonrpc": "2.0", "id": 2, "method": "model/list", "params": {}},
                ]
                r = subprocess.run([exe, "app-server"], input="\n".join(json.dumps(x) for x in msgs) + "\n",
                                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=4, **NOWIN)
                for ln in (r.stdout or "").splitlines():
                    try:
                        resp = json.loads(ln)
                    except ValueError:
                        continue
                    if isinstance(resp, dict) and resp.get("id") == 2:
                        for m in ((resp.get("result") or {}).get("data") or []):
                            if isinstance(m, dict):
                                add(m.get("id") or m.get("model"), m.get("displayName") or m.get("id") or "", m.get("supportedReasoningEfforts") or [])
            except (OSError, subprocess.TimeoutExpired, ValueError):
                pass
    return {"models": [c["id"] for c in catalog], "efforts": efforts, "catalog": catalog}


def discover_api_models(url, kind="openai", key="", timeout=15):
    """Query `GET /models` on an OpenAI-compatible or Anthropic Messages API endpoint. Never raises."""
    u = str(url or "").strip()
    if not re.match(r"^https?://[^\s\"'<>]{1,300}$", u):
        return {"ok": False, "models": [], "catalog": [], "detail": "invalid URL"}
    hdr = {"User-Agent": "cadfabric"}
    if kind == "anthropic":
        base = re.sub(r"/messages/?$", "", u.rstrip("/"))
        if not urllib.parse.urlparse(base).path.rstrip("/"):
            base = base.rstrip("/") + "/v1"
        target = base + "/models?limit=100"
        hdr["anthropic-version"] = "2023-06-01"
        if key:
            hdr["x-api-key"] = key
    else:
        base = u.split("/chat/completions")[0].rstrip("/")
        target = base + "/models"
        if key:
            hdr["Authorization"] = "Bearer " + key
    try:
        with urllib.request.urlopen(urllib.request.Request(target, headers=hdr), timeout=timeout) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"ok": False, "models": [], "catalog": [], "detail": "HTTP %d" % e.code}
    except Exception as e:                                                   # noqa: BLE001 - discovery must stay safe
        return {"ok": False, "models": [], "catalog": [], "detail": str(getattr(e, "reason", None) or type(e).__name__)[:120]}
    catalog, seen = [], set()
    raw_items = (d.get("data") if isinstance(d, dict) else None) or (d.get("models") if isinstance(d, dict) else None) or []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or item.get("name") or item.get("model") or "").strip()
        if not mid or not _MODEL.match(mid) or mid in seen:
            continue
        seen.add(mid)
        catalog.append({"id": mid, "label": str(item.get("display_name") or mid).strip()})
    return {"ok": True, "models": [c["id"] for c in catalog], "catalog": catalog, "detail": "%d model" % len(catalog)}


def _agy_model_effort(model, effort):
    """Strip any baked effort suffix (-low/-medium/-high/-xhigh/-max) when an explicit --effort is also chosen so `agy` does not reject the pair."""
    m = str(model or "").strip()
    e = str(effort or "").strip()
    if m and e:
        m = _AGY_SUFFIX.sub(r"\1", m)
    return m, e


def discover_agy_models(cfg=None, home=None, probe_cli=False):
    """Discover Antigravity CLI (`agy`) models & efforts from settings/cache and live `agy models` output."""
    efforts = list(CLI_PRESETS["agy"]["efforts"])
    for e in _AGY_CACHE.get("efforts") or ():
        if e not in efforts:
            efforts.append(e)
    catalog, seen = [], set()

    def add(mid, label="", meffs=(), sec="main"):
        s = str(mid or "").strip()
        if not s or not _MODEL.match(s) or s in seen:
            return
        seen.add(s)
        clean_e = _valid_efforts(meffs, efforts)
        catalog.append({"id": s, "label": str(label or s).strip(), "efforts": clean_e or list(efforts), "alias": False, "section": sec})

    for base in CLI_PRESETS["agy"]["models"]:
        add(base, base + " (base)", sec="main")

    sdir = os.path.join(home or os.path.expanduser("~"), ".gemini", "antigravity-cli")
    sp = os.path.join(sdir, "settings.json")
    if os.path.isfile(sp):
        try:
            with open(sp, encoding="utf-8") as f:
                sd = json.load(f)
            if isinstance(sd, dict) and sd.get("model"):
                add(sd["model"], str(sd["model"]) + " (settings.json)", sec="main")
        except (OSError, ValueError, TypeError):
            pass

    for c in _AGY_CACHE.get("catalog") or ():
        if isinstance(c, dict):
            add(c.get("id"), c.get("label") or "", c.get("efforts") or (), sec=c.get("section") or "main")

    if probe_cli:
        exe = find_exe("agy", cfg)
        if exe:
            try:
                r = subprocess.run([exe, "models"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=12, **NOWIN)
                clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", r.stdout or "")
                parsed_full = []
                for ln in re.split(r"[\r\n]+", clean):
                    m = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._:/@\-]{0,79})(?:\t|\s{2,})(.+?)\s*$", ln)
                    if not m:
                        continue
                    slug, label = m.group(1).strip(), m.group(2).strip()
                    msuf = _AGY_SUFFIX.match(slug)
                    if msuf:
                        base_slug, suf_eff = msuf.group(1), msuf.group(2)
                        if suf_eff not in efforts:
                            efforts.append(suf_eff)
                        add(base_slug, re.sub(r"\s*\([^)]+\)\s*$", "", label), sec="main")
                        parsed_full.append((slug, label, "variant"))
                    else:
                        parsed_full.append((slug, label, "main"))
                for slug, label, sec in parsed_full:
                    add(slug, label, sec=sec)
                if parsed_full:
                    _AGY_CACHE["models"] = [c["id"] for c in catalog]
                    _AGY_CACHE["efforts"] = list(efforts)
                    _AGY_CACHE["catalog"] = list(catalog)
            except (OSError, subprocess.TimeoutExpired, ValueError):
                pass
    return {"models": [c["id"] for c in catalog], "efforts": efforts, "catalog": catalog}


def presets_info(cfg=None):
    """What the UI needs to build the model / effort pickers (merging CLI_PRESETS with live local discovery)."""
    cl = discover_claude_models()
    cx = discover_codex_models(cfg, probe_rpc=False)
    ag = discover_agy_models(cfg, probe_cli=False)
    return {
        "codex": {"label": CLI_PRESETS["codex"]["label"], "efforts": cx["efforts"], "models": cx["models"], "catalog": cx["catalog"]},
        "claude": {"label": CLI_PRESETS["claude"]["label"], "efforts": cl["efforts"], "models": cl["models"], "catalog": cl["catalog"]},
        "agy": {"label": CLI_PRESETS["agy"]["label"], "efforts": ag["efforts"], "models": ag["models"], "catalog": ag["catalog"]},
    }


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
        if kind == "openai" and a.get("think"):
            out["think"] = True
        out["external"] = bool(a["external"]) if "external" in a else not is_private_host(url)
    elif out["type"] == "cli":
        preset = a.get("preset") or "custom"
        if preset not in CLI_PRESETS and preset != "custom":
            return None, "unknown CLI preset"
        out["preset"] = preset
        if preset != "custom":
            model, effort = str(a.get("model", "") or "").strip(), str(a.get("effort", "") or "").strip()
            fb = str(a.get("fallback_model", "") or "").strip() if preset == "claude" else ""
            pi = presets_info().get(preset) or {}
            allowed_efforts = tuple(dict.fromkeys(list(CLI_PRESETS[preset]["efforts"]) + list(pi.get("efforts") or ())))
            if model and not _MODEL.match(model):
                return None, "model name is not valid"
            if fb and not _MODEL.match(fb):
                return None, "fallback model name is not valid"
            if effort and effort not in allowed_efforts:
                return None, "effort must be one of: " + ", ".join(allowed_efforts)
            out.update({k: v for k, v in (("model", model), ("effort", effort), ("fallback_model", fb)) if v})
            if a.get("web"):
                out["web"] = True                                                    # may search / fetch web pages; local files stay out of reach
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
    if not isinstance(lst, list) or len(lst) > 24:
        return None, "advisors must be a list of at most 24"
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


def worker_names(cfg):
    """Any model in the unified pool (default Qwen API gateway, custom API/CLI models, Codex CLI, Antigravity CLI) can act as the worker."""
    return ["qwen", "nothink", "think", "codex", "agy"] + [a["id"] for a in (cfg.get("advisors") or [])]


def is_external(cfg, aid):
    a = find(cfg, aid)
    return aid in BUILTIN_EXTERNAL if a is None else bool(a.get("external"))


def meta(cfg):
    """What the UI lists: built-ins first, then the user's advisors."""
    out = [{"id": b, "label": BUILTIN_LABELS[b], "external": b in BUILTIN_EXTERNAL, "builtin": True} for b in BUILTIN]
    for a in cfg.get("advisors") or []:
        d = {"id": a["id"], "label": a["label"], "external": bool(a.get("external")), "builtin": False, "type": a["type"]}
        d.update({k: a[k] for k in ("kind", "url", "model", "effort", "fallback_model", "preset", "command", "web", "think") if a.get(k)})
        d["has_key"] = bool(a.get("key_file") and os.path.isfile(os.path.expanduser(a["key_file"])))
        out.append(d)
    return out


def models_meta(cfg):
    """Unified model pool displayed in Settings and role pickers (Cố vấn / Thực thi)."""
    gw_model = (cfg or {}).get("model") or ""
    gw_url = (cfg or {}).get("url") or ""
    out = [
        {"id": "qwen", "label": ("Qwen API (" + gw_model + ")") if gw_model else "Qwen API (gateway mặc định)",
         "external": False, "builtin": True, "gateway": True, "type": "api", "kind": "openai", "url": gw_url, "model": gw_model},
        {"id": "think", "label": "Qwen API (suy nghĩ sâu)",
         "external": False, "builtin": True, "gateway": True, "type": "api", "kind": "openai", "url": gw_url, "model": gw_model, "think": True},
    ]
    for a in meta(cfg):
        if not a.get("builtin"):
            out.append(a)
    out.append({"id": "codex", "label": BUILTIN_LABELS["codex"], "external": True, "builtin": True, "type": "cli", "preset": "codex"})
    out.append({"id": "agy", "label": BUILTIN_LABELS["agy"], "external": True, "builtin": True, "type": "cli", "preset": "agy"})
    out.append({"id": "codex-relay", "label": BUILTIN_LABELS["codex-relay"], "external": True, "builtin": True, "planner_only": True})
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


def ask(a, prompt, cfg=None, timeout=300, key=None, raw=False, skills=False):
    """-> (text, error). Never raises."""
    try:
        if a["type"] == "api":
            return _ask_api(a, prompt, timeout, key if key is not None else _read_key(a))
        return _ask_cli(a, prompt, cfg, timeout, raw, skills)
    except Exception as e:                                                   # noqa: BLE001 - an advisor must never crash the agent
        return "", "%s: %s" % (type(e).__name__, str(e)[:160])


def can_see_images(a):
    """Claude Code CLI, Antigravity CLI (agy), and Anthropic Messages API advisors accept image inputs."""
    return bool(a) and ((a.get("type") == "cli" and a.get("preset") in ("claude", "agy")) or (a.get("type") == "api" and a.get("kind") == "anthropic"))


def clip_prompt(prompt, limit):
    """Preserve head (TASK) and tail (ISSUES/WORKER REPORT/instructions) when a prompt exceeds `limit` chars."""
    if len(prompt) <= limit:
        return prompt
    head = max(200, int(limit * 0.35))
    tail = max(200, limit - head - 80)
    if head + tail >= len(prompt):
        return prompt
    omitted = len(prompt) - head - tail
    return prompt[:head] + ("\n...[middle truncated (%d chars) to fit advisor limit]...\n" % omitted) + prompt[-tail:]


def _run_agy(a, text, cfg=None, timeout=600, skills=False, images=None):
    """Run Antigravity CLI (`agy`) over NDJSON stream-json in an empty permission-locked tempdir. -> (text, error, usage)."""
    try:
        with tempfile.TemporaryDirectory(prefix="cadfabric-agy-") as wd:
            argv, _ = cli_argv(a, cfg, wd, skills=skills)
            body = text
            if images:
                paths = []
                for i, im in enumerate(images):
                    ext_i = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}[im["mime"]]
                    pth = os.path.join(wd, "picture-%d.%s" % (i + 1, ext_i))
                    with open(pth, "wb") as f:
                        f.write(base64.b64decode(im["data"]))
                    paths.append(pth)
                wd_norm = re.sub(r"^[A-Za-z]:", "", wd.replace("\\", "/"))
                perms = {"allow": ["read_file(" + wd_norm + "/*)"], "deny": ["command(*)", "write_file(*)", "mcp(*)"]}
                if a.get("web"):
                    perms["allow"].append("read_url(*)")
                if "--settings" in argv:
                    argv[argv.index("--settings") + 1] = json.dumps({"permissions": perms})
                argv += ["--add-dir", wd]
                body = "Look at these picture files first (view_file / read_file): " + ", ".join(paths) + "\n\n" + text
            stdin = json.dumps({"event": "user", "message": {"content": body}}) + "\n"
            try:
                r = subprocess.run(argv, input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, cwd=wd, **NOWIN)
            except subprocess.TimeoutExpired:
                return "", "timed out after %ds" % timeout, {}
        res = {}
        for ln in (r.stdout or "").splitlines():
            try:
                x = json.loads(ln)
            except ValueError:
                continue
            if isinstance(x, dict) and x.get("event") == "result":
                res = x.get("result") or {}
            elif isinstance(x, dict) and "response" in x and "status" in x:
                res = x
        u = res.get("usage") or {}
        resp = (res.get("response") or "").strip()
        status = str(res.get("status") or "")
        if r.returncode != 0 or (status and status != "SUCCESS") or not resp:
            err_msg = res.get("error") or resp or ((r.stderr or "").strip().splitlines() or ["no output"])[-1]
            return "", "exit %d: %s" % (r.returncode, str(err_msg)[:200]), {}
        cin = u.get("cache_read_tokens") or 0
        raw_in = u.get("input_tokens") or 0
        return resp, "", {"input": max(0, raw_in - cin), "cache_read": cin, "cache_write": 0,
                          "output": u.get("output_tokens") or 0, "usd": 0, "estimated": False}
    except Exception as e:                                                   # noqa: BLE001 - an advisor must never crash the agent
        return "", "%s: %s" % (type(e).__name__, str(e)[:160]), {}


def ask_metered(a, prompt, cfg=None, timeout=600, system=None, images=None, max_tokens=None, temperature=0.2):
    """Like ask(raw=True) but also returns token usage. -> (text, error, usage). Claude CLI, Agy CLI & API models return real token counts when available."""
    cli_lim = int(os.environ.get("CADFABRIC_CLI_CHARS", "180000"))
    if a.get("type") == "api":
        return _ask_api_metered(a, prompt, timeout, _read_key(a), system=system, max_tokens=max_tokens, temperature=temperature, images=images)
    if a.get("type") == "cli" and a.get("preset") == "agy":
        clipped = clip_prompt(prompt, cli_lim)
        full = ((system + "\n\n") if system else "") + clipped
        return _run_agy(a, full, cfg, timeout, images=images)
    if a.get("type") == "cli" and a.get("preset") == "claude":
        try:
            with tempfile.TemporaryDirectory(prefix="cadfabric-adv-") as wd:
                argv, _ = cli_argv(a, cfg, wd)
                if "--output-format" in argv:
                    argv[argv.index("--output-format") + 1] = "json"
                else:
                    argv += ["--output-format", "json"]
                if system:
                    argv += ["--system-prompt", system]
                clipped = clip_prompt(prompt, cli_lim)
                stdin = clipped
                if images and (os.name == "nt" or os.environ.get("CADFABRIC_IMAGES_AS_FILES")):
                    # the CLI exits silently on Windows when given base64 image blocks: save them in the throw-away folder and let the model read them with the Read tool only
                    paths = []
                    for i, im in enumerate(images):
                        pth = os.path.join(wd, "picture-%d.%s" % (i + 1, {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}[im["mime"]]))
                        with open(pth, "wb") as f:
                            f.write(base64.b64decode(im["data"]))
                        paths.append(pth)
                    i = argv.index("--tools"); argv[i:i + 2] = ["--tools", "Read", "--allowedTools", "Read"]
                    argv += ["--add-dir", wd]
                    stdin = "Look at these picture files with the Read tool first: " + ", ".join(paths) + "\n\n" + clipped
                    images = None
                if images:                                                       # images go in as base64 content blocks over stream-json
                    argv[argv.index("--output-format") + 1] = "stream-json"
                    argv += ["--input-format", "stream-json", "--verbose"]
                    blocks = [{"type": "image", "source": {"type": "base64", "media_type": im["mime"], "data": im["data"]}} for im in images]
                    stdin = json.dumps({"type": "user", "message": {"role": "user", "content": blocks + [{"type": "text", "text": clipped}]}}) + "\n"
                try:
                    r = subprocess.run(argv, input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, cwd=wd, **NOWIN)
                except subprocess.TimeoutExpired:
                    return "", "timed out after %ds" % timeout, {}
            if images:
                d = {}
                for ln in (r.stdout or "").splitlines():
                    try:
                        x = json.loads(ln)
                    except ValueError:
                        continue
                    if isinstance(x, dict) and x.get("type") == "result":
                        d = x
            else:
                d = json.loads(r.stdout or "{}")
            u = d.get("usage") or {}
            text = (d.get("result") or "").strip()
            if r.returncode != 0 or d.get("is_error") or not text:
                err_msg = text or ((r.stderr or "").strip().splitlines() or ["no output"])[-1]
                return "", "exit %d: %s" % (r.returncode, str(err_msg)[:200]), {}
            return text, "", {"input": u.get("input_tokens", 0), "cache_read": u.get("cache_read_input_tokens", 0), "cache_write": u.get("cache_creation_input_tokens", 0),
                              "output": u.get("output_tokens", 0), "usd": d.get("total_cost_usd", 0) or 0, "estimated": False}
        except Exception as e:                                                   # noqa: BLE001 - an advisor must never crash the agent
            return "", "%s: %s" % (type(e).__name__, str(e)[:160]), {}
    text, err = ask(a, ((system + "\n\n") if system else "") + prompt, cfg, timeout, raw=True)
    est = {"input": (len(prompt) + len(system or "")) // 4, "output": len(text) // 4, "cache_read": 0, "cache_write": 0, "usd": 0, "estimated": True}
    return text, err, est


def valid_model_override(m):
    s = str(m or "").strip()
    return True if not s else bool(_MODEL.match(s))


def valid_effort_override(e, cfg=None):
    s = str(e or "").strip()
    if not s:
        return True
    if not _EFFORT.match(s):
        return False
    allowed = set(ALL_EFFORTS)
    for info in (presets_info(cfg) or {}).values():
        allowed.update(info.get("efforts") or ())
    return s in allowed


def parse_model_spec(spec, cfg=None):
    """Parse 'base[:submodel][@effort]' -> (base_id, submodel, effort). Keeps plain IDs and Ollama tags like qwen:32b intact."""
    s = str(spec or "").strip()
    effort = ""
    if "@" in s:
        head, tail = s.rsplit("@", 1)
        if head and valid_effort_override(tail, cfg):
            s, effort = head, tail
    submodel = ""
    if ":" in s:
        head, tail = s.split(":", 1)
        if head:
            s, submodel = head, tail
    return s, submodel, effort


def chat_model(cfg, model_id, llm, system, user, timeout=900, max_tokens=4000, temperature=0.2, images=None, submodel=None, effort=None):
    """Run any model from the unified pool (default Qwen gateway, custom API/CLI, Codex CLI) -> (text, usage)."""
    base_id, sp_model, sp_eff = parse_model_spec(model_id, cfg) if isinstance(model_id, str) else (model_id, "", "")
    m_over = str((sp_model if submodel is None else submodel) or "").strip()
    e_over = str((sp_eff if effort is None else effort) or "").strip()
    if not valid_model_override(m_over):
        raise ValueError("invalid model name %r" % m_over)
    if not valid_effort_override(e_over, cfg):
        raise ValueError("invalid effort %r" % e_over)
    if base_id in ("off", "qwen", "nothink", "think", "", None):
        think = (e_over in ("medium", "high", "xhigh", "max", "think")) if e_over else (base_id == "think")
        kw = {"think": think, "max_tokens": max_tokens, "temperature": temperature}
        if m_over:
            kw["model"] = m_over
        m = llm.chat([{"role": "system", "content": system}, {"role": "user", "content": user}], **kw)
        u = m.get("_usage") or {}
        return m.get("content") or "", {"input": u.get("prompt_tokens", 0), "output": u.get("completion_tokens", 0),
                                        "cache_read": 0, "cache_write": 0, "usd": 0, "estimated": False}
    base = {"id": base_id, "type": "cli", "preset": base_id, "external": True} if base_id in ("codex", "agy") else find(cfg, base_id)
    if base is None:
        raise ValueError("unknown model %r" % model_id)
    adv = dict(base)
    if m_over:
        adv["model"] = m_over
    if e_over:
        if adv.get("type") == "cli" and adv.get("preset") in CLI_PRESETS:
            adv["effort"] = e_over
        elif adv.get("type") == "api" and adv.get("kind") == "openai":
            adv["think"] = e_over in ("medium", "high", "xhigh", "max", "think")
    t, err, u = ask_metered(adv, user, cfg, timeout, system=system, images=images, max_tokens=max_tokens, temperature=temperature)
    if err:
        raise RuntimeError(err)
    return t, u


def _ask_api(a, prompt, timeout, key):
    t, err, _ = _ask_api_metered(a, prompt, timeout, key)
    return t, err


def _ask_api_metered(a, prompt, timeout, key, system=None, max_tokens=None, temperature=0.2, images=None):
    api_lim = int(os.environ.get("CADFABRIC_API_CHARS", "120000"))
    clipped = clip_prompt(prompt, api_lim)
    user_text = clipped if system is not None else (PREFIX + clipped)
    hdr = {"Content-Type": "application/json", "User-Agent": "cadfabric"}
    if a["kind"] == "anthropic":
        if key:
            hdr["x-api-key"] = key
        hdr["anthropic-version"] = "2023-06-01"
        content = ([{"type": "image", "source": {"type": "base64", "media_type": im["mime"], "data": im["data"]}} for im in (images or [])]
                   + [{"type": "text", "text": user_text}]) if images else user_text
        body = {"model": a["model"], "max_tokens": max_tokens or int(os.environ.get("CADFABRIC_API_MAX_TOKENS", "16384")),
                "temperature": temperature, "messages": [{"role": "user", "content": content}]}
        if system:
            body["system"] = system
        retry_codes = (429, 502, 503, 529)
    else:
        if key:
            hdr["Authorization"] = "Bearer " + key
        msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user_text}]
        body = {"messages": msgs, "max_tokens": max_tokens or int(os.environ.get("CADFABRIC_OPENAI_MAX_TOKENS", "8192")), "temperature": temperature}
        if a.get("model"):
            body["model"] = a["model"]
        if a.get("think"):
            body["chat_template_kwargs"] = {"enable_thinking": True}
        retry_codes = (429, 502, 503)
    d = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(a["url"], json.dumps(body).encode(), hdr), timeout=timeout) as r:
                d = json.loads(r.read())
            break
        except urllib.error.HTTPError as e:
            raw_err = e.read()[:160].decode("utf-8", "replace")
            if e.code not in retry_codes or attempt == 4:
                return "", "HTTP %d from %s: %s" % (e.code, urllib.parse.urlparse(a["url"]).netloc, raw_err), {}
            time.sleep(2 + 2 * attempt)
        except urllib.error.URLError as e:
            return "", "cannot reach %s (%s)" % (a["url"], e.reason), {}
    if a["kind"] == "anthropic":
        out = "".join(b.get("text", "") for b in (d or {}).get("content", []) if b.get("type") == "text")
        u = (d or {}).get("usage")
        usage = {"input": u.get("input_tokens", 0), "output": u.get("output_tokens", 0),
                 "cache_read": u.get("cache_read_input_tokens", 0), "cache_write": u.get("cache_creation_input_tokens", 0),
                 "usd": 0, "estimated": False} if isinstance(u, dict) else {"input": (len(user_text) + len(system or "")) // 4, "output": len(out) // 4, "cache_read": 0, "cache_write": 0, "usd": 0, "estimated": True}
    else:
        out = (((d or {}).get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        u = (d or {}).get("usage")
        usage = {"input": u.get("prompt_tokens", 0), "output": u.get("completion_tokens", 0),
                 "cache_read": 0, "cache_write": 0, "usd": 0, "estimated": False} if isinstance(u, dict) and ("prompt_tokens" in u or "completion_tokens" in u) else {"input": (len(user_text) + len(system or "")) // 4, "output": len(out) // 4, "cache_read": 0, "cache_write": 0, "usd": 0, "estimated": True}
    return (out.strip(), "", usage) if out.strip() else ("", "the advisor returned no text", {})


def cli_argv(a, cfg, cwd, skills=False):
    """-> (argv, prompt_in_args). Pure function (tested)."""
    if a["preset"] in CLI_PRESETS:
        p = CLI_PRESETS[a["preset"]]
        exe = find_exe(p["exe"], cfg)
        if not exe:
            raise RuntimeError("%s not found on this machine (install it and log in)" % p["label"])
        base = [x.replace("{cwd}", cwd) for x in p["args"]]
        if a["preset"] == "agy":
            perms = {"deny": ["command(*)", "read_file(*)", "write_file(*)", "mcp(*)"]}
            if a.get("web"):
                perms["allow"] = ["read_url(*)"]
            if skills:
                base = [x for x in base if x != "--disable-slash-commands"]
            m_use, e_use = _agy_model_effort(a.get("model") or "", a.get("effort") or "")
            extra = (["--settings", json.dumps({"permissions": perms})]
                     + (p["model_flag"] + [m_use] if m_use else [])
                     + (p["effort_flag"](e_use) if e_use else []))
            at = len(base) if p["at"] is None else p["at"]
            return [exe] + base[:at] + extra + base[at:], False
        if skills and "--tools" in base:                                            # claude only sees its skills through the Skill tool (and not with slash commands disabled): offer that one tool, nothing else
            base = [x for x in base if x != "--disable-slash-commands"]
            i = base.index("--tools"); base[i:i + 2] = ["--tools", "Skill"]
        pre = []
        if a.get("web"):
            if "web_tools" in p:                                                      # claude: swap the empty tool list for web tools only
                i = base.index("--tools"); base[i:i + 2] = p["web_tools"]
            else:                                                                     # codex: a top-level flag, before `exec`
                pre = p["web_args"]
        extra = ((p["model_flag"] + [a["model"]] if a.get("model") else [])
                 + (p["fallback_flag"] + [a["fallback_model"]] if a.get("fallback_model") and "fallback_flag" in p else [])
                 + (p["effort_flag"](a["effort"]) if a.get("effort") else []))
        at = len(base) if p["at"] is None else p["at"]
        return [exe] + pre + base[:at] + extra + base[at:], False
    parts = shlex.split(a["command"], posix=(os.name != "nt"))
    exe = shutil.which(parts[0]) or (parts[0] if os.path.isfile(parts[0]) else None)
    if not exe:
        raise RuntimeError("program %r not found" % parts[0])
    in_args = any("{prompt}" in x for x in parts)
    return [exe] + [x.replace("{cwd}", cwd) for x in parts[1:]], in_args


def _ask_cli(a, prompt, cfg, timeout, raw=False, skills=False):
    cli_lim = int(os.environ.get("CADFABRIC_CLI_CHARS", "180000"))
    clipped = clip_prompt(prompt, cli_lim)
    text = clipped if raw else (WEB_PREFIX if a.get("web") else PREFIX) + clipped
    if a.get("preset") == "agy":
        out, err, _ = _run_agy(a, text, cfg, timeout, skills=skills)
        return out, err
    with tempfile.TemporaryDirectory(prefix="cadfabric-adv-") as wd:
        argv, in_args = cli_argv(a, cfg, wd, skills)
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
