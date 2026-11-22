"""One place for settings. Nothing machine specific is hard coded and no secret lives in a file this package writes.

Config file (first that exists): $CADFABRIC_CONFIG, ./cadfabric.json, <user config dir>/cadfabric/config.json
  Linux/Mac: ~/.config/cadfabric/config.json     Windows: %APPDATA%\\cadfabric\\config.json
Keys: url, model, key_file (path to a file holding the API key), planner, relay {host, codex}, codex_bin, data_dir
Environment overrides: CADFABRIC_URL, CADFABRIC_MODEL, CADFABRIC_KEY (the key itself), CADFABRIC_KEY_FILE, CADFABRIC_PLANNER, CADFABRIC_DATA_DIR."""
import json, os, sys

DEFAULTS = {"url": "http://127.0.0.1:18080/v1/chat/completions", "model": "", "key_file": None, "planner": "nothink",
            "relay": {"host": "", "workdir": "", "codex": "codex"}, "codex_bin": None, "data_dir": None, "advisors": [], "viewer": {"kind": "auto", "command": ""}, "workspace": ""}
PLANNERS = ("off", "nothink", "think", "codex", "codex-relay")


def config_dir():
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "cadfabric")
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "cadfabric")


def config_path():
    for p in (os.environ.get("CADFABRIC_CONFIG"), os.path.join(os.getcwd(), "cadfabric.json"), os.path.join(config_dir(), "config.json")):
        if p and os.path.isfile(p):
            return p
    return None


def load():
    cfg = json.loads(json.dumps(DEFAULTS))
    path = config_path()
    if path:
        with open(path, encoding="utf-8-sig") as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    for env, k in (("CADFABRIC_URL", "url"), ("CADFABRIC_MODEL", "model"), ("CADFABRIC_KEY_FILE", "key_file"), ("CADFABRIC_PLANNER", "planner"),
                   ("CADFABRIC_DATA_DIR", "data_dir")):
        if os.environ.get(env):
            cfg[k] = os.environ[env]
    cfg["_path"] = path
    from . import advisors
    if cfg["planner"] not in advisors.names(cfg):
        raise SystemExit("config: planner must be one of %s (got %r)" % (", ".join(advisors.names(cfg)), cfg["planner"]))
    return cfg


def read_key(cfg):
    """The API key from $CADFABRIC_KEY or the key file. Empty string is allowed (a local gateway without auth)."""
    if os.environ.get("CADFABRIC_KEY"):
        return os.environ["CADFABRIC_KEY"].strip()
    p = cfg.get("key_file")
    if not p:
        return ""
    try:
        with open(os.path.expanduser(os.path.expandvars(p)), encoding="utf-8-sig") as f:
            return f.read().strip()
    except OSError as e:
        raise SystemExit("API key file not readable (%s): %s" % (p, e))


def data_dir(cfg):
    d = cfg.get("data_dir") or os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share") if sys.platform != "win32"
                                           else os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "cadfabric")
    os.makedirs(d, exist_ok=True)
    return d


# ---- settings written from the web UI (only these fields; every value is validated because some end up in a command line) ----
import re
_HOST = re.compile(r"^[A-Za-z0-9_.@:\-\[\]]{1,120}$")
_PATH = re.compile(r"^[A-Za-z0-9_ ./\\:~\-]{0,260}$")


def user_config_path():
    return os.environ.get("CADFABRIC_CONFIG") or os.path.join(config_dir(), "config.json")


def validate_settings(s):
    """-> (clean dict, error text). Raises nothing."""
    out = {}
    url = str(s.get("url", "")).strip()
    if not re.match(r"^https?://[^\s\"'<>]{1,300}$", url):
        return None, "URL must start with http:// or https://"
    out["url"] = url
    model = str(s.get("model", "")).strip()
    if len(model) > 120 or re.search(r"[\s\"'<>]", model):
        return None, "model name is not valid"
    out["model"] = model
    from . import advisors, viewers
    if "viewer" in s:
        vw, err = viewers.validate(s.get("viewer"))
        if err:
            return None, err
        out["viewer"] = vw
    if "workspace" in s:
        from . import workspace
        ws, err = workspace.check_folder(s.get("workspace"))
        if err:
            return None, err
        out["workspace"] = ws
    if "advisors" in s:
        lst, err = advisors.validate_list(s.get("advisors"))
        if err:
            return None, err
        out["advisors"] = lst
    planner = s.get("planner")
    known = list(advisors.BUILTIN) + [a["id"] for a in (out.get("advisors") if "advisors" in out else s.get("_current_advisors") or [])]
    if planner not in known:
        return None, "unknown planner"
    out["planner"] = planner
    rel = s.get("relay") or {}
    host, workdir, codex = str(rel.get("host", "")).strip(), str(rel.get("workdir", "")).strip(), str(rel.get("codex", "") or "codex").strip()
    if host and (not _HOST.match(host) or host.startswith("-")):
        return None, "relay host is not valid (use name, user@name or an IP)"
    if not _PATH.match(workdir) or not _PATH.match(codex):
        return None, "relay folder / codex path contain characters that are not allowed"
    out["relay"] = {"host": host, "workdir": workdir, "codex": codex}
    return out, ""


def save_settings(clean, key=None, advisor_keys=None):
    """Merge `clean` into the user config file. `key` (if given) goes to <config dir>/key.txt, never into the JSON."""
    path = user_config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cur = {}
    if os.path.isfile(path):
        with open(path, encoding="utf-8-sig") as f:
            cur = json.load(f)
    cur.update({k: v for k, v in clean.items() if k not in ("relay", "advisors")})              # viewer replaces the whole dict
    cur["relay"] = dict(cur.get("relay") or {}, **clean["relay"])
    if "advisors" in clean:
        cdir, old = os.path.dirname(path), {a["id"]: a for a in cur.get("advisors") or []}
        for a in clean["advisors"]:
            k = (advisor_keys or {}).get(a["id"])
            if k:
                a["key_file"] = _write_secret(os.path.join(cdir, "key-%s.txt" % a["id"]), k)
            elif a["type"] == "api":
                a["key_file"] = (old.get(a["id"]) or {}).get("key_file")           # no new key typed: keep the saved one
        for gone in set(old) - {a["id"] for a in clean["advisors"]}:                # a removed advisor leaves no key behind
            kf = old[gone].get("key_file")
            if kf and os.path.dirname(os.path.abspath(kf)) == os.path.abspath(cdir) and os.path.isfile(kf):
                os.unlink(kf)
        cur["advisors"] = clean["advisors"]
    if key:
        kp = os.path.join(os.path.dirname(path), "key.txt")
        fd = os.open(kp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(key.strip() + "\n")
        try:
            os.chmod(kp, 0o600)
        except OSError:
            pass
        cur["key_file"] = kp
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cur, f, indent=2); f.write("\n")
    os.replace(tmp, path)
    return path


def _write_secret(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(value.strip() + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path
